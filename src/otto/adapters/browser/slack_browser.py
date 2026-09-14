"""
Browser / native Slack adapter — read-only.

Extracts Slack messages from open Slack browser tabs and from the Slack
desktop app's accessibility tree. No write methods. Maintains the
read-only safety invariant.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from otto.adapters.base import ConnectionStatus, RawEvent
from otto.adapters.browser.reader import BrowserContentReader, SlackAppRead
from otto.storage.models import (
    ConnectionState,
    ContentBlock,
    ContentType,
    HealthStatus,
    SourceType,
)
from otto.utils.content_parser import parse_slack_message_lines, parse_slack_page_text

logger = logging.getLogger("otto.adapters.browser.slack_browser")

# Otto can only *see* the channel that is currently on screen. To keep
# previously viewed channels in the briefing we remember their events for a
# while (per process). Bounded by age and count.
_CHANNEL_CACHE_TTL = 24 * 3600
_CHANNEL_CACHE_MAX = 30
_GLOBAL_SLACK_CHANNEL_CACHE: dict[str, tuple[float, list[RawEvent]]] = {}


# Slack re-renders a message's time as it ages ("Just now" → "4 minutes ago"
# → "7:15 PM" → "Today at 7:15 PM"). The first resolution wins, so a message
# keeps one timestamp — and one id — for as long as the process lives.
_FIRST_SEEN_TS: dict[str, datetime] = {}
_FIRST_SEEN_MAX = 4000

# Which conversation the Slack window showed at the last read, so a thumbnail
# of that window is only ever attached to items from *that* conversation.
_VISIBLE: dict[str, Any] = {"channel": "", "at": 0.0}
VISIBLE_CHANNEL_TTL = 15 * 60


def clear_slack_channel_cache() -> None:
    """Clear the multi-channel cache and the per-process memos (useful in tests)."""
    _GLOBAL_SLACK_CHANNEL_CACHE.clear()
    _FIRST_SEEN_TS.clear()
    _VISIBLE.update(channel="", at=0.0)


def visible_channel() -> str:
    """Channel/DM the Slack window showed at the last read (``""`` if unknown or stale)."""
    if time.time() - float(_VISIBLE["at"]) > VISIBLE_CHANNEL_TTL:
        return ""
    return str(_VISIBLE["channel"])


def _note_visible(channel: str) -> None:
    _VISIBLE.update(channel=channel, at=time.time())


def _stable_timestamp(channel: str, sender: str, text: str, time_str: str, now: datetime) -> datetime:
    """
    The message's timestamp, remembered per (channel, sender, text) so later
    renderings of the same message resolve to the same minute.
    """
    resolved = BrowserSlackAdapter._timestamp_from_time_str(time_str, now) if time_str else now
    if not time_str:
        return resolved
    key = hashlib.sha256(f"{channel}|{sender}|{text[:80]}".encode("utf-8")).hexdigest()[:24]
    kept = _FIRST_SEEN_TS.get(key)
    if kept is not None:
        return kept
    if len(_FIRST_SEEN_TS) >= _FIRST_SEEN_MAX:
        for old in list(_FIRST_SEEN_TS)[: _FIRST_SEEN_MAX // 4]:
            _FIRST_SEEN_TS.pop(old, None)
    _FIRST_SEEN_TS[key] = resolved
    return resolved


def _minute_key(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _cache_channel(channel: str, events: list[RawEvent]) -> None:
    now = time.time()
    _GLOBAL_SLACK_CHANNEL_CACHE[channel] = (now, events)
    stale = [c for c, (ts, _) in _GLOBAL_SLACK_CHANNEL_CACHE.items() if now - ts > _CHANNEL_CACHE_TTL]
    for c in stale:
        _GLOBAL_SLACK_CHANNEL_CACHE.pop(c, None)
    while len(_GLOBAL_SLACK_CHANNEL_CACHE) > _CHANNEL_CACHE_MAX:
        oldest = min(_GLOBAL_SLACK_CHANNEL_CACHE.items(), key=lambda kv: kv[1][0])[0]
        _GLOBAL_SLACK_CHANNEL_CACHE.pop(oldest, None)


# Cache workspace info so we only read the file once per process
_WORKSPACE_INFO: dict[str, str] | None = None
_SLACK_STATE_PATH = "~/Library/Application Support/Slack/storage/root-state.json"


def _load_workspace_info() -> dict[str, str]:
    """Read the selected Slack workspace domain and team ID from Slack's local state."""
    global _WORKSPACE_INFO
    if _WORKSPACE_INFO is not None:
        return _WORKSPACE_INFO

    info: dict[str, str] = {}
    try:
        state_path = os.path.expanduser(os.environ.get("OTTO_SLACK_STATE_PATH", _SLACK_STATE_PATH))
        if os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            workspaces = data.get("workspaces", {}) or {}
            meta = data.get("workspacesMeta", {}) or {}
            selected = meta.get("selectedWorkspaceId", "")
            ws_data = workspaces.get(selected)
            if not ws_data and workspaces:
                selected = next(iter(workspaces))
                ws_data = workspaces[selected]
            if ws_data:
                info = {"team_id": str(selected), "domain": str(ws_data.get("domain", "") or "")}
                logger.debug("Slack workspace: %s (%s)", info["domain"], selected)
    except Exception as e:
        logger.debug("Could not read Slack workspace info: %s", e)

    _WORKSPACE_INFO = info
    return info


