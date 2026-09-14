"""
Radar — the part of the briefing that looks across days, not screens.

Each refresh the radar

1. remembers every message that was read (:mod:`otto.intelligence.knowledge`),
2. extracts commitments, asks and dated items from the *new* ones
   (:mod:`otto.intelligence.commitments`) and keeps their lifecycle honest —
   a promise is closed when its owner later says it is done, an event is
   over once it has happened, stale items fade instead of nagging forever,
3. detects recurring series and metric trends (:mod:`otto.intelligence.trends`),
4. turns all of that into one compact, explainable payload:

   ``todo``      — things *you* owe (your promises, asks aimed at you)
   ``waiting``   — things others owe you
   ``open_calls``— "can someone…" asks nobody has taken (a chance to help)
   ``upcoming``  — deadlines and events in the next two weeks
   ``patterns``  — recurring series with cadence, next-expected and trends
   ``attention`` — short data-driven observations (a late nightly job, a
                   metric that jumped, a channel that got 3× busier)
   ``memory``    — how much Otto has remembered (so the user can judge it)

Everything is read-only with respect to the sources and stays on the
machine. Confidence thresholds are deliberately conservative: the radar
would rather miss a vague hint than manufacture a to-do.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from otto.intelligence import commitments as cm
from otto.intelligence.knowledge import KnowledgeStore, Message, default_store
from otto.intelligence.trends import Series, channel_shifts, humanize_duration, series_from

logger = logging.getLogger("otto.intelligence.radar")

EXTRACT_MAX_AGE_DAYS = 14        # older messages are history, not open loops
DEADLINE_GRACE_DAYS = 3          # keep an overdue item visible this long, then let it fade
UNDATED_TTL_DAYS = 10            # an undated promise/ask fades after this
EVENT_OVER_AFTER_S = 6 * 3600    # an event is over once it has happened
UPCOMING_WINDOW_DAYS = 14
TRENDS_WINDOW_DAYS = 30
MAX_ROWS = 8
MAX_PATTERNS = 6
MIN_RUNS_FOR_ATTENTION = 4

MIN_CONFIDENCE = {"todo": 0.45, "waiting": 0.5, "open_calls": 0.5, "deadline": 0.45, "event": 0.4}


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def self_names() -> list[str]:
    """Names that mean "the user": config ``[user]`` plus what the sources showed as "you"."""
    from otto.utils.identity import self_names as _names
    return _names()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def _extract_from(store: KnowledgeStore, messages: Iterable[Message], names: list[str], *, now: float) -> int:
    added = 0
    cutoff = now - EXTRACT_MAX_AGE_DAYS * 86400
    for m in messages:
        if m.ts < cutoff:
            continue
        try:
            found = cm.extract(m.text, sender=m.sender, ts=m.when, channel=m.channel,
                               self_names=names, is_bot=m.is_bot)
        except Exception as e:      # never let a regex corner case break the refresh
            logger.debug("commitment extraction failed: %s", e)
            continue
        for c in found:
            row = {
                "id": c.id, "kind": c.kind, "who": c.who, "what": c.what, "channel": m.channel,
                "due": c.due.timestamp() if c.due else None, "due_text": c.due_text,
                "created": m.ts, "updated": now, "status": "open", "confidence": c.confidence,
                "for_you": c.for_you, "source_key": m.key, "url": m.url,
            }
            if c.kind == "ask" and c.open_call:
                row["kind"] = "open_call"
            if store.upsert_commitment(row):
                added += 1
    return added


def _settle(store: KnowledgeStore, new_messages: list[Message], names: list[str], *, now: float) -> None:
    """Close, expire or keep every open item."""
    open_items = store.commitments(status="open", limit=500)
    if not open_items:
        return
    by_channel: dict[str, list[Message]] = {}
    for m in new_messages:
        by_channel.setdefault(m.channel.lower(), []).append(m)

    for item in open_items:
        due = item.get("due")
        kind = item["kind"]
        created = float(item.get("created") or now)

        # 1. Time-based fading.
        if kind == "event" and due is not None and now > float(due) + EVENT_OVER_AFTER_S:
            store.set_commitment_status(item["id"], "expired", now=now)
            continue
        if due is not None and now > float(due) + DEADLINE_GRACE_DAYS * 86400:
            store.set_commitment_status(item["id"], "expired", now=now)
            continue
        if due is None and now > created + UNDATED_TTL_DAYS * 86400:
            store.set_commitment_status(item["id"], "expired", now=now)
            continue

        # 2. Completion by a later message in the same channel.
        later = [m for m in by_channel.get(item["channel"].lower(), []) if m.ts > created]
        if not later:
            continue
        who = (item.get("who") or "").lower()
        for m in later:
            sender_low = (m.sender or "").lower()
            sender_is_you = cm.is_you(m.sender, names)
            if kind == "promise":
                responsible = sender_low == who or (cm.is_you(item.get("who", ""), names) and sender_is_you)
            elif kind == "ask":
                # An ask aimed at you is done when *you* say so; an ask you made is done when they say so.
                responsible = sender_is_you if item.get("for_you") else (sender_low != who)
            elif kind == "open_call":
                responsible = sender_low != who
            else:
                responsible = False
            if responsible and cm.looks_completed(m.text, item["what"]):
                store.set_commitment_status(item["id"], "done", now=now)
                break


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

def _clock(when: datetime) -> str:
    """Clock suffix, blank for the implicit end-of-business time day-level phrases resolve to."""
    if when.hour == cm.END_OF_DAY_HOUR and when.minute == 0:
        return ""
    return " " + when.strftime("%H:%M")


def due_label(due: float | None, *, now: float) -> tuple[str, bool, bool]:
    """→ (label, overdue, soon). Labels read like a person would say them."""
    if due is None:
        return "", False, False
    delta = float(due) - now
    when = datetime.fromtimestamp(float(due), tz=timezone.utc).astimezone()
    today = datetime.fromtimestamp(now, tz=timezone.utc).astimezone().date()
    if delta < 0:
        return f"overdue {humanize_duration(-delta)}", True, True
    if when.date() == today:
        return f"today{_clock(when) or ' EOD'}", False, True
    if when.date() == today + timedelta(days=1):
        return f"tomorrow{_clock(when)}", False, delta < 36 * 3600
    if delta < 6 * 86400:      # a weekday name is only unambiguous inside the coming week
        return when.strftime("%a") + _clock(when), False, False
    return when.strftime("%b %d"), False, False


def _pattern_id(key: str) -> str:
    return "pattern:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _row(item: dict[str, Any], *, now: float, names: list[str]) -> dict[str, Any]:
    label, overdue, soon = due_label(item.get("due"), now=now)
    who = item.get("who") or ""
    if cm.is_you(who, names):
        who = "you"
    return {
        "id": f"radar:{item['id']}",
        "kind": item["kind"],
        "who": who,
        "what": item["what"],
        "channel": item.get("channel", ""),
        "due": datetime.fromtimestamp(float(item["due"]), tz=timezone.utc).isoformat() if item.get("due") else "",
        "due_label": label,
        "overdue": overdue,
        "soon": soon,
        "age": humanize_duration(now - float(item.get("created") or now)),
        "confidence": round(float(item.get("confidence") or 0.0), 2),
        "url": item.get("url", ""),
    }


def _sort_key(row: dict[str, Any]) -> tuple:
    return (0 if row["overdue"] else 1, 0 if row["due"] else 1, row["due"] or "9", -row["confidence"])


def _bucket(items: list[dict[str, Any]], names: list[str], *, now: float, hidden: set[str]) -> dict[str, list[dict[str, Any]]]:
    todo: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    open_calls: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []
    horizon = now + UPCOMING_WINDOW_DAYS * 86400
    for item in items:
        conf = float(item.get("confidence") or 0.0)
        kind = item["kind"]
        row = _row(item, now=now, names=names)
        if row["id"] in hidden:
            continue
        who_is_you = cm.is_you(item.get("who", ""), names)
        if kind == "promise":
            if who_is_you and conf >= MIN_CONFIDENCE["todo"]:
                todo.append(row)
            elif not who_is_you and conf >= MIN_CONFIDENCE["waiting"]:
                waiting.append(row)
        elif kind == "ask":
            if item.get("for_you") and conf >= MIN_CONFIDENCE["todo"]:
                todo.append(row)
            elif who_is_you and conf >= MIN_CONFIDENCE["waiting"]:
                waiting.append(row)
        elif kind == "open_call":
            if conf >= MIN_CONFIDENCE["open_calls"]:
                open_calls.append(row)
        elif kind in ("deadline", "event"):
            due = item.get("due")
            if due is None or float(due) > horizon:
                continue
            if conf >= MIN_CONFIDENCE[kind] or item.get("for_you"):
                upcoming.append(row)
    for lst in (todo, waiting, open_calls, upcoming):
        lst.sort(key=_sort_key)
    return {
        "todo": todo[:MAX_ROWS], "waiting": waiting[:MAX_ROWS],
        "open_calls": open_calls[:MAX_ROWS], "upcoming": upcoming[:MAX_ROWS],
    }


def _pattern_dict(s: Series, *, now: float) -> dict[str, Any]:
    next_label = ""
    if s.next_expected:
        when = datetime.fromtimestamp(s.next_expected, tz=timezone.utc).astimezone()
        today = datetime.fromtimestamp(now, tz=timezone.utc).astimezone().date()
        if when.date() == today:
            next_label = f"today ~{when.strftime('%H:%M')}"
        elif when.date() == today + timedelta(days=1):
            next_label = f"tomorrow ~{when.strftime('%H:%M')}"
        else:
            next_label = when.strftime("%a %b %d ~%H:%M")
    return {
        "id": _pattern_id(s.key),
        "label": s.label,
        "channel": s.channel,
        "sender": s.sender,
        "n": s.n,
        "span": humanize_duration(max(0.0, s.last_ts - s.first_ts)),
        "cadence": s.cadence,
        "regularity": round(s.regularity, 2),
        "next_expected": next_label,
        "late_by": humanize_duration(s.late_by_s) if s.late_by_s else "",
        "last_seen": humanize_duration(now - s.last_ts) + " ago",
        "metrics": [m.to_dict() for m in s.metrics],
    }


def _attention(series: list[Series], shifts, *, now: float) -> list[str]:
    notes: list[str] = []
    for s in series:
        if s.late_by_s and s.periodic:
            notes.append(
                f"“{s.label}” in {s.channel} usually arrives {s.cadence}; nothing for "
                f"{humanize_duration(now - s.last_ts)} ({humanize_duration(s.late_by_s)} past due)."
            )
    for s in series:
        for m in s.moving_metrics[:1]:
            # Thin evidence earns a note only for a dramatic move: three runs
            # where the last is 10% off the others is a pattern row, not an alert.
            dramatic = abs(m.last - m.usual) >= 2.0 * max(abs(m.usual), 1.0)
            if len(m.values) < MIN_RUNS_FOR_ATTENTION and not dramatic:
                continue
            # Said the way a colleague would: "duration 54 min, usually 6 min".
            d = m.to_dict()
            direction = "up" if m.direction == "up" else "down"
            notes.append(
                f"{s.channel} · “{s.label}”: {d['name']} is {direction} — {d['last']}, usually {d['usual']} "
                f"({len(m.values)} runs)."
            )
    for sh in shifts[:2]:
        notes.append(
            f"{sh.channel} is {sh.direction} than usual: {sh.this_week} messages this week vs {sh.last_week} last week."
        )
    return notes[:5]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def empty_radar() -> dict[str, Any]:
    return {"todo": [], "waiting": [], "open_calls": [], "upcoming": [], "patterns": [], "attention": [],
            "memory": {"messages": 0, "channels": 0, "people": 0, "days": 0}, "self_names": []}


def update(
    events: Iterable[Any],
    *,
    store: KnowledgeStore | None = None,
    hidden: Iterable[str] = (),
    now: float | None = None,
) -> dict[str, Any]:
    """Remember ``events`` (normalised), advance the lifecycle and build the radar payload."""
    now = now if now is not None else time.time()
    store = store or default_store()
    names = self_names()
    try:
        new_keys = store.remember(events, now=now)
        new_messages: list[Message] = []
        for i in range(0, len(new_keys), 400):
            chunk = new_keys[i:i + 400]
            new_messages.extend(store.messages(keys=chunk, limit=len(chunk)))
        if new_messages:
            added = _extract_from(store, new_messages, names, now=now)
            if added:
                logger.info("radar: %d new open loop(s) from %d new message(s)", added, len(new_messages))
        _settle(store, new_messages, names, now=now)
        return build(store=store, hidden=hidden, now=now, names=names)
    except Exception as e:
        logger.warning("radar update failed: %s", e)
        return empty_radar()


_TRENDS_TTL_S = 300
_trends_cache: dict[str, Any] = {}


def _trends(store: KnowledgeStore, memory: dict[str, Any], *, now: float) -> tuple[list[Series], list]:
    """
    Series + channel shifts over the last month. Scanning tens of thousands of
    rows every minute is wasteful when nothing changed, so the result is reused
    for a few minutes unless the store grew.
    """
    signature = (str(store.path), int(memory.get("messages") or 0), memory.get("last_ts"))
    cached = _trends_cache.get("entry")
    if cached and cached["signature"] == signature and now - cached["at"] < _TRENDS_TTL_S:
        series = cached["series"]
    else:
        recent = store.messages(since=now - TRENDS_WINDOW_DAYS * 86400, limit=20_000)
        series = [s for s in series_from(recent, now=now) if s.periodic or s.metrics]
        counts: dict[str, list[int]] = {}
        for ch in store.channels(since=now - 14 * 86400):
            if ch.total >= 8:
                counts[ch.channel] = store.daily_counts(days=14, now=now, channel=ch.channel)
        _trends_cache["entry"] = {"signature": signature, "at": now, "series": series, "shifts": channel_shifts(counts)}
        cached = _trends_cache["entry"]
    # "Late" depends on the clock, not on the data — refresh it on every call.
    for s in series:
        if s.next_expected is not None and s.cadence_s:
            tolerance = max(0.35 * s.cadence_s, 600.0)
            s.late_by_s = (now - s.next_expected) if now > s.next_expected + tolerance else None
    series.sort(key=lambda s: (0 if s.late_by_s else 1, 0 if s.moving_metrics else 1, 0 if s.periodic else 1, -s.n))
    return series, cached["shifts"]


def build(
    *,
    store: KnowledgeStore | None = None,
    hidden: Iterable[str] = (),
    now: float | None = None,
    names: list[str] | None = None,
) -> dict[str, Any]:
    """Radar payload from the store without ingesting anything new."""
    now = now if now is not None else time.time()
    store = store or default_store()
    names = names if names is not None else self_names()
    hidden_set = set(hidden)

    buckets = _bucket(store.commitments(status="open", limit=500), names, now=now, hidden=hidden_set)
    memory = store.stats(now=now)

    series, shifts = _trends(store, memory, now=now)
    series = [s for s in series if _pattern_id(s.key) not in hidden_set]
    payload = {
        **buckets,
        "patterns": [_pattern_dict(s, now=now) for s in series[:MAX_PATTERNS]],
        "attention": _attention(series, shifts, now=now),
        "memory": memory,
        "self_names": names,
    }
    return payload


def count(radar: dict[str, Any]) -> int:
    return sum(len(radar.get(k) or []) for k in ("todo", "waiting", "open_calls", "upcoming"))
