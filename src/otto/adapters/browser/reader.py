from __future__ import annotations
"""
Browser content reader — macOS AppleScript interface.

Reads tab titles, URLs, and visible page content from Chrome/Safari
and native macOS apps using osascript. All operations are READ-ONLY.

THERE IS NO click(). NO keystroke(). NO set(). NO DOM mutation.
Only `get` and read-only JavaScript (`document.body.innerText`).
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from dataclasses import dataclass, field

from otto import paths
from otto.utils.content_parser import html_to_text

logger = logging.getLogger("otto.adapters.browser.reader")

# AppleScript timeout for each call (seconds)
OSASCRIPT_TIMEOUT = 45

# Calls slower than this are logged with their target app so a pending
# permission prompt or a slow UI-tree walk shows up in `otto logs`.
SLOW_OSASCRIPT_SECONDS = 5.0

# The native Accessibility reader (ax_dump.py) walks the UI tree for at most
# this long; AX_READER_TIMEOUT is the outer guard around the whole process
# (it also covers the one-time wait for Slack to publish its tree after Otto
# announces itself, up to ax_dump.DEFAULT_WARMUP_MS).
AX_READER_BUDGET_MS = 3000
AX_READER_TIMEOUT = 12.0
AX_DUMP_SCRIPT = Path(__file__).with_name("ax_dump.py")

# Last (chrome running, safari running, tab count) — the tab line is logged at
# INFO only when it changes, so `otto logs` is not one line per minute.
_LAST_TAB_SIGNATURE: dict[str, tuple] = {}


def ax_reader_command(app_name: str) -> list[str] | None:
    """argv that dumps *app_name*'s window text, or ``None`` where it cannot run.

    The reader is run under **this** interpreter (``sys.executable``): macOS
    attributes Apple platform binaries spawned by the engine to the engine's
    own code identity, so the single Accessibility grant the user makes for
    Otto covers it. (A compiled helper is its own identity and is denied
    under launchd even when Otto is allowed — see ax_dump.py.)

    ``OTTO_NATIVE_AX=0`` disables it (the AppleScript walk is used instead).
    """
    if os.environ.get("OTTO_NATIVE_AX", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    if sys.platform != "darwin" or not sys.executable or not AX_DUMP_SCRIPT.exists():
        return None
    return [sys.executable, str(AX_DUMP_SCRIPT), app_name, "--max-ms", str(AX_READER_BUDGET_MS)]

# Cache TTL for extracted content (seconds)
CONTENT_CACHE_TTL = 60

# One tab enumeration serves every adapter in a refresh (Slack, Gmail, Jira,
# Calendar all ask); Chrome answers Apple Events serially, so six concurrent
# walks of the tab list are what made connect() hit its 15 s time-box.
TAB_LIST_TTL = 20.0

# Maximum content length to extract from a single page
MAX_PAGE_CONTENT_LENGTH = 50_000


def _dump_extracts_enabled() -> bool:
    """``OTTO_DUMP_EXTRACTS=1`` or ``[debug] dump_extracts = true`` in config.toml."""
    env = os.environ.get("OTTO_DUMP_EXTRACTS", "").strip().lower()
    if env:
        return env in ("1", "true", "yes", "on")
    try:
        from otto.config import ConfigManager
        return bool(ConfigManager().get_or("debug.dump_extracts", False))
    except Exception:
        return False


@dataclass
class BrowserTab:
    """A single browser tab."""
    browser: str        # "chrome" or "safari"
    title: str
    url: str
    window_index: int = 1
    tab_index: int = 1


@dataclass
class ExtractedContent:
    """Content extracted from a browser tab or app."""
    source: str         # "chrome", "safari", "calendar.app", "slack.app"
    url: str
    title: str
    text: str
    extracted_at: float = field(default_factory=time.time)


@dataclass
class SlackAppRead:
    """One structured read of Slack.app (``ax_dump.py --json``).

    ``windows`` holds ``(title, text)`` per window, main window first — a
    popped-out thread or a second workspace window is read too. ``conversations``
    is the sidebar inventory: every conversation Slack lists, with the state
    Slack draws next to it (``unread``, ``badge`` — a mention count or True,
    ``selected``, ``muted``, ``self``) and its ``section`` ("Channels",
    "Direct messages", …). It is what Otto knows about conversations it cannot
    see without opening them.
    """
    windows: list[tuple[str, str]] = field(default_factory=list)
    conversations: list[dict] = field(default_factory=list)
    announced: bool = False
    truncated: bool = False
    extracted_at: float = field(default_factory=time.time)

    @property
    def text(self) -> str:
        """The main window's text (title first), as the text reader returns it."""
        return self.windows[0][1] if self.windows else ""

    @classmethod
    def from_payload(cls, payload: dict) -> "SlackAppRead":
        windows = [
            (str(w.get("title") or ""), str(w.get("text") or ""))
            for w in (payload.get("windows") or []) if isinstance(w, dict)
        ]
        conversations = []
        for c in payload.get("conversations") or []:
            if not isinstance(c, dict) or not str(c.get("name") or "").strip():
                continue
            badge = c.get("badge")
            conversations.append({
                "name": str(c.get("name")).strip(),
                "section": str(c.get("section") or ""),
                "dm": bool(c.get("dm")),
                "unread": bool(c.get("unread")),
                "badge": int(badge) if isinstance(badge, int) and not isinstance(badge, bool) else bool(badge),
                "selected": bool(c.get("selected")),
                "muted": bool(c.get("muted")),
                "self": bool(c.get("self")),
            })
        return cls(windows=windows, conversations=conversations,
                   announced=bool(payload.get("announced")), truncated=bool(payload.get("truncated")))


