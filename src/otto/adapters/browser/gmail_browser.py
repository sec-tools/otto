from __future__ import annotations

"""
Browser Gmail adapter — read-only.

Extracts email data from active Gmail tabs in the browser.
No write methods. Maintains the read-only safety invariant.
"""

import hashlib
import logging
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any

from otto.adapters.base import ConnectionStatus, RawEvent
from otto.adapters.browser.reader import BrowserContentReader, BrowserTab
from otto.storage.models import (
    ConnectionState,
    ContentBlock,
    ContentType,
    HealthStatus,
    SourceType,
)
try:
    from otto.utils.content_parser import parse_gmail_page_text
except ImportError:
    # Fallback if not defined
    def parse_gmail_page_text(text: str) -> list[Any]:
        return []

logger = logging.getLogger("otto.adapters.browser.gmail_browser")


class BrowserGmailAdapter:
    """
    Read-only Browser Gmail adapter.

    Uses AppleScript to extract content from Gmail tabs.
    
    THERE IS NO send(). NO draft(). NO modify(). NO delete(). NO trash().
    """

    def __init__(self, reader: BrowserContentReader, account_id: str) -> None:
        self._reader = reader
        self._account_id = account_id
        self._connected = False
        self._seen_ids: OrderedDict[str, None] = OrderedDict()
        self._max_seen_ids = 10_000

    def _mark_seen(self, source_id: str) -> None:
        self._seen_ids[source_id] = None
        self._seen_ids.move_to_end(source_id)
        if len(self._seen_ids) > self._max_seen_ids:
            self._seen_ids.popitem(last=False)

    @property
    def name(self) -> str:
        return f"browser_gmail:{self._account_id}"

    @property
    def source_type(self) -> SourceType:
        return SourceType.EMAIL

    @property
    def mode(self) -> str:
        return "browser"

    async def connect(self) -> ConnectionStatus:
        try:
            tabs = await self._reader.list_all_tabs()
            gmail_tabs = self._reader.filter_tabs_by_url(tabs, ["mail.google.com"])
            if gmail_tabs:
                self._connected = True
                return ConnectionStatus(state=ConnectionState.HEALTHY)
            return ConnectionStatus(
                state=ConnectionState.FAILED,
                error="No Gmail tabs found",
            )
        except Exception as e:
            logger.debug("Gmail browser connect failed: %s", e)
            return ConnectionStatus(
                state=ConnectionState.FAILED,
                error=str(e),
            )

    async def poll(self, since: datetime) -> list[RawEvent]:
        if not self._connected:
            return []

        try:
            tabs = await self._reader.list_all_tabs()
            gmail_tabs = self._reader.filter_tabs_by_url(tabs, ["mail.google.com"])
            events: list[RawEvent] = []

            for tab in gmail_tabs:
                content = await self._reader.extract_tab_content(tab)
                if not content or not content.text:
                    continue

                # Strategy 1: Parse full page content for individual emails
                snippets = parse_gmail_page_text(content.text)
                for snippet in snippets:
                    source_id = hashlib.sha256(
                        f"{snippet.subject}{snippet.sender}{snippet.snippet[:100]}".encode("utf-8")
                    ).hexdigest()

                    if source_id in self._seen_ids:
                        continue
                    self._mark_seen(source_id)

                    timestamp = self._parse_time_str(snippet.time_str)
                    thread_id = hashlib.sha256(snippet.subject.encode("utf-8")).hexdigest()

                    events.append(
                        RawEvent(
                            source=SourceType.EMAIL,
                            source_id=source_id,
                            source_url=tab.url,
                            timestamp=timestamp,
                            title=snippet.subject,
                            content_blocks=[ContentBlock(type=ContentType.TEXT, text=snippet.snippet)],
                            plain_text=snippet.snippet,
                            sender_name=snippet.sender,
                            sender_email=snippet.sender,
                            thread_id=thread_id,
                            is_auto_generated=False,
                        )
                    )

                # Strategy 2: Extract metadata from tab title when full parsing returns empty
                # Tab titles like "Inbox (112) - you@example.com - Gmail" contain:
                #   - Unread count (112)
                #   - Account email (you@example.com)
                #   - Current view (Inbox, Sent, etc.)
                if not snippets and tab.title:
                    tab_event = self._event_from_tab_title(tab)
                    if tab_event:
                        source_id = tab_event.source_id
                        if source_id not in self._seen_ids:
                            self._mark_seen(source_id)
                            events.append(tab_event)

            return events
        except Exception as e:
            logger.debug("Gmail browser poll failed: %s", e)
            return []

    def _event_from_tab_title(self, tab: BrowserTab) -> RawEvent | None:
        """Extract a summary RawEvent from a Gmail tab's title."""
        import re

        title = tab.title or ""
        # Parse "Inbox (112) - you@example.com - Gmail"
        # or "(3) you@example.com - Gmail"
        unread_match = re.search(r'\((\d+)\)', title)
        email_match = re.search(r'[\w.+-]+@[\w.-]+\.\w+', title)
        view_match = re.search(r'^(\w+)', title)

        unread_count = int(unread_match.group(1)) if unread_match else 0
        account_email = email_match.group(0) if email_match else ""
        view_name = view_match.group(1) if view_match else "Inbox"

        if not account_email and not unread_count:
            return None

        plain_text = f"Gmail {view_name}: {unread_count} unread messages"
        if account_email:
            plain_text += f" for {account_email}"

        source_id = hashlib.sha256(
            f"gmail_tab:{account_email}:{view_name}:{unread_count}".encode("utf-8")
        ).hexdigest()

        return RawEvent(
            source=SourceType.EMAIL,
            source_id=source_id,
            source_url=tab.url,
            timestamp=datetime.now(timezone.utc),
            title=f"Gmail {view_name} ({unread_count} unread)",
            content_blocks=[ContentBlock(type=ContentType.TEXT, text=plain_text)],
            plain_text=plain_text,
            sender_name="",
            sender_email=account_email,
            thread_id=f"gmail_inbox:{account_email}",
            is_auto_generated=False,
            raw_metadata={
                "unread_count": unread_count,
                "account": account_email,
                "view": view_name,
                "extraction_mode": "tab_title",
            },
        )

    async def fetch_thread(self, thread_id: str) -> list[RawEvent]:
        return []

    async def health_check(self) -> HealthStatus:
        try:
            if not self._connected:
                return HealthStatus.UNHEALTHY
            tabs = await self._reader.list_all_tabs()
            gmail_tabs = self._reader.filter_tabs_by_url(tabs, ["mail.google.com"])
            if gmail_tabs:
                return HealthStatus.HEALTHY
            return HealthStatus.DEGRADED
        except Exception:
            return HealthStatus.UNHEALTHY

    async def disconnect(self) -> None:
        self._connected = False
        self._reader.clear_cache()

    @staticmethod
    def _parse_time_str(time_str: str) -> datetime:
        """Heuristic parser for Gmail time strings."""
        now = datetime.now(timezone.utc)
        if not time_str:
            return now
        ts = time_str.lower().strip()

        try:
            if "ago" in ts:
                parts = ts.split()
                if len(parts) >= 2:
                    try:
                        val = int(parts[0])
                    except ValueError:
                        return now
                    unit = parts[1]
                    if "min" in unit:
                        return now - timedelta(minutes=val)
                    elif "hour" in unit:
                        return now - timedelta(hours=val)
                    elif "day" in unit:
                        return now - timedelta(days=val)
            elif "yesterday" in ts:
                return now - timedelta(days=1)
            elif ":" in ts and ("am" in ts or "pm" in ts):
                dt = datetime.strptime(ts, "%I:%M %p").replace(
                    year=now.year, month=now.month, day=now.day, tzinfo=timezone.utc
                )
                if dt > now:
                    dt -= timedelta(days=1)
                return dt
            else:
                dt = datetime.strptime(ts, "%b %d").replace(
                    year=now.year, tzinfo=timezone.utc
                )
                if dt > now:
                    dt = dt.replace(year=now.year - 1)
                return dt
        except Exception:
            pass
        return now
