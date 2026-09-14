"""
HTML rendering for the briefing page.

Design rules:

* **The panel, with room.** The page is the menu bar's Briefings panel laid
  out for a window: the same header (Briefings · updated · ↻), the same
  headline, digest sentence and ▲/◇ notes, the same rows (accent bar, title,
  chips, one line, meta, thumbnail), the same radar and footer bar. What the
  panel cannot show — the full text, the reasons spelled out, the checklist,
  the picture at size — folds out under a row. One fact appears once: the
  digest's heads-ups are written from the radar's notes, so the radar does
  not repeat them; the headline says what the time-stamp and tally would.
* **Concise first.** One line per item (title), one line of insight, then
  everything else behind a click. The header answers "do I need to do
  anything?" in a single sentence.
* **Evidence, not adjectives.** Under every title sit the reasons the item is
  in front of *this* user — asked of you, your directive, your thread, a
  deadline, a severity, your focus — computed from the data
  (``intelligence/relevance.py``), never asserted by the model. The line
  after them is the model's one sentence in the user's terms, built on those
  reasons. The header repeats the tally, so the page proves its relevance
  before anything is read.
* **No inline JavaScript / no inline styles.** All behaviour is attached via
  delegated listeners in a single nonce'd ``<script>``; every dynamic value
  travels in ``data-*`` attributes. This is what makes the strict CSP
  possible — a stray unescaped string can never become executable.
* **Self-updating, quietly.** The page polls ``/api/status`` and swaps in
  new content when the engine has refreshed, preserving expanded cards and
  scroll position. No full-page reloads.
"""
from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple

from otto.web.security import new_nonce, safe_url

_SRC_NAMES = {'slack': 'Slack', 'gmail': 'Gmail', 'email': 'Email', 'calendar': 'Calendar', 'jira': 'Jira'}


def h(text: Any) -> str:
    """HTML-escape (quotes included)."""
    return html.escape(str(text if text is not None else ""), quote=True)


_h = h  # backwards-compatible alias


def _clean_ax_text(text: str) -> str:
    from otto.web.collect import clean_ax_text
    return clean_ax_text(text)


def urgency_score(u: Any) -> float:
    if isinstance(u, (int, float)):
        return float(u)
    return {"critical": 0.9, "high": 0.75, "medium": 0.5, "low": 0.2}.get(str(u).lower(), 0.1)


