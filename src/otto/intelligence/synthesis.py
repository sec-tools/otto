"""
Briefing-level synthesis — the short "here's what's going on" a sharp
colleague would say about everything on your briefing right now.

Per-item classification tells you why *one* thread matters. This module looks
across all of them plus the radar (open loops, upcoming dates, recurring
series) and produces:

* ``digest``       — one or two sentences on what matters most right now
* ``connections``  — items that belong together (same incident, same person,
                      cause and effect), named by item id
* ``predictions``  — what is likely to happen or be needed next, with the
                      facts it rests on
* ``heads_up``     — things that are easy to miss (an ask nobody answered,
                      a series that is late)

Costs at most one model call per *changed* briefing: the input is fingerprinted
and the result cached (``synthesis_cache.json``), so a quiet hour where nothing
new arrives costs nothing. Without a model the digest is composed locally from
the same facts and says less — but never nothing.

Everything the model sees has been through the same PII redaction and
prompt-injection scrub as the conversations themselves; everything it returns
is checked for write intent, clipped, and its item ids verified against the
briefing before any of it is shown.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable

from otto import paths
from otto.llm.injection_defense import sanitize_for_llm
from otto.llm.prompts import SYNTHESIZE_BRIEFING
from otto.utils.pii_redactor import redact_pii

logger = logging.getLogger("otto.intelligence.synthesis")

MAX_ITEMS = 14                 # the most urgent items go in; the long tail is summarised by count
MAX_RADAR_ROWS = 6             # per radar section
MAX_DIGEST_CHARS = 240
MAX_NOTE_CHARS = 200
MAX_LIST = 3
INPUT_MAX_CHARS = 7000
LLM_TIMEOUT_S = 15.0
CACHE_TTL_S = 6 * 3600
CACHE_MAX = 40

_RADAR_LABELS = (
    ("todo", "to do"),
    ("waiting", "waiting on"),
    ("open_calls", "nobody has taken this"),
    ("upcoming", "coming up"),
)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def digest_input(items: list[dict[str, Any]], radar: dict[str, Any] | None) -> tuple[str, set[str]]:
    """
    The facts the digest may use, as compact text, plus the set of item ids in it.

    *items* are visible items as :func:`otto.web.render.flatten_items` returns
    them (most urgent first). Only what is on the briefing goes in — dismissed
    and snoozed items are not the model's business.
    """
    from otto.web.render import item_reasons, item_title

    lines: list[str] = []
    ids: set[str] = set()
    shown = items[:MAX_ITEMS]
    for it in shown:
        iid = str(it.get("id") or "")
        if not iid:
            continue
        ids.add(iid)
        channel = str(it.get("_channel_name") or "")
        sender = str(it.get("sender") or "")
        when = str(it.get("time_display") or "")
        try:
            urgency = float(it.get("_urgency_score") or 0.0)
        except (TypeError, ValueError):
            urgency = 0.0
        meta = " · ".join(b for b in (channel, sender, when) if b)
        lines.append(f"- [{iid}] ({meta}; urgency {urgency:.1f}) {item_title(it)}")
        why = ", ".join(str(r.get("label")) for r in item_reasons(it)[:3])
        if why:
            lines.append(f"  ties: {why}")
        tie = str(it.get("for_you") or "").strip()
        if tie:
            lines.append(f"  for you: {tie}")
        summary = str(it.get("summary") or "").strip()
        if summary and summary.lower() != tie.lower():
            lines.append(f"  summary: {summary[:300]}")
    if len(items) > len(shown):
        lines.append(f"- ({len(items) - len(shown)} quieter items not listed)")

    radar = radar or {}
    for key, label in _RADAR_LABELS:
        rows = [r for r in (radar.get(key) or []) if isinstance(r, dict)][:MAX_RADAR_ROWS]
        if not rows:
            continue
        lines.append(f"radar · {label}:")
        for r in rows:
            who = str(r.get("who") or "")
            what = str(r.get("what") or "")
            due = str(r.get("due_label") or "")
            flag = " OVERDUE" if r.get("overdue") else (" soon" if r.get("soon") else "")
            bits = " · ".join(b for b in (who, str(r.get("channel") or ""), due) if b)
            lines.append(f"  - {what}" + (f" ({bits}{flag})" if bits or flag else ""))
    series = [p for p in (radar.get("patterns") or []) if isinstance(p, dict)][:MAX_RADAR_ROWS]
    if series:
        lines.append("radar · keeps coming back:")
        for p in series:
            timing = f"late {p['late_by']}" if p.get("late_by") else (f"next {p['next_expected']}" if p.get("next_expected") else "")
            bits = " · ".join(b for b in (str(p.get("channel") or ""), str(p.get("cadence") or ""), timing) if b and b != "irregular")
            lines.append(f"  - {p.get('label', '')}" + (f" ({bits})" if bits else ""))
    for note in (radar.get("attention") or [])[:3]:
        if note:
            lines.append(f"radar · note: {note}")

    text = "\n".join(lines)
    return text[:INPUT_MAX_CHARS], ids


def fingerprint(text: str, salt: str = "") -> str:
    return hashlib.sha256(f"{salt}\x1f{text}".encode("utf-8", errors="replace")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Local fallback
# ---------------------------------------------------------------------------

def local_digest(items: list[dict[str, Any]], radar: dict[str, Any] | None, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """The same shape as the model's answer, composed from the facts alone."""
    from otto.web.render import headline, item_title

    radar = radar or {}
    heads: list[str] = []
    overdue = [r for r in (radar.get("todo") or []) if isinstance(r, dict) and r.get("overdue")]
    if overdue:
        heads.append(f"Overdue: {overdue[0].get('what', '')}" + (f" ({len(overdue)} in all)" if len(overdue) > 1 else ""))
    waiting = [r for r in (radar.get("waiting") or []) if isinstance(r, dict)]
    if waiting:
        who = str(waiting[0].get("who") or "someone")
        heads.append(f"Still waiting on {who}: {waiting[0].get('what', '')}")
    late = [p for p in (radar.get("patterns") or []) if isinstance(p, dict) and p.get("late_by")]
    if late:
        heads.append(f"{late[0].get('label', 'A recurring update')} is late by {late[0]['late_by']}")
    for note in (radar.get("attention") or []):
        if note and len(heads) < MAX_LIST:
            heads.append(str(note))

    digest = headline(items, data)
    urgent = [x for x in items if float(x.get("_urgency_score") or 0) >= 0.7]
    if urgent:
        first = item_title(urgent[0])
        where = str(urgent[0].get("_channel_name") or "")
        lead = f"{first}" + (f" in {where}" if where else "")
        digest = f"{lead}. " + (f"{len(urgent) - 1} more need you." if len(urgent) > 1 else "Nothing else is pressing.")
    return {
        "digest": _clip(digest, MAX_DIGEST_CHARS),
        "connections": [],
        "predictions": [],
        "heads_up": [_clip(hh, MAX_NOTE_CHARS) for hh in heads[:MAX_LIST]],
        "source": "local",
        "items": len(items),      # what it was written about; 0 = radar only
    }


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

