from __future__ import annotations
"""
Browser calendar adapter — read-only.

Fetches calendar events from Calendar.app and Google Calendar tabs.
No write methods. All operations are strictly read-only.
"""

import hashlib
import logging
from collections import OrderedDict
from datetime import datetime, timezone

from otto.adapters.base import ConnectionStatus, RawEvent
from otto.adapters.browser.reader import BrowserContentReader
from otto.storage.models import (
    ConnectionState,
    ContentBlock,
    ContentType,
    HealthStatus,
    SourceType,
)
from otto.utils.content_parser import parse_calendar_events_text

logger = logging.getLogger("otto.adapters.browser.calendar_browser")


class BrowserCalendarAdapter:
    """
    Read-only Browser Calendar adapter.
    
    Reads calendar events using BrowserContentReader (AppleScript).
    THERE IS NO write(). NO create(). NO update(). NO delete().
    All operations are strictly read-only.
    """

    def __init__(self, reader: BrowserContentReader, account_id: str) -> None:
        self._reader = reader
        self._account_id = account_id
        self._connected = False
        self._prefer_browser = True  # Prefer Google Calendar tab over Calendar.app
        self._seen_ids: OrderedDict[str, None] = OrderedDict()
        self._max_seen_ids = 10_000

    def _mark_seen(self, source_id: str) -> None:
        self._seen_ids[source_id] = None
        self._seen_ids.move_to_end(source_id)
        if len(self._seen_ids) > self._max_seen_ids:
            self._seen_ids.popitem(last=False)

    @property
    def name(self) -> str:
        return f"browser_calendar:{self._account_id}"

    @property
    def source_type(self) -> SourceType:
        return SourceType.CALENDAR

    @property
    def mode(self) -> str:
        return "browser"

    async def connect(self) -> ConnectionStatus:
        """Verify calendar availability — prefers Google Calendar in browser."""
        try:
            # Check Google Calendar tabs first (no extra macOS permissions needed)
            tabs = await self._reader.list_all_tabs()
            cal_tabs = self._reader.filter_tabs_by_url(tabs, ["calendar.google.com"])
            if cal_tabs:
                self._connected = True
                self._prefer_browser = True
                logger.info("Connected to Google Calendar via browser tab")
                return ConnectionStatus(state=ConnectionState.HEALTHY)

            # Fallback: check Calendar.app availability
            cal_text = await self._reader.extract_calendar_events()
            if cal_text:
                self._connected = True
                self._prefer_browser = False
                logger.info("Connected to Calendar.app (fallback)")
                return ConnectionStatus(state=ConnectionState.HEALTHY)

            return ConnectionStatus(
                state=ConnectionState.FAILED,
                error="No Google Calendar tab or Calendar.app found. Open calendar.google.com in Chrome."
            )
        except Exception as e:
            return ConnectionStatus(state=ConnectionState.FAILED, error=str(e))

    async def poll(self, since: datetime) -> list[RawEvent]:
        if not self._connected:
            return []
            
        try:
            text = ""

            # Strategy 1: Google Calendar browser tab (preferred — no extra permissions)
            if self._prefer_browser:
                tabs = await self._reader.list_all_tabs()
                cal_tabs = self._reader.filter_tabs_by_url(tabs, ["calendar.google.com"])
                if cal_tabs:
                    content = await self._reader.extract_tab_content(cal_tabs[0])
                    if content and content.text:
                        text = content.text

            # Strategy 2: Calendar.app fallback
            if not text:
                text = await self._reader.extract_calendar_events()

            # Strategy 3: Try browser tabs even if not _prefer_browser
            if not text:
                tabs = await self._reader.list_all_tabs()
                cal_tabs = self._reader.filter_tabs_by_url(tabs, ["calendar.google.com"])
                if cal_tabs:
                    content = await self._reader.extract_tab_content(cal_tabs[0])
                    if content:
                        text = content.text
            
            snippets = parse_calendar_events_text(text) if text else []
            
            events: list[RawEvent] = []
            
            for snippet in snippets:
                # generate source_id
                raw_id = f"{snippet.title}_{snippet.start_str}".encode("utf-8")
                source_id = hashlib.md5(raw_id).hexdigest()
                
                if source_id in self._seen_ids:
                    continue
                self._mark_seen(source_id)
                
                # timestamp
                try:
                    if "T" in snippet.start_str:
                        timestamp = datetime.fromisoformat(snippet.start_str.replace("Z", "+00:00"))
                    else:
                        timestamp = datetime.strptime(snippet.start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    timestamp = datetime.now(timezone.utc)
                
                if timestamp < since:
                    continue

                recipients = [{"email": attendee, "field": "attendee"} for attendee in getattr(snippet, 'attendees', [])]
                
                content_parts = [f"Event: {snippet.title}"]
                if getattr(snippet, 'location', ''):
                    content_parts.append(f"Location: {snippet.location}")
                if getattr(snippet, 'description', ''):
                    content_parts.append(f"Description: {snippet.description[:500]}")
                if getattr(snippet, 'attendees', []):
                    content_parts.append(f"Attendees: {', '.join(snippet.attendees[:10])}")
                    
                plain_text = "\n".join(content_parts)
                
                raw_metadata = {
                    "calendar_id": "browser_calendar",
                    "status": "confirmed",
                    "all_day": "T" not in snippet.start_str,
                    "attendee_count": len(getattr(snippet, 'attendees', [])),
                }

                event = RawEvent(
                    source=SourceType.CALENDAR,
                    source_id=source_id,
                    source_url="",
                    timestamp=timestamp,
                    title=snippet.title,
                    content_blocks=[ContentBlock(type=ContentType.TEXT, text=plain_text)],
                    plain_text=plain_text,
                    sender_name="",
                    sender_email="",
                    recipients=recipients,
                    thread_id=source_id,
                    is_auto_generated=False,
                    raw_metadata=raw_metadata
                )
                events.append(event)
                
            return events
            
        except Exception as e:
            logger.error("Browser Calendar poll failed: %s", e)
            return []

    async def get_upcoming(self, hours: int = 2) -> list[RawEvent]:
        """Get events in the next N hours (for meeting prep triggers)."""
        now = datetime.now(timezone.utc)
        return await self.poll(now)

    async def fetch_thread(self, thread_id: str) -> list[RawEvent]:
        """Fetch a single thread (returns empty list for browser calendar)."""
        return []

    async def health_check(self) -> HealthStatus:
        if not self._connected:
            return HealthStatus.UNHEALTHY
        try:
            status = await self.connect()
            return HealthStatus.HEALTHY if status.state == ConnectionState.HEALTHY else HealthStatus.DEGRADED
        except Exception:
            return HealthStatus.UNHEALTHY

    async def disconnect(self) -> None:
        self._connected = False
