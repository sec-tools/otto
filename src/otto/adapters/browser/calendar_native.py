from __future__ import annotations

"""
Native Calendar adapter — read-only.

Uses EventKit via AppleScriptObjC to extract today's calendar events.
Handles recurring events correctly and runs without launching Calendar.app.

THERE IS NO create(). NO modify(). NO delete(). NO accept(). NO decline().
"""

import asyncio
import hashlib
import logging
import shutil
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from otto.adapters.base import ConnectionStatus, RawEvent
from otto.storage.models import (
    ConnectionState,
    ContentBlock,
    ContentType,
    HealthStatus,
    SourceType,
)

logger = logging.getLogger("otto.adapters.browser.calendar_native")

# Timeout for osascript calls
_CALENDAR_TIMEOUT = 20
# Today's events do not change by the minute; one EventKit read serves a few
# refreshes. Module-level because adapters are recreated every refresh.
_CACHE_TTL_S = 180.0
_CACHE: dict[str, Any] = {"at": 0.0, "raw": None}


def reset_cache() -> None:
    _CACHE.update(at=0.0, raw=None)

# AppleScript that uses EventKit to get today's events (handles recurrences)
_EVENTKIT_SCRIPT = '''
use AppleScript version "2.4"
use framework "Foundation"
use framework "EventKit"
use scripting additions

property currentApp : a reference to current application

set eventStore to currentApp's EKEventStore's alloc()'s init()
eventStore's requestFullAccessToEventsWithCompletion:(missing value)

set cal to currentApp's NSCalendar's currentCalendar()
set startDate to cal's startOfDayForDate:(currentApp's NSDate's date())
set endDate to startDate's dateByAddingTimeInterval:(24 * 3600)

set allCalendars to eventStore's calendarsForEntityType:(currentApp's EKEntityTypeEvent)
set predicate to eventStore's predicateForEventsWithStartDate:startDate endDate:endDate calendars:allCalendars

set foundEvents to eventStore's eventsMatchingPredicate:predicate

set output to {}
repeat with anEvent in foundEvents
    set evTitle to (anEvent's title()) as string
    set evStart to ((anEvent's startDate())'s description()) as string
    set evEnd to ((anEvent's endDate())'s description()) as string
    set evLoc to (anEvent's location())
    if evLoc is missing value then set evLoc to ""
    set evNotes to (anEvent's notes())
    if evNotes is missing value then set evNotes to ""

    set attNames to {}
    try
        set attCount to (anEvent's attendees()'s |count|()) as integer
        repeat with i from 0 to (attCount - 1)
            try
                set att to (anEvent's attendees()'s objectAtIndex:i)
                set attName to (att's name()) as string
                if attName is not "" then set end of attNames to attName
            end try
        end repeat
    end try

    set AppleScript's text item delimiters to ", "
    set attString to attNames as text
    set AppleScript's text item delimiters to ""

    set calTitle to ((anEvent's calendar())'s title()) as string
    set eventLine to calTitle & " ||| " & evTitle & " ||| " & evStart & " ||| " & evEnd & " ||| " & (evLoc as string) & " ||| " & (evNotes as string) & " ||| " & attString
    set end of output to eventLine
end repeat

set AppleScript's text item delimiters to linefeed
return output as text
'''

# Simpler fallback that uses Calendar.app directly (misses recurring events)
_SIMPLE_SCRIPT = '''
set todayStart to (current date)
set time of todayStart to 0
set todayEnd to todayStart + (24 * 3600)

set output to {}
tell application "Calendar"
    repeat with aCal in (get every calendar)
        set calName to name of aCal
        try
            set todayEvents to (every event of aCal whose (start date >= todayStart and start date < todayEnd))
            repeat with anEvent in todayEvents
                set evTitle to summary of anEvent
                set evStart to (start date of anEvent) as string
                set evEnd to (end date of anEvent) as string
                set evLoc to location of anEvent
                if evLoc is missing value then set evLoc to ""
                set evNotes to description of anEvent
                if evNotes is missing value then set evNotes to ""
                set eventLine to calName & " ||| " & evTitle & " ||| " & evStart & " ||| " & evEnd & " ||| " & evLoc & " ||| " & evNotes & " ||| "
                set end of output to eventLine
            end repeat
        end try
    end repeat
end tell

set AppleScript's text item delimiters to linefeed
return output as text
'''


