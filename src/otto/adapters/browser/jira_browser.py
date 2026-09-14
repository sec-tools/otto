from __future__ import annotations
"""
Browser Jira adapter — read-only.

Fetches Jira issues from active browser tabs.
No write methods. All operations are strictly read-only.
"""

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
from otto.utils.content_parser import parse_jira_page_text

logger = logging.getLogger("otto.adapters.browser.jira_browser")


class BrowserJiraAdapter:
    """
    Read-only Browser Jira adapter.
    
    Reads Jira pages using BrowserContentReader.
    THERE IS NO write(). NO create(). NO update(). NO delete().
    All operations are strictly read-only.
    """

    def __init__(self, reader: BrowserContentReader, account_id: str) -> None:
        self._reader = reader
        self._account_id = account_id
        self._connected = False
        self._base_url = ""
        self._seen_ids: OrderedDict[str, None] = OrderedDict()
        self._max_seen_ids = 10_000

    def _mark_seen(self, source_id: str) -> None:
        self._seen_ids[source_id] = None
        self._seen_ids.move_to_end(source_id)
        if len(self._seen_ids) > self._max_seen_ids:
            self._seen_ids.popitem(last=False)

    @property
    def name(self) -> str:
        return f"browser_jira:{self._account_id}"

    @property
    def source_type(self) -> SourceType:
        return SourceType.JIRA

    @property
    def mode(self) -> str:
        return "browser"

    async def connect(self) -> ConnectionStatus:
        """Verify Jira tabs availability."""
        try:
            tabs = await self._reader.list_all_tabs()
            jira_tabs = self._reader.filter_tabs_by_url(tabs, [".atlassian.net", "jira."])
            if jira_tabs:
                self._connected = True
                
                # Try to extract base URL from the first tab
                first_url = jira_tabs[0].url
                if "://" in first_url:
                    parts = first_url.split("/")
                    if len(parts) >= 3:
                        self._base_url = "/".join(parts[:3])
                        
                return ConnectionStatus(state=ConnectionState.HEALTHY)

            return ConnectionStatus(
                state=ConnectionState.FAILED,
                error="No Jira tabs found."
            )
        except Exception as e:
            return ConnectionStatus(state=ConnectionState.FAILED, error=str(e))

    async def poll(self, since: datetime) -> list[RawEvent]:
        if not self._connected:
            return []
            
        try:
            tabs = await self._reader.list_all_tabs()
            jira_tabs = self._reader.filter_tabs_by_url(tabs, [".atlassian.net", "jira."])
            
            events: list[RawEvent] = []
            
            for tab in jira_tabs:
                content = await self._reader.extract_tab_content(tab)
                if not content or not content.text:
                    continue
                    
                snippets = parse_jira_page_text(content.text)
                for snippet in snippets:
                    if not getattr(snippet, 'key', None):
                        continue
                        
                    if snippet.key in self._seen_ids:
                        continue
                    self._mark_seen(snippet.key)
                    
                    key = snippet.key
                    summary = getattr(snippet, 'summary', '')
                    
                    source_url = f"{self._base_url}/browse/{key}" if self._base_url else tab.url
                    title = f"[{key}] {summary}"
                    
                    content_parts = [title]
                    if getattr(snippet, 'description', ''):
                        content_parts.append(f"Description: {snippet.description[:500]}")
                        
                    plain_text = "\n".join(content_parts)
                    
                    raw_metadata = {
                        "project": key.split("-")[0] if "-" in key else "",
                        "status": getattr(snippet, 'status', 'Unknown'),
                        "priority": getattr(snippet, 'priority', 'None'),
                        "issue_type": getattr(snippet, 'issue_type', 'Task'),
                        "assignee": getattr(snippet, 'assignee', ''),
                        "comment_count": getattr(snippet, 'comment_count', 0),
                    }
                    
                    event = RawEvent(
                        source=SourceType.JIRA,
                        source_id=key,
                        source_url=source_url,
                        timestamp=datetime.now(timezone.utc), # approximation from browser
                        title=title,
                        content_blocks=[ContentBlock(type=ContentType.TEXT, text=plain_text)],
                        plain_text=plain_text,
                        sender_name="",
                        sender_email="",
                        thread_id=key,
                        is_auto_generated=False,
                        raw_metadata=raw_metadata
                    )
                    events.append(event)
                    
            return events
            
        except Exception as e:
            logger.error("Browser Jira poll failed: %s", e)
            return []

    async def fetch_thread(self, thread_id: str) -> list[RawEvent]:
        """Fetch a single thread (returns empty list for browser jira)."""
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
        self._base_url = ""