def _sidebar_key(name: str, dm: bool) -> str:
    """The channel-cache key a sidebar entry corresponds to (``ops`` / ``dm-alice``)."""
    clean = (name or "").strip().lstrip("#@").strip()
    return f"dm-{clean}" if dm else clean


def _display(key: str) -> str:
    return f"@{key[3:]}" if key.startswith("dm-") else f"#{key}"


def slack_coverage(read: SlackAppRead, read_channels: set[str]) -> dict[str, Any]:
    """
    What Otto has and has not seen of Slack, from the sidebar Slack draws.

    ``read_channels`` are channel-cache keys read this refresh or remembered.
    A conversation Slack marks unread (or mention-badged) that is not the one
    on screen holds messages nobody — Otto included — has looked at yet;
    those are the ``unseen`` list, each with a link that opens it. Muted
    conversations are counted but never nagged about.
    """
    conversations = list(read.conversations or [])
    unseen: list[dict[str, Any]] = []
    unread_n = mention_n = 0
    for c in conversations:
        if c.get("self"):
            continue
        key = _sidebar_key(c.get("name", ""), bool(c.get("dm")) or str(c.get("section", "")).lower().startswith("direct"))
        badge = c.get("badge")
        mention = bool(badge)
        unread = bool(c.get("unread")) or mention
        if not unread or c.get("muted"):
            continue
        unread_n += 1
        mention_n += int(mention)
        if c.get("selected"):
            continue       # on screen right now: read above, not "unseen"
        unseen.append({
            "key": key,
            "title": _display(key),
            "section": str(c.get("section") or ""),
            "mention": mention,
            "badge": int(badge) if isinstance(badge, int) and not isinstance(badge, bool) else 0,
            "url": _build_slack_url(key),
        })
    unseen.sort(key=lambda u: (not u["mention"], -u["badge"], u["title"].lower()))
    listed = [c for c in conversations if not c.get("self")]
    return {
        "listed": len(listed),
        "read": sorted(_display(k) for k in read_channels),
        "unread": unread_n,
        "mentions": mention_n,
        "unseen": unseen,
        "windows": len(read.windows),
        "truncated": bool(read.truncated),
    }


def coverage_sentence(coverage: dict[str, Any] | None) -> str:
    """One quiet line for `otto status` and the page: "4 of 9 conversations read · 2 unread not opened"."""
    if not coverage:
        return ""
    listed = int(coverage.get("listed") or 0)
    read = len(coverage.get("read") or [])
    if not listed and not read:
        return ""
    parts = []
    if listed:
        parts.append(f"{min(read, listed) if read else 0} of {listed} conversation{'s' if listed != 1 else ''} read")
    else:
        parts.append(f"{read} conversation{'s' if read != 1 else ''} read")
    unseen = coverage.get("unseen") or []
    if unseen:
        mentions = sum(1 for u in unseen if u.get("mention"))
        bit = f"{len(unseen)} unread not opened"
        if mentions:
            bit += f" ({mentions} mention{'s' if mentions == 1 else ''} you)"
        parts.append(bit)
    return " · ".join(parts)


