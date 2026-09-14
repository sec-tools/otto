"""
Why this is in front of you — the evidence, not the adjective.

A briefing earns trust when every card can answer "why me, why now" with
something the user can check: a directive of theirs it touches, an ask or a
mention aimed at them, a thread they wrote in, a deadline, a severity, a
person they hear from every day (or have never heard from), a project they
said they care about. This module derives those *reasons* from the data —
never from the model — so the page can show them as-is and the model can be
asked to explain in the same terms instead of inventing involvement.

Each :class:`Reason` is short enough to sit on one line of a card and carries
the evidence as ``detail`` for a tooltip or the CLI. Reasons are ordered by
how directly they tie the item to the user; the strongest few are shown.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from otto.intelligence import commitments as cm
from otto.intelligence.trends import humanize_duration

MAX_REASONS = 4
WEEK_SECONDS = 7 * 86400
# A person you hear from at least this often in a week is a "regular".
REGULAR_MIN_MESSAGES = 3

_EVERYONE = re.compile(r"(?<![\w@])@(here|channel|everyone)\b", re.IGNORECASE)
_COUNT_CRITICAL = re.compile(r"\b(\d{1,3})\s*critical\b", re.IGNORECASE)
_COUNT_HIGH = re.compile(r"\b(\d{1,3})\s*high\b", re.IGNORECASE)
_BRACKET_SEVERITY = re.compile(r"\[(CRITICAL|HIGH)\s+(\d{1,2}(?:\.\d)?)\]")
_CVSS = re.compile(r"\bCVSS[:\s]*(\d{1,2}(?:\.\d)?)\b", re.IGNORECASE)

_OPPORTUNITY_TYPES = {
    "new_project": "new project", "collaboration": "collaboration", "role_opening": "role opening",
    "process_improvement": "process improvement", "cost_saving": "cost saving",
    "tool_recommendation": "tool worth a look", "knowledge_sharing": "knowledge sharing",
    "reconnection": "reconnection", "event": "event", "deal": "deal",
}

# kind → (tone, weight). Tone is the visual family; weight orders reasons.
_KINDS: dict[str, tuple[str, int]] = {
    "asked": ("you", 100),
    "answered": ("you", 96),
    "reply": ("you", 95),
    "dm": ("you", 90),
    "mention": ("you", 85),
    "directive": ("directive", 80),
    "overdue": ("due", 78),
    "severity": ("alert", 75),
    "deadline": ("due", 70),
    "focus": ("focus", 65),
    "promised": ("you", 60),
    "waiting": ("quiet", 55),
    "open_call": ("quiet", 50),
    "opportunity": ("opportunity", 45),
    "event": ("due", 42),
    "thread": ("quiet", 40),
    "new_person": ("quiet", 35),
    "handled": ("quiet", 33),
    "title": ("quiet", 32),
    "person": ("quiet", 30),
    "everyone": ("quiet", 25),
    "own": ("quiet", 10),
}

# Reactions Slack users leave to say "seen / on it / done" — an ask carrying
# one of these has been picked up by somebody, which changes what it asks of you.
ACK_REACTIONS = frozenset({
    "white_check_mark", "heavy_check_mark", "ballot_box_with_check", "eyes", "+1", "thumbsup",
    "ok_hand", "raised_hands", "pray", "saluting_face", "on_it", "done", "check",
})


@dataclass(frozen=True)
class Reason:
    kind: str
    label: str
    detail: str = ""

    @property
    def tone(self) -> str:
        return _KINDS.get(self.kind, ("quiet", 0))[0]

    @property
    def weight(self) -> int:
        return _KINDS.get(self.kind, ("quiet", 0))[1]

    def to_dict(self) -> dict[str, str]:
        d = {"kind": self.kind, "label": self.label, "tone": self.tone}
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class PeopleIndex:
    """Who the user hears from, computed once per refresh from the knowledge store."""
    week: dict[str, int]        # lowercase sender → messages in the last 7 days
    known: set[str]             # lowercase senders ever remembered (humans)

    @classmethod
    def empty(cls) -> "PeopleIndex":
        return cls(week={}, known=set())

    @classmethod
    def from_store(cls, store: Any, *, now: float | None = None) -> "PeopleIndex":
        now = now if now is not None else time.time()
        if store is None:
            return cls.empty()
        try:
            week = {p.sender.lower(): int(p.total) for p in store.people(since=now - WEEK_SECONDS) if p.sender}
            known = {p.sender.lower() for p in store.people() if p.sender}
        except Exception:
            return cls.empty()
        return cls(week=week, known=known)

    def weekly(self, sender: str) -> int:
        return self.week.get((sender or "").strip().lower(), 0)

    def is_known(self, sender: str) -> bool:
        return (sender or "").strip().lower() in self.known


def _aware(ts: Any) -> datetime:
    if not isinstance(ts, datetime):
        return datetime.now(timezone.utc)
    if ts.tzinfo is None:
        return ts.astimezone(timezone.utc)
    return ts


def _ago(ts: Any, now: float) -> str:
    delta = now - _aware(ts).timestamp()
    return "just now" if delta < 60 else f"{humanize_duration(delta)} ago"


def _clip(text: str, limit: int = 90) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def opportunity_kind(kind: str) -> str:
    """``tool_recommendation`` → ``tool worth a look``."""
    k = (kind or "").strip().lower()
    return _OPPORTUNITY_TYPES.get(k, k.replace("_", " ")) if k and k != "null" else ""


def severity_label(text: str) -> str:
    """``"4 critical · 2 high"`` / ``"CRITICAL 9.8"`` / ``"CVSS 9.8"``; '' when nothing non-zero."""
    t = text or ""
    crit = _COUNT_CRITICAL.search(t)
    high = _COUNT_HIGH.search(t)
    parts = []
    if crit and int(crit.group(1)) > 0:
        parts.append(f"{int(crit.group(1))} critical")
    if high and int(high.group(1)) > 0:
        parts.append(f"{int(high.group(1))} high")
    if parts:
        return " · ".join(parts)
    m = _BRACKET_SEVERITY.search(t)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    m = _CVSS.search(t)
    if m and float(m.group(1)) >= 7.0:
        return f"CVSS {m.group(1)}"
    return ""


def focus_hits(text: str, focus: Iterable[str]) -> list[str]:
    """Which of the user's focus terms appear in ``text`` (whole words; hyphens and slashes allowed)."""
    hits: list[str] = []
    low = text or ""
    for term in focus or ():
        t = str(term or "").strip()
        if len(t) < 3:
            continue
        if re.search(r"(?<![\w-])" + re.escape(t) + r"(?![\w-])", low, re.IGNORECASE):
            hits.append(t)
    return hits


