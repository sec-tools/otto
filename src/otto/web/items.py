"""
The briefing as a native client sees it — ``GET /api/items``.

The web page renders HTML; the menu bar panel and the CLI want the same
decisions (what is *Important*, what is *For you*, which radar rows are still
open, what the digest says) as plain JSON without re-deriving them. This module
applies exactly the grouping :func:`otto.web.render.render_body` uses and
flattens each item to the handful of fields a row needs.

Nothing here is new judgement: titles, lines, chips and groups are the ones the
page shows. The payload carries the thumbnail both as the URL the engine serves
and as the absolute file path, so a native client on this machine can show the
original PNG at full resolution without a copy.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from otto import paths
from otto.web.render import (
    digest_is_stale, digest_notes, flatten_items, for_you_line, headline, is_opportunity, item_reasons, item_title,
    note_id, radar_rows, source_health, urgency_level, worth_knowing_count,
    _RADAR_SECTIONS, RECURRING_HEADING, WORTH_KNOWING_LABEL, WORTH_KNOWING_SUB, WORTH_KNOWING_EMPTY, _short_reason, _SRC_NAMES,
)
from otto.web.security import safe_url

IMPORTANT_MIN = 0.7
FOR_YOU_MIN = 0.3

GROUPS: tuple[tuple[str, str], ...] = (
    ("important", "Needs you"),
    ("for_you", "For you"),
    ("worth_a_look", "Worth a look"),
    ("also_noticed", "Also noticed"),
)


def client_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """One row's worth of an item, with every URL checked and the thumbnail resolved."""
    shot = str(item.get("screenshot") or "").strip()
    shot_path = ""
    if shot and "/" not in shot and not shot.startswith("."):
        candidate = paths.screenshots_dir() / shot
        shot_path = str(candidate) if candidate.is_file() else ""
    urgency = float(item.get("_urgency_score") or 0.0)
    reasons = [
        {"kind": str(r.get("kind") or ""), "label": str(r.get("label") or ""),
         "tone": str(r.get("tone") or "quiet"), "detail": str(r.get("detail") or "")}
        for r in item_reasons(item)
    ]
    return {
        "id": str(item.get("id") or ""),
        "title": item_title(item),
        "line": for_you_line(item),
        "summary": str(item.get("summary") or ""),
        "why": reasons,
        "urgency": str(item.get("urgency") or ""),
        "urgency_score": round(urgency, 3),
        "level": urgency_level(item),
        "source": str(item.get("_source_group") or ""),
        "channel": str(item.get("_channel_name") or ""),
        "sender": str(item.get("sender") or ""),
        "time": str(item.get("time_display") or ""),
        "timestamp": str(item.get("timestamp") or ""),
        "url": safe_url(item.get("source_url") or ""),
        "external_url": safe_url(item.get("external_url") or ""),
        "screenshot_url": f"/static/screenshots/{shot}" if shot_path else "",
        "screenshot_path": shot_path,
        "action_items": [a for a in (item.get("action_items") or []) if isinstance(a, str)][:5],
        "occurrences": int(item.get("occurrence_count") or 1),
    }


def _group(items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    important = [x for x in items if x["_urgency_score"] >= IMPORTANT_MIN]
    for_you = [x for x in items if FOR_YOU_MIN <= x["_urgency_score"] < IMPORTANT_MIN]
    quiet = [x for x in items if x["_urgency_score"] < FOR_YOU_MIN]
    return {
        "important": important,
        "for_you": for_you,
        "worth_a_look": [x for x in quiet if is_opportunity(x)],
        "also_noticed": [x for x in quiet if not is_opportunity(x)],
    }


def _radar(data: Dict[str, Any], hidden: Iterable[str]) -> Dict[str, Any]:
    rows = radar_rows(data, hidden)
    sections = []
    for key, heading, glyph in _RADAR_SECTIONS:
        if not rows.get(key):
            continue
        sections.append({
            "key": key, "heading": heading, "glyph": glyph,
            "rows": [{
                "id": str(r.get("id") or ""), "kind": str(r.get("kind") or ""),
                "what": str(r.get("what") or ""), "who": str(r.get("who") or ""),
                "channel": str(r.get("channel") or ""), "due": str(r.get("due_label") or ""),
                "overdue": bool(r.get("overdue")), "soon": bool(r.get("soon")),
                "url": safe_url(r.get("url") or ""),
            } for r in rows[key]],
        })
    recurring = [{
        "id": str(p.get("id") or ""), "label": str(p.get("label") or ""), "channel": str(p.get("channel") or ""),
        "cadence": str(p.get("cadence") or ""), "late_by": str(p.get("late_by") or ""),
        "next_expected": str(p.get("next_expected") or ""), "n": int(p.get("n") or 0), "span": str(p.get("span") or ""),
    } for p in rows.get("patterns") or []]
    radar = data.get("radar") or {}
    hidden_set = set(hidden)
    return {
        "sections": sections,
        "recurring": recurring,
        "recurring_heading": RECURRING_HEADING,
        "attention": [str(a) for a in (radar.get("attention") or []) if a and note_id(a) not in hidden_set][:3],
        "memory": radar.get("memory") or {},
    }


def build_items(data: Dict[str, Any], hidden: Iterable[str] = ()) -> Dict[str, Any]:
    """Everything a native client needs to draw the briefing, grouped the way the page groups it."""
    from otto.intelligence.relevance import header_why

    hidden = set(hidden)
    items = flatten_items(data, hidden)
    grouped = _group(items)
    _online, failed = source_health(data)
    blocked = [_SRC_NAMES.get(s, s.title()) for s, why in failed if _short_reason(why) != "not open"]
    digest = data.get("digest") or {}
    if digest_is_stale(digest, len(items)):
        digest = {}          # written about items you have since cleared; the next refresh rewrites it
    # Heads-ups, predictions and the radar are "worth knowing", not
    # notifications: the panel keeps them behind one button, and each note has
    # an id so it can be dismissed like a row (and all of it cleared at once).
    notes = digest_notes(data, hidden, n_items=len(items))
    return {
        "headline": headline(items, data),
        "why": header_why(items),
        "digest": {
            "text": str(digest.get("digest") or ""),
            "connections": digest.get("connections") or [],
            "predictions": [p for p in (digest.get("predictions") or [])
                            if not (isinstance(p, dict) and note_id(p.get("note") or "") in hidden)],
            "heads_up": [t for t in (digest.get("heads_up") or []) if note_id(t) not in hidden],
            "source": str(digest.get("source") or ""),
        },
        "generated_at": str(data.get("generated_at") or ""),
        "generated_at_human": str(data.get("generated_at_human") or ""),
        "count": len(items),
        "important_count": len(grouped["important"]),
        "groups": [
            {"key": key, "label": label, "items": [client_item(x) for x in grouped[key]]}
            for key, label in GROUPS if grouped[key]
        ],
        "radar": _radar(data, hidden),
        "worth_knowing": {
            "label": WORTH_KNOWING_LABEL, "sub": WORTH_KNOWING_SUB, "empty": WORTH_KNOWING_EMPTY,
            "count": worth_knowing_count(data, hidden, notes=notes),
            "notes": notes,
        },
        "blocked_sources": blocked[:3],
        "screenshots": data.get("screenshots") or {},
    }