def _learn_self_name(lines: list[str]) -> None:
    """Slack renders the user's own DM entry as "<name> (you)" — remember that name (see utils.identity)."""
    from otto.utils.identity import remember_self_name

    for i, line in enumerate(lines):
        if line.strip().lower() in ("(you)", "you") and i > 0:
            prev = lines[i - 1].strip()
            if 2 < len(prev) < 40:
                remember_self_name(prev)


_CHANNEL_ID_RE = re.compile(r"^[CDG][A-Z0-9]{8,}$")


def _build_slack_url(channel: str, message_ts: str = "", info: dict[str, str] | None = None) -> str:
    """
    Deep link that opens the right channel/DM (or exact message) in Slack.

    * With a channel **ID** and a message timestamp → permalink
      ``https://<domain>.slack.com/archives/<ID>/p<ts>``.
    * With a channel ID only → ``slack://channel?team=<team>&id=<ID>`` (opens
      the desktop app directly) or the web archives URL.
    * With a channel **name** → ``https://<domain>.slack.com/app_redirect?channel=<name>``
      (Slack resolves names server-side).
    * Without workspace info → ``slack://open``.
    """
    info = info if info is not None else _load_workspace_info()
    domain = (info.get("domain") or "").strip()
    team_id = (info.get("team_id") or "").strip()

    raw = (channel or "").strip()
    name = raw.lstrip("#").strip()
    if name.lower().startswith("dm-"):
        name = "@" + name[3:]
    is_id = bool(_CHANNEL_ID_RE.match(name.upper())) and name.upper() == name

    if is_id:
        if message_ts:
            ts = message_ts.replace(".", "")
            if not ts.startswith("p"):
                ts = "p" + ts
            if domain:
                return f"https://{domain}.slack.com/archives/{name}/{ts}"
        if team_id:
            return f"slack://channel?team={team_id}&id={name}"
        if domain:
            return f"https://{domain}.slack.com/archives/{name}"
        return "slack://open"

    if not name or name.startswith("@"):
        # DMs cannot be addressed by user *name* in a URL; open the workspace.
        return f"slack://open?team={team_id}" if team_id else "slack://open"

    if domain:
        return f"https://{domain}.slack.com/app_redirect?channel={quote(name, safe='')}"
    if team_id:
        return f"slack://open?team={team_id}"
    return "slack://open"


_NOISE_PATTERNS = (
    'star channel', 'channel details', 'invite teammates',
    'start huddle', 'more actions', 'jump to first',
    'mark as read', 'search in channel', 'more channel actions',
    'add canvas', 'add and edit', 'loading messages',
    'history navigation', 'back in history', 'forward in history',
    'show history', 'chat with slackbot', 'help, with',
    'switch workspaces', 'manage my sidebar', 'new message',
    'get 50% off', 'invite people', 'connect apps',
    'sidebar width', 'primary view actions', 'channel or user name',
    'channels and direct messages', 'edit notifications', 'open in new window',
    'remove preview', 'huddles', 'directories', 'canvas', 'folder',
    'more huddles', 'add channels', 'direct messages', 'agents &',
    'characters remaining', 'loading ', 'more entry', 'more entries',
    'more repl', 'unread message', 'jump to bottom', 'scrolling',
    'thread replies', 'reply', 'viewing thread', 'close right sidebar',
    'send now', 'send later', 'schedule message', 'format message',
    'attach file', 'add reaction', 'bookmark this', 'pin message',
    'share message', 'copy link', 'message actions',
)

_BOT_MARKERS = re.compile(
    r'\b(?:bot|integration|notify|automation|webhook)\b|\bscan (?:of|complete)\b|\badded by\b|\bcompleted with\b',
    re.IGNORECASE,
)
_APP_BADGE = re.compile(r'\bAPP\b')      # Slack's badge is upper-case; "the app" in prose is not a bot


def looks_automated(*texts: str) -> bool:
    """Bot / integration message? Checks the badge and the usual bot vocabulary."""
    return any(t and (_BOT_MARKERS.search(t) or _APP_BADGE.search(t)) for t in texts)


