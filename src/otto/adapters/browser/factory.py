from __future__ import annotations
"""
Adapter factory — intelligent source selection.

Automatically selects the best adapter for each source type:
1. API adapter (if credentials are available in macOS Keychain)
2. Browser adapter (if the relevant app/tab is open)
3. Native app adapter (if the macOS app is available)

The factory produces adapters that implement the same SourceAdapter
protocol — the intelligence pipeline never knows the difference.
"""

import logging
from typing import Any

from otto.adapters.base import SourceAdapter
from otto.adapters.browser.reader import BrowserContentReader
from otto.storage.models import SourceType

logger = logging.getLogger("otto.adapters.browser.factory")


class AdapterUnavailableError(Exception):
    """Raised when no adapter (API or browser) is available for a source."""
    pass


class AdapterFactory:
    """Factory object for creating and discovering adapters."""

    def __init__(self, reader: BrowserContentReader | None = None) -> None:
        self.reader = reader or BrowserContentReader()

    async def create_adapter(
        self,
        source: SourceType,
        http_client: Any = None,
        account_id: str = "",
        credential: str | None = None,
        base_url: str = "",
    ) -> SourceAdapter:
        return await create_adapter(
            source=source,
            http_client=http_client,
            account_id=account_id,
            reader=self.reader,
            credential=credential,
            base_url=base_url,
        )

    def create_all_adapters(
        self,
        http_client: Any = None,
        account_id: str = "",
        sources: list[SourceType] | None = None,
        credentials: dict[str, str] | None = None,
        base_url: str = "",
    ) -> list[SourceAdapter]:
        if sources is None:
            sources = [SourceType.EMAIL, SourceType.SLACK, SourceType.CALENDAR, SourceType.JIRA]
        if credentials is None:
            credentials = {}

        adapters: list[SourceAdapter] = []
        for source in sources:
            cred = credentials.get(source.value)
            adapter = None
            if cred and http_client:
                adapter = _create_api_adapter(source, http_client, account_id, base_url)
            if not adapter:
                adapter = _create_browser_adapter(source, self.reader, account_id)
            if adapter:
                adapters.append(adapter)
        return adapters


async def create_adapter(
    source: SourceType,
    http_client: Any = None,
    account_id: str = "",
    reader: BrowserContentReader | None = None,
    credential: str | None = None,
    base_url: str = "",
) -> SourceAdapter:
    """
    Create the best available adapter for a given source type.

    Strategy:
    1. If credential is provided → use API adapter
    2. If no credential → try browser/native app adapter
    3. If nothing available → raise AdapterUnavailableError

    Args:
        source: The source type (EMAIL, SLACK, JIRA, CALENDAR).
        http_client: InstrumentedHttpClient for API adapters.
        account_id: User's account identifier.
        reader: BrowserContentReader instance (created if None and needed).
        credential: API credential (if available from Keychain).
        base_url: Base URL for Jira (only needed for API adapter).

    Returns:
        A SourceAdapter instance ready to connect.
    """
    # Strategy 1: API adapter with credential
    if credential and http_client:
        adapter = _create_api_adapter(source, http_client, account_id, base_url)
        if adapter:
            logger.info("Using API adapter for %s", source.value)
            return adapter

    # Strategy 2: Browser / native app adapter
    if reader is None:
        reader = BrowserContentReader()

    adapter = _create_browser_adapter(source, reader, account_id)
    if adapter:
        logger.info("Using browser adapter for %s (no API key)", source.value)
        return adapter

    raise AdapterUnavailableError(
        f"No API key and no browser/app detected for {source.value}. "
        f"Please open {_source_hint(source)} in your browser, or add an API key."
    )


async def create_all_adapters(
    http_client: Any = None,
    account_id: str = "",
    sources: list[SourceType] | None = None,
    credentials: dict[str, str] | None = None,
    base_url: str = "",
) -> list[SourceAdapter]:
    """
    Create adapters for all requested sources, using best available method.

    Returns a list of successfully created adapters (skips unavailable sources).
    """
    if sources is None:
        sources = [SourceType.EMAIL, SourceType.SLACK, SourceType.CALENDAR, SourceType.JIRA]
    if credentials is None:
        credentials = {}

    reader = BrowserContentReader()
    adapters: list[SourceAdapter] = []

    for source in sources:
        try:
            credential = credentials.get(source.value)
            adapter = await create_adapter(
                source=source,
                http_client=http_client,
                account_id=account_id,
                reader=reader,
                credential=credential,
                base_url=base_url,
            )
            adapters.append(adapter)
        except AdapterUnavailableError as e:
            logger.info("Skipping %s: %s", source.value, e)
        except Exception as e:
            logger.error("Error creating adapter for %s: %s", source.value, e)

    return adapters


def _create_api_adapter(
    source: SourceType,
    http_client: Any,
    account_id: str,
    base_url: str = "",
) -> SourceAdapter | None:
    """Create an API-based adapter if the source type is supported."""
    try:
        if source == SourceType.EMAIL:
            from otto.adapters.gmail import GmailAdapter
            return GmailAdapter(http_client, account_id)
        elif source == SourceType.SLACK:
            from otto.adapters.slack import SlackAdapter
            return SlackAdapter(http_client, account_id)
        elif source == SourceType.CALENDAR:
            from otto.adapters.calendar import CalendarAdapter
            return CalendarAdapter(http_client, account_id)
        elif source == SourceType.JIRA:
            from otto.adapters.jira import JiraAdapter
            return JiraAdapter(http_client, base_url or "https://jira.atlassian.net", account_id)
    except ImportError as e:
        logger.warning("API adapter import failed for %s: %s", source.value, e)
    return None


def _create_browser_adapter(
    source: SourceType,
    reader: BrowserContentReader,
    account_id: str,
) -> SourceAdapter | None:
    """Create a browser-based adapter."""
    try:
        if source == SourceType.EMAIL:
            from otto.adapters.browser.gmail_browser import BrowserGmailAdapter
            return BrowserGmailAdapter(reader, account_id)
        elif source == SourceType.SLACK:
            from otto.adapters.browser.slack_browser import BrowserSlackAdapter
            return BrowserSlackAdapter(reader, account_id)
        elif source == SourceType.CALENDAR:
            # Prefer native EventKit adapter (handles recurring events, no app needed)
            try:
                from otto.adapters.browser.calendar_native import NativeCalendarAdapter
                return NativeCalendarAdapter()
            except ImportError:
                pass
            from otto.adapters.browser.calendar_browser import BrowserCalendarAdapter
            return BrowserCalendarAdapter(reader, account_id)
        elif source == SourceType.JIRA:
            from otto.adapters.browser.jira_browser import BrowserJiraAdapter
            return BrowserJiraAdapter(reader, account_id)
    except ImportError as e:
        logger.warning("Browser adapter import failed for %s: %s", source.value, e)
    return None


def _source_hint(source: SourceType) -> str:
    """User-friendly hint for which app/tab to open."""
    hints = {
        SourceType.EMAIL: "Gmail (mail.google.com)",
        SourceType.SLACK: "Slack (app.slack.com or Slack.app)",
        SourceType.CALENDAR: "Google Calendar (calendar.google.com)",
        SourceType.JIRA: "Jira (*.atlassian.net)",
    }
    return hints.get(source, str(source))