def _due_bits(c: cm.Commitment, now: float) -> tuple[str, bool]:
    """→ (label like 'tomorrow 5 PM', overdue?) for a commitment's due date."""
    if c.due is None:
        return "", False
    from otto.intelligence.radar import due_label
    label, overdue, _soon = due_label(_aware(c.due).timestamp(), now=now)
    return label, overdue


def reasons_for(
    conv: Any,
    events: Sequence[Any],
    *,
    names: Iterable[str],
    channel: str = "",
    profile: dict[str, Any] | None = None,
    people: PeopleIndex | None = None,
    now: float | None = None,
) -> list[Reason]:
    """The provable reasons this conversation is for the user, strongest first (≤ ``MAX_REASONS``).

    ``events`` are oldest → newest. ``names`` are the user's own display names
    (see :mod:`otto.utils.identity`); ``profile`` is ``{"role": …, "focus": […]}``.
    """
    evs = [e for e in events if e is not None]
    if not evs:
        return []
    now = now if now is not None else time.time()
    names = [n for n in (names or ()) if n]
    profile = profile or {}
    newest = evs[-1]
    newest_sender = (getattr(newest, "sender", "") or "").strip()
    newest_is_you = cm.is_you(newest_sender, names)
    newest_is_bot = bool(getattr(newest, "is_auto_generated", False))
    text_new = getattr(newest, "plain_text_extract", "") or ""
    text_all = " \n".join((getattr(e, "plain_text_extract", "") or "") for e in evs)
    channel = channel or (getattr(conv, "subject", "") or "")
    is_dm = channel.startswith("@") or channel.lower().startswith("dm-")
    own = [e for e in evs if cm.is_you((getattr(e, "sender", "") or "").strip(), names)]

    found: dict[str, Reason] = {}

    def put(kind: str, label: str, detail: str = "") -> None:
        if kind not in found:
            found[kind] = Reason(kind, label, _clip(detail))

    # --- your involvement ------------------------------------------------
    if newest_is_you:
        detail = f"you wrote {_ago(newest.timestamp, now)}"
        if len(evs) > 1:
            detail = f"you replied {_ago(newest.timestamp, now)} in a thread of {len(evs)}"
        put("own", "Your message", detail)
    elif own:
        last_own = own[-1]
        who = newest_sender or "someone"
        put("reply", "New reply in your thread",
            f"{who} replied {_ago(newest.timestamp, now)}; you wrote {_ago(last_own.timestamp, now)}")
    if is_dm and not newest_is_you:
        put("dm", "Direct message", f"from {newest_sender}" if newest_sender else "")
    if not newest_is_you and cm.mentions_you(text_new, names):
        put("mention", "Mentions you", f"{newest_sender or 'someone'} named you")
    m_all = _EVERYONE.search(text_new)
    if m_all and not newest_is_you:
        put("everyone", "@" + m_all.group(1).lower(), "pinged the whole channel")

    # --- asks, promises, deadlines in the newest message -----------------
    try:
        commitments = cm.extract(
            text_new, sender=newest_sender, ts=_aware(getattr(newest, "timestamp", None)),
            channel=channel, self_names=names, is_bot=newest_is_bot,
        )
    except Exception:
        commitments = []
    for c in sorted(commitments, key=lambda c: -c.confidence):
        due, overdue = _due_bits(c, now)          # due_label already says "overdue 2 h"
        due_suffix = (f" · {due}" if overdue else f" · due {due}") if due else ""
        if c.kind == "ask" and c.for_you:
            put("asked", "Asked of you", c.what + due_suffix)
        elif c.kind == "ask" and c.open_call:
            put("open_call", "Nobody has taken this", c.what + due_suffix)
        elif c.kind == "ask" and cm.is_you(c.who, names):
            put("waiting", "You're waiting on this", c.what + due_suffix)
        elif c.kind == "promise" and cm.is_you(c.who, names):
            put("promised", "You promised", c.what + due_suffix)
        elif c.kind == "promise" and c.who:
            put("waiting", f"{c.who} promised", c.what + due_suffix)
        elif c.kind == "deadline" and due:
            put("overdue" if overdue else "deadline", due[0].upper() + due[1:] if overdue else f"Due {due}", c.what)
        elif c.kind == "event" and due:
            put("event", f"Event {due}", c.what)

    # --- what you told Otto matters ---------------------------------------
    directive = (getattr(conv, "matched_directive", "") or "").strip()
    if directive:
        put("directive", "Your directive", directive)
    hits = focus_hits(text_all, profile.get("focus") or [])
    if hits:
        more = f" · also {', '.join(hits[1:4])}" if len(hits) > 1 else ""
        put("focus", f"Your focus: {hits[0]}", "listed under [user] focus in your config" + more)

    # --- what the message itself proves -----------------------------------
    sev = severity_label(text_all)
    if sev:
        put("severity", sev, "severity stated in the message")
    score = float(getattr(conv, "opportunity_score", 0.0) or 0.0)
    kind = opportunity_kind(getattr(conv, "opportunity_type", "") or "")
    # Your own words are not an opportunity *for you* — you already know them.
    if score >= 0.4 and (kind or getattr(conv, "opportunity_description", "")) and not newest_is_you:
        put("opportunity", "Opportunity" + (f" · {kind}" if kind else ""),
            getattr(conv, "opportunity_description", "") or "")

    # --- what the reader knew beyond the text (Slack API: reactions, titles) --
    meta_new = getattr(newest, "meta", None) or {}
    acks = sorted(set(str(r) for r in (meta_new.get("reactions") or [])) & ACK_REACTIONS)
    if acks and not newest_is_you and any(c.kind == "ask" for c in commitments):
        put("handled", "Someone's on it", "reacted with :" + ": :".join(acks[:3]) + ": — picked up, not necessarily done")
    # You asked something earlier in this thread and somebody answered since.
    if own and not newest_is_you and "reply" in found:
        asked_by_you = any(
            "?" in (getattr(e, "plain_text_extract", "") or "") for e in own
        )
        if asked_by_you:
            found.pop("reply", None)
            put("answered", "Reply to your question",
                f"{newest_sender or 'someone'} answered {_ago(newest.timestamp, now)}; "
                f"you asked {_ago(own[-1].timestamp, now)}")
    title = str(meta_new.get("sender_title") or "").strip()
    if title and newest_sender and not newest_is_you and not newest_is_bot:
        put("title", f"{newest_sender} · {title}", "title from their Slack profile")

    # --- who it is from -----------------------------------------------------
    if people is not None and newest_sender and not newest_is_you and not newest_is_bot:
        n = people.weekly(newest_sender)
        if n >= REGULAR_MIN_MESSAGES:
            put("person", f"{newest_sender} · {n} messages this week", "someone you hear from regularly")
        elif not people.is_known(newest_sender):
            put("new_person", f"First message from {newest_sender}", "not seen in Otto's memory before")
    if len(evs) > 1 and "own" not in found and "reply" not in found and "answered" not in found:
        senders = {(getattr(e, "sender", "") or "").strip() for e in evs} - {""}
        put("thread", f"Thread · {len(evs)} messages", ", ".join(sorted(senders)[:4]))

    # An ask aimed at you already implies the mention; a reply already implies the thread.
    if "asked" in found:
        found.pop("mention", None)
    if "reply" in found or "own" in found or "answered" in found:
        found.pop("thread", None)
    if "new_person" in found:
        found.pop("person", None)
    if "title" in found and ("person" in found or "new_person" in found):
        # One line about the person is enough; the title rides along as detail.
        who = found.get("person") or found.get("new_person")
        if who is not None:
            found[who.kind] = Reason(who.kind, who.label, _clip(f"{title} · {who.detail}" if who.detail else title))
        found.pop("title", None)

    ordered = sorted(found.values(), key=lambda r: -r.weight)
    return ordered[:MAX_REASONS]