class BrowserSlackAdapter:
    """
    Read-only Browser Slack adapter.

    Uses AppleScript to extract content from Slack tabs or the native app.

    THERE IS NO send(). NO modify(). NO delete(). NO chat_postMessage().
    """

    def __init__(self, reader: BrowserContentReader, workspace_id: str, **_ignored: Any) -> None:
        self._reader = reader
        self._workspace_id = workspace_id
        self._connected = False
        self._seen_ids: OrderedDict[str, None] = OrderedDict()
        self._max_seen_ids = 10_000
        self.timings: dict[str, float] = {}
        # What the last read covered (see slack_coverage): the collector puts
        # it on the source status, so the page, the panel and `otto status`
        # can say which conversations were read and which are waiting unread.
        self.coverage: dict[str, Any] = {}

    def _mark_seen(self, source_id: str) -> None:
        self._seen_ids[source_id] = None
        self._seen_ids.move_to_end(source_id)
        if len(self._seen_ids) > self._max_seen_ids:
            self._seen_ids.popitem(last=False)

    @property
    def name(self) -> str:
        return f"browser_slack:{self._workspace_id}"

    @property
    def source_type(self) -> SourceType:
        return SourceType.SLACK

    @property
    def mode(self) -> str:
        return "browser"

    @property
    def last_error(self) -> str:
        """Last reader error (e.g. ``not allowed assistive access``) — lets the
        collector tell "Slack is quiet" from "Slack could not be read"."""
        return getattr(self._reader, "last_error", "") or ""

    async def connect(self) -> ConnectionStatus:
        """Presence check only — is Slack open in a tab or as an app?

        The actual read happens in :meth:`poll` under the (longer) poll
        time-box; connect must stay cheap and must not raise permission
        prompts just to learn that Slack is closed.
        """
        try:
            tabs = await self._reader.list_all_tabs()
            slack_tabs = self._reader.filter_tabs_by_url(tabs, ["app.slack.com", ".slack.com/client"])
            native_running = await self._reader.app_is_running("Slack")
            if slack_tabs or native_running:
                self._connected = True
                return ConnectionStatus(state=ConnectionState.HEALTHY)
            self._connected = False
            return ConnectionStatus(state=ConnectionState.FAILED, error="Slack is not open")
        except Exception as e:
            logger.debug("Slack browser connect failed: %s", e)
            return ConnectionStatus(state=ConnectionState.FAILED, error=str(e))

    async def poll(self, since: datetime) -> list[RawEvent]:
        if not self._connected:
            return []

        try:
            # Where a slow Slack read spends its time (surfaced in the refresh log).
            self.timings = {}
            t0 = time.monotonic()
            tabs = await self._reader.list_all_tabs()
            self.timings["tabs"] = round(time.monotonic() - t0, 2)
            slack_tabs = self._reader.filter_tabs_by_url(tabs, ["app.slack.com", ".slack.com/client"])
            events: list[RawEvent] = []

            # The desktop app (Accessibility) and the browser tabs (osascript)
            # are independent processes: read them at the same time.
            async def _native() -> SlackAppRead | None:
                t = time.monotonic()
                try:
                    return await self._read_slack_app()
                except Exception as e:
                    logger.debug("Native Slack extraction error: %s", e)
                    return None
                finally:
                    self.timings["app"] = round(time.monotonic() - t, 2)

            async def _tabs() -> list:
                t = time.monotonic()
                out = []
                try:
                    for tab in slack_tabs or []:
                        out.append((tab, await self._reader.extract_tab_content(tab)))
                finally:
                    if slack_tabs:
                        self.timings["tab_content"] = round(time.monotonic() - t, 2)
                return out

            native_read, tab_contents = await asyncio.gather(_native(), _tabs())

            # 1. Native Slack desktop app (accessibility tree): every window,
            #    then what the sidebar says about the conversations not shown.
            if native_read is not None and native_read.windows:
                self._process_app_read(native_read, events)

            # 2. Open Slack browser tabs
            for tab, content in tab_contents:
                if not content or not content.text:
                    continue
                snippets = parse_slack_page_text(content.text)
                # The tab title ("security-alerts (Channel) - acme - Slack") is
                # the reliable source for which conversation this is.
                titled = self._channel_from_title(getattr(tab, "title", "") or "")
                if titled:
                    ch = titled[0]
                    display = f"@{ch[3:]}" if ch.startswith("dm-") else f"#{ch}"
                    for s in snippets:
                        s.channel = display
                if snippets:
                    self._process_snippets(snippets, tab.url, events)
                else:
                    tab_event = self._event_from_tab_title(tab)
                    if tab_event and tab_event.source_id not in self._seen_ids:
                        self._mark_seen(tab_event.source_id)
                        events.append(tab_event)

            return events
        except Exception as e:
            logger.debug("Slack browser poll failed: %s", e)
            return []

    async def _read_slack_app(self) -> SlackAppRead | None:
        """The structured read when the reader offers it; otherwise the text read wrapped as one window."""
        structured = getattr(self._reader, "extract_slack_app", None)
        if callable(structured):
            try:
                maybe = structured()
                read = await maybe if inspect.isawaitable(maybe) else None
            except Exception as e:
                logger.debug("structured Slack read failed: %s", e)
                read = None
            if isinstance(read, SlackAppRead):
                return read
        text = await self._reader.extract_slack_app_content() or ""
        if not text:
            return None
        title = text.replace("\r", "\n").split("\n", 1)[0].strip()
        return SlackAppRead(windows=[(title, text)])

    def _process_app_read(self, read: SlackAppRead, events: list[RawEvent]) -> None:
        """Every Slack window becomes messages; the sidebar becomes coverage."""
        read_now: list[str] = []
        main_channel = ""
        for index, (_title, text) in enumerate(read.windows):
            if not text or not text.strip():
                continue
            channel = self._process_native_content(text, events)
            if channel:
                read_now.append(channel)
                if index == 0:
                    main_channel = channel
        if main_channel:
            _note_visible(_display(main_channel))    # thumbnails follow the main window
        remembered = {c for c, (ts, _e) in _GLOBAL_SLACK_CHANNEL_CACHE.items() if time.time() - ts <= _CHANNEL_CACHE_TTL}
        self.coverage = slack_coverage(read, set(read_now) | remembered)
        if self.coverage.get("unseen"):
            logger.debug("Slack sidebar: %d unread conversation(s) not on screen", len(self.coverage["unseen"]))

    def _process_snippets(self, snippets: list[Any], url: str, events: list[RawEvent]) -> None:
        now = datetime.now(timezone.utc)
        ids: list[str] = []
        for snippet in snippets:
            channel = snippet.channel or "Unknown"
            title = channel if channel.startswith(('#', '@')) else f"#{channel}"
            ts = _stable_timestamp(title, snippet.sender, snippet.text, snippet.time_str, now)
            source_id = hashlib.sha256(
                f"{title}:{snippet.sender}:{_minute_key(ts) if snippet.time_str else ''}:{snippet.text[:80]}".encode("utf-8")
            ).hexdigest()
            ids.append(source_id)
            if source_id in self._seen_ids:
                continue
            self._mark_seen(source_id)

            is_bot = snippet.is_bot or looks_automated(snippet.sender or "", snippet.text)
            meta = {"channel_name": title.lstrip('#@'), "extraction_mode": "browser_tab"}
            if snippet.time_str:
                meta["time"] = snippet.time_str
            parent = ids[snippet.reply_to] if 0 <= getattr(snippet, "reply_to", -1) < len(ids) - 1 else None
            if parent:
                meta["reply_to"] = parent
            events.append(
                RawEvent(
                    source=SourceType.SLACK,
                    source_id=source_id,
                    source_url=url,
                    timestamp=ts,
                    title=title,
                    content_blocks=[ContentBlock(type=ContentType.TEXT, text=snippet.text)],
                    plain_text=snippet.text,
                    sender_name=snippet.sender,
                    thread_id=parent,
                    is_auto_generated=is_bot,
                    raw_metadata=meta,
                )
            )

    # -- native app ----------------------------------------------------------

    @staticmethod
    def _detect_channel(lines: list[str]) -> str:
        """Work out which channel/DM the Slack window is showing."""
        for line in lines:
            stripped = line.strip()
            wt_match = re.match(r'^([\w-]+)\s*\(Channel\)', stripped)
            if wt_match:
                return wt_match.group(1)
            cd_match = re.match(r'^Channel\s+([\w][\w-]+)$', stripped)
            if cd_match and cd_match.group(1) not in ('or', 'details', 'Tabs'):
                return cd_match.group(1)
            dm_match = re.match(r'^([\w.-]+)\s*\((?:Direct message|DM)\)', stripped, re.IGNORECASE)
            if dm_match:
                return f"dm-{dm_match.group(1)}"
            conv_match = re.search(r'conversation with @?([\w.-]+)', stripped, re.IGNORECASE)
            if conv_match:
                return f"dm-{conv_match.group(1)}"

        # Self-space / notes-to-self
        has_self_space = any("this is your space" in l.lower() for l in lines)
        has_you = any(l.strip().lower() in ("(you)", "you") for l in lines)
        if has_self_space or has_you:
            for i, l in enumerate(lines):
                if l.strip().lower() in ("(you)", "you") and i > 0:
                    prev = lines[i - 1].strip()
                    if 2 < len(prev) < 25 and prev.lower() not in ("messages", "dms", "home", "activity"):
                        return f"dm-{prev}"
            return "dm-self"
        return "general"

    @staticmethod
    def _split_messages(lines: list[str]) -> list[str]:
        """Split AX text lines into message chunks, dropping UI chrome."""
        chunks: list[str] = []
        current: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped or len(stripped) < 3:
                continue
            low = stripped.lower()
            if any(noise in low for noise in _NOISE_PATTERNS):
                continue
            # Sidebar entries: short, no punctuation
            if len(stripped) < 15 and not any(c in stripped for c in (':', '.', '/', '—')):
                continue

            is_boundary = bool(
                re.match(r'^\w+\s+\d+\w*\s+at\s+\d+:\d+', stripped)      # "Aug 21st at 6:55:49 AM"
                or re.match(r'^\d+:\d+\s*(AM|PM)', stripped)               # "12:33 PM"
                or re.match(r'^(?:Today|Yesterday)\s+at\s+\d+:\d+', stripped, re.IGNORECASE)
            )
            if is_boundary and current:
                text = '\n'.join(current)
                if len(text) > 20:
                    chunks.append(text)
                current = [stripped]
            else:
                current.append(stripped)
        if current:
            text = '\n'.join(current)
            if len(text) > 20:
                chunks.append(text)
        return chunks

    @staticmethod
    def _channel_from_title(line: str) -> tuple[str, str] | None:
        """
        Channel + workspace from a Slack.app window title.

        ``"* security-alerts (Channel) - demo - Slack"`` → ``("security-alerts", "demo")``;
        DMs (``"alice (DM) - demo - Slack"``) → ``("dm-alice", "demo")``. The
        leading ``*`` / ``•`` is Slack's unread marker.
        """
        title = re.sub(r'^[\*•●◦\s]+', '', line.strip())
        m = re.match(r'^(.+?)\s*\((Channel|DM|Direct message|Group)\)\s*-\s*(.+?)\s*-\s*Slack\s*$', title, re.IGNORECASE)
        if not m:
            return None
        name, kind, workspace = m.group(1).strip(), m.group(2).lower(), m.group(3).strip()
        if kind in ("dm", "direct message", "group"):
            return f"dm-{name}", workspace
        return name, workspace

    @staticmethod
    def _timestamp_from_time_str(time_str: str, now: datetime | None = None) -> datetime:
        """
        Turn a Slack header time (``"8:19 AM"``, ``"Aug 21st at 6:55:49 AM"``,
        ``"Yesterday at 9:00 AM"``) into an aware UTC datetime. Slack shows
        local time; a clock time later than now means yesterday. Anything
        unparseable is ``now``.
        """
        now = now or datetime.now(timezone.utc)
        local_now = now.astimezone()
        s = (time_str or "").strip()
        if not s:
            return now
        try:
            rel = re.match(
                r'^(?:(?P<now>just now|now)|(?P<n>a|an|\d{1,3})\s*(?P<unit>second|sec|minute|min|hour|hr|day)s?\s+ago)$',
                s, re.IGNORECASE,
            )
            if rel:
                # "4 minutes ago" is Slack's floor of the age; the minute is what
                # we can know, so drop the seconds rather than invent them.
                if rel.group("now"):
                    delta = timedelta(0)
                else:
                    n = 1 if rel.group("n").lower() in ("a", "an") else int(rel.group("n"))
                    unit = rel.group("unit").lower()
                    seconds = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400}[unit]
                    delta = timedelta(seconds=n * seconds)
                return (now - delta).replace(second=0, microsecond=0)
            t = re.search(r'(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?', s, re.IGNORECASE)
            if not t:
                return now
            hour, minute = int(t.group(1)), int(t.group(2))
            second = int(t.group(3) or 0)
            meridiem = (t.group(4) or "").upper()
            if meridiem == "PM" and hour < 12:
                hour += 12
            elif meridiem == "AM" and hour == 12:
                hour = 0
            if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
                return now
            day = local_now.date()
            md = re.match(r'^([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\s+at\b', s)
            if md:
                month_name = md.group(1)[:3].title()
                months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
                if month_name not in months:
                    return now
                year = int(md.group(3)) if md.group(3) else local_now.year
                day = day.replace(year=year, month=months.index(month_name) + 1, day=int(md.group(2)))
            elif s.lower().startswith("yesterday"):
                day = day - timedelta(days=1)
            candidate = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=local_now.tzinfo)
            if not md and candidate > local_now + timedelta(minutes=5):
                candidate -= timedelta(days=1)                       # 7:35 PM seen at 1 PM → yesterday
            if candidate > local_now + timedelta(days=1):
                candidate = candidate.replace(year=candidate.year - 1)  # "Dec 30 at …" seen in January
            return candidate.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            return now

    def _process_native_content(self, raw_text: str, events: list[RawEvent]) -> str:
        """Turn one Slack.app window's accessibility dump into message events.

        Returns the channel key the window showed (``ops`` / ``dm-alice``; ``""``
        when the dump was empty).
        """
        normalized = raw_text.replace('\r', '\n')
        lines = [l.strip() for l in normalized.split('\n') if l.strip()]
        if not lines:
            return ""

        # The window title is authoritative for "which conversation is this".
        workspace = ""
        titled = self._channel_from_title(lines[0])
        if titled:
            channel, workspace = titled
            body_lines = lines[1:]
        else:
            channel = self._detect_channel(lines)
            body_lines = [
                l for l in lines
                if not re.match(r'^Channel\s+[\w][\w-]+$', l) and '(Channel)' not in l
            ]
        is_dm = channel.startswith("dm-")
        display_title = f"@{channel[3:]}" if is_dm else f"#{channel}"
        you_present = any("(you)" in l.lower() for l in lines)
        _learn_self_name(lines)
        _note_visible(display_title)
        now = datetime.now(timezone.utc)

        # Header-based parse (sender / APP / time / body), shared with the web path.
        messages: list[tuple[str, str, str, bool, int]] = [
            (s.sender, s.time_str, s.text, s.is_bot, s.reply_to)
            for s in parse_slack_message_lines(body_lines, channel=display_title)
        ]
        if not messages:
            # Legacy layouts without recognisable headers: chunk on date lines.
            messages = [("", "", chunk, False, -1) for chunk in self._split_messages(body_lines)]

        new_channel_events: list[RawEvent] = []
        ids: list[str] = []
        for sender_name, time_str, text, badge_bot, reply_to in messages:
            # The id is built from the *resolved* minute, not the rendered time,
            # so "4:54 PM" and "Today at 4:54 PM" (channel view and thread
            # panel) are one message, and "4 minutes ago" does not become a new
            # message every refresh.
            ts = _stable_timestamp(channel, sender_name, text, time_str, now)
            source_id = hashlib.sha256(
                f"slack_native:{channel}:{sender_name}:{_minute_key(ts) if time_str else ''}:{text[:80]}".encode("utf-8")
            ).hexdigest()
            ids.append(source_id)
            if source_id in self._seen_ids:
                continue
            self._mark_seen(source_id)

            is_bot = badge_bot or looks_automated(sender_name or "", text)
            if not sender_name:
                if is_dm:
                    sender_name = "You" if ("self" in channel or you_present) else channel[3:]
                elif "github" in text.lower() and is_bot:
                    sender_name = "GitHub"

            meta = {"channel_name": channel, "extraction_mode": "native_app_ax_tree"}
            if workspace:
                meta["workspace"] = workspace
            if time_str:
                meta["time"] = time_str
            # A thread reply joins its parent's conversation (same grouping key
            # downstream), so the briefing reads the thread, not a lone reply.
            parent = ids[reply_to] if 0 <= reply_to < len(ids) - 1 else None
            if parent:
                meta["reply_to"] = parent

            new_channel_events.append(
                RawEvent(
                    source=SourceType.SLACK,
                    source_id=source_id,
                    source_url=_build_slack_url(channel),
                    timestamp=ts,
                    title=display_title,
                    content_blocks=[ContentBlock(type=ContentType.TEXT, text=text)],
                    plain_text=text,
                    sender_name=sender_name,
                    thread_id=parent,
                    is_auto_generated=is_bot,
                    raw_metadata=meta,
                )
            )

        if new_channel_events:
            _cache_channel(channel, new_channel_events)
        events.extend(new_channel_events)

        # Merge remembered events from other recently viewed channels.
        for cached_chan, (_ts, cached_evs) in list(_GLOBAL_SLACK_CHANNEL_CACHE.items()):
            if cached_chan == channel:
                continue
            for cev in cached_evs:
                if cev.source_id not in self._seen_ids:
                    self._mark_seen(cev.source_id)
                    events.append(cev)
        return channel

    # -- titles ------------------------------------------------------------

    @staticmethod
    def _parse_slack_title(title: str) -> tuple[str, str] | None:
        m = re.match(r'^(.+?)\s*\((?:Channel|DM|Group)\)\s*-\s*(.+?)\s*-\s*Slack', title)
        if not m:
            m = re.match(r'^(.+?)\s*-\s*(.+?)\s*-\s*Slack\s*$', title)
        if not m:
            return None
        return m.group(1).strip(), m.group(2).strip()

    def _event_from_tab_title(self, tab: Any) -> RawEvent | None:
        """Extract a RawEvent from a Slack tab's title (fallback when no messages parsed)."""
        parsed = self._parse_slack_title(getattr(tab, 'title', '') or "")
        if not parsed:
            return None
        channel_name, workspace = parsed
        plain_text = f"Slack channel #{channel_name} is open in workspace {workspace}"
        return RawEvent(
            source=SourceType.SLACK,
            source_id=hashlib.sha256(f"slack_tab:{workspace}:{channel_name}".encode("utf-8")).hexdigest(),
            source_url=getattr(tab, 'url', '') or "",
            timestamp=datetime.now(timezone.utc),
            title=f"#{channel_name}",
            content_blocks=[ContentBlock(type=ContentType.TEXT, text=plain_text)],
            plain_text=plain_text,
            sender_name="",
            is_auto_generated=False,
            raw_metadata={"channel_name": channel_name, "workspace": workspace, "extraction_mode": "tab_title"},
        )

    def _event_from_window_title(self, window_title: str) -> RawEvent | None:
        """Extract a RawEvent from a Slack.app window title."""
        parsed = self._parse_slack_title(window_title)
        if not parsed:
            return None
        channel_name, workspace = parsed
        plain_text = f"Slack.app: viewing #{channel_name} in workspace {workspace}"
        return RawEvent(
            source=SourceType.SLACK,
            source_id=hashlib.sha256(f"slack_native:{workspace}:{channel_name}".encode("utf-8")).hexdigest(),
            source_url=_build_slack_url(channel_name),
            timestamp=datetime.now(timezone.utc),
            title=f"#{channel_name}",
            content_blocks=[ContentBlock(type=ContentType.TEXT, text=plain_text)],
            plain_text=plain_text,
            sender_name="",
            is_auto_generated=False,
            raw_metadata={"channel_name": channel_name, "workspace": workspace, "extraction_mode": "native_app_title"},
        )

    async def fetch_thread(self, thread_id: str) -> list[RawEvent]:
        return []

    async def health_check(self) -> HealthStatus:
        try:
            if not self._connected:
                return HealthStatus.UNHEALTHY
            tabs = await self._reader.list_all_tabs()
            slack_tabs = self._reader.filter_tabs_by_url(tabs, ["app.slack.com", ".slack.com/client"])
            if slack_tabs or await self._reader.app_is_running("Slack"):
                return HealthStatus.HEALTHY
            return HealthStatus.DEGRADED
        except Exception:
            return HealthStatus.UNHEALTHY

    async def disconnect(self) -> None:
        self._connected = False
        self._reader.clear_cache()
