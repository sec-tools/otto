from __future__ import annotations
"""
Content parsing utilities.

Handles HTML→text extraction, MIME multipart parsing, and
content normalization for the ingestion pipeline.
"""

import hashlib
import html
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from io import StringIO

logger = logging.getLogger("otto.utils.content_parser")


class _HTMLTextExtractor(HTMLParser):
    """Simple HTML→text extractor that strips tags and normalizes whitespace."""

    SKIP_TAGS = {"script", "style", "head", "meta", "link"}

    def __init__(self) -> None:
        super().__init__()
        self._result = StringIO()
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag.lower() in ("br", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"):
            self._result.write("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._result.write(data)

    def get_text(self) -> str:
        return self._result.getvalue()


def html_to_text(html_content: str) -> str:
    """
    Convert HTML content to plain text.

    Strips tags, removes script/style blocks, normalizes whitespace,
    and decodes HTML entities.

    Args:
        html_content: Raw HTML string.

    Returns:
        Plain text representation.
    """
    if not html_content:
        return ""

    try:
        extractor = _HTMLTextExtractor()
        extractor.feed(html_content)
        text = extractor.get_text()
    except Exception:
        # Fallback: regex-based strip
        text = re.sub(r"<[^>]+>", " ", html_content)

    # Decode HTML entities
    text = html.unescape(text)

    # Normalize whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    return text


def strip_email_signature(text: str) -> str:
    """
    Strip common email signature patterns.

    Removes content after common signature delimiters like
    '-- ', 'Sent from my iPhone', legal disclaimers, etc.
    """
    # Common signature delimiters
    patterns = [
        r"\n--\s*\n",              # Standard sig delimiter
        r"\nSent from my ",         # Mobile signature
        r"\nGet Outlook for ",
        r"\n_{5,}",                 # Underscore dividers
        r"\nConfidentiality Notice",
        r"\nThis email and any ",
        r"\nDISCLAIMER:",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            text = text[:match.start()].rstrip()
            break

    return text


def content_hash(text: str) -> str:
    """
    Generate a SHA-256 hash of text content.

    Used for deduplication and change detection.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def truncate_for_llm(text: str, max_chars: int = 2000) -> str:
    """Truncate text for LLM input, breaking at sentence boundaries when possible."""
    if len(text) <= max_chars:
        return text

    # Try to break at a sentence boundary
    truncated = text[:max_chars]
    last_period = truncated.rfind(". ")
    if last_period > max_chars * 0.7:
        return truncated[:last_period + 1]

    return truncated + "..."


@dataclass
class EmailSnippet:
    subject: str
    sender: str
    snippet: str
    time_str: str
    is_unread: bool


@dataclass
class MessageSnippet:
    sender: str
    text: str
    time_str: str
    channel: str
    is_bot: bool
    # Index (into the list this snippet came back in) of the message this one
    # replies to — Slack's thread panel lists replies under their parent — or
    # -1 for a top-level message.
    reply_to: int = -1


@dataclass
class IssueSnippet:
    key: str
    summary: str
    status: str
    priority: str
    assignee: str
    description: str
    comment_count: int


@dataclass
class CalendarSnippet:
    title: str
    start_str: str
    end_str: str
    location: str
    description: str
    attendees: list[str]


def parse_gmail_page_text(text: str) -> list[EmailSnippet]:
    """
    Parse Gmail's rendered inbox text into a list of EmailSnippet objects.
    Looks for senders, subjects, unread indicators, and timestamps.
    """
    snippets = []
    try:
        blocks = re.split(r'\n\s*\n', text)
        for block in blocks:
            try:
                lines = [line.strip() for line in block.split('\n') if line.strip()]
                if len(lines) >= 3:
                    sender = lines[0]
                    subject = lines[1]
                    snippet = " ".join(lines[2:-1]) if len(lines) > 3 else lines[2]
                    time_str = lines[-1]
                    is_unread = False
                    
                    if "unread" in sender.lower() or sender.startswith("*") or sender.startswith("•"):
                        is_unread = True
                        sender = re.sub(r'^(?:\*|•|\bunread\b)\s*', '', sender, flags=re.IGNORECASE).strip()
                    
                    if re.search(r'\d', time_str) or "ago" in time_str:
                        snippets.append(EmailSnippet(
                            subject=subject,
                            sender=sender,
                            snippet=snippet,
                            time_str=time_str,
                            is_unread=is_unread
                        ))
                    if len(snippets) >= 50:
                        break
            except Exception:
                continue
    except Exception:
        pass
    return snippets[:50]


# ---------------------------------------------------------------------------
# Slack message lists
#
# Slack renders a message as a header — sender, an optional "APP" badge, a
# timestamp — followed by the body. Read through the Accessibility tree
# (Slack.app) each of those is its own line; read through the web client's
# innerText the header is usually one line ("Alice  10:30 AM"). Both the
# desktop and the web adapters feed their lines through
# ``parse_slack_message_lines`` so they behave identically.
# ---------------------------------------------------------------------------

_SLACK_TIME = (
    r'(?i:\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?'                                   # 8:19 AM · 14:05 · 6:55:49 AM
    r'|(?:Today|Yesterday)\s+at\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?'           # Today at 9:15 AM
    r'|[A-Z][a-z]{2,8}\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\s+at\s+'           # Aug 21st at 6:55:49 AM
    r'\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)'
)
# Fresh messages — the thread panel especially — carry a relative stamp
# ("4 minutes ago", "Just now") instead of a clock time. Accepted only as a
# line of its own (or after an APP badge), never inside prose, so a sentence
# ending in "…10 minutes ago" is not mistaken for a header.
_SLACK_RELATIVE_TIME = (
    r'(?i:just now|now|(?:a|an|\d{1,3})\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\s+ago)'
)
_SLACK_TIME_LINE = re.compile(rf'^(?:{_SLACK_TIME}|{_SLACK_RELATIVE_TIME})$')
_SLACK_INLINE_HEADER = re.compile(rf'^(?P<sender>\S.{{0,59}}?)\s+(?P<app>(?:APP|\[BOT\])\s+)?(?P<time>{_SLACK_TIME})$')
# The web client puts the badge and the time on one line: "APP  10:57 AM".
_SLACK_BADGE_TIME_LINE = re.compile(rf'^(?:APP|\[BOT\])\s+(?P<time>{_SLACK_TIME}|{_SLACK_RELATIVE_TIME})$')
# Thread summary under a message ("1 reply", "3 new replies") — the clock
# time that follows it is the last reply's time, not a new message.
_SLACK_REPLIES_LINE = re.compile(r'^\d+\s+(?:new\s+)?repl(?:y|ies)$', re.IGNORECASE)
_SLACK_VIEW_THREAD = re.compile(r'^(?:view|see)\s+(?:thread|all\s+replies|\d+\s+(?:more\s+)?repl(?:y|ies))$', re.IGNORECASE)
_SLACK_APP_BADGES = ("APP", "[BOT]", "BOT")
_SLACK_DIVIDER_DATE = re.compile(
    r'^(?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,\s*)?(?P<date>[A-Z][a-z]+\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?)$'
)

# Words a display name never ends with, but a sentence fragment ("moved to
# 3:00 PM") does — protects the one-line header check from prose.
_NOT_A_NAME_TAIL = {
    "to", "at", "by", "on", "in", "is", "was", "for", "until", "till", "from", "the",
    "a", "an", "of", "and", "or", "around", "before", "after", "about", "till", "since",
}

# Day separators Slack inserts between messages.
_SLACK_DIVIDER = re.compile(
    r'^(?:Today|Yesterday|(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day)'
    r'(?:,\s*[A-Z][a-z]+\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?)?$'
    r'|^[A-Z][a-z]+\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?$'
)

# Chrome that sits inside or right after the message list.
_SLACK_CHROME_LINES = {
    '#home', 'home', 'dms', 'dm', 'activity', 'files', 'agents & tools', 'agents',
    'admin', 'huddles', 'directories', 'directory', 'threads', 'later', 'add channels',
    'invite people', 'connect apps', 'messages', 'add canvas', 'canvas', 'list',
    'folder', 'preferences', 'profile', 'sign out', 'search', 'latest messages',
    'new messages', 'new', 'view thread', 'also sent to the channel', 'upgrade plan',
    'slack is trying to connect.', 'slack is trying to connect', 'add reaction',
    # A link unfurl's controls sit between the preview text and the next
    # message's time; "Remove preview" would otherwise pass for a sender.
    'remove preview', 'show preview', 'hide preview',
    'jump to first unread message (⌘j)', 'mark as read (esc)', 'jump to bottom',
    # Thread panel: title, close button, composer ("Reply…", "Also send to #chan").
    'thread', 'close', 'reply', 'reply…', 'reply...', 'reply in thread', 'also send to',
    'follow thread', 'following', 'unfollow thread',
}
_SLACK_CHROME_PATTERNS = (
    re.compile(r'^\d+\s+characters? remaining$', re.IGNORECASE),
    re.compile(r'^get \d+% off', re.IGNORECASE),
    re.compile(r'^\d+ days? left$', re.IGNORECASE),
    re.compile(r'^slack works better', re.IGNORECASE),
    re.compile(r'^message (?:#|@)', re.IGNORECASE),           # composer placeholder
    re.compile(r'^message\s+[\w.-]+$', re.IGNORECASE),        # "Message security-alerts"
    re.compile(r'^shift \+ return to add a new line$', re.IGNORECASE),
    re.compile(r'^loading(?:\.\.\.|…)?$', re.IGNORECASE),
    re.compile(r'^loading (?:messages|more|older|newer|replies|conversation)\b', re.IGNORECASE),  # "Loading messages for eng…"
    re.compile(r'^search\s+\S+$', re.IGNORECASE),             # "Search demo" (workspace search box)
    re.compile(r'^\d{1,3}$'),                                  # unread badges
    re.compile(r'^[A-Z]$'),                                    # workspace avatar initial
    re.compile(r'^\d+ (?:new )?repl(?:y|ies)$', re.IGNORECASE),
    re.compile(r'^last reply ', re.IGNORECASE),
    # Thread composer: the "Also send to #channel" checkbox and its AX label "Channel <name>".
    re.compile(r'^also send to(?:\s+#?[\w.-]+)?$', re.IGNORECASE),
    re.compile(r'^channel\s+#?[\w.-]+$', re.IGNORECASE),
    re.compile(r'^(?:view|see) (?:thread|all replies|\d+ (?:more )?repl(?:y|ies))$', re.IGNORECASE),
    re.compile(r'^\d+ (?:people|members?|person)(?: (?:are|is) typing)?$', re.IGNORECASE),
    re.compile(r'^(?:someone|[\w.-]+) is typing(?:…|\.\.\.)?$', re.IGNORECASE),
    re.compile(r'^(?:edited)$', re.IGNORECASE),
    re.compile(r'^\(edited\)$', re.IGNORECASE),
    re.compile(r'^drag and drop important stuff here$', re.IGNORECASE),
    re.compile(r'^invite teammates$', re.IGNORECASE),
    re.compile(r'^(?:starred|channels|direct messages|agents & apps)$', re.IGNORECASE),
)


def is_slack_chrome_line(line: str) -> bool:
    """True for sidebar / toolbar / composer text that is not message content."""
    low = line.strip().lower()
    if not low or low in _SLACK_CHROME_LINES:
        return True
    return any(p.match(low) for p in _SLACK_CHROME_PATTERNS)


def is_slack_time_line(line: str) -> bool:
    return bool(_SLACK_TIME_LINE.match(line.strip()))


def _looks_like_slack_sender(line: str) -> bool:
    """A display name / app name: short, a few words, no prose punctuation."""
    s = line.strip()
    if not s or len(s) > 32 or s in _SLACK_APP_BADGES:
        return False
    if is_slack_time_line(s) or is_slack_chrome_line(s) or _SLACK_DIVIDER.match(s):
        return False
    if s[0] in '[•*->#@(' or '://' in s or ':' in s:
        return False
    if s[-1] in '.!?,;':
        return False
    words = s.split()
    if len(words) > 3 or words[-1].lower() in _NOT_A_NAME_TAIL:
        return False
    return True


def parse_slack_message_lines(lines: list[str], channel: str = "", default_sender: str = "") -> list[MessageSnippet]:
    """
    Split the visible lines of a Slack conversation into messages.

    A message starts at a timestamp line (or an inline ``sender  time``
    header) and runs until the next header; the sender is the line before
    the timestamp (skipping an ``APP`` badge), or — when Slack collapsed the
    header because the same person posted again — the previous message's
    sender. Everything before the first header (sidebar, toolbar) and every
    chrome line is discarded.

    Thread panel: Slack lists a parent, an ``N replies`` line, then the
    replies (usually with relative stamps such as ``4 minutes ago``). Those
    replies come back with ``reply_to`` pointing at the parent so the adapter
    can keep the thread together. In the channel view the same ``N replies``
    line is followed by the last reply's time and ``View thread`` — neither is
    a message.
    """
    clean = [ln.replace('\xa0', ' ').strip() for ln in lines if ln and ln.strip()]
    n = len(clean)
    headers: list[tuple[int, int, str, str, bool]] = []   # (start, body_start, sender, time, is_bot)
    chan_names = {channel.strip().lower(), channel.strip().lstrip('#@').lower()} - {""}

    # Day dividers ("Wednesday, September 2nd", "Yesterday") date the times
    # that follow them; a bare "7:36" under such a divider is not today.
    day_context = ""

    def _dated(time_str: str) -> str:
        if not day_context or re.search(r'\bat\b|\bago\b|^(?:just )?now$', time_str, re.IGNORECASE):
            return time_str
        return f"{day_context} at {time_str}"

    for i, line in enumerate(clean):
        divider = _SLACK_DIVIDER.match(line)
        if divider:
            dated = _SLACK_DIVIDER_DATE.match(line)
            low = line.lower()
            if dated:
                day_context = dated.group("date")
            elif low in ("today", "yesterday"):
                day_context = line.title()
            else:
                day_context = ""          # weekday alone — not enough to date it
            continue
        if _SLACK_TIME_LINE.match(line):
            if i >= 1 and _SLACK_REPLIES_LINE.match(clean[i - 1]):
                continue                  # "1 reply" / "Today at 7:15 PM" — the thread summary
            sender, is_bot, start = "", False, i
            if i >= 2 and clean[i - 1] in _SLACK_APP_BADGES and _looks_like_slack_sender(clean[i - 2]):
                sender, is_bot, start = clean[i - 2], True, i - 2
            elif i >= 1 and _looks_like_slack_sender(clean[i - 1]):
                sender, start = clean[i - 1], i - 1
            headers.append((start, i + 1, sender, _dated(line), is_bot))
            continue
        badge = _SLACK_BADGE_TIME_LINE.match(line)
        if badge:
            sender, start = "", i
            if i >= 1 and _looks_like_slack_sender(clean[i - 1]):
                sender, start = clean[i - 1], i - 1
            headers.append((start, i + 1, sender, _dated(badge.group("time").strip()), True))
            continue
        m = _SLACK_INLINE_HEADER.match(line)
        if m and not is_slack_chrome_line(line) and _looks_like_slack_sender(m.group("sender")):
            headers.append((i, i + 1, m.group("sender").strip(), _dated(m.group("time").strip()), bool(m.group("app"))))

    snippets: list[MessageSnippet] = []
    previous_sender = default_sender
    # Index (in ``snippets``) of the thread parent while walking the replies
    # listed under it in the thread panel; -1 in the plain channel view.
    thread_parent = -1
    for k, (start, body_start, sender, time_str, is_bot) in enumerate(headers):
        end = headers[k + 1][0] if k + 1 < len(headers) else n
        raw_body = clean[body_start:end]
        body = [
            ln for ln in raw_body
            if not is_slack_chrome_line(ln) and not _SLACK_DIVIDER.match(ln)
            and not _SLACK_TIME_LINE.match(ln) and ln.lower() not in chan_names
        ]
        text = "\n".join(body).strip()
        if not text:
            continue
        sender = sender or previous_sender
        previous_sender = sender or previous_sender
        snippets.append(MessageSnippet(
            sender=sender, text=text, time_str=time_str, channel=channel, is_bot=is_bot, reply_to=thread_parent,
        ))
        # What follows this message decides whether the next one is its reply:
        # "N replies" alone opens the thread panel's reply list; "N replies"
        # followed by the last reply's time / "View thread" / "Last reply …" is
        # the channel view's summary and opens nothing.
        for j, ln in enumerate(raw_body):
            if not _SLACK_REPLIES_LINE.match(ln):
                continue
            after = raw_body[j + 1:]
            summary = (
                (after and _SLACK_TIME_LINE.match(after[0]))
                or any(_SLACK_VIEW_THREAD.match(x) or x.lower().startswith("last reply") for x in after)
            )
            thread_parent = -1 if summary else len(snippets) - 1
        if len(snippets) >= 100:
            break
    return snippets


def parse_slack_page_text(text: str) -> list[MessageSnippet]:
    """
    Parse the Slack web client's rendered page text into MessageSnippets.

    The channel comes from a leading ``#channel`` line when there is one; the
    messages come from :func:`parse_slack_message_lines`.
    """
    try:
        text = (text or "").replace('\r\n', '\n').replace('\r', '\n')
        lines = [ln.strip() for ln in text.split('\n') if ln.strip() and not is_slack_chrome_line(ln)]
        if not lines:
            return []

        # Only an explicit "#channel" line is trusted here; the adapter knows
        # the real conversation from the tab title and overrides this.
        channel = "Unknown"
        for idx, line in enumerate(lines[:3]):
            if re.match(r'^#[a-zA-Z0-9_-]+$', line) and len(line) < 50:
                channel = line
                del lines[idx]
                break

        return parse_slack_message_lines(lines, channel=channel)
    except Exception:
        return []


def parse_jira_page_text(text: str) -> list[IssueSnippet]:
    """
    Parse Jira issue pages into a list of IssueSnippet objects.
    Looks for PROJECT-123 patterns and standard Jira fields.
    """
    snippets = []
    try:
        pattern = r'([A-Z]+-\d+)'
        blocks = re.split(pattern, text)
        for i in range(1, len(blocks), 2):
            try:
                key = blocks[i]
                content = blocks[i+1]
                
                lines = [line.strip() for line in content.split('\n') if line.strip()]
                summary = lines[0] if lines else ""
                status = "Unknown"
                priority = "Unknown"
                assignee = "Unknown"
                description = ""
                comment_count = 0
                
                status_match = re.search(r'(?:status|state):\s*([a-zA-Z\s]+)', content, re.IGNORECASE)
                if status_match: status = status_match.group(1).strip()
                
                priority_match = re.search(r'(?:priority):\s*([a-zA-Z\s]+)', content, re.IGNORECASE)
                if priority_match: priority = priority_match.group(1).strip()
                
                assignee_match = re.search(r'(?:assignee):\s*([a-zA-Z\s]+)', content, re.IGNORECASE)
                if assignee_match: assignee = assignee_match.group(1).strip()
                
                desc_match = re.search(r'description:?\s*(.*?)(?:comments?:?|status:?|priority:?|$)', content, re.IGNORECASE | re.DOTALL)
                if desc_match: description = desc_match.group(1).strip()[:200]
                
                comments_match = re.findall(r'comment', content.lower())
                comment_count = len(comments_match)
                
                snippets.append(IssueSnippet(
                    key=key,
                    summary=summary,
                    status=status,
                    priority=priority,
                    assignee=assignee,
                    description=description,
                    comment_count=comment_count
                ))
                if len(snippets) >= 50:
                    break
            except Exception:
                continue
    except Exception:
        pass
    return snippets[:50]


def parse_calendar_events_text(text: str) -> list[CalendarSnippet]:
    """
    Parse Calendar.app AppleScript output or Google Calendar rendered text
    into a list of CalendarSnippet objects.
    """
    snippets = []
    try:
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        for line in lines:
            try:
                if '|||' in line:
                    parts = line.split('|||')
                    if len(parts) >= 6:
                        snippets.append(CalendarSnippet(
                            title=parts[0],
                            start_str=parts[1],
                            end_str=parts[2],
                            location=parts[3],
                            description=parts[4],
                            attendees=[a.strip() for a in parts[5].split(',') if a.strip()]
                        ))
                else:
                    if re.search(r'\d{1,2}:\d{2}.*?-.*?\d{1,2}:\d{2}', line):
                        snippets.append(CalendarSnippet(
                            title=line,
                            start_str="",
                            end_str="",
                            location="",
                            description="",
                            attendees=[]
                        ))
            except Exception:
                continue
    except Exception:
        pass
    return snippets