def why_line(reasons: Iterable[Any]) -> str:
    """``"Asked of you · Your directive · 4 critical"`` for a text surface."""
    labels = []
    for r in reasons or ():
        label = r.label if isinstance(r, Reason) else (r or {}).get("label", "")
        if label:
            labels.append(label)
    return " · ".join(labels)


def evidence_text(reasons: Iterable[Reason]) -> str:
    """Bullet list of reasons with their evidence, for the model's context."""
    lines = []
    for r in reasons or ():
        lines.append(f"- {r.label}" + (f": {r.detail}" if r.detail else ""))
    return "\n".join(lines)


# Header summary: which kinds are worth a phrase, in the order they are said.
_HEADLINE_PHRASES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("asked",), "{n} asks you for something", "{n} ask you for something"),
    (("reply",), "{n} new reply in a thread of yours", "{n} new replies in threads of yours"),
    (("answered",), "{n} answer to a question of yours", "{n} answers to questions of yours"),
    (("mention", "dm"), "{n} is addressed to you", "{n} are addressed to you"),
    (("overdue", "deadline"), "{n} has a deadline", "{n} have deadlines"),
    (("directive",), "{n} touches a directive of yours", "{n} touch your directives"),
    (("severity",), "{n} security finding", "{n} security findings"),
    (("focus",), "{n} is about your focus", "{n} are about your focus"),
    (("opportunity",), "{n} opportunity", "{n} opportunities"),
)


def header_why(items: Iterable[dict[str, Any]], limit: int = 4) -> str:
    """One line for the page header: what the visible items prove about the user's involvement."""
    counts: dict[str, int] = {}
    for item in items or ():
        kinds = {(r or {}).get("kind", "") for r in (item.get("why") or []) if isinstance(r, dict)}
        for kinds_group, _one, _many in _HEADLINE_PHRASES:
            if kinds & set(kinds_group):
                counts[kinds_group[0]] = counts.get(kinds_group[0], 0) + 1
    phrases = []
    for kinds_group, one, many in _HEADLINE_PHRASES:
        n = counts.get(kinds_group[0], 0)
        if n:
            phrases.append((one if n == 1 else many).format(n=n))
    return " · ".join(phrases[:limit])