def flatten_items(data: Dict[str, Any], hidden: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """All visible items, annotated with source/channel/urgency, sorted by urgency."""
    from otto.web.state import is_hidden

    hidden_set = set(hidden)
    out: List[Dict[str, Any]] = []
    for section in data.get("sections", []):
        sec_source = section.get("source", section.get("title", "Other"))
        for ch in section.get("channels", []):
            ch_name = ch.get("name", "")
            for item in ch.get("items", []):
                if is_hidden(item, hidden_set):
                    continue
                copy = dict(item)
                copy['_source_group'] = sec_source
                copy['_channel_name'] = ch_name
                copy['_urgency_score'] = urgency_score(item.get('urgency'))
                out.append(copy)
    out.sort(key=lambda x: (x['_urgency_score'], x.get("timestamp") or ""), reverse=True)
    return out


# Nothing left on the briefing. Casual on purpose: what sits under it (the
# radar, a note or two) is Otto keeping you posted, not a list of demands.
ALL_CLEAR = "All clear — just keeping you up to date."


def digest_is_stale(digest: Dict[str, Any] | None, visible_count: int) -> bool:
    """A digest written about items that have since been cleared says things like
    "2 more need you" under an all-clear headline; hide it until the next refresh
    rewrites it (a digest written about the radar alone is kept)."""
    if not isinstance(digest, dict) or not digest:
        return False
    written_about = digest.get("items")
    return visible_count == 0 and isinstance(written_about, int) and written_about > 0


def headline(all_items: List[Dict[str, Any]], data: Dict[str, Any] | None = None) -> str:
    total = len(all_items)
    n_action = sum(1 for x in all_items if x['_urgency_score'] >= 0.7)
    n_worth = sum(1 for x in all_items if 0.3 <= x['_urgency_score'] < 0.7)
    n_look = sum(1 for x in all_items if x['_urgency_score'] < 0.3 and is_opportunity(x))
    look = f" · {n_look} worth a look" if n_look else ""
    if total == 0:
        if data is not None:
            # Honest empty state: "all clear" only when at least one source was actually read.
            online, failed = source_health(data)
            blocked = [s for s, why in failed if _short_reason(why) != "not open"]
            closed = [s for s, why in failed if _short_reason(why) == "not open"]
            names = lambda xs: ", ".join(_SRC_NAMES.get(s, s.title()) for s in xs[:3])  # noqa: E731
            if blocked and not online:
                return f"Nothing read yet — Otto could not read {names(blocked)}."
            if closed and not online:
                return f"Nothing to read yet — open {names(closed)} and Otto will start."
        return ALL_CLEAR
    if n_action:
        s = f"{n_action} item{'s' if n_action != 1 else ''} needing your attention"
        if n_worth:
            s += f" · {n_worth} other update{'s' if n_worth != 1 else ''}"
        return s + look
    if n_worth:
        return f"{n_worth} update{'s' if n_worth != 1 else ''} for you today" + look
    if n_look:
        return f"Nothing urgent · {n_look} worth a look"
    return f"{total} note{'s' if total != 1 else ''} noticed"


_SPECIFIC_HINT = re.compile(
    r"\d|['\"“‘][^'\"”’]{3,}['\"”’]|\b(?:critical|high|medium|cvss|rce|injection|deadline|due|overdue|blocked|outage|"
    r"incident|failed|failing|broken|regression|deploy|release|invoice|contract|offer|interview)\b",
    re.IGNORECASE,
)
_URL_TAIL = re.compile(r"\s*(?:\b(?:in|at|on|from|see)\s+)?https?://\S+", re.IGNORECASE)


def _is_specific(text: str) -> bool:
    """Does this line say *what* happened (numbers, severities, quoted names)?

    "Review the full report notebook for the scan findings" is not specific;
    "Triage: 2 HIGH findings — Command Injection (api)" is.
    """
    return bool(_SPECIFIC_HINT.search(text or ""))


def _first_sentence(text: str, limit: int) -> str:
    for end in ('. ', '! ', '? '):
        idx = text.find(end)
        if 0 < idx < limit:
            return text[:idx + 1].strip()
    return ""


_ELABORATION = re.compile(
    r"^(?:including|includes?|which|such as|and|but|as|with|while|suggesting|indicating|due to|because|so|"
    r"where|whereas|although|though|since|notably|especially|particularly|e\.g\.|i\.e\.)\b", re.IGNORECASE,
)


def _clause(text: str, limit: int) -> str:
    """Cut an over-long sentence before an elaborating clause, not mid-word.

    "…identified 4 critical and 2 high severity vulnerabilities, including Rem…"
    becomes "…identified 4 critical and 2 high severity vulnerabilities". A list
    ("issue 7, issue 8, issue 9…") is not a clause and is still truncated with an ellipsis.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    for sep in (", ", "; ", " — ", " – ", " ("):
        cut = head.rfind(sep)
        if cut >= int(limit * 0.55) and _ELABORATION.match(text[cut + len(sep):]):
            return head[:cut].rstrip(" .,;:—–(")
    return head[:limit - 1].rstrip(' .,;') + '…'


def _fit(title: str, limit: int) -> str:
    title = _URL_TAIL.sub("", title).strip().rstrip(":,;—-")
    if not title:
        return ""
    title = title[0].upper() + title[1:]
    return title[:limit - 1].rstrip() + '…' if len(title) > limit else title


def item_title(item: Dict[str, Any]) -> str:
    """Best short title, in order of how much it tells at a glance:

    short raw text > a *specific* action item > the summary's first sentence >
    a generic action item > truncated raw text.
    """
    occ = item.get("occurrence_count", 1)
    occ_suffix = f" ({occ}x)" if occ and occ > 1 else ""

    t = _clean_ax_text(item.get("text", "") or "")
    if t and 10 < len(t) <= 120:
        return t[0].upper() + t[1:] + occ_suffix

    actions = [a for a in (item.get("action_items", []) or []) if isinstance(a, str) and len(a) > 10]
    action = _fit(actions[0], 120) if actions else ""
    if action and _is_specific(action):
        return action + occ_suffix

    s = (item.get("summary", "") or "").strip()
    summary_title = ""
    if s:
        summary_title = _first_sentence(s, 140) or _clause(s, 140)
    if summary_title and (_is_specific(summary_title) or not action):
        return summary_title + occ_suffix

    if action:
        return action + occ_suffix

    if t and len(t) > 120:
        return t[:117] + '…' + occ_suffix

    if summary_title:
        return summary_title + occ_suffix
    return "New item" + occ_suffix


_DAY_NAMES = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}


def brief_insight(item: Dict[str, Any], now: datetime | None = None, *, chips: bool = False) -> str:
    """One context line: deadline awareness, sender, directive, or the best sentence of analysis.

    With ``chips=True`` the sender and the directive are left out — the card
    already shows them as evidence chips — so the line adds something new.
    """
    now = now or datetime.now()
    parts: List[str] = []
    text_lower = ((item.get("text", "") or "") + " " + (item.get("summary", "") or "")).lower()

    for day_name, day_num in _DAY_NAMES.items():
        if re.search(rf'\b{day_name}\b', text_lower):
            days_ahead = (day_num - now.weekday()) % 7
            if days_ahead == 0:
                parts.append(f"{day_name.title()} is today")
            elif days_ahead == 1:
                parts.append(f"{day_name.title()} is tomorrow")
            else:
                parts.append(f"{day_name.title()} (in {days_ahead} days)")
            break

    sender = item.get("sender", "") or ""
    ch = item.get("_channel_name", "") or ""
    if sender and ch and sender.lower() in ch.lower():
        parts.append("Self-reminder")
    elif sender and not chips:
        parts.append(f"From {sender}")

    matched = item.get("matched_directive", "") or ""
    if not matched:
        m = re.search(r'standing directive:\s*["\']?([^"\']+)["\']?', item.get("relevance", "") or "", re.IGNORECASE)
        if m:
            matched = m.group(1).strip()
    if matched and not chips:
        parts.append(f'Relates to: "{matched[:60]}"')

    analysis = item.get("relevance", "") or item.get("ai_analysis", "") or ""
    title_words = set((item.get("text", "") or "").lower().split())

    def _best_sentence(limit: int) -> str:
        for end in ('. ', '! ', '— ', '; '):
            idx = analysis.find(end)
            if 0 < idx < limit:
                sentence = analysis[:idx + 1].strip()
                if len(set(sentence.lower().split()) - title_words) > 3:
                    return sentence
                return ""
        return ""

    if parts:
        if len(parts) < 3 and analysis:
            sentence = _best_sentence(100)
            if sentence:
                parts.append(sentence)
            elif len(analysis) > 15 and len(parts) < 2:
                parts.append(analysis[:100] + '…' if len(analysis) > 100 else analysis)
        return " · ".join(parts)

    if analysis:
        sentence = _best_sentence(130)
        if sentence:
            return sentence
        if len(analysis) > 20:
            return analysis[:130] + '…' if len(analysis) > 130 else analysis
    return ""


def _source_line(item: Dict[str, Any]) -> str:
    """"Slack · #eng · alice" — the same meta line the panel shows under a row (CSS adds the dots)."""
    src = (item.get('_source_group', '') or '').lower()
    ch = item.get('_channel_name', '') or ''
    sender = item.get('sender', '') or ''
    src_name = _SRC_NAMES.get(src, src.title() if src else 'Source')
    parts = [f'<span class="src">{h(src_name)}</span>']
    if ch:
        parts.append(f'<span class="ch">{h(ch)}</span>')
    if sender and sender.lower().lstrip('@') not in ch.lower():
        parts.append(f'<span class="who">{h(sender)}</span>')
    return ''.join(parts)


def _link(url: str, label: str, cls: str) -> str:
    url = safe_url(url)
    if not url:
        return ""
    return f'<a class="{cls}" href="{h(url)}" target="_blank" rel="noopener noreferrer">{label}</a>'


# --- Why this is for you ----------------------------------------------------
# Each item carries ``why``: short, provable reasons computed from the data
# (see intelligence/relevance.py). They are the first thing under the title,
# because "why am I looking at this?" is the first question a reader has.

MAX_CHIPS = 4
_TONES = frozenset({"you", "directive", "alert", "due", "focus", "opportunity", "quiet"})


def item_reasons(item: Dict[str, Any]) -> List[Dict[str, str]]:
    """The item's reasons as dicts with at least a ``label``; malformed entries are dropped."""
    out: List[Dict[str, str]] = []
    for r in item.get("why") or []:
        if isinstance(r, dict) and str(r.get("label") or "").strip():
            out.append(r)
    return out


def is_opportunity(item: Dict[str, Any]) -> bool:
    """Worth a look on its own merits even when nothing about it is urgent."""
    reasons = item_reasons(item)
    if any(r.get("kind") == "own" for r in reasons):
        return False            # your own message is not a find for you
    try:
        score = float(item.get("opportunity_score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return score >= 0.5 or any(r.get("kind") == "opportunity" for r in reasons)


def _chip(r: Dict[str, str]) -> str:
    tone = r.get("tone") if r.get("tone") in _TONES else "quiet"
    title = f' title="{h(r["detail"])}"' if r.get("detail") else ''
    return f'<span class="wy wy-{tone}"{title}>{h(r["label"])}</span>'


def _why_chips(reasons: List[Dict[str, str]]) -> str:
    if not reasons:
        return ""
    return '<div class="why">' + ''.join(_chip(r) for r in reasons[:MAX_CHIPS]) + '</div>'


def _why_detail(reasons: List[Dict[str, str]]) -> str:
    if not reasons:
        return ""
    rows = ''.join(
        f'<li><span class="wy-k">{h(r["label"])}</span>'
        + (f'<span class="wy-d">{h(r["detail"])}</span>' if r.get("detail") else '')
        + '</li>'
        for r in reasons
    )
    return f'<div class="detail-section"><div class="detail-label">Why this is for you</div><ul class="why-list">{rows}</ul></div>'


def for_you_line(item: Dict[str, Any]) -> str:
    """The line under the title: the model's sentence in the user's own terms, else the local insight."""
    fy = re.sub(r"\s+", " ", str(item.get("for_you") or "")).strip()
    if fy:
        return fy
    return brief_insight(item, chips=bool(item_reasons(item)))


def urgency_level(item: Dict[str, Any]) -> str:
    """``critical`` / ``high`` / '' — the visual weight of a card, from its urgency score."""
    u = item.get("_urgency_score")
    u = float(u) if isinstance(u, (int, float)) else urgency_score(item.get("urgency"))
    return "critical" if u >= 0.9 else "high" if u >= 0.7 else ""


def render_item(item: Dict[str, Any]) -> str:
    title = item_title(item)
    url = safe_url(item.get("source_url", ""))
    reasons = item_reasons(item)
    insight = for_you_line(item)
    t_display = item.get("time_display", "") or ""
    item_id = item.get("id", "") or ""
    ai_summary = item.get("ai_analysis", "") or item.get("relevance", "") or ""
    screenshot = item.get("screenshot", "") or ""
    link_intel = item.get("link_intelligence") or None
    id_attr = f' data-id="{h(item_id)}"' if item_id else ''
    level = urgency_level(item)
    level_cls = f" urg-{level}" if level else ""
    level_el = f'<span class="lvl lvl-{level}">{level.title()}</span>' if level else ''

    insight_el = f'<div class="insight">{h(insight)}</div>' if insight and insight != title else ''
    # Hover controls sit in the right-hand column, as in the panel: snooze, dismiss.
    controls = (
        '<span class="ctl">'
        '<button type="button" class="snz" data-action="snooze" data-hours="1" title="Snooze for an hour">zzz</button>'
        '<button type="button" class="snz" data-action="snooze" data-hours="tomorrow" title="Snooze until 8:00 tomorrow">Tomorrow</button>'
        '<button type="button" class="x" data-action="dismiss" title="Dismiss" aria-label="Dismiss">✕</button>'
        '</span>'
    ) if item_id else ''
    time_el = f'<span class="t">{h(t_display)}</span>' if t_display else ''

    acts = item.get("action_items", []) or []
    action_el = ''
    if acts:
        rows = ''.join(
            f'<label class="todo-item"><input type="checkbox"><span>{h(a)}</span></label>' for a in acts
        )
        action_el = f'<div class="detail-section"><div class="detail-label">Action Checklist</div><div class="todo-list">{rows}</div></div>'

    intel_el = ''
    if link_intel:
        features = link_intel.get("key_features", []) or []
        useful = link_intel.get("why_useful", []) or []
        features_html = ('<div class="intel-tags">' + ''.join(f'<span class="intel-tag">{h(f)}</span>' for f in features) + '</div>') if features else ''
        useful_html = (
            '<div class="why-useful-box"><div class="why-useful-heading">Why it matters</div>'
            '<ul class="why-useful-list">' + ''.join(f'<li>{h(u)}</li>' for u in useful) + '</ul></div>'
        ) if useful else ''
        intel_el = f'''<div class="detail-section">
  <div class="detail-label">Link</div>
  <div class="intel-card">
    <div class="intel-header"><span class="intel-title">{h(link_intel.get("title", ""))}</span>{_link(link_intel.get("url", ""), "Visit ↗", "intel-link")}</div>
    <div class="intel-summary">{h(link_intel.get("summary", ""))}</div>
    {features_html}{useful_html}
  </div>
</div>'''

    summary_el = f'<div class="detail-section"><div class="detail-label">Context</div><div class="detail-text">{h(ai_summary)}</div></div>' if ai_summary else ''

    # The picture of the source: a thumbnail beside the row (as in the panel),
    # the full-width original inside the expanded details. Both open the viewer.
    screenshot_el = thumb_el = ''
    if screenshot and re.fullmatch(r'[A-Za-z0-9_-]+\.png', screenshot):
        shot_src = f"/static/screenshots/{screenshot}"
        thumb_el = (f'<button type="button" class="thumb" data-action="lightbox" data-shot="{h(shot_src)}" data-link="{h(url)}" '
                    f'title="Where this came from — click to enlarge"><img src="{h(shot_src)}" alt="" loading="lazy"></button>')
        screenshot_el = f'''<div class="detail-section">
  <div class="detail-label">Original message</div>
  <div class="screenshot-box" data-action="lightbox" data-shot="{h(shot_src)}" data-link="{h(url)}" title="Click to enlarge">
    <img class="screenshot-img" src="{h(shot_src)}" alt="Slack message preview" loading="lazy">
  </div>
</div>'''

    ext_url = safe_url(item.get("external_url") or (link_intel.get("url") if link_intel else "") or "")
    src_name = _SRC_NAMES.get((item.get('_source_group') or '').lower(), 'the source')
    buttons = [
        _link(url, f"Open in {h(src_name)} ↗", "btn btn-primary") if url else "",
        _link(ext_url, "Open link ↗", "btn") if ext_url else "",
        '<button type="button" class="btn" data-action="snooze" data-hours="1">Snooze 1h</button>' if item_id else '',
        '<button type="button" class="btn" data-action="snooze" data-hours="tomorrow" title="Until 8:00 tomorrow">Tomorrow</button>' if item_id else '',
        '<button type="button" class="btn btn-dismiss" data-action="dismiss">Dismiss</button>' if item_id else '',
    ]
    actions_bar = '<div class="detail-actions-bar">' + ''.join(b for b in buttons if b) + '</div>'

    # Laid out like a panel row — accent bar, text column, thumbnail column —
    # with the details folded under the text. The title is a real button:
    # keyboard users Tab to it and press Enter/Space, screen readers hear
    # "collapsed/expanded"; the rest of the row stays clickable.
    return f'''<div class="card clickable{level_cls}"{id_attr}>
  <span class="bar" aria-hidden="true"></span>
  <div class="body">
    <button type="button" class="title" data-action="toggle" aria-expanded="false">{h(title)}</button>
    {_why_chips(reasons)}
    {insight_el}
    <div class="meta">{level_el}{_source_line(item)}{time_el}<span class="expand-cue">Details</span></div>
  </div>
  <div class="side">{controls}{thumb_el}</div>
  <div class="detail">{_why_detail(reasons)}{summary_el}{action_el}{intel_el}{screenshot_el}{actions_bar}</div>
</div>'''


def _render_low_priority(items: List[Dict[str, Any]]) -> str:
    rows = []
    for item in items:
        title = item_title(item)
        url = safe_url(item.get("source_url", ""))
        item_id = item.get("id", "") or ""
        id_attr = f' data-id="{h(item_id)}"' if item_id else ''
        dismiss = '<button class="x" data-action="dismiss" title="Dismiss" aria-label="Dismiss">✕</button>' if item_id else ''
        body = _link(url, h(title), "lp-link") if url else h(title)
        reasons = item_reasons(item)
        tag = f'<span class="lp-why">{h(reasons[0]["label"])}</span>' if reasons else ''
        rows.append(f'<div class="lp"{id_attr}>{body}{tag}{dismiss}</div>')
    return (f'<div class="group"><details><summary class="label label-toggle">Also noticed · {len(items)}</summary>'
            + ''.join(rows) + '</details></div>')


def _problems(data: Dict[str, Any]) -> List[Dict[str, str]]:
    """What needs a look about Otto itself (core/health.py); [] when nothing does."""
    try:
        from otto.core.health import problems
        return problems(data, menubar_installed=False)
    except Exception:
        return []


# The page cannot run the fix itself; it says where the button is.
_PAGE_HINT = {"permissions": "Fix… in the menu bar", "slack": "Connect Slack… in the menu bar",
              "config": "Edit Config… in the menu bar", "menubar": "open Otto.app", "restart": "restart Otto from the menu bar"}


ABOUT_OTTO_HEADING = "About Otto"


def render_problems(data: Dict[str, Any], found: List[Dict[str, str]] | None = None) -> str:
    """"⚠︎ Slack needs a macOS permission — … · Fix… in the menu bar", one row each
    under an *About Otto* heading, at the top of the Worth knowing drawer (they
    are about Otto, not notifications); '' when nothing needs a look. ``found``
    is :func:`_problems`' answer when the caller already has it."""
    rows = []
    for p in (_problems(data) if found is None else found)[:4]:
        action = str(p.get("action") or "")
        hint = "Remove key in the menu bar" if action.startswith("key_remove:") else _PAGE_HINT.get(action, "")
        text = h(p["title"]) + (f' <span class="prob-d">{h(p["detail"])}</span>' if p.get("detail") else "")
        rows.append(f'<li class="prob"><span class="ng">⚠︎</span><span class="nt">{text}'
                    + (f'<span class="prob-h">{h(hint)}</span>' if hint else "") + '</span></li>')
    if not rows:
        return ""
    return (f'<div class="rsec problems"><div class="rlabel">{ABOUT_OTTO_HEADING} <span class="rcount">{len(rows)}</span></div>'
            f'<ul class="notes problems">{"".join(rows)}</ul></div>')


def _render_empty(data: Dict[str, Any]) -> str:
    try:
        from otto.intelligence.history import load_directives
        directives = load_directives()
    except Exception:
        directives = []

    online, failed = source_health(data)
    closed = [s for s, why in failed if _short_reason(why) == "not open"]     # nothing to read, nothing wrong
    blocked = [(s, why) for s, why in failed if _short_reason(why) != "not open"]  # permission / timeout / error

    def _name(s: str) -> str:
        return _SRC_NAMES.get(s, s.title())

    pills = []
    for s in online:
        pills.append(f'<span class="status-on">{h(_name(s))} · Connected</span>')
    for s, why in blocked:
        pills.append(f'<span class="status-off" title="{h(why)}">{h(_name(s))} · {h(_short_reason(why))}</span>')
    for s in closed:
        pills.append(f'<span class="status-idle">{h(_name(s))} · not open</span>')
    monitor_html = f'<div class="monitor"><span class="monitor-label">Monitoring</span>{" ".join(pills)}</div>' if pills else ''

    known = set(online) | {s for s, _ in failed}
    hints = {"gmail": "Open Gmail in Safari or Chrome", "calendar": "Grant Calendar access in System Settings → Privacy",
             "jira": "Open Jira in your browser", "slack": "Open Slack (app or browser)"}
    unknown = [n for n in ("slack", "calendar", "gmail", "jira") if n not in known]
    suggest_html = ''
    if unknown:
        suggest_html = '<div class="suggestions">' + ''.join(
            f'<div class="suggest-item">{h(hints.get(s, f"Open {s.title()}"))} to expand coverage</div>' for s in unknown[:2]
        ) + '</div>'

    directives_html = ''
    if directives:
        rows = ''.join(f'<div class="directive">{h(d.get("directive", "")[:80])}</div>' for d in directives[:3])
        directives_html = f'<div class="directives"><div class="monitor-label">Standing directives</div>{rows}</div>'

    # The headline above already says it ("All clear — just keeping you up to
    # date." / "Nothing read yet — …"); what needs fixing is in the header's
    # rows. This block only shows what Otto is watching.
    return f'''<div class="empty-state">
  {monitor_html}{directives_html}{suggest_html}
</div>'''


def source_health(data: Dict[str, Any]) -> Tuple[List[str], List[Tuple[str, str]]]:
    """``(online_sources, [(failed_source, reason), …])`` from a briefing payload.

    Uses ``source_status`` when the collector provided it; falls back to the
    older ``sources_polled`` list (everything polled counted as online).
    """
    statuses = data.get("source_status")
    if isinstance(statuses, list) and statuses:
        online = [s.get("source", "") for s in statuses if s.get("ok") and s.get("source")]
        failed = [(s.get("source", ""), s.get("error") or "unavailable") for s in statuses if not s.get("ok") and s.get("source")]
        return online, failed
    online = [s.get("source", "") for s in data.get("sections", []) if s.get("source")]
    for p in data.get("sources_polled") or []:
        name = str(p).split(":")[0].replace("browser_", "").replace("native_", "")
        if name and name not in online:
            online.append(name)
    return online, []


def _short_reason(why: str) -> str:
    w = (why or "").lower()
    if "accessibility" in w:
        return "needs Accessibility"
    if "automation" in w or "prompt" in w or "permission" in w:
        return "needs permission"
    if "not open" in w:
        return "not open"
    return "unavailable"


# ---------------------------------------------------------------------------
# Radar — open loops, upcoming dates, recurring series (see intelligence/radar.py)
# ---------------------------------------------------------------------------

_RADAR_SECTIONS: Tuple[Tuple[str, str, str], ...] = (
    # key, heading, glyph
    ("todo", "To do", "○"),
    ("waiting", "Waiting on", "◔"),
    ("open_calls", "Nobody has taken this", "◌"),
    ("upcoming", "Coming up", "▸"),
    # Conversations Slack marks unread that nobody has opened (screen reader
    # only — the sidebar is read, the messages are not; see collect._add_unread_slack).
    ("unread", "Unread in Slack", "●"),
)
# Things that keep coming back (a daily scan, a weekly report) and how their
# numbers move — said the way a colleague would, not as "patterns".
RECURRING_HEADING = "Keeps coming back"


def radar_rows(data: Dict[str, Any], hidden: Iterable[str] = ()) -> Dict[str, List[Dict[str, Any]]]:
    """Visible radar rows per section (dismissed ids removed)."""
    radar = data.get("radar") or {}
    hidden_set = set(hidden)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for key, _heading, _glyph in _RADAR_SECTIONS:
        out[key] = [r for r in (radar.get(key) or []) if r.get("id") not in hidden_set]
    out["patterns"] = [p for p in (radar.get("patterns") or []) if p.get("id") not in hidden_set]
    return out


def _radar_row(row: Dict[str, Any], glyph: str) -> str:
    who = row.get("who") or ""
    what = row.get("what") or ""
    kind = row.get("kind") or ""
    lead = ""
    if kind in ("promise", "ask", "open_call") and who and who != "you":
        lead = f'<span class="rwho">{h(who)}</span> '
    due = row.get("due_label") or ""
    due_cls = "rdue overdue" if row.get("overdue") else ("rdue soon" if row.get("soon") else "rdue")
    meta_bits = [b for b in (row.get("channel") or "",) if b]
    url = safe_url(row.get("url") or "")
    what_html = f'<a class="rlink" href="{h(url)}" target="_blank" rel="noopener noreferrer">{h(what)}</a>' if url else h(what)
    return (
        f'<div class="rr" data-id="{h(row.get("id", ""))}">'
        f'<span class="rk">{glyph}</span>'
        f'<span class="rwhat">{lead}{what_html}</span>'
        f'<span class="rmeta">{h(" · ".join(meta_bits))}</span>'
        + (f'<span class="{due_cls}">{h(due)}</span>' if due else "")
        + '<button class="x" data-action="dismiss" title="Dismiss" aria-label="Dismiss">✕</button>'
        '</div>'
    )


def _pattern_row(p: Dict[str, Any]) -> str:
    bits = [p.get("channel") or ""]
    if p.get("cadence") and p.get("cadence") != "irregular":
        bits.append(p["cadence"])
    if p.get("late_by"):
        bits.append(f'late {p["late_by"]}')
    elif p.get("next_expected"):
        bits.append(f'next {p["next_expected"]}')
    bits.append(f'{p.get("n", 0)}× in {p.get("span", "")}'.strip())
    metrics = ""
    for m in (p.get("metrics") or [])[:2]:
        arrow = {"up": "↑", "down": "↓"}.get(m.get("direction"), "")
        cls = "rmetric " + (m.get("direction") or "flat")
        metrics += (
            f'<span class="{cls}">{h(m.get("name", ""))} <span class="spark">{h(m.get("spark", ""))}</span> '
            f'{h(m.get("last", ""))}{(" " + arrow) if arrow else ""}'
            f'<span class="dim"> (usually {h(m.get("usual", ""))})</span></span>'
        )
    late_cls = " late" if p.get("late_by") else ""
    return (
        f'<div class="rr pat{late_cls}" data-id="{h(p.get("id", ""))}">'
        f'<span class="rk">▪</span>'
        f'<span class="rwhat">{h(p.get("label", ""))}</span>'
        f'<span class="rmeta">{h(" · ".join(b for b in bits if b))}</span>'
        + (f'<span class="rmetrics">{metrics}</span>' if metrics else "")
        + '<button class="x" data-action="dismiss" title="Stop tracking" aria-label="Stop tracking">✕</button>'
        '</div>'
    )


def render_radar(data: Dict[str, Any], hidden: Iterable[str] = ()) -> str:
    """The radar's sections and the recurring series, one row each, for the
    Worth knowing drawer; '' when there is nothing worth a line. The radar's
    data-driven notes ("#ops is busier than usual…") are not drawn here: they
    are the digest's heads-ups' raw material and appear once, as notes (see
    :func:`digest_notes`)."""
    radar = data.get("radar") or {}
    rows = radar_rows(data, hidden)
    if not any(rows.values()):
        return ""
    parts = ['<div class="group radar">']
    for key, heading, glyph in _RADAR_SECTIONS:
        if not rows.get(key):
            continue
        parts.append(f'<div class="rsec"><div class="rlabel">{heading} <span class="rcount">{len(rows[key])}</span></div>')
        parts.extend(_radar_row(r, glyph) for r in rows[key])
        parts.append('</div>')
    if rows.get("patterns"):
        # Recurring series and their numbers. Payload key and ids stay
        # ``patterns`` / ``pattern:<sha>`` so existing dismissals keep working.
        parts.append(f'<div class="rsec"><div class="rlabel">{RECURRING_HEADING} <span class="rcount">{len(rows["patterns"])}</span></div>')
        parts.extend(_pattern_row(p) for p in rows["patterns"])
        parts.append('</div>')
    mem = radar.get("memory") or {}
    if mem.get("messages"):
        parts.append(
            f'<div class="rfoot">From memory · {int(mem.get("messages", 0)):,} messages · {int(mem.get("channels", 0))} channels · '
            f'{int(mem.get("people", 0))} people · {int(mem.get("days", 0))} day{"s" if int(mem.get("days", 0)) != 1 else ""}</div>'
        )
    parts.append('</div>')
    return "".join(parts)


# ---------------------------------------------------------------------------
# Worth knowing — the notes and the radar, kept apart from the items
# ---------------------------------------------------------------------------
#
# Nothing here needs you: the digest's ▲ heads-ups and ◇ predictions, the
# radar's open loops and dates, the recurring series. The page and the panel
# show it behind one click ("Worth knowing · N"), never in the list of
# notifications, and it clears separately — every note has an id so it can be
# dismissed like a row (`POST /api/clear` with `scope=notes` takes all of it).

WORTH_KNOWING_LABEL = "Worth knowing"
WORTH_KNOWING_SUB = "Heads-ups, what's likely next, your radar, and anything about Otto itself — none of it a notification."
WORTH_KNOWING_EMPTY = "Nothing more to know right now."
NOTES_HEADING = "Notes"          # the ▲ ◇ • block, between About Otto and the radar's sections


def note_id(text: Any) -> str:
    """A stable id for a heads-up / prediction / attention note, so it can be dismissed like a row."""
    norm = " ".join(str(text).split()).lower()
    return "note:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


def digest_notes(data: Dict[str, Any], hidden: Iterable[str] = (), *, n_items: int | None = None) -> List[Dict[str, Any]]:
    """The digest's heads-ups and predictions — or, when it has no heads-ups, the
    radar's own attention notes they are written from (one or the other, never
    both) — each with an id, minus the dismissed ones. A digest written about
    items since cleared is stale and contributes nothing."""
    hidden_set = set(hidden)
    if n_items is None:
        n_items = len(flatten_items(data, hidden_set))
    digest = data.get("digest") or {}
    if not isinstance(digest, dict) or digest_is_stale(digest, n_items):
        digest = {}
    notes: List[Dict[str, Any]] = []
    for t in (digest.get("heads_up") or [])[:3]:
        if t:
            notes.append({"id": note_id(t), "kind": "heads_up", "text": str(t), "basis": ""})
    for p in (digest.get("predictions") or [])[:3]:
        if isinstance(p, dict) and p.get("note"):
            notes.append({"id": note_id(p["note"]), "kind": "prediction", "text": str(p["note"]), "basis": str(p.get("basis") or "")})
    if not any(n["kind"] == "heads_up" for n in notes):
        radar = data.get("radar") or {}
        for a in (radar.get("attention") or [])[:3]:
            if a:
                notes.append({"id": note_id(a), "kind": "attention", "text": str(a), "basis": ""})
    return [n for n in notes if n["id"] not in hidden_set]


def worth_knowing_ids(data: Dict[str, Any], hidden: Iterable[str] = ()) -> set:
    """Every id the Worth knowing view shows — notes, radar rows, recurring series — i.e. what ``clear scope=notes`` dismisses."""
    hidden_set = set(hidden)
    rows = radar_rows(data, hidden_set)
    ids = {n["id"] for n in digest_notes(data, hidden_set)}
    ids |= {str(r["id"]) for key in rows for r in rows[key] if r.get("id")}
    return ids


def worth_knowing_count(data: Dict[str, Any], hidden: Iterable[str] = (), *, notes: List[Dict[str, Any]] | None = None) -> int:
    hidden_set = set(hidden)
    if notes is None:
        notes = digest_notes(data, hidden_set)
    return len(notes) + sum(len(v) for v in radar_rows(data, hidden_set).values())


def _note_row(n: Dict[str, Any]) -> str:
    glyph, cls = {"heads_up": ("▲", "n-head"), "prediction": ("◇", "n-pred")}.get(n.get("kind", ""), ("•", "n-quiet"))
    title = f' title="{h(n["basis"])}"' if n.get("basis") else ""
    return (
        f'<div class="rr note {cls}" data-id="{h(n["id"])}"{title}>'
        f'<span class="rk ng">{glyph}</span><span class="rwhat">{h(n["text"])}</span>'
        '<button class="x" data-action="dismiss" title="Dismiss" aria-label="Dismiss">✕</button></div>'
    )


def render_worth_knowing(data: Dict[str, Any], hidden: Iterable[str] = (), notes: List[Dict[str, Any]] | None = None,
                         *, problems_shown: bool = False) -> str:
    """The body of the Worth knowing drawer: the notes, then the radar's sections.
    (The *About Otto* rows sit above it, in their own block; with those on show
    an empty body says nothing rather than "nothing more to know".)"""
    hidden_set = set(hidden)
    if notes is None:
        notes = digest_notes(data, hidden_set)
    notes_html = "".join(_note_row(n) for n in notes)
    radar_html = render_radar(data, hidden_set)
    if not notes_html and not radar_html:
        return "" if problems_shown else f'<p class="wk-empty">{WORTH_KNOWING_EMPTY}</p>'
    if notes_html:
        notes_html = (f'<div class="rsec wk-notes"><div class="rlabel">{NOTES_HEADING} <span class="rcount">{len(notes)}</span></div>'
                      f'{notes_html}</div>')
    return notes_html + radar_html


def render_body(data: Dict[str, Any], hidden: Iterable[str] = ()) -> Dict[str, str]:
    """Render the swappable parts of the page: header summary, meta line, main content, the Worth knowing drawer."""
    all_items = flatten_items(data, hidden)
    action_required = [x for x in all_items if x['_urgency_score'] >= 0.7]
    worth_knowing = [x for x in all_items if 0.3 <= x['_urgency_score'] < 0.7]
    quiet = [x for x in all_items if x['_urgency_score'] < 0.3]
    # An opportunity is rarely urgent; it must not vanish into the fold.
    worth_a_look = [x for x in quiet if is_opportunity(x)]
    low_priority = [x for x in quiet if not is_opportunity(x)]
    digest_html = render_digest(data, all_items)
    # Heads-ups, predictions, the radar and what needs a look about Otto itself
    # are not notifications: they live in the Worth knowing drawer (one click
    # away; the notes and rows clear on their own, the About Otto rows go when fixed).
    notes = digest_notes(data, hidden, n_items=len(all_items))
    found = _problems(data)[:4]
    problems_html = render_problems(data, found)
    wk_html = render_worth_knowing(data, hidden, notes, problems_shown=bool(found))
    wk_clearable = worth_knowing_count(data, hidden, notes=notes)
    wk_count = wk_clearable + len(found)

    if not all_items:
        main = _render_empty(data)
    else:
        main = ''
        if action_required:
            main += _group("Needs you", action_required)
        if worth_knowing:
            main += _group("For you", worth_knowing)
        if worth_a_look:
            main += _group("Worth a look", worth_a_look)
        if low_priority:
            main += _render_low_priority(low_priority)

    from otto.intelligence.relevance import header_why
    why = header_why(all_items)

    # Under the headline: only what is *wrong* with coverage; the time is in the header's corner.
    _, failed = source_health(data)
    blocked = [s for s, why in failed if _short_reason(why) != "not open"]
    meta = (h(", ".join(_SRC_NAMES.get(s, s.title()) for s in blocked[:3])) + " unavailable") if blocked else ""

    n = len(all_items)
    footer = (f"{n} item{'s' if n != 1 else ''}" + (f" · {len(action_required)} need{'s' if len(action_required) == 1 else ''} you"
                                                     if action_required else "")) if n else "Up to date"
    return {"summary": h(headline(all_items, data)), "why": h(why), "meta": meta,
            "digest": digest_html, "problems": problems_html, "main": main,
            "wk": wk_html, "wk_count": str(wk_count), "wk_clearable": str(wk_clearable),
            "count": str(n), "important": str(len(action_required)), "footer": h(footer),
            "updated": h(short_time(data.get("generated_at_human", "") or ""))}


def _group(label: str, items: List[Dict[str, Any]]) -> str:
    return (f'<div class="group"><div class="label">{h(label)} <span class="cnt">{len(items)}</span></div>'
            + '\n'.join(render_item(i) for i in items) + '</div>')


def short_time(generated_at_human: str) -> str:
    """"Saturday, September 12 2026 · 10:49 AM" → "Updated 10:49 AM" (what the panel's corner says)."""
    text = (generated_at_human or "").strip()
    if not text:
        return ""
    if " · " in text:
        return "Updated " + text.rsplit(" · ", 1)[1]
    if " at " in text:
        return "Updated " + text.rsplit(" at ", 1)[1]
    return text


def render_digest(data: Dict[str, Any], all_items: List[Dict[str, Any]]) -> str:
    """
    The short version, under the headline: what it all adds up to, then the
    ⇄ notes — items that belong together (see :mod:`otto.intelligence.synthesis`).
    The ▲ heads-ups and ◇ predictions are not about the items and live in the
    Worth knowing drawer instead. '' when there is nothing beyond the headline
    to say — in the quiet state in particular, no sentence about nothing.
    """
    digest = data.get("digest") or {}
    if not isinstance(digest, dict) or digest_is_stale(digest, len(all_items)) or not all_items:
        return ""
    text = str(digest.get("digest") or "").strip()
    if digest.get("source") != "model" and text.lower() == headline(all_items, data).lower():
        text = ""
    ids = {str(x.get("id") or "") for x in all_items}
    rows: List[str] = []
    for c in (digest.get("connections") or [])[:3]:
        cids = [i for i in (c.get("ids") or []) if i in ids] if isinstance(c, dict) else []
        if len(cids) >= 2 and c.get("note"):
            # The numbers flow with the sentence (inline), each jumping to its item.
            links = "".join(f'<button type="button" class="dg-ref" data-goto="{h(i)}" title="Show this item">{n}</button>'
                            for n, i in enumerate(cids, 1))
            rows.append(f'<li class="n-conn" title="Connected"><span class="ng">⇄</span><span class="nt">{h(c["note"])} {links}</span></li>')
    if not text and not rows:
        return ""
    body = (f'<p class="dg-text">{h(text)}</p>' if text else "") + (f'<ul class="notes">{"".join(rows)}</ul>' if rows else "")
    return f'<div class="dg">{body}</div>'


CSS = """
:root{--bg:#f2f2f7;--sf:#fff;--tx:#1d1d1f;--dim:#6e6e73;--subtle:#8e8e93;--ac:#0a7aff;--bd:rgba(0,0,0,.09);--hv:rgba(0,0,0,.045);--red:#ff3b30;--orange:#ff9500;--orange-t:#b25e00;--indigo:#5856d6;--ok:#34c759;--shadow:0 1px 2px rgba(0,0,0,.05),0 10px 34px rgba(0,0,0,.07)}
@media(prefers-color-scheme:dark){:root{--bg:#1c1c1e;--sf:#2a2a2c;--tx:#f5f5f7;--dim:#98989d;--subtle:#8e8e93;--ac:#409cff;--bd:rgba(255,255,255,.1);--hv:rgba(255,255,255,.06);--orange:#ff9f0a;--orange-t:#ffb340;--indigo:#7d7aff;--shadow:0 1px 2px rgba(0,0,0,.4),0 10px 34px rgba(0,0,0,.35)}}
*{margin:0;padding:0;box-sizing:border-box}
html{background:var(--bg)}
body{font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',system-ui,sans-serif;background:var(--bg);color:var(--tx);-webkit-font-smoothing:antialiased;line-height:1.45;font-size:13px;padding:28px 16px 40px}
.sheet{max-width:720px;margin:0 auto;background:var(--sf);border:1px solid var(--bd);border-radius:14px;box-shadow:var(--shadow);overflow:clip}
/* header — the panel's, with room to breathe */
.hdr{padding:16px 20px 14px;border-bottom:1px solid var(--bd)}
.bar-row{display:flex;align-items:center;gap:8px}
.ring{position:relative;width:20px;height:20px;flex:none}
.ring-o{position:absolute;inset:1px;border:2.2px solid var(--tx);border-radius:50%}
.ring-n{position:absolute;right:-9px;bottom:-3px;font-size:10px;font-weight:700;line-height:1;background:var(--sf);padding:0 2px;font-variant-numeric:tabular-nums;color:var(--tx)}
.ring-n:empty{display:none}
.hdr h1{font-size:19px;font-weight:700;letter-spacing:-.2px;margin-left:6px}
.live{font-size:12px;color:var(--subtle);margin-left:8px;display:inline-flex;align-items:center;gap:6px;flex:1;min-width:0}
.live::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--ok);opacity:.8;flex:none}
.live.busy::before{background:var(--ac);animation:pulse 1s ease-in-out infinite}
.live.stale::before{background:var(--subtle)}
@keyframes pulse{50%{opacity:.2}}
.tb{font:inherit;font-size:15px;color:var(--dim);background:none;border:0;border-radius:6px;width:28px;height:26px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center}
.tb:hover{background:var(--hv);color:var(--tx)}
.tb[disabled]{opacity:.4;cursor:default;background:none}
.s{font-size:15px;color:var(--dim);margin-top:8px;line-height:1.4}
.s2{display:block;color:var(--subtle);font-size:12.5px;margin-top:2px}
.s2:empty{display:none}
.d{font-size:12px;color:var(--orange-t);margin-top:4px}
.d:empty,#digest:empty,#problems:empty{display:none}
.dg-text{font-size:14px;line-height:1.5;color:var(--tx);margin-top:8px}
/* notes: ▲ heads up · ◇ likely · ⇄ connected · ⚠︎ needs a look — the panel's glyphs */
.notes{list-style:none;margin:8px 0 0;padding:0;display:flex;flex-direction:column;gap:5px}
.notes li{display:flex;gap:8px;align-items:baseline;font-size:13px;line-height:1.45;color:var(--tx)}
.ng{flex:none;width:12px;text-align:center;font-size:11px;font-weight:700}
.n-head .ng,.prob .ng{color:var(--orange)}.n-pred .ng{color:var(--indigo)}.n-conn .ng{color:var(--ac)}
.n-quiet .ng{color:var(--subtle)}.n-quiet .nt{color:var(--dim)}
.dg.quiet .label{padding:10px 0 2px}
.nt{min-width:0}
.dg-ref{font:inherit;font-size:10.5px;font-weight:700;color:var(--ac);background:rgba(10,122,255,.12);border:0;border-radius:9px;min-width:18px;height:18px;line-height:18px;padding:0 5px;cursor:pointer;margin-left:3px;vertical-align:1px}
.dg-ref:hover{background:var(--ac);color:#fff}
.prob-d{color:var(--dim)}
.prob-h{color:var(--ac);font-weight:600;margin-left:6px;white-space:nowrap}
.wk .notes.problems{padding:0 10px 4px}.wk .rsec.problems{padding-bottom:4px}
/* body */
.w{padding:6px 10px 10px}
.group{margin-top:6px}
.label{font-size:11px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:.6px;padding:10px 10px 4px;display:flex;gap:6px;align-items:baseline}
.cnt{font-weight:500;color:var(--subtle)}
.label-toggle{cursor:pointer;list-style:none}
.label-toggle::-webkit-details-marker{display:none}
.label-toggle::after{content:"▸";font-size:10px;color:var(--subtle);margin-left:2px}
details[open]>.label-toggle::after{content:"▾"}
/* a row, as in the panel: accent bar · text · thumbnail */
.card{display:grid;grid-template-columns:3px minmax(0,1fr) auto;gap:0 12px;padding:9px 10px 9px 6px;border-radius:9px;position:relative;transition:opacity .25s,transform .25s,background .15s}
.card .bar{grid-row:1/3}
.card .detail{grid-column:2/4}
.card.clickable{cursor:pointer}
.card.clickable:hover,.card:focus-within,.card.expanded{background:var(--hv)}
.card.flash{box-shadow:0 0 0 2px var(--ac) inset}
.card,.lp,.rr,.label{scroll-margin:12px 0 56px}
.card.leaving,.lp.leaving,.rr.leaving{opacity:0;transform:scale(.98)}
.bar{width:3px;border-radius:1.5px;background:transparent;margin:2px 0}
.card.urg-critical .bar{background:var(--red)}
.card.urg-high .bar{background:var(--orange)}
.body{min-width:0}
.title{font:inherit;font-size:15px;font-weight:600;line-height:1.35;text-align:left;background:none;border:0;padding:0;margin:0;color:inherit;cursor:pointer;display:block;width:100%}
.title:focus-visible,.x:focus-visible,.snz:focus-visible,.btn:focus-visible,.tb:focus-visible,.label-toggle:focus-visible,.ft button:focus-visible,.dg-ref:focus-visible,.thumb:focus-visible{outline:2px solid var(--ac);outline-offset:2px;border-radius:4px}
.why{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.wy{font-size:11px;font-weight:600;line-height:1;padding:4px 7px;border-radius:5px;white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis;cursor:default}
.wy-you{background:var(--ac);color:#fff}
.wy-directive{background:rgba(10,122,255,.12);color:var(--ac)}
.wy-alert{background:rgba(255,59,48,.12);color:#d70015}
.wy-due{background:rgba(255,149,0,.16);color:var(--orange-t)}
.wy-focus{background:rgba(88,86,214,.12);color:#4b48c4}
.wy-opportunity{background:rgba(52,199,89,.14);color:#1f8a3d}
.wy-quiet{background:var(--hv);color:var(--dim);font-weight:500}
@media(prefers-color-scheme:dark){.wy-alert{color:#ff6961}.wy-focus{color:#8f8cff}.wy-opportunity{color:#4cd964}.wy-quiet{background:rgba(255,255,255,.1)}}
.insight{font-size:13px;color:var(--dim);margin-top:5px;line-height:1.45}
.meta{font-size:12px;color:var(--subtle);margin-top:5px;display:flex;align-items:baseline;flex-wrap:wrap}
.meta>span+span:not(.expand-cue)::before{content:"·";margin:0 5px}
.lvl{font-weight:600}
.lvl-critical{color:var(--red)}.lvl-high{color:var(--orange-t)}
.t{font-variant-numeric:tabular-nums}
.expand-cue{margin-left:auto;padding-left:10px;color:var(--ac);opacity:0;transition:opacity .15s}
.expand-cue::after{content:" ▾"}
.card:hover .expand-cue,.card:focus-within .expand-cue,.card.expanded .expand-cue{opacity:.9}
.card.expanded .expand-cue::after{content:" ▴"}
.side{display:flex;flex-direction:column;align-items:flex-end;gap:6px;min-width:46px}
.ctl{display:inline-flex;gap:2px;height:20px;visibility:hidden}
.card:hover .ctl,.card:focus-within .ctl{visibility:visible}
.snz,.x{font:inherit;font-size:11px;color:var(--dim);background:none;border:0;cursor:pointer;padding:2px 6px;border-radius:5px;line-height:16px}
.snz:hover{background:var(--ac);color:#fff}
.x:hover{background:var(--red);color:#fff}
.thumb{display:block;width:96px;height:60px;padding:0;border:.5px solid var(--bd);border-radius:6px;overflow:hidden;background:#fff;cursor:zoom-in}
.thumb img{width:100%;height:100%;object-fit:cover;object-position:top;display:block}
.card.expanded .thumb{display:none}
/* the details, folded under the text */
.detail{display:none;margin-top:12px;padding-top:12px;border-top:1px solid var(--bd);cursor:default}
.card.expanded .detail{display:block}
.detail-section{margin-bottom:14px}
.detail-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--subtle);margin-bottom:6px}
.detail-text{font-size:13px;line-height:1.5}
.why-list{list-style:none;margin:0;padding:0;font-size:13px;line-height:1.5}
.why-list li{padding:2px 0;display:flex;gap:8px;align-items:baseline}
.wy-k{font-weight:600;flex:none}
.wy-d{color:var(--dim)}
.intel-card{background:var(--sf);border:1px solid var(--bd);border-radius:10px;padding:12px 14px}
.intel-header{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:6px}
.intel-title{font-weight:600;font-size:13px}
.intel-link{font-size:12px;color:var(--ac);text-decoration:none;font-weight:600;white-space:nowrap}
.intel-link:hover{text-decoration:underline}
.intel-summary{font-size:13px;line-height:1.45;margin-bottom:8px}
.intel-tags{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px}
.intel-tag{font-size:11px;padding:2px 8px;border-radius:12px;background:var(--hv);color:var(--dim)}
.why-useful-box{border-top:1px solid var(--bd);padding-top:8px}
.why-useful-heading{font-size:12px;font-weight:600;margin-bottom:4px}
.why-useful-list{margin:0;padding-left:18px;font-size:12px;line-height:1.5}
.todo-list{display:flex;flex-direction:column;gap:2px}
.todo-item{display:flex;align-items:flex-start;gap:8px;font-size:13px;cursor:pointer;padding:3px 0}
.todo-item input{margin-top:3px;cursor:pointer;accent-color:var(--ac)}
.todo-item.done span{text-decoration:line-through;opacity:.5}
.screenshot-box{border:1px solid var(--bd);border-radius:10px;overflow:hidden;background:#fff;cursor:zoom-in}
.screenshot-img{width:100%;max-height:440px;object-fit:contain;object-position:top;display:block}
.detail-actions-bar{display:flex;gap:6px;flex-wrap:wrap;margin-top:2px}
.btn{display:inline-flex;align-items:center;padding:5px 12px;font:inherit;font-size:12px;font-weight:500;border-radius:7px;border:1px solid var(--bd);background:var(--sf);color:var(--tx);text-decoration:none;cursor:pointer}
.btn:hover{background:var(--hv)}
.btn-primary{background:var(--ac);color:#fff;border-color:var(--ac)}
.btn-primary:hover{background:var(--ac);opacity:.9}
.btn-dismiss:hover{background:var(--red);color:#fff;border-color:var(--red)}
/* also noticed: one line each */
.lp{display:flex;align-items:baseline;gap:8px;padding:6px 10px 6px 21px;font-size:13px;border-radius:8px;transition:opacity .25s,transform .25s}
.lp:hover{background:var(--hv)}
.lp-link{color:inherit;text-decoration:none;flex:1;min-width:0}
.lp-link:hover{text-decoration:underline}
.lp-why{font-size:11px;color:var(--subtle);white-space:nowrap}
.lp .x{visibility:hidden}.lp:hover .x,.lp:focus-within .x{visibility:visible}
/* radar */
.rsec{padding:2px 0}
.rlabel{font-size:10.5px;font-weight:600;color:var(--subtle);text-transform:uppercase;letter-spacing:.6px;padding:6px 10px 2px}
.rcount{font-weight:500;margin-left:2px}
.rr{display:flex;align-items:baseline;gap:6px;padding:4px 10px 4px 12px;font-size:13px;line-height:1.4;border-radius:8px;transition:opacity .25s,transform .25s;flex-wrap:wrap}
.rr:hover{background:var(--hv)}
.rk{color:var(--subtle);flex:none;width:12px;text-align:center;font-size:12px}
.rwhat{flex:1 1 220px;min-width:0}
.rwho{font-weight:600}.rwho::after{content:":"}
.rlink{color:inherit;text-decoration:none}.rlink:hover{text-decoration:underline}
.rmeta{font-size:12px;color:var(--subtle)}
.rdue{font-size:12px;color:var(--dim);font-variant-numeric:tabular-nums;white-space:nowrap}
.rdue.soon{color:var(--orange-t);font-weight:600}.rdue.overdue{color:var(--red);font-weight:600}
.rr .x{visibility:hidden;margin-left:auto}.rr:hover .x,.rr:focus-within .x{visibility:visible}
.rr.pat.late .rmeta{color:var(--orange-t)}
.rmetrics{display:flex;gap:12px;flex-wrap:wrap;flex-basis:100%;padding-left:18px;font-size:12px;color:var(--dim)}
.rmetric.up{color:var(--red)}.rmetric.down{color:var(--ok)}
.spark{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:1px;color:var(--ac)}
.dim{color:var(--subtle)}
.rfoot{font-size:11px;color:var(--subtle);padding:6px 10px 2px}
/* Worth knowing — the drawer above the footer: notes, then the radar; its own Clear */
.wk{border-top:1px solid var(--bd);background:var(--bg);padding:6px 10px 12px}
.wk-head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;padding:6px 10px 2px}
.wk-head .label{padding:0}
.wk-sub{flex:1;min-width:0;font-size:12px;color:var(--subtle)}
.wk-head button{background:none;border:0;color:var(--ac);cursor:pointer;font:inherit;font-size:12px;font-weight:500;padding:2px 6px;border-radius:5px}
.wk-head button:hover{background:var(--hv)}
.wk-head button[disabled]{opacity:.4;cursor:default;background:none}
.wk-notes{padding:2px 0}
.rr.note .rk{font-size:11px;font-weight:700}
.wk-empty{font-size:13px;color:var(--dim);padding:10px 12px 4px}
#wk-toggle.on{background:var(--hv)}
/* nothing on the briefing: what Otto is watching, quietly */
.empty-state{color:var(--dim);padding:8px 10px 4px;font-size:13px}
.empty-state:empty{display:none}
.monitor{display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:0 10px}
.monitor-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--dim);width:100%;padding:10px 0 4px}
.status-on,.status-off,.status-idle{font-size:12px;padding:3px 10px;border-radius:20px;font-weight:500}
.status-on{background:rgba(52,199,89,.14);color:#1f8a3d}
.status-off{background:rgba(255,149,0,.16);color:var(--orange-t)}
.status-idle{background:var(--hv);color:var(--subtle)}
@media(prefers-color-scheme:dark){.status-on{color:#4cd964}}
.directives{padding:0 10px}
.directive{font-size:13px;padding:2px 0 2px 12px;color:var(--dim)}
.suggestions{padding:8px 10px 0}.suggest-item{font-size:12px;color:var(--subtle);padding:3px 0 3px 12px}
.suggest-item::before,.directive::before{content:"•";color:var(--subtle);margin:0 8px 0 -12px}
/* footer — the panel's bar, stuck to the bottom of the window */
.ft{position:sticky;bottom:0;display:flex;align-items:center;gap:10px;padding:8px 20px;border-top:1px solid var(--bd);font-size:12px;color:var(--subtle);background:var(--sf);background:color-mix(in srgb,var(--sf) 88%,transparent);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
.ft-mid{flex:1;text-align:center;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ft button{background:none;border:0;color:var(--ac);cursor:pointer;font:inherit;font-weight:500;padding:2px 6px;border-radius:5px}
.ft button:hover{background:var(--hv)}
.ft button[disabled]{opacity:.4;cursor:default;background:none}
/* the picture at full size */
.lightbox-modal{position:fixed;inset:0;background:rgba(0,0,0,.72);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);z-index:9999;display:flex;align-items:center;justify-content:center;opacity:0;pointer-events:none;transition:opacity .2s}
.lightbox-modal.active{opacity:1;pointer-events:auto}
.lightbox-content{max-width:94vw;max-height:92vh;display:flex;flex-direction:column;background:var(--sf);border:1px solid var(--bd);border-radius:14px;overflow:hidden;box-shadow:0 24px 60px rgba(0,0,0,.45);transform:scale(.97);transition:transform .2s}
.lightbox-modal.active .lightbox-content{transform:scale(1)}
.lightbox-topbar{display:flex;justify-content:space-between;align-items:center;padding:8px 14px;border-bottom:1px solid var(--bd);font-size:12px;color:var(--dim)}
.lightbox-hint{font-weight:600;color:var(--ac);text-decoration:none}
.lightbox-close{background:none;border:none;font:inherit;font-size:14px;color:var(--dim);cursor:pointer;padding:4px 8px;border-radius:6px}
.lightbox-close:hover{background:var(--hv);color:var(--tx)}
.lightbox-img-wrap{overflow:auto;max-height:calc(92vh - 40px);display:flex;align-items:center;justify-content:center;background:#111}
.lightbox-img{max-width:94vw;max-height:calc(92vh - 40px);object-fit:contain;display:block}
@media(max-width:560px){body{padding:0}.sheet{border-radius:0;border-left:0;border-right:0}.hdr{padding:14px 14px 12px}.ft{padding:8px 12px}.thumb{width:72px;height:46px}.expand-cue{display:none}}
"""

JS = r"""
(function(){
  'use strict';
  var $=function(s,r){return (r||document).querySelector(s)};
  var live=$('#live'), lastUpdated=Number(document.body.getAttribute('data-updated')||0);

  function post(path,params){
    var body=new URLSearchParams(params||{}).toString();
    return fetch(path,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body,credentials:'same-origin'})
      .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r; });
  }
  // A row slides out, then the page re-reads itself so the header count, the
  // "why" tally and empty group headings follow (the server already knows).
  function leave(el){ if(!el)return; el.classList.add('leaving'); setTimeout(function(){el.remove(); refreshView(true);},260); }
  function failed(){ setLive('stale','Could not reach Otto'); setTimeout(poll,3000); }
  function cardOf(el){ return el.closest('.rr')||el.closest('.card')||el.closest('.lp'); }
  function setExpanded(card,on){
    card.classList.toggle('expanded',on);
    var b=card.querySelector('.title[data-action="toggle"]'); if(b) b.setAttribute('aria-expanded',on?'true':'false');
    if(on&&card.getAttribute('data-id')) feedback('expand',card.getAttribute('data-id'));
  }
  // "Tomorrow" means 8:00 tomorrow local time, not a fixed number of hours.
  function snoozeHours(v){
    if(v!=='tomorrow') return v||'1';
    var d=new Date(); d.setDate(d.getDate()+1); d.setHours(8,0,0,0);
    return Math.max(1,(d.getTime()-Date.now())/3600000).toFixed(2);
  }

  document.addEventListener('click',function(e){
    var t=e.target;
    var act=t.closest('[data-action]');
    if(act){
      var action=act.getAttribute('data-action');
      var host=cardOf(act), id=host?host.getAttribute('data-id'):'';
      if(action==='toggle'){ if(host) setExpanded(host,!host.classList.contains('expanded')); return; }
      if(action==='dismiss'){ e.stopPropagation(); if(id){post('/api/dismiss',{id:id}).then(function(){leave(host)}).catch(failed)} return; }
      if(action==='snooze'){ e.stopPropagation(); if(id){post('/api/snooze',{id:id,hours:snoozeHours(act.getAttribute('data-hours'))}).then(function(){leave(host)}).catch(failed)} return; }
      if(action==='lightbox'){ e.stopPropagation(); openLightbox(act.getAttribute('data-shot'),act.getAttribute('data-link')); return; }
      if(action==='close-lightbox'){ closeLightbox(); return; }
      if(action==='refresh'){ manualRefresh(act); return; }
      if(action==='clear'){ clearAll(act); return; }
      if(action==='toggle-wk'){ toggleWK(); return; }
      if(action==='clear-notes'){ clearNotes(act); return; }
    }
    var ref=t.closest('.dg-ref');
    if(ref){ gotoItem(ref.getAttribute('data-goto')); return; }
    // Opening the source is the clearest "this mattered": tell Otto so
    // channels and people you keep opening rise over time (and vice versa).
    var link=t.closest('a[href]');
    if(link){ var lc=cardOf(link); if(lc&&lc.getAttribute('data-id')&&!link.closest('.lightbox-modal')) feedback('open',lc.getAttribute('data-id')); return; }
    // Text inside the open details can be selected without folding the row.
    if(t.closest('a,button,input,label,.detail')) return;
    var card=t.closest('.card.clickable');
    if(card){ setExpanded(card,!card.classList.contains('expanded')); }
  });
  var fedBack={};
  function feedback(kind,id){
    if(!id||fedBack[kind+id]) return; fedBack[kind+id]=1;
    try{ post('/api/feedback',{kind:kind,id:id}).catch(function(){}); }catch(e){}
  }
  function gotoItem(id){
    var c=document.querySelector('.card[data-id="'+id+'"],.lp[data-id="'+id+'"]'); if(!c) return;
    var fold=c.closest('details'); if(fold) fold.open=true;
    if(c.classList.contains('card')) setExpanded(c,true);
    c.scrollIntoView({behavior:'smooth',block:'center'});
    c.classList.add('flash'); setTimeout(function(){c.classList.remove('flash')},1600);
  }
  // Ticks on the action checklist are yours, not Otto's: kept in this browser
  // (localStorage, keyed by item + text) so they survive the 15 s re-render.
  function doneKey(box){ var c=box.closest('[data-id]'), s=box.parentElement.querySelector('span'); return 'otto.done:'+(c?c.getAttribute('data-id'):'')+'|'+(s?s.textContent:''); }
  function restoreDone(){ try{ Array.prototype.forEach.call(document.querySelectorAll('.todo-item input'),function(b){ var on=localStorage.getItem(doneKey(b))==='1'; b.checked=on; b.parentElement.classList.toggle('done',on); }); }catch(e){} }
  document.addEventListener('change',function(e){
    if(!e.target.matches('.todo-item input')) return;
    e.target.parentElement.classList.toggle('done',e.target.checked);
    try{ if(e.target.checked) localStorage.setItem(doneKey(e.target),'1'); else localStorage.removeItem(doneKey(e.target)); }catch(err){}
  });
  restoreDone();
  document.addEventListener('keydown',function(e){ if(e.key==='Escape') closeLightbox(); });

  var modal=$('#lightbox-modal');
  function openLightbox(src,link){
    if(!modal||!src)return;
    $('#lightbox-img').src=src;
    var a=$('#lightbox-open'); if(a){ if(link){a.href=link;a.hidden=false}else{a.hidden=true} }
    modal.classList.add('active');
  }
  function closeLightbox(){ if(modal) modal.classList.remove('active'); }
  if(modal){ modal.addEventListener('click',function(e){ if(e.target===modal) closeLightbox(); }); }

  function setLive(state,text){ if(!live)return; live.className='live'+(state?' '+state:''); live.textContent=text; }
  function setText(sel,text){ var el=$(sel); if(el) el.textContent=text||''; }
  function setHTML(sel,html){ var el=$(sel); if(el) el.innerHTML=html||''; }

  var expandedIds=function(){ return Array.prototype.map.call(document.querySelectorAll('.card.expanded[data-id]'),function(c){return c.getAttribute('data-id')}); };

  function refreshView(force){
    return fetch('/briefing?partial=1',{credentials:'same-origin'}).then(function(r){return r.json()}).then(function(p){
      var open=expandedIds(), y=window.scrollY, fold=$('#main details'), foldOpen=!!(fold&&fold.open);
      setHTML('#summary',p.summary); setHTML('#meta',p.meta); setHTML('#main',p.main);
      var why=$('#why'); if(why) why.innerHTML=p.why||'';
      var dg=$('#digest'); if(dg) dg.innerHTML=p.digest||'';
      setHTML('#problems',p.problems); setHTML('#ft-count',p.footer);
      setText('#ring-n',Number(p.important)>0?p.important:'');
      syncClear(Number(p.count));
      setHTML('#wk-body',p.wk); syncWK(Number(p.wk_count),Number(p.wk_clearable));
      open.forEach(function(id){ var c=document.querySelector('.card[data-id="'+id+'"]'); if(c) setExpanded(c,true); });
      fold=$('#main details'); if(fold&&foldOpen) fold.open=true;
      restoreDone();
      window.scrollTo(0,y);
      document.title=(Number(p.count)>0?'('+p.count+') ':'')+'Otto Briefings';
      setLive('','Updated just now');
    }).catch(function(){ setLive('stale','Offline'); });
  }

  var refreshing=false;
  function poll(){
    fetch('/api/status',{credentials:'same-origin'}).then(function(r){return r.json()}).then(function(s){
      if(s.refreshing){ setLive('busy','Refreshing…'); }
      else if(s.last_updated&&s.last_updated>lastUpdated){ lastUpdated=s.last_updated; refreshView(); }
      else { var age=s.last_updated?Math.round((Date.now()/1000-s.last_updated)/60):null; setLive('',age===null?'Live':age<1?'Updated just now':'Updated '+age+'m ago'); }
    }).catch(function(){ setLive('stale','Engine offline'); });
  }
  setInterval(poll,15000);
  document.addEventListener('visibilitychange',function(){ if(!document.hidden) poll(); });

  function refreshButtons(on){ Array.prototype.forEach.call(document.querySelectorAll('[data-action="refresh"]'),function(b){ b.disabled=on; }); }
  function manualRefresh(){
    if(refreshing)return; refreshing=true;
    refreshButtons(true); setLive('busy','Refreshing…');
    function done(){ refreshing=false; refreshButtons(false); }
    post('/api/refresh').then(function(){
      var tries=0, iv=setInterval(function(){
        tries++;
        fetch('/api/status',{credentials:'same-origin'}).then(function(r){return r.json()}).then(function(s){
          if((s.last_updated&&s.last_updated>lastUpdated)||tries>40){ clearInterval(iv); done(); if(s.last_updated>lastUpdated){lastUpdated=s.last_updated; refreshView();} else setLive('','Live'); }
        }).catch(function(){ clearInterval(iv); done(); });
      },1500);
    }).catch(done);
  }

  // One click, no question: the items leave the briefing, the radar stays,
  // and Otto remembers what it read (nothing is deleted).
  function syncClear(count){ var b=$('[data-action="clear"]'); if(b) b.disabled=count===0; }   // nothing to clear → greyed, as in the panel
  function clearAll(btn){
    if(!document.querySelectorAll('.card[data-id],.lp[data-id]').length)return;
    btn.disabled=true;
    post('/api/clear').then(function(){ refreshView(true); }).catch(function(){ failed(); btn.disabled=false; });
  }

  // Worth knowing — heads-ups, what's likely next, the radar, what needs a
  // look about Otto itself — is not a notification: it sits behind one button
  // and clears on its own (the About Otto rows go when fixed, so Clear counts
  // only the notes and rows). Whether the drawer is open is this browser's
  // choice and survives the re-render.
  var wk=$('#wk'), wkToggle=$('#wk-toggle');
  function syncWK(count,clearable){
    setText('#wk-cnt',String(count));
    if(wkToggle){ wkToggle.textContent='Worth knowing · '+count; wkToggle.hidden=count===0&&!!(wk&&wk.hidden); }
    var cb=$('[data-action="clear-notes"]'); if(cb) cb.disabled=clearable===0;
  }
  function setWK(open){
    if(!wk)return; wk.hidden=!open;
    if(wkToggle){ wkToggle.setAttribute('aria-expanded',open?'true':'false'); wkToggle.classList.toggle('on',open); }
    try{ localStorage.setItem('otto.wk',open?'1':'0'); }catch(e){}
  }
  function toggleWK(){ var open=!!(wk&&wk.hidden); setWK(open); if(open&&wk) wk.scrollIntoView({behavior:'smooth',block:'nearest'}); }
  function clearNotes(btn){
    btn.disabled=true;
    post('/api/clear',{scope:'notes'}).then(function(){ refreshView(true); }).catch(function(){ failed(); btn.disabled=false; });
  }
  try{ if(localStorage.getItem('otto.wk')==='1') setWK(true); }catch(e){}
})();
"""


def render_page(data: Dict[str, Any], hidden: Iterable[str] = (), nonce: str | None = None,
                last_updated: float = 0.0) -> str:
    """The whole page: the panel's layout — header, rows, radar, footer — with room for the details."""
    nonce = nonce or new_nonce()
    parts = render_body(data, hidden)
    count = int(parts["count"])
    important = int(parts["important"])
    wk_count = int(parts["wk_count"])
    wk_clearable = int(parts["wk_clearable"])
    title_prefix = f"({count}) " if count else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light dark">
<title>{title_prefix}Otto Briefings</title>
<style nonce="{nonce}">{CSS}</style>
</head>
<body data-updated="{last_updated:.0f}">
<div class="sheet">
<header class="hdr">
  <div class="bar-row">
    <span class="ring" aria-hidden="true"><span class="ring-o"></span><span class="ring-n" id="ring-n">{important if important else ""}</span></span>
    <h1>Briefings</h1>
    <span class="live" id="live">{parts["updated"] or "Live"}</span>
    <button type="button" class="tb" data-action="refresh" title="Refresh now" aria-label="Refresh now">↻</button>
  </div>
  <div class="s"><span id="summary">{parts["summary"]}</span><span class="s2" id="why">{parts["why"]}</span></div>
  <div class="d" id="meta">{parts["meta"]}</div>
  <div id="digest">{parts["digest"]}</div>
</header>
<div class="w" id="main">
{parts["main"]}
</div>
<section class="wk" id="wk" aria-label="{WORTH_KNOWING_LABEL}" hidden>
  <div class="wk-head">
    <span class="label">{WORTH_KNOWING_LABEL} <span class="cnt" id="wk-cnt">{wk_count}</span></span>
    <span class="wk-sub">{WORTH_KNOWING_SUB}</span>
    <button type="button" data-action="clear-notes" title="Dismiss every note and radar row here (one click; nothing is deleted from Otto's memory)"{"" if wk_clearable else " disabled"}>Clear</button>
  </div>
  <div id="problems">{parts["problems"]}</div>
  <div id="wk-body">{parts["wk"]}</div>
</section>
<footer class="ft">
  <span id="ft-count">{parts["footer"]}</span>
  <span class="ft-mid" title="Otto only reads. Nothing leaves this Mac except what a model key you added sends to that provider.">Read-only · All data stays on your Mac</span>
  <button type="button" data-action="toggle-wk" id="wk-toggle" aria-expanded="false" aria-controls="wk" title="{WORTH_KNOWING_SUB}"{"" if wk_count else " hidden"}>{WORTH_KNOWING_LABEL} · {wk_count}</button>
  <button type="button" data-action="refresh">Refresh</button>
  <button type="button" data-action="clear" title="Dismiss everything on the briefing (one click; items stay in Otto's memory)"{"" if count else " disabled"}>Clear</button>
</footer>
</div>
<div id="lightbox-modal" class="lightbox-modal">
  <div class="lightbox-content">
    <div class="lightbox-topbar">
      <a id="lightbox-open" class="lightbox-hint" href="#" target="_blank" rel="noopener noreferrer" hidden>Open in Slack ↗</a>
      <button type="button" class="lightbox-close" data-action="close-lightbox" title="Close (Esc)">✕</button>
    </div>
    <div class="lightbox-img-wrap"><img id="lightbox-img" class="lightbox-img" src="" alt="Enlarged preview"></div>
  </div>
</div>
<script nonce="{nonce}">{JS}</script>
</body>
</html>"""


def render_briefing_html(data: Dict[str, Any], nonce: str | None = None, hidden: Iterable[str] | None = None) -> str:
    """Full page. ``hidden`` defaults to the user's dismissed ∪ snoozed IDs."""
    if hidden is None:
        from otto.web import state
        hidden = state.hidden_ids()
    return render_page(data, hidden, nonce=nonce)


LOADING_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="color-scheme" content="light dark">
<title>Otto Briefing</title>
<meta http-equiv="refresh" content="4">
<style nonce="{nonce}">body{{font-family:-apple-system,system-ui,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#fbfbfd;color:#1d1d1f}}
@media(prefers-color-scheme:dark){{body{{background:#000;color:#f5f5f7}}}}
.loader{{text-align:center}}.spinner{{width:36px;height:36px;border:3px solid rgba(127,127,127,.2);border-top-color:#0071e3;border-radius:50%;animation:spin 1s linear infinite;margin:0 auto 16px}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}p{{opacity:.7;font-size:14px}}</style></head>
<body><div class="loader"><div class="spinner"></div><p>Gathering your briefing…</p></div></body></html>"""

# Shown when rendering a request raises: the engine is still running and the
# next refresh usually clears it — say so, and point at the log.
ERROR_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="color-scheme" content="light dark">
<title>Otto Briefing</title>
<meta http-equiv="refresh" content="30">
<style>body{{font-family:-apple-system,system-ui,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#fbfbfd;color:#1d1d1f}}
@media(prefers-color-scheme:dark){{body{{background:#000;color:#f5f5f7}}}}
.box{{max-width:520px;text-align:center;padding:24px}}h1{{font-size:18px;font-weight:600;margin:0 0 8px}}p{{opacity:.75;font-size:14px;line-height:1.5}}code{{font-size:12px;opacity:.7}}</style></head>
<body><div class="box"><h1>Otto could not draw this page</h1>
<p>The engine is still running and will try again on the next refresh. If this keeps happening, <code>otto logs</code> has the details.</p>
<p><code>{detail}</code></p></div></body></html>"""