class BrowserContentReader:
    """
    Reads content from Chrome/Safari tabs and native macOS apps.

    Uses osascript (AppleScript) for all interactions. Every call is:
    - Read-only (only `get` and `execute javascript` with read-only DOM queries)
    - Timeout-bounded (10s max per call)
    - Error-isolated (a single tab failure never affects others)
    - Cached (60s TTL to avoid hammering the browser)

    THERE IS NO click(). NO keystroke(). NO set(). NO DOM mutation.
    """

    def __init__(self) -> None:
        self._cache: dict[str, ExtractedContent] = {}
        self._slack_reads: dict[str, SlackAppRead] = {}
        self._cache_ttl = CONTENT_CACHE_TTL
        # Last osascript stderr (e.g. "not allowed assistive access"), so the
        # collector can explain *why* a source is unreadable.
        self.last_error: str = ""
        self._dump_extracts = _dump_extracts_enabled()
        self._tabs_cache: tuple[float, list[BrowserTab]] | None = None
        self._tabs_lock: asyncio.Lock | None = None

    def _debug_dump(self, label: str, text: str) -> None:
        """
        Parser debugging aid, off by default: with ``[debug] dump_extracts =
        true`` in ``config.toml`` (or ``OTTO_DUMP_EXTRACTS=1``) every raw
        extraction is written to ``<data dir>/debug/`` (last 20 kept) so a
        mis-parsed Slack or Gmail page can be reproduced in a unit test.
        """
        if not self._dump_extracts or not text:
            return
        try:
            folder = paths.data_dir() / "debug"
            folder.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', label)[:40] or "extract"
            stamp = time.strftime("%Y%m%d-%H%M%S")
            (folder / f"{stamp}-{safe}.txt").write_text(text, encoding="utf-8")
            files = sorted(folder.glob("*.txt"), key=lambda p: p.stat().st_mtime)
            for old in files[:-20]:
                old.unlink(missing_ok=True)
        except OSError as e:
            logger.debug("extract dump failed: %s", e)

    async def is_browser_available(self) -> bool:
        """Check if Chrome or Safari is running."""
        # Try Chrome first (simpler, no System Events/Accessibility needed)
        try:
            result = await self._run_osascript(
                'tell application "Google Chrome" to return (count of windows)'
            )
            if result and result.strip().isdigit() and int(result.strip()) > 0:
                return True
        except Exception:
            pass
        # Try Safari
        try:
            result = await self._run_osascript(
                'tell application "Safari" to return (count of windows)'
            )
            if result and result.strip().isdigit() and int(result.strip()) > 0:
                return True
        except Exception:
            pass
        return False

    # Two Apple Events per *window* (all titles, all URLs) instead of two per
    # *tab*: browsers answer Apple Events serially at ~50-100 ms each, so a
    # 14-tab window used to cost ~2.5 s of every refresh; now ~0.2 s.
    _TAB_LIST_SCRIPT = '''
tell application "{app}"
    set tabList to {{}}
    repeat with w from 1 to (count of windows)
        set tTitles to {title_prop} of tabs of window w
        set tURLs to URL of tabs of window w
        repeat with t from 1 to (count of tTitles)
            set tabTitle to ""
            set tabURL to ""
            try
                set tabTitle to (item t of tTitles) as text
            end try
            try
                set tabURL to (item t of tURLs) as text
            end try
            set end of tabList to (w as text) & "||" & (t as text) & "||" & tabTitle & "||" & tabURL
        end repeat
    end repeat
    return tabList
end tell
'''

    async def list_chrome_tabs(self) -> list[BrowserTab]:
        """List all open Chrome tabs with their titles and URLs."""
        try:
            result = await self._run_osascript(self._TAB_LIST_SCRIPT.format(app="Google Chrome", title_prop="title"))
            return self._parse_tab_list(result, "chrome")
        except Exception as e:
            logger.debug("Failed to list Chrome tabs: %s", e)
            return []

    async def list_safari_tabs(self) -> list[BrowserTab]:
        """List all open Safari tabs with their titles and URLs."""
        try:
            result = await self._run_osascript(self._TAB_LIST_SCRIPT.format(app="Safari", title_prop="name"))
            return self._parse_tab_list(result, "safari")
        except Exception as e:
            logger.debug("Failed to list Safari tabs: %s", e)
            return []

    async def list_all_tabs(self) -> list[BrowserTab]:
        """
        List tabs from the browsers that are *running*.

        Scripting a browser that is closed would launch it (AppleScript
        starts its target), which a read-only assistant must never do, so
        each browser is checked with ``pgrep`` first. The result is shared
        for ``TAB_LIST_TTL`` seconds and concurrent callers wait for the one
        enumeration in flight instead of each starting their own.
        """
        if self._tabs_lock is None:
            self._tabs_lock = asyncio.Lock()
        async with self._tabs_lock:
            if self._tabs_cache and (time.monotonic() - self._tabs_cache[0]) < TAB_LIST_TTL:
                return list(self._tabs_cache[1])

            async def _none() -> list[BrowserTab]:
                return []

            started = time.monotonic()
            chrome_running, safari_running = await asyncio.gather(
                self.app_is_running("Google Chrome"), self.app_is_running("Safari"),
            )
            presence_s = time.monotonic() - started
            chrome_tabs, safari_tabs = await asyncio.gather(
                self.list_chrome_tabs() if chrome_running else _none(),
                self.list_safari_tabs() if safari_running else _none(),
                return_exceptions=True,
            )
            tabs: list[BrowserTab] = []
            if isinstance(chrome_tabs, list):
                tabs.extend(chrome_tabs)
            if isinstance(safari_tabs, list):
                tabs.extend(safari_tabs)
            took = time.monotonic() - started
            signature = (chrome_running, safari_running, len(tabs))
            # One line when something changed or the read was slow; otherwise
            # the per-minute bookkeeping stays out of the log.
            level = logging.INFO if signature != _LAST_TAB_SIGNATURE.get("v") or took >= 3.0 else logging.DEBUG
            _LAST_TAB_SIGNATURE["v"] = signature
            logger.log(
                level, "Browser tabs: chrome=%s safari=%s → %d tab(s) in %.1fs (presence %.2fs)",
                "running" if chrome_running else "closed", "running" if safari_running else "closed", len(tabs),
                took, presence_s,
            )
            self._tabs_cache = (time.monotonic(), list(tabs))
            return tabs

    async def extract_tab_content(self, tab: BrowserTab) -> ExtractedContent | None:
        """
        Extract visible text content from a browser tab.

        Uses JavaScript `document.body.innerText` — read-only, no DOM mutation.
        Results are cached for 60 seconds.
        """
        cache_key = self._cache_key(tab.url)
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached.extracted_at) < self._cache_ttl:
            return cached

        try:
            if tab.browser == "chrome":
                text = await self._extract_chrome_tab(tab)
            elif tab.browser == "safari":
                text = await self._extract_safari_tab(tab)
            else:
                return None

            if not text:
                return None

            content = ExtractedContent(
                source=tab.browser,
                url=tab.url,
                title=tab.title,
                text=text[:MAX_PAGE_CONTENT_LENGTH],
            )
            self._cache[cache_key] = content
            self._debug_dump(f"tab-{tab.browser}-{tab.title[:30]}", f"TITLE: {tab.title}\nURL: {tab.url}\n\n{content.text}")
            return content

        except Exception as e:
            logger.debug("Failed to extract content from %s: %s", tab.url, e)
            return None

    async def extract_calendar_events(self) -> str:
        """
        Read events from macOS Calendar.app via AppleScript.

        Returns a structured text representation of today's and upcoming events.
        READ-ONLY: only reads event properties, never creates/modifies events.
        """
        cache_key = "_calendar_app_"
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached.extracted_at) < self._cache_ttl:
            return cached.text

        # Only read Calendar.app if it is already running: `tell application
        # "Calendar"` would otherwise launch it, and Otto never opens apps.
        if not await self.app_is_running("Calendar"):
            self.last_error = "Calendar is not running"
            logger.debug("Calendar.app not running; skipping")
            return ""
        quick_check = await self._run_osascript(
            'tell application "Calendar" to return (count of calendars)'
        )
        if not quick_check or not quick_check.strip().isdigit():
            logger.debug("Calendar.app not accessible")
            return ""

        # Fetch events for today + tomorrow only (2 day window is fast)
        script = '''
tell application "Calendar"
    set today to current date
    set endDate to today + (2 * days)
    set eventList to {}
    set eventCount to 0
    repeat with cal in calendars
        if eventCount >= 20 then exit repeat
        try
            set calEvents to (every event of cal whose start date >= today and start date <= endDate)
            repeat with evt in calEvents
                if eventCount >= 20 then exit repeat
                set evtSummary to summary of evt
                set evtStart to start date of evt
                set evtEnd to end date of evt
                set evtLoc to ""
                try
                    set evtLoc to location of evt
                end try
                set evtDesc to ""
                try
                    set evtDesc to description of evt
                end try
                set evtAttendees to ""
                try
                    set evtAttendees to (name of every attendee of evt) as text
                end try
                set end of eventList to evtSummary & "|||" & (evtStart as text) & "|||" & (evtEnd as text) & "|||" & evtLoc & "|||" & evtDesc & "|||" & evtAttendees
                set eventCount to eventCount + 1
            end repeat
        end try
    end repeat
    return eventList
end tell
'''
        try:
            result = await self._run_osascript(script)
            if result:
                content = ExtractedContent(
                    source="calendar.app",
                    url="calendar://local",
                    title="Calendar Events",
                    text=result,
                )
                self._cache[cache_key] = content
            return result or ""
        except Exception as e:
            logger.debug("Failed to read Calendar.app: %s", e)
            return ""

    async def extract_slack_app(self) -> SlackAppRead | None:
        """
        Structured read of Slack.app: every window's text plus the sidebar
        inventory (see :class:`SlackAppRead`).

        Returns ``None`` when the native reader cannot run here — the caller
        falls back to :meth:`extract_slack_app_content` — and an empty read
        (with ``last_error`` set) when it ran and Slack was closed, denied or
        blank. Cached for the same TTL as the text read, and it seeds the text
        cache so nothing is read twice in one refresh.
        """
        cache_key = "_slack_app_json_"
        cached = self._slack_reads.get(cache_key)
        if cached and (time.time() - cached.extracted_at) < self._cache_ttl:
            return cached
        command = ax_reader_command("Slack")
        if not command:
            return None
        payload = await self._run_ax_reader_json(command + ["--json"])
        if payload is None:
            return None
        read = SlackAppRead.from_payload(payload) if payload else SlackAppRead()
        if read.windows:
            self._slack_reads[cache_key] = read
            self._cache["_slack_app_"] = ExtractedContent(
                source="slack.app", url="slack://native", title=read.windows[0][0] or "Slack", text=read.text,
            )
            self._debug_dump("slack-app", "\n\n".join(text for _t, text in read.windows))
            if read.announced:
                logger.info("Slack is now publishing its accessibility tree (announced Otto as an assistive client)")
        return read

    async def extract_slack_app_content(self) -> str:
        """
        Read visible content from Slack.app via AppleScript Accessibility.

        Slack is an Electron app with limited AX introspection, so we use
        multiple strategies to extract what we can.

        READ-ONLY: only reads window content, never sends messages.
        """
        cache_key = "_slack_app_"
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached.extracted_at) < self._cache_ttl:
            return cached.text

        result = ""

        # Strategy 0: native Accessibility reader (ax_dump.py). Milliseconds
        # instead of tens of seconds, bounded, and it needs no System Events
        # Automation grant. ``None`` means it cannot run here — fall through
        # to AppleScript; ``""`` is a definitive answer (denied, app closed,
        # nothing readable) and must not trigger the slow path.
        native = await self._run_ax_reader("Slack")
        if native is not None:
            if native:
                self._cache[cache_key] = ExtractedContent(
                    source="slack.app", url="slack://native", title="Slack", text=native,
                )
                self._debug_dump("slack-app", native)
            return native

        # Strategy 1: System Events window name (most reliable for Electron)
        # Returns something like "all-demo (Channel) - demo - Slack"
        name_script = '''
tell application "System Events"
    if exists process "Slack" then
        try
            return name of window 1 of process "Slack"
        on error
            return ""
        end try
    end if
    return ""
end tell
'''
        window_name = await self._run_osascript(name_script)
        if window_name and window_name.strip():
            result = window_name.strip()

        # Strategy 2: Try to get AX text values from Slack's UI tree
        text_script = '''
tell application "System Events"
    if exists process "Slack" then
        tell process "Slack"
            try
                set allElems to entire contents of window 1
                set textResult to ""
                set counter to 0
                repeat with elem in allElems
                    if counter > 400 then exit repeat
                    try
                        set eVal to value of elem
                        if eVal is not missing value and eVal is not "" and (length of (eVal as text)) > 2 then
                            set textResult to textResult & (eVal as text) & return
                        end if
                    end try
                    set counter to counter + 1
                end repeat
                return textResult
            on error
                return ""
            end try
        end tell
    end if
    return ""
end tell
'''
        text_content = await self._run_osascript(text_script)
        if text_content and len(text_content.strip()) > len(result):
            result = text_content.strip()

        if result:
            content = ExtractedContent(
                source="slack.app",
                url="slack://native",
                title="Slack",
                text=result,
            )
            self._cache[cache_key] = content

        return result

    def filter_tabs_by_url(self, tabs: list[BrowserTab], patterns: list[str]) -> list[BrowserTab]:
        """Filter tabs whose URLs contain any of the given patterns."""
        matched: list[BrowserTab] = []
        for tab in tabs:
            url_lower = tab.url.lower() if tab.url else ""
            for pattern in patterns:
                if pattern.lower() in url_lower:
                    matched.append(tab)
                    break
        return matched

    def clear_cache(self) -> None:
        """Clear the content cache."""
        self._cache.clear()
        self._slack_reads.clear()

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    async def _extract_chrome_tab(self, tab: BrowserTab) -> str:
        """
        Extract text from a Chrome tab using multiple strategies.

        Strategy 1: JavaScript execution (requires 'Allow JavaScript from Apple Events')
        Strategy 2: Chrome source/page text via AppleScript properties
        Strategy 3: Tab title + URL metadata (always works, limited data)
        """
        # Strategy 1: JavaScript (best quality, needs user opt-in)
        js_script = f'''
tell application "Google Chrome"
    tell tab {tab.tab_index} of window {tab.window_index}
        return execute javascript "document.body.innerText.substring(0, 50000)"
    end tell
end tell
'''
        result = await self._run_osascript(js_script)
        if result:
            return result

        # Strategy 2: Get page source and strip HTML (doesn't need JS enabled)
        source_script = f'''
tell application "Google Chrome"
    tell tab {tab.tab_index} of window {tab.window_index}
        return source
    end tell
end tell
'''
        source = await self._run_osascript(source_script)
        if source:
            text = html_to_text(source[:100000])
            if text and len(text) > 20:
                return text

        # Strategy 3: Tab title + URL (always available, limited data)
        logger.info(
            "Chrome JS and source extraction unavailable for tab %d. "
            "To enable full content extraction: Chrome → View → Developer → "
            "Allow JavaScript from Apple Events",
            tab.tab_index,
        )
        return self._tab_metadata_text(tab)

    async def _extract_safari_tab(self, tab: BrowserTab) -> str:
        """
        Extract text from a Safari tab using multiple strategies.

        Strategy 1: JavaScript execution
        Strategy 2: Safari page source property
        Strategy 3: Tab title + URL metadata
        """
        # Strategy 1: JavaScript
        js_script = f'''
tell application "Safari"
    tell tab {tab.tab_index} of window {tab.window_index}
        return do JavaScript "document.body.innerText.substring(0, 50000)"
    end tell
end tell
'''
        result = await self._run_osascript(js_script)
        if result:
            return result

        # Strategy 2: Safari page source
        source_script = f'''
tell application "Safari"
    return source of tab {tab.tab_index} of window {tab.window_index}
end tell
'''
        source = await self._run_osascript(source_script)
        if source:
            text = html_to_text(source[:100000])
            if text and len(text) > 20:
                return text

        # Strategy 3: Tab metadata fallback
        return self._tab_metadata_text(tab)

    @staticmethod
    def _tab_metadata_text(tab: BrowserTab) -> str:
        """Build minimal content from tab title and URL when full extraction fails."""
        parts = [tab.title]
        if tab.url:
            parts.append(f"URL: {tab.url}")
        return "\n".join(parts)

    async def _run_osascript(self, script: str) -> str:
        """
        Execute an AppleScript via osascript subprocess.

        Bounded by OSASCRIPT_TIMEOUT seconds. Returns stdout or empty string.
        A timeout *or* an outer cancellation (the adapter time-box in the
        collector) kills the child so no osascript is left waiting on a
        permission dialog after the refresh has moved on.
        """
        proc = None
        target = self._script_target(script)
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=OSASCRIPT_TIMEOUT
            )
            elapsed = time.monotonic() - started
            if elapsed >= SLOW_OSASCRIPT_SECONDS:
                logger.warning("osascript → %s took %.1fs", target, elapsed)
            if proc.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
                if "not allowed assistive access" in err_msg.lower():
                    logger.warning("Accessibility permission required. Enable in System Settings → Privacy → Accessibility.")
                elif err_msg:
                    logger.debug("osascript error: %s", err_msg)
                self.last_error = err_msg
                return ""
            self.last_error = ""
            return stdout.decode("utf-8", errors="replace").strip() if stdout else ""
        except asyncio.TimeoutError:
            logger.warning("osascript → %s timed out after %ds", target, OSASCRIPT_TIMEOUT)
            self.last_error = "timed out"
            await self._reap(proc)
            return ""
        except asyncio.CancelledError:
            logger.warning("osascript → %s cancelled after %.1fs (adapter time-box)", target, time.monotonic() - started)
            self.last_error = "timed out"
            await self._reap(proc)
            raise
        except FileNotFoundError:
            logger.error("osascript not found — not running on macOS?")
            return ""
        except Exception as e:
            logger.debug("osascript execution failed: %s", e)
            return ""

    async def app_is_running(self, app_name: str) -> bool:
        """Cheap presence check (``pgrep -x``) — no permissions, no prompts."""
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep", "-x", app_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            return proc.returncode == 0
        except asyncio.CancelledError:
            await self._reap(proc)
            raise
        except Exception:
            await self._reap(proc)
            return False

    async def _run_ax_reader(self, app_name: str) -> str | None:
        """
        Dump *app_name*'s window text with the native Accessibility reader.

        Returns the text (``""`` when denied / closed / empty — with
        ``last_error`` set) or ``None`` when the reader cannot run here so
        the caller can fall back to AppleScript.
        """
        command = ax_reader_command(app_name)
        if not command:
            return None
        return await self._spawn_ax_reader(command, app_name)

    async def _run_ax_reader_json(self, command: list[str]) -> dict | None:
        """``ax_dump.py … --json`` → the payload dict; ``{}`` when Slack was closed,
        denied or blank (``last_error`` set); ``None`` when the reader cannot run."""
        out = await self._spawn_ax_reader(command, command[2] if len(command) > 2 else "Slack")
        if out is None:
            return None
        if not out:
            return {}
        try:
            payload = json.loads(out)
        except ValueError:
            self.last_error = "reader returned unreadable output"
            return {}
        return payload if isinstance(payload, dict) else {}

    async def _spawn_ax_reader(self, command: list[str], app_name: str) -> str | None:
        """Run one reader process under the engine's interpreter; see :meth:`_run_ax_reader` for the contract."""
        proc = None
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=AX_READER_TIMEOUT)
            elapsed = time.monotonic() - started
            if elapsed >= SLOW_OSASCRIPT_SECONDS:
                logger.warning("Accessibility read of %s took %.1fs", app_name, elapsed)
            err = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
            if proc.returncode == 0:
                self.last_error = ""
                return stdout.decode("utf-8", errors="replace").strip() if stdout else ""
            if proc.returncode == 2:
                self.last_error = "not allowed assistive access"
                logger.warning(
                    "Accessibility permission required to read %s — run `otto permissions` "
                    "(System Settings → Privacy & Security → Accessibility).", app_name,
                )
                return ""
            self.last_error = err or f"{app_name} is not running"
            return ""
        except asyncio.TimeoutError:
            logger.warning("Accessibility read of %s timed out after %.0fs", app_name, AX_READER_TIMEOUT)
            self.last_error = "timed out"
            await self._reap(proc)
            return ""
        except asyncio.CancelledError:
            await self._reap(proc)
            raise
        except Exception as e:
            logger.debug("Accessibility reader unavailable (%s); falling back to AppleScript", e)
            await self._reap(proc)
            return None

    @staticmethod
    def _script_target(script: str) -> str:
        """Best-effort label for logs: the first ``tell application "X"`` in a script."""
        m = re.search(r'tell application "([^"]+)"', script)
        return m.group(1) if m else "osascript"

    @staticmethod
    async def _reap(proc: Any) -> None:
        """Kill a child and collect it (best effort, never raises)."""
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except Exception:
            pass

    def _parse_tab_list(self, raw: str, browser: str) -> list[BrowserTab]:
        """Parse the AppleScript tab list output into BrowserTab objects."""
        if not raw:
            return []

        tabs: list[BrowserTab] = []
        # AppleScript returns comma-separated list items
        for item in raw.split(", "):
            item = item.strip()
            if not item:
                continue
            parts = item.split("||")
            if len(parts) >= 4:
                try:
                    window_idx = int(parts[0])
                    tab_idx = int(parts[1])
                    title = parts[2]
                    url = parts[3]
                    tabs.append(BrowserTab(
                        browser=browser,
                        title=title,
                        url=url,
                        window_index=window_idx,
                        tab_index=tab_idx,
                    ))
                except (ValueError, IndexError):
                    continue
        return tabs

    @staticmethod
    def _cache_key(url: str) -> str:
        """Generate a cache key from a URL."""
        return hashlib.md5(url.encode()).hexdigest()