def _parse_event_line(line: str) -> dict[str, str] | None:
    """Parse a pipe-delimited event line into a dict."""
    parts = line.split(" ||| ")
    if len(parts) < 4:
        return None
    return {
        "calendar": parts[0].strip(),
        "title": parts[1].strip(),
        "start": parts[2].strip(),
        "end": parts[3].strip(),
        "location": parts[4].strip() if len(parts) > 4 else "",
        "notes": parts[5].strip() if len(parts) > 5 else "",
        "attendees": parts[6].strip() if len(parts) > 6 else "",
    }


def _format_time_range(start_str: str, end_str: str) -> str:
    """Format event time range for display."""
    # Try to parse and format nicely
    import re
    # EventKit format: "2026-09-08 14:30:00 +0000"
    # Calendar format: "Tuesday, September 8, 2026 at 2:30:00 PM"
    for fmt in ["%Y-%m-%d %H:%M:%S %z", "%A, %B %d, %Y at %I:%M:%S %p"]:
        try:
            start = datetime.strptime(start_str.strip(), fmt)
            end = datetime.strptime(end_str.strip(), fmt)
            return f"{start.strftime('%I:%M %p')} – {end.strftime('%I:%M %p')}"
        except (ValueError, IndexError):
            continue
    # Fallback: try regex for time
    start_match = re.search(r'(\d{1,2}:\d{2}(?:\s*[AP]M)?)', start_str)
    end_match = re.search(r'(\d{1,2}:\d{2}(?:\s*[AP]M)?)', end_str)
    if start_match and end_match:
        return f"{start_match.group(1)} – {end_match.group(1)}"
    return start_str[:20]


