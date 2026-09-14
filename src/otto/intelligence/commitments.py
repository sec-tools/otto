"""
Commitments, asks, deadlines and dated events — the "don't drop this" layer.

Pure functions over message text. Nothing here touches a source or the
network; the store and the radar decide what to do with the results.

What is extracted (``Commitment.kind``):

* ``promise``  — the sender says they will do something
                 ("I'll send the deck by Thursday", "let me look into it").
* ``ask``      — someone asks a person to do something ("can you review…",
                 "please update the ticket", "reminder: submit timesheets").
                 ``for_you`` is set when the message names the user or is a DM.
* ``deadline`` — something is due at a time ("cert expires Sep 20",
                 "deadline is EOD Friday").
* ``event``    — something happens at a time ("demo on Tuesday at 3pm",
                 "I'll be out next week").

Due dates are resolved with :func:`find_due` relative to the message time
in the machine's local time zone — "by Friday" means the Friday after the
message was written, not after Otto happened to read it.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY_RE = r"(?P<wd>mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)(?:[a-z]*day)?"
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_RE = r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "couple of": 2, "few": 3}

END_OF_DAY_HOUR = 17

# Day anchors ("Friday", "tomorrow", "Sep 14"), time-of-day modifiers ("EOD",
# "tonight", "this afternoon") and clock times ("3pm") are matched separately
# and combined, so "EOD Friday", "Friday EOD" and "Friday at 3pm" all resolve.
_ANCHOR_RE = re.compile(
    r"(?P<eow>\b(?:eow|end of (?:the |this )?week)\b)"
    r"|(?P<eom>\b(?:eom|end of (?:the |this )?month)\b)"
    r"|(?P<eoq>\b(?:eoq|end of (?:the |this )?quarter)\b)"
    r"|(?P<today>\b(?:later )?today\b)"
    r"|(?P<tomorrow>\b(?:first thing )?tomorrow\b)"
    r"|(?P<next_week>\bnext week\b)"
    r"|(?P<this_week>\bthis week\b)"
    r"|(?P<in_delta>\bin (?P<delta_n>\d{1,3}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|couple of|few) "
    r"(?P<delta_u>minutes?|mins?|hours?|hrs?|days?|weeks?|months?)\b)"
    r"|(?P<weekday>\b(?P<wd_prefix>next |this |last |coming )?" + _WEEKDAY_RE + r"\b)"
    r"|(?P<month_day>\b" + _MONTH_RE + r"\s+(?P<md_day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<md_year>20\d{2}))?\b)"
    r"|(?P<day_month>\b(?P<dm_day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<dm_mon>jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b)"
    r"|(?P<iso>\b(?P<iso_y>20\d{2})-(?P<iso_m>\d{2})-(?P<iso_d>\d{2})\b)"
    r"|(?P<slash>\b(?P<sl_m>\d{1,2})/(?P<sl_d>\d{1,2})(?:/(?P<sl_y>\d{2,4}))?\b)",
    re.IGNORECASE,
)

_MODIFIER_RE = re.compile(
    r"(?P<eod>\b(?:eod|cob|end of (?:the )?(?:day|business)|close of business)\b)"
    r"|(?P<morning>\b(?:this |in the |tomorrow )?morning\b|\bfirst thing\b)"
    r"|(?P<afternoon>\b(?:this |in the |tomorrow )?afternoon\b)"
    r"|(?P<evening>\b(?:this |in the |tomorrow )?(?:evening|night)\b|\btonight\b)",
    re.IGNORECASE,
)
_MODIFIER_HOUR = {"eod": END_OF_DAY_HOUR, "morning": 9, "afternoon": 14, "evening": 20}

_TIME_RE = re.compile(
    r"(?:\b(?:at|by|before|around|until|till|@)\s*)?"
    r"(?:(?P<noon>\bnoon\b)|(?P<midnight>\bmidnight\b)|"
    r"\b(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm|a\.m\.|p\.m\.)\b|"
    r"\b(?P<h24>\d{1,2}):(?P<m24>\d{2})\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Due:
    when: datetime              # aware, UTC
    text: str                   # the phrases that produced it, joined
    precise: bool               # True when a clock time was given
    phrases: tuple[str, ...] = ()


def _local(ref: datetime) -> datetime:
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return ref.astimezone()


def _at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _last_day_of_month(day: datetime) -> datetime:
    nxt = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


def _parse_time(text: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """A clock time in ``text`` → ((hour, minute), span). Bare numbers without am/pm are ignored."""
    for m in _TIME_RE.finditer(text):
        if m.group("noon"):
            return (12, 0), m.span()
        if m.group("midnight"):
            return (23, 59), m.span()
        if m.group("h"):
            hour = int(m.group("h"))
            minute = int(m.group("m") or 0)
            if hour > 12 or minute > 59:
                continue
            ampm = m.group("ampm").lower().replace(".", "")
            if ampm == "pm" and hour != 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
            return (hour, minute), m.span()
        if m.group("h24"):
            hour, minute = int(m.group("h24")), int(m.group("m24"))
            if hour <= 23 and minute <= 59:
                return (hour, minute), m.span()
    return None


def _resolve_anchor(m: "re.Match[str]", text: str, now: datetime) -> tuple[datetime | None, int, bool]:
    """→ (local day-level datetime, default hour, exact) or (None, …) when the phrase is history."""
    base = _at(now, 0)
    kind = m.lastgroup
    default_hour = END_OF_DAY_HOUR
    if kind == "eow":
        ahead = (4 - now.weekday()) % 7           # Friday; on a weekend this is next Friday
        if ahead == 0 and now.hour >= END_OF_DAY_HOUR:
            ahead = 7
        return _at(base + timedelta(days=ahead), END_OF_DAY_HOUR), default_hour, False
    if kind == "eom":
        return _at(_last_day_of_month(base), END_OF_DAY_HOUR), default_hour, False
    if kind == "eoq":
        q_end_month = ((now.month - 1) // 3 + 1) * 3
        return _at(_last_day_of_month(base.replace(month=q_end_month, day=1)), END_OF_DAY_HOUR), default_hour, False
    if kind == "today":
        return _at(base, END_OF_DAY_HOUR), default_hour, False
    if kind == "tomorrow":
        hour = 9 if m.group(0).lower().startswith("first thing") else END_OF_DAY_HOUR
        return _at(base + timedelta(days=1), hour), hour, False
    if kind == "next_week":
        ahead = (7 - now.weekday()) % 7 or 7
        return _at(base + timedelta(days=ahead), 9), 9, False
    if kind == "this_week":
        ahead = (4 - now.weekday()) % 7
        return _at(base + timedelta(days=ahead), END_OF_DAY_HOUR), default_hour, False
    if kind == "in_delta":
        n_raw = m.group("delta_n").lower()
        n = int(n_raw) if n_raw.isdigit() else _NUM_WORDS.get(n_raw, 1)
        unit = m.group("delta_u").lower()
        if unit.startswith("min"):
            return now + timedelta(minutes=n), default_hour, True
        if unit.startswith(("hour", "hr")):
            return now + timedelta(hours=n), default_hour, True
        if unit.startswith("day"):
            return _at(base + timedelta(days=n), END_OF_DAY_HOUR), default_hour, False
        if unit.startswith("week"):
            return _at(base + timedelta(weeks=n), END_OF_DAY_HOUR), default_hour, False
        return _at(base + timedelta(days=30 * n), END_OF_DAY_HOUR), default_hour, False
    if kind == "weekday":
        prefix = (m.group("wd_prefix") or "").strip().lower()
        before = text[max(0, m.start() - 24):m.start()].lower()
        if prefix == "last" or re.search(r"\b(?:last|past|since|yesterday)\s*$", before):
            return None, default_hour, False      # "last Friday" is history, not a deadline
        target = _weekday_index(m.group("wd"))
        ahead = (target - now.weekday()) % 7
        if ahead == 0 and (now.hour >= END_OF_DAY_HOUR or prefix == "next"):
            ahead = 7
        if prefix == "next" and ahead and (base + timedelta(days=ahead)).isocalendar()[1] == now.isocalendar()[1]:
            ahead += 7
        return _at(base + timedelta(days=ahead), END_OF_DAY_HOUR), default_hour, False
    if kind == "month_day":
        month = _MONTHS[m.group("mon")[:3].lower()]
        year = int(m.group("md_year")) if m.group("md_year") else now.year
        return _safe_date(base, year, month, int(m.group("md_day")), m.group("md_year") is None), default_hour, False
    if kind == "day_month":
        return _safe_date(base, now.year, _MONTHS[m.group("dm_mon").lower()[:3]], int(m.group("dm_day")), True), default_hour, False
    if kind == "iso":
        return _safe_date(base, int(m.group("iso_y")), int(m.group("iso_m")), int(m.group("iso_d")), False), default_hour, False
    if kind == "slash":
        mo, d = int(m.group("sl_m")), int(m.group("sl_d"))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return None, default_hour, False
        y_raw = m.group("sl_y")
        before = text[max(0, m.start() - 16):m.start()].lower()
        if not y_raw and not re.search(r"\b(?:by|due|on|before|until|till|deadline|for|from|starting|through)\s*$", before):
            return None, default_hour, False      # "2/3 of the plan" is a fraction, not February 3rd
        year = now.year if not y_raw else (int(y_raw) + 2000 if len(y_raw) == 2 else int(y_raw))
        return _safe_date(base, year, mo, d, y_raw is None), default_hour, False
    return None, default_hour, False


def find_due(text: str, *, ref: datetime) -> Due | None:
    """
    The due date a sentence talks about, resolved relative to ``ref``.

    Day-level phrases resolve to 17:00 local ("end of business"); "EOD",
    "morning"/"afternoon"/"tonight" and clock times refine that. Phrases
    that are clearly history ("last Friday", dates more than a day ago)
    return None.
    """
    if not text:
        return None
    now = _local(ref)
    base = _at(now, 0)
    spans: list[tuple[int, int]] = []
    when: datetime | None = None
    default_hour = END_OF_DAY_HOUR
    exact = False

    def _phrases() -> tuple[str, ...]:
        merged: list[list[int]] = []
        for s, e in sorted(spans):
            if merged and s <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        return tuple(text[s:e].strip() for s, e in merged if text[s:e].strip())

    anchor = _ANCHOR_RE.search(text)
    if anchor:
        when, default_hour, exact = _resolve_anchor(anchor, text, now)
        if when is None:
            return None
        spans.append(anchor.span())
        if exact:
            return Due(when=when.astimezone(timezone.utc), text=" ".join(_phrases()), precise=True, phrases=_phrases())

    modifier = _MODIFIER_RE.search(text)
    if modifier:
        spans.append(modifier.span())
        hour = _MODIFIER_HOUR[modifier.lastgroup]
        if when is None:
            when = _at(base, hour)
            if modifier.lastgroup != "eod" and when < now - timedelta(hours=1):
                when += timedelta(days=1)          # "this evening" said at 23:00 → tomorrow evening
        elif when.hour == default_hour and when.minute == 0:
            when = _at(when, hour)
            default_hour = hour

    clock = _parse_time(text)
    precise = clock is not None
    if clock is not None:
        (hour, minute), span = clock
        spans.append(span)
        if when is None:
            when = _at(base, hour, minute)
            if when < now - timedelta(minutes=5):
                when += timedelta(days=1)
        elif when.hour == default_hour and when.minute == 0:
            when = _at(when, hour, minute)

    if when is None or when < now - timedelta(days=1):
        return None
    phrases = _phrases()
    return Due(when=when.astimezone(timezone.utc), text=" ".join(phrases), precise=precise, phrases=phrases)


def _weekday_index(token: str) -> int:
    t = token.lower()[:3]
    for i, name in enumerate(WEEKDAYS):
        if name.startswith(t):
            return i
    return 0


def _safe_date(base: datetime, year: int, month: int, day: int, infer_year: bool) -> datetime | None:
    try:
        when = _at(base.replace(year=year, month=month, day=day), END_OF_DAY_HOUR)
    except ValueError:
        return None
    if infer_year and when < base - timedelta(days=45):
        try:
            when = when.replace(year=year + 1)
        except ValueError:
            return None
    return when


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Commitment:
    kind: str               # promise | ask | deadline | event
    who: str                # sender for promises/asks (the one who owes / who asked)
    what: str
    due: datetime | None    # aware UTC
    due_text: str
    confidence: float
    for_you: bool
    open_call: bool = False     # "can someone…" — nobody owns it yet

    @property
    def id(self) -> str:
        norm = re.sub(r"[^a-z0-9 ]+", " ", self.what.lower())
        norm = re.sub(r"\s+", " ", norm).strip()[:120]
        return hashlib.sha256(f"{self.kind}|{self.who.lower()}|{norm}".encode("utf-8")).hexdigest()[:20]


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+|(?<=[a-z0-9\)])\s+[-•]\s+")
# A clause runs to the end of the sentence; punctuation glued to the next
# character (URLs, "v1.2", "e.g.") does not end it.
_CLAUSE = r"(?P<what>(?:[^.!?\n]|[.!?](?=\S)){3,180})"

_PROMISE_STRONG = re.compile(
    r"\b(?:i|we)(?:['’]ll| will|['’]m going to| am going to| plan to| intend to| aim to| should have| should be able to)"
    r"\s+" + _CLAUSE,
    re.IGNORECASE,
)
_PROMISE_SOFT = re.compile(r"\b(?:let me|lemme|i can|i could|i['’]d be happy to|happy to)\s+" + _CLAUSE, re.IGNORECASE)
_OUT_OF_OFFICE = re.compile(
    r"\b(?:i|we)(?:['’]ll| will|['’]m| am)\s+(?:be\s+)?(?:out|ooo|off|away|on pto|on vacation|on leave|offline|unavailable|"
    r"travell?ing|in transit|out of (?:the )?office)\b",
    re.IGNORECASE,
)
_ASK_DIRECT = re.compile(
    r"\b(?:can|could|would|will|cld|wd)\s+(?:you|u|ya|someone|somebody|anyone|anybody)\s+(?:please\s+|pls\s+|plz\s+|kindly\s+)?"
    r"(?!believe|imagine|guess|even|hear|wait|feel|think|see why|see how|blame|tell how|remember when|be serious|be more)"
    + _CLAUSE,
    re.IGNORECASE,
)
_ASK_MIND = re.compile(r"\b(?:would you|do you|d'you) mind\s+" + _CLAUSE, re.IGNORECASE)
_ASK_NEED = re.compile(
    r"\b(?:need|needs|want|wanted|would like|would love|['’]d like|['’]d love)\s+(?:you|u|someone|somebody)\s+to\s+" + _CLAUSE,
    re.IGNORECASE,
)
_ASK_PLEASE = re.compile(
    r"\b(?:please|pls|plz|kindly)\s+(?!find attached|see attached|note that|note:|let me know if|be advised|ignore|disregard)"
    + _CLAUSE,
    re.IGNORECASE,
)
_ASK_REMINDER = re.compile(
    r"\b(?:reminder|friendly reminder|gentle reminder|don['’]?t forget|do not forget|remember)\s*(?:that|to|:|-)?\s*" + _CLAUSE,
    re.IGNORECASE,
)
_ASK_ACTION = re.compile(r"\b(?:action items?|todo|to-do|next steps?)\s*[:\-]\s*" + _CLAUSE, re.IGNORECASE)
_ASK_REVIEW = re.compile(
    r"\b(?:ptal|please take a look|take a look at|review requested|requesting (?:a )?review|needs? (?:a )?review|"
    r"ready for review|looking for (?:a )?reviewer|eyes on)\b\s*(?:at\s+|on\s+|:\s*)?(?P<what>(?:[^.!?\n]|[.!?](?=\S)){0,180})",
    re.IGNORECASE,
)
_ASK_CHASE = re.compile(r"\b(?:any updates?|status|eta|news)\s+on\s+" + _CLAUSE, re.IGNORECASE)

_DEADLINE_CUE = re.compile(
    r"\b(?:due|deadline|by|before|no later than|until|till|expires?|expiring|expiry|renews?|renewal|cut-?off|"
    r"needs? to be \w+ by|must be \w+ by|has to be \w+ by|submit|submission|last (?:day|chance|call)|closes?|closing)\b",
    re.IGNORECASE,
)
_EVENT_CUE = re.compile(
    r"\b(?:demo|meeting|sync|standup|stand-up|review|retro|planning|kick-?off|offsite|all-?hands|town ?hall|interview|"
    r"workshop|webinar|talk|presentation|call|launch|release|go-?live|cutover|migration|maintenance|downtime|freeze|"
    r"deploy(?:ment)?|rollout|outage|window|starts?|begins?|happening|scheduled|celebration|lunch|dinner|party|"
    r"happy hour|1:1|one-on-one|onboarding|training|office hours)\b",
    re.IGNORECASE,
)
_QUESTION_ONLY = re.compile(r"^\s*(?:will|would|could|can|should|shall|do|does|did|is|are|was|were)\s+(?:i|we)\b", re.IGNORECASE)
_COMPLETION = re.compile(
    r"\b(?:done|sent|shipped|merged|fixed|posted|shared|deployed|completed|finished|resolved|closed|updated|pushed|"
    r"submitted|delivered|uploaded|scheduled|booked|approved|reviewed|replied|answered|handled|taken care of|sorted|"
    r"landed|released|published|filed|created|added|opened the pr|pr is up|here it is|here you go|attached|as promised)\b",
    re.IGNORECASE,
)
_STOPWORDS = frozenset(
    "the a an and or but if then this that these those it its is are was were be been being to of in on at for with "
    "from by as into about over after before between out up down off again further than too very can will just should "
    "now you your yours we our ours they them their i me my mine he she his her him them what which who whom when where "
    "why how all any both each few more most other some such no nor not only own same so also please pls thanks thank "
    "would could shall might must let lets get got have has had do does did doing there here today tomorrow week".split()
)


def _clean_what(what: str, due_phrases: Iterable[str] = ()) -> str:
    """Tidy a clause for display: drop the date words (shown separately), fillers and stray punctuation."""
    w = re.sub(r"\s+", " ", what or "").strip(" \t-–—:;,")
    for phrase in sorted((p for p in due_phrases if p), key=len, reverse=True):
        w = re.sub(
            r"\s*\b(?:by|before|until|till|on|at|due|for|around)?\s*" + re.escape(phrase) + r"(?![a-z0-9])\s*",
            " ", w, flags=re.IGNORECASE,
        )
    w = re.sub(r"\s+([,;:])", r"\1", w)
    w = re.sub(r"\s+", " ", w).strip(" \t-–—:;,.")
    w = re.sub(r"^(?:please|pls|plz|kindly|just|also|then|and|so)\s+", "", w, flags=re.IGNORECASE)
    w = re.sub(r"\s+(?:by|before|until|till|on|at|for|and|or|to|the|a)$", "", w, flags=re.IGNORECASE)
    return w[:160]


def mentions_you(text: str, self_names: Iterable[str]) -> bool:
    low = (text or "").lower()
    for name in self_names:
        n = (name or "").strip().lower()
        if len(n) >= 3 and re.search(r"(?<![a-z0-9])@?" + re.escape(n) + r"(?![a-z0-9])", low):
            return True
    return False


def is_you(sender: str, self_names: Iterable[str]) -> bool:
    s = (sender or "").strip().lower()
    if not s:
        return False
    if s in ("you", "me", "(you)"):
        return True
    return any(s == (n or "").strip().lower() for n in self_names if n)


def significant_words(text: str) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9_\-']{3,}", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS and not _COMPLETION.fullmatch(w)}


def looks_completed(text: str, what: str) -> bool:
    """A later message reads like the thing in ``what`` got done."""
    if not _COMPLETION.search(text or ""):
        return False
    overlap = significant_words(text) & significant_words(what)
    return len(overlap) >= 1


def extract(
    text: str,
    *,
    sender: str,
    ts: datetime,
    channel: str = "",
    self_names: Iterable[str] = (),
    is_bot: bool = False,
) -> list[Commitment]:
    """All commitments in one message. Deduplicated by ``Commitment.id``."""
    if not text or len(text) < 8:
        return []
    names = [n for n in self_names if n]
    is_dm = channel.startswith("@") or channel.lower().startswith("dm-")
    you_named = mentions_you(text, names)
    sender_is_you = is_you(sender, names)
    out: dict[str, Commitment] = {}

    def _put(c: Commitment) -> None:
        if len(c.what) < 3:
            return
        prev = out.get(c.id)
        if prev is None or c.confidence > prev.confidence:
            out[c.id] = c

    for raw_sentence in _SENTENCE_SPLIT.split(text):
        sentence = (raw_sentence or "").strip()
        if len(sentence) < 8 or len(sentence) > 400:
            continue
        due = find_due(sentence, ref=ts)
        due_when = due.when if due else None
        due_text = due.text if due else ""
        due_phrases = due.phrases if due else ()

        # Absences are events, not promises: "I'll be out Thu–Fri".
        ooo = _OUT_OF_OFFICE.search(sentence)
        if ooo and not is_bot:
            keyword = re.sub(r"^(?:be\s+)?", "", ooo.group(0).split(None, 1)[-1].strip(), flags=re.IGNORECASE)
            rest = re.sub(r"\s+", " ", sentence[ooo.end():]).strip(" .,;:!")[:60]
            _put(Commitment(
                kind="event", who=sender, what=f"{sender or 'someone'} {keyword} {rest}".strip(),
                due=due_when, due_text=due_text, confidence=0.6 if due else 0.4, for_you=False,
            ))
            continue

        matched = False
        if not is_bot and not _QUESTION_ONLY.match(sentence):
            for pattern, base_conf in ((_PROMISE_STRONG, 0.6), (_PROMISE_SOFT, 0.4)):
                pm = pattern.search(sentence)
                if not pm:
                    continue
                what = _clean_what(pm.group("what"), due_phrases)
                weak = re.match(r"^(?:be|need|try|see|think|check back|keep you posted|let you know)\b", what, re.I)
                if weak and not due:
                    continue
                _put(Commitment(
                    kind="promise", who=sender, what=what, due=due_when, due_text=due_text,
                    confidence=min(0.95, base_conf + (0.25 if due else 0.0)),
                    for_you=sender_is_you,
                ))
                matched = True
                break
        if matched:
            continue        # a sentence is one thing: a promise is not also an ask

        for pattern, base_conf, needs_person in (
            (_ASK_DIRECT, 0.55, True), (_ASK_MIND, 0.55, True), (_ASK_NEED, 0.55, True),
            (_ASK_REMINDER, 0.5, False), (_ASK_ACTION, 0.45, False), (_ASK_REVIEW, 0.5, False),
            (_ASK_PLEASE, 0.4, False), (_ASK_CHASE, 0.35, False),
        ):
            am = pattern.search(sentence)
            if not am:
                continue
            what = _clean_what(am.group("what") or "", due_phrases)
            if pattern is _ASK_REVIEW and not what:
                what = "take a look at " + (channel or "the thread")
            if len(what) < 6 and pattern is _ASK_PLEASE:
                break           # "…, please join." — the sentence is about the event, handled below
            if len(what) < 3:
                continue
            addressed = am.group(0).lower()
            open_call = bool(re.search(r"\b(?:someone|somebody|anyone|anybody)\b", addressed))
            for_you = (you_named or (is_dm and not sender_is_you)) and not sender_is_you
            if sender_is_you:
                # You asking someone else → you are waiting on them.
                _put(Commitment(
                    kind="ask", who=sender, what=what, due=due_when, due_text=due_text,
                    confidence=min(0.9, base_conf + (0.2 if due else 0.0)), for_you=False,
                ))
                matched = True
                break
            if is_bot and not for_you:
                break
            conf = base_conf + (0.2 if due else 0.0) + (0.15 if for_you else 0.0) - (0.1 if (needs_person and open_call) else 0.0)
            _put(Commitment(
                kind="ask", who=sender, what=what, due=due_when, due_text=due_text,
                confidence=max(0.2, min(0.95, conf)), for_you=for_you, open_call=open_call and not for_you,
            ))
            matched = True
            break

        if matched or due is None or sentence.rstrip().endswith("?"):
            continue        # questions are asks or nothing — never a deadline

        # A dated sentence with a deadline/event cue but no personal clause.
        if _DEADLINE_CUE.search(sentence):
            _put(Commitment(
                kind="deadline", who="", what=_clean_what(sentence, due_phrases), due=due_when, due_text=due_text,
                confidence=0.6 if re.search(r"\b(?:due|deadline|expires?|expiring|renew)", sentence, re.I) else 0.45,
                for_you=you_named,
            ))
        elif _EVENT_CUE.search(sentence):
            _put(Commitment(
                kind="event", who="", what=_clean_what(sentence, due_phrases), due=due_when, due_text=due_text,
                confidence=0.45 if due.precise else 0.35, for_you=you_named,
            ))

    return sorted(out.values(), key=lambda c: -c.confidence)