async def synthesize(
    llm: Any,
    items: list[dict[str, Any]],
    radar: dict[str, Any] | None,
    *,
    data: dict[str, Any] | None = None,
    profile_salt: str = "",
    cache_path: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """
    The digest for this briefing: cached → model → local, in that order.

    Never raises; a failed or slow model falls back to the local digest for
    this refresh and is retried when the briefing next changes.
    """
    text, ids = digest_input(items, radar)
    fallback = local_digest(items, radar, data)
    if not items and not any((radar or {}).get(k) for k, _ in _RADAR_LABELS):
        return fallback

    key = fingerprint(text, profile_salt)
    cache = _Cache(cache_path or paths.synthesis_cache_file())
    cached = cache.get(key, now=now)
    if cached is not None:
        return {**cached, "cached": True}
    if llm is None:
        return fallback

    prompt_input = sanitize_for_llm(redact_pii(text), max_length=INPUT_MAX_CHARS)
    try:
        response = await asyncio.wait_for(
            llm.complete(task="briefing_synthesize", system_prompt=SYNTHESIZE_BRIEFING,
                         user_message=prompt_input, max_tokens=700),
            timeout=LLM_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.info("briefing synthesis hit its %.0fs budget; using the local digest", LLM_TIMEOUT_S)
        return fallback
    except Exception as e:
        logger.warning("briefing synthesis failed: %s", e)
        return fallback
    if not response or not getattr(response, "content", ""):
        return fallback

    result = parse_synthesis(response.content, ids)
    if result is None:
        return fallback
    result["source"] = "model"
    result["model"] = str(getattr(response, "model", "") or "")
    result["items"] = len(items)
    if not result.get("digest"):
        result["digest"] = fallback["digest"]
    if not result.get("heads_up"):
        result["heads_up"] = fallback["heads_up"]
    cache.put(key, result, now=now)
    return result


def parse_synthesis(content: str, known_ids: Iterable[str]) -> dict[str, Any] | None:
    """Validate and clip the model's JSON; None when it is unusable."""
    from otto.intelligence.classifier import _extract_json, to_second_person
    from otto.llm.write_detector import scan_for_write_intent

    if scan_for_write_intent(content).has_write_intent:
        logger.warning("write intent in briefing synthesis — quarantined")
        return None
    try:
        data = _extract_json(content)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    known = set(known_ids)

    digest = to_second_person(_clean(data.get("digest")))
    connections: list[dict[str, Any]] = []
    for c in (data.get("connections") or [])[:MAX_LIST * 2]:
        if not isinstance(c, dict):
            continue
        cids = [str(i) for i in (c.get("ids") or []) if str(i) in known]
        note = to_second_person(_clean(c.get("note")))
        if len(cids) >= 2 and note:
            connections.append({"ids": cids[:6], "note": _clip(note, MAX_NOTE_CHARS)})
    predictions: list[dict[str, str]] = []
    for p in (data.get("predictions") or [])[:MAX_LIST * 2]:
        if not isinstance(p, dict):
            continue
        note = to_second_person(_clean(p.get("note")))
        if note:
            predictions.append({"note": _clip(note, MAX_NOTE_CHARS), "basis": _clip(_clean(p.get("basis")), MAX_NOTE_CHARS)})
    heads = [_clip(to_second_person(_clean(x)), MAX_NOTE_CHARS) for x in (data.get("heads_up") or []) if _clean(x)]
    return {
        "digest": _clip(digest, MAX_DIGEST_CHARS),
        "connections": connections[:MAX_LIST],
        "predictions": predictions[:MAX_LIST],
        "heads_up": heads[:MAX_LIST],
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _clean(value: Any) -> str:
    if value is None or isinstance(value, (list, dict, bool)):
        return ""
    return " ".join(str(value).split()).strip()


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for end in (". ", "! ", "? "):
        idx = cut.rfind(end)
        if idx > limit // 2:
            return cut[:idx + 1]
    idx = cut.rfind(" ")
    return (cut[:idx] if idx > limit // 2 else cut).rstrip(" ,;:—-") + "…"


class _Cache:
    """A tiny JSON file: fingerprint → result, pruned by age and count."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, Any]:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return {k: v for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, ValueError) as e:
            logger.debug("synthesis cache unreadable (%s); starting fresh", e)
        return {}

    def get(self, key: str, *, now: float | None = None) -> dict[str, Any] | None:
        entry = self._load().get(key)
        if not entry:
            return None
        if (now if now is not None else time.time()) - float(entry.get("_ts", 0)) >= CACHE_TTL_S:
            return None
        return {k: v for k, v in entry.items() if not k.startswith("_")}

    def put(self, key: str, value: dict[str, Any], *, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        data = {k: v for k, v in self._load().items() if now - float(v.get("_ts", 0)) < CACHE_TTL_S}
        data[key] = {**value, "_ts": now}
        if len(data) > CACHE_MAX:
            data = dict(sorted(data.items(), key=lambda kv: float(kv[1].get("_ts", 0)), reverse=True)[:CACHE_MAX])
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.debug("could not write synthesis cache: %s", e)
