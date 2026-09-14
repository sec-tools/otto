"""
Persistent per-user UI state: dismissed items, snoozes, delivered
notifications.

Small JSON files in the data dir, written atomically. Everything is loaded
once per operation (not once per item) and expired entries are pruned on
load so the files never grow unbounded.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from otto import paths

logger = logging.getLogger("otto.web.state")

_LOCK = threading.RLock()

# Dismissals older than this are forgotten (the underlying item is long gone).
DISMISSED_TTL = timedelta(days=14)
NOTIFIED_TTL = timedelta(days=7)


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("Could not write %s: %s", path.name, e)
        try:
            tmp.unlink()
        except OSError:
            pass


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not read %s (%s); starting fresh", path.name, e)
        return default


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Dismissed
# ---------------------------------------------------------------------------

def load_dismissed() -> set[str]:
    """IDs the user dismissed (expired entries pruned)."""
    with _LOCK:
        raw = _read_json(paths.dismissed_file(), [])
        now = _now()
        # Two on-disk formats are accepted: legacy list[str] and {id: iso_ts}.
        if isinstance(raw, list):
            return {str(x) for x in raw if x}
        if isinstance(raw, dict):
            kept: set[str] = set()
            changed = False
            for item_id, ts in raw.items():
                dt = _parse_ts(ts)
                if dt is None or now - dt <= DISMISSED_TTL:
                    kept.add(str(item_id))
                else:
                    changed = True
            if changed:
                _atomic_write_json(paths.dismissed_file(), {i: raw[i] for i in kept if i in raw})
            return kept
        return set()


def save_dismissed(ids: set[str]) -> None:
    """Replace the dismissed set (timestamps are preserved for existing IDs)."""
    with _LOCK:
        raw = _read_json(paths.dismissed_file(), {})
        existing = raw if isinstance(raw, dict) else {}
        now_iso = _now().isoformat()
        data = {i: existing.get(i, now_iso) for i in ids if i}
        _atomic_write_json(paths.dismissed_file(), data)


def dismiss(item_ids: list[str] | set[str]) -> int:
    with _LOCK:
        current = load_dismissed()
        new = {i for i in item_ids if i} - current
        if new:
            save_dismissed(current | new)
        return len(new)


# ---------------------------------------------------------------------------
# Snoozed
# ---------------------------------------------------------------------------

def load_snoozed() -> dict[str, str]:
    """Active snoozes ``{item_id: wake_iso}``; expired/corrupt entries are pruned."""
    with _LOCK:
        raw = _read_json(paths.snoozed_file(), {})
        if not isinstance(raw, dict):
            return {}
        now = _now()
        kept: dict[str, str] = {}
        for item_id, wake in raw.items():
            dt = _parse_ts(wake)
            if dt is not None and dt > now:
                kept[str(item_id)] = wake
        if kept != raw:
            _atomic_write_json(paths.snoozed_file(), kept)
        return kept


def save_snoozed(data: dict[str, str]) -> None:
    with _LOCK:
        _atomic_write_json(paths.snoozed_file(), dict(data))


def snooze(item_id: str, hours: float) -> datetime:
    """Snooze an item for ``hours`` (clamped to 15 min … 1 year). Returns wake time."""
    hours = min(max(float(hours), 0.25), 24 * 365)
    wake = _now() + timedelta(hours=hours)
    with _LOCK:
        data = load_snoozed()
        data[item_id] = wake.isoformat()
        save_snoozed(data)
    return wake


def is_snoozed(item_id: str, snoozed: dict[str, str] | None = None) -> bool:
    if not item_id:
        return False
    data = snoozed if snoozed is not None else load_snoozed()
    return item_id in data


def hidden_ids() -> set[str]:
    """Everything that should not be shown right now: dismissed ∪ snoozed."""
    return load_dismissed() | set(load_snoozed().keys())


def item_ids(item: dict) -> set[str]:
    """Every id an item answers to: its own plus those of the copies folded into it.

    The same message often arrives twice — read off the screen and through
    the Slack API — and the two copies hash to different ids (their text
    differs by a rendered mention or a link label). Dismissing, snoozing and
    notifying by *all* of them keeps the decision when a later refresh sees
    only one copy, which is how cleared items used to come back.
    """
    ids = {str(item.get("id") or "")}
    ids.update(str(i) for i in (item.get("ids") or []) if i)
    ids.discard("")
    return ids


def is_hidden(item: dict, hidden: set[str]) -> bool:
    return bool(hidden) and not item_ids(item).isdisjoint(hidden)


# ---------------------------------------------------------------------------
# Notified
# ---------------------------------------------------------------------------

def load_notified() -> dict[str, str]:
    """``{item_id: iso_ts}`` of items already surfaced as a notification."""
    with _LOCK:
        raw = _read_json(paths.notified_file(), {})
        if not isinstance(raw, dict):
            return {}
        now = _now()
        kept = {k: v for k, v in raw.items() if (dt := _parse_ts(v)) and now - dt <= NOTIFIED_TTL}
        if kept != raw:
            _atomic_write_json(paths.notified_file(), kept)
        return kept


def mark_notified(item_ids: list[str] | set[str]) -> None:
    with _LOCK:
        data = load_notified()
        now_iso = _now().isoformat()
        for i in item_ids:
            if i:
                data[i] = now_iso
        _atomic_write_json(paths.notified_file(), data)


def clear_all_state() -> None:
    """Testing / `otto reset` helper."""
    with _LOCK:
        for p in (paths.dismissed_file(), paths.snoozed_file(), paths.notified_file()):
            try:
                p.unlink()
            except OSError:
                pass