class NativeCalendarAdapter:
    """
    Read-only native Calendar adapter.

    Uses EventKit via AppleScriptObjC to extract today's events.
    Falls back to Calendar.app AppleScript if EventKit fails.

    THERE IS NO create(). NO modify(). NO delete(). NO accept(). NO decline().
    """

    def __init__(self) -> None:
        self._connected = False
        self._seen_ids: OrderedDict[str, None] = OrderedDict()
        self._max_seen_ids = 500
        self.last_error = ""
        self.timings: dict[str, float] = {}

    def _mark_seen(self, source_id: str) -> None:
        self._seen_ids[source_id] = None
        if len(self._seen_ids) > self._max_seen_ids:
            self._seen_ids.popitem(last=False)

    @property
    def name(self) -> str:
        return "native_calendar"

    @property
    def source_type(self) -> SourceType:
        return SourceType.CALENDAR

    @property
    def mode(self) -> str:
        return "native"

    async def connect(self) -> ConnectionStatus:
        """EventKit needs no app to be open — only osascript. No Apple Event is
        sent here: connect must stay cheap and prompt for nothing."""
        if shutil.which("osascript"):
            self._connected = True
            return ConnectionStatus(state=ConnectionState.HEALTHY)
        self._connected = False
        return ConnectionStatus(state=ConnectionState.FAILED, error="osascript not available")

    async def _osascript(self, script: str, label: str) -> tuple[int, str, str]:
        """Run one script asynchronously (never blocks the event loop) and
        time-boxed. Returns ``(returncode, stdout, stderr)``; ``-1`` on timeout."""
        proc = None
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=_CALENDAR_TIMEOUT)
            return proc.returncode or 0, out.decode("utf-8", "replace").strip(), err.decode("utf-8", "replace").strip()
        except asyncio.TimeoutError:
            logger.warning("Calendar (%s) timed out after %ds", label, _CALENDAR_TIMEOUT)
            await _reap(proc)
            return -1, "", "timed out"
        except asyncio.CancelledError:
            await _reap(proc)
            raise
        except Exception as e:
            await _reap(proc)
            return -1, "", str(e)
        finally:
            self.timings[label] = round(time.monotonic() - started, 2)

    @staticmethod
    async def _calendar_app_running() -> bool:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep", "-x", "Calendar", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            return proc.returncode == 0
        except Exception:
            await _reap(proc)
            return False

    async def _read_raw(self) -> str:
        """Today's events as EventKit lines; cached for a few minutes."""
        if _CACHE["raw"] is not None and time.monotonic() - _CACHE["at"] < _CACHE_TTL_S:
            self.timings["cached"] = 0.0
            return _CACHE["raw"]

        raw_output = ""
        rc, out, err = await self._osascript(_EVENTKIT_SCRIPT, "eventkit")
        if rc == 0:
            raw_output = out
            self.last_error = ""
        else:
            low = err.lower()
            if "not authorized" in low or ("access" in low and "denied" in low) or "1743" in low:
                self.last_error = "needs Calendar access — allow the macOS prompt for the engine's interpreter"
            else:
                self.last_error = err[:120] or "EventKit read failed"
            logger.debug("EventKit failed (rc=%s): %s", rc, err[:200])

        # Fallback: Calendar.app — but never launch it. `tell application
        # "Calendar"` starts the app when it is closed, which Otto must not do.
        if rc != 0 and await self._calendar_app_running():
            rc2, out2, err2 = await self._osascript(_SIMPLE_SCRIPT, "calendar_app")
            if rc2 == 0:
                raw_output = out2
                self.last_error = ""
            else:
                logger.debug("Calendar.app fallback failed: %s", err2[:200])

        if rc == 0 or raw_output:
            _CACHE.update(at=time.monotonic(), raw=raw_output)
        return raw_output

    async def poll(self, since: datetime) -> list[RawEvent]:
        """Get today's calendar events."""
        if not self._connected:
            return []
        self.timings = {}
        events: list[RawEvent] = []
        raw_output = await self._read_raw()
        if not raw_output:
            return []

        # Parse events
        now = datetime.now(timezone.utc)
        for line in raw_output.split("\n"):
            line = line.strip()
            if not line:
                continue

            parsed = _parse_event_line(line)
            if not parsed or not parsed["title"]:
                continue

            # Skip all-day system events and birthdays
            cal_name = parsed["calendar"].lower()
            if any(skip in cal_name for skip in ["birthday", "holiday", "found in", "siri"]):
                continue

            source_id = hashlib.sha256(
                f"calendar:{parsed['title']}:{parsed['start']}".encode()
            ).hexdigest()

            if source_id in self._seen_ids:
                continue
            self._mark_seen(source_id)

            # Build display text
            time_range = _format_time_range(parsed["start"], parsed["end"])
            parts = [f"📅 {parsed['title']} — {time_range}"]
            if parsed["location"]:
                parts.append(f"📍 {parsed['location']}")
            if parsed["attendees"]:
                parts.append(f"👥 {parsed['attendees']}")
            plain_text = "\n".join(parts)

            events.append(
                RawEvent(
                    source=SourceType.CALENDAR,
                    source_id=source_id,
                    source_url="ical://",
                    timestamp=now,
                    title=parsed["title"],
                    content_blocks=[ContentBlock(type=ContentType.TEXT, text=plain_text)],
                    plain_text=plain_text,
                    sender_name=parsed["calendar"],
                    is_auto_generated=False,
                    raw_metadata={
                        "calendar_name": parsed["calendar"],
                        "start_time": parsed["start"],
                        "end_time": parsed["end"],
                        "location": parsed["location"],
                        "attendees": parsed["attendees"],
                        "notes": parsed["notes"],
                    },
                )
            )

        logger.info("Calendar: found %d events today", len(events))
        return events

    async def fetch_thread(self, thread_id: str) -> list[RawEvent]:
        return []

    async def health_check(self) -> HealthStatus:
        status = await self.connect()
        return HealthStatus.HEALTHY if status.state == ConnectionState.HEALTHY else HealthStatus.UNHEALTHY

    async def disconnect(self) -> None:
        self._connected = False


async def _reap(proc: Any) -> None:
    """Kill a child and collect it (best effort, never raises)."""
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    except Exception:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except Exception:
        pass
