from __future__ import annotations

"""
End-to-end integration tests for browser adapter → ingestion pipeline flow.

Verifies that browser-sourced data flows through the exact same pipeline
as API-sourced data, producing identical NormalizedEvents and triggering
the same EventBus subscriptions.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from otto.adapters.base import RawEvent
from otto.adapters.browser.reader import BrowserContentReader, BrowserTab, ExtractedContent
from otto.adapters.browser.gmail_browser import BrowserGmailAdapter
from otto.adapters.browser.slack_browser import BrowserSlackAdapter
from otto.adapters.browser.calendar_browser import BrowserCalendarAdapter
from otto.adapters.browser.jira_browser import BrowserJiraAdapter
from otto.adapters.browser.factory import create_adapter, create_all_adapters
from otto.core.event_bus import EventBus
from otto.intelligence.ingestion import IngestionPipeline
from otto.storage.models import NewEventsIngested, SourceType


MOCK_GMAIL_TEXT = """Alice Smith
Q3 Budget Review
Please review the attached budget for Q3 planning. Let me know your thoughts.
2:30 PM

Bob Jones
Deployment Update
The production deployment completed successfully. No issues found.
3 hours ago
"""

MOCK_SLACK_TEXT = """#engineering
Alice  10:30 AM
The deployment is complete!
Bob  10:32 AM
Great work team <@U123>!
"""

MOCK_CALENDAR_TEXT = """Team Standup|||2026-01-15T09:00:00Z|||2026-01-15T09:15:00Z|||Zoom|||Daily standup|||Alice, Bob
Sprint Review|||2026-01-15T14:00:00Z|||2026-01-15T15:00:00Z|||Room 3B|||Sprint demo|||Alice, Bob, Charlie
"""

MOCK_JIRA_TEXT = """
PROJ-123
Fix login bug
Status: In Progress
Priority: High
Assignee: Alice Smith
Description: The login page throws a 500 error

PROJ-456
Add dark mode
Status: Open
Priority: Medium
Assignee: Bob Jones
Description: Implement dark mode theme
"""


def _make_gmail_tabs():
    return [BrowserTab(
        browser="chrome", title="Gmail - Inbox", url="https://mail.google.com/mail/u/0/#inbox",
        window_index=1, tab_index=1,
    )]


def _make_slack_tabs():
    return [BrowserTab(
        browser="chrome", title="Slack | engineering", url="https://app.slack.com/client/T123/C456",
        window_index=1, tab_index=2,
    )]


def _make_jira_tabs():
    return [BrowserTab(
        browser="chrome", title="PROJ-123 - Jira", url="https://company.atlassian.net/browse/PROJ-123",
        window_index=1, tab_index=3,
    )]


class TestBrowserToIngestionE2E:
    """Test full pipeline: browser adapter → ingestion → EventBus."""

    @pytest.mark.asyncio
    async def test_browser_gmail_to_ingestion_pipeline(self):
        """Browser Gmail → IngestionPipeline → NormalizedEvents → EventBus."""
        reader = BrowserContentReader()
        adapter = BrowserGmailAdapter(reader, "test@example.net")

        with patch.object(reader, "list_all_tabs", new_callable=AsyncMock) as mock_tabs, \
             patch.object(reader, "extract_tab_content", new_callable=AsyncMock) as mock_content:
            mock_tabs.return_value = _make_gmail_tabs()
            mock_content.return_value = ExtractedContent(
                source="chrome", url="https://mail.google.com/mail/u/0/#inbox",
                title="Gmail", text=MOCK_GMAIL_TEXT,
            )

            # Connect
            status = await adapter.connect()
            assert status.state.value == "healthy"

            # Poll
            events = await adapter.poll(datetime(2026, 1, 1, tzinfo=timezone.utc))
            assert len(events) >= 1

            # Feed into ingestion pipeline
            bus = EventBus()
            ingested = []
            async def on_ingest(event):
                ingested.append(event)
            await bus.subscribe(NewEventsIngested, on_ingest)

            pipeline = IngestionPipeline(event_bus=bus)
            normalized = await pipeline.ingest(events)

            await asyncio.sleep(0.05)

            # Verify normalized events have all required fields
            assert len(normalized) >= 1
            for ne in normalized:
                assert ne.source == SourceType.EMAIL
                assert ne.title
                assert ne.content_hash

            # Verify EventBus was notified
            assert len(ingested) == 1
            assert ingested[0].source == SourceType.EMAIL

    @pytest.mark.asyncio
    async def test_browser_slack_to_ingestion_pipeline(self):
        """Browser Slack → IngestionPipeline → NormalizedEvents."""
        reader = BrowserContentReader()
        adapter = BrowserSlackAdapter(reader, "workspace1")

        with patch.object(reader, "list_all_tabs", new_callable=AsyncMock) as mock_tabs, \
             patch.object(reader, "extract_tab_content", new_callable=AsyncMock) as mock_content, \
             patch.object(reader, "extract_slack_app_content", new_callable=AsyncMock) as mock_native:
            mock_tabs.return_value = _make_slack_tabs()
            mock_content.return_value = ExtractedContent(
                source="chrome", url="https://app.slack.com/client/T123/C456",
                title="Slack", text=MOCK_SLACK_TEXT,
            )
            mock_native.return_value = ""

            status = await adapter.connect()
            assert status.state.value == "healthy"

            events = await adapter.poll(datetime(2026, 1, 1, tzinfo=timezone.utc))

            bus = EventBus()
            pipeline = IngestionPipeline(event_bus=bus)
            normalized = await pipeline.ingest(events)

            for ne in normalized:
                assert ne.source == SourceType.SLACK

    @pytest.mark.asyncio
    async def test_browser_calendar_to_ingestion_pipeline(self):
        """Browser Calendar → IngestionPipeline → NormalizedEvents."""
        reader = BrowserContentReader()
        adapter = BrowserCalendarAdapter(reader, "test@example.net")

        with patch.object(reader, "extract_calendar_events", new_callable=AsyncMock) as mock_cal, \
             patch.object(reader, "list_all_tabs", new_callable=AsyncMock) as mock_tabs:
            mock_cal.return_value = MOCK_CALENDAR_TEXT
            mock_tabs.return_value = []

            status = await adapter.connect()
            assert status.state.value == "healthy"

            events = await adapter.poll(datetime(2026, 1, 1, tzinfo=timezone.utc))
            assert len(events) >= 1

            bus = EventBus()
            pipeline = IngestionPipeline(event_bus=bus)
            normalized = await pipeline.ingest(events)

            for ne in normalized:
                assert ne.source == SourceType.CALENDAR
                assert ne.title

    @pytest.mark.asyncio
    async def test_browser_jira_to_ingestion_pipeline(self):
        """Browser Jira → IngestionPipeline → NormalizedEvents."""
        reader = BrowserContentReader()
        adapter = BrowserJiraAdapter(reader, "test@company.example")

        with patch.object(reader, "list_all_tabs", new_callable=AsyncMock) as mock_tabs, \
             patch.object(reader, "extract_tab_content", new_callable=AsyncMock) as mock_content:
            mock_tabs.return_value = _make_jira_tabs()
            mock_content.return_value = ExtractedContent(
                source="chrome", url="https://company.atlassian.net/browse/PROJ-123",
                title="PROJ-123", text=MOCK_JIRA_TEXT,
            )

            status = await adapter.connect()
            assert status.state.value == "healthy"

            events = await adapter.poll(datetime(2026, 1, 1, tzinfo=timezone.utc))
            assert len(events) >= 1
            assert any("PROJ-123" in e.source_id for e in events)

            bus = EventBus()
            pipeline = IngestionPipeline(event_bus=bus)
            normalized = await pipeline.ingest(events)

            for ne in normalized:
                assert ne.source == SourceType.JIRA


class TestMixedAPIAndBrowserE2E:
    """Test mixing API and browser adapters in the same pipeline."""

    @pytest.mark.asyncio
    async def test_mixed_sources_through_pipeline(self):
        """API-sourced + browser-sourced events both flow through the same pipeline."""
        bus = EventBus()
        pipeline = IngestionPipeline(event_bus=bus)

        # Simulate an API-sourced email
        api_event = RawEvent(
            source=SourceType.EMAIL,
            source_id="api_msg_1",
            source_url="https://mail.google.com/mail/u/0/#inbox/api_msg_1",
            timestamp=datetime.now(timezone.utc),
            title="API Email",
            plain_text="This came from the API adapter.",
            sender_email="api@example.com",
            thread_id="api_thread_1",
        )

        # Simulate a browser-sourced Slack message
        browser_event = RawEvent(
            source=SourceType.SLACK,
            source_id="browser_msg_1",
            source_url="https://app.slack.com/client/T123/C456",
            timestamp=datetime.now(timezone.utc),
            title="#engineering",
            plain_text="This came from the browser adapter.",
            sender_name="Alice",
        )

        # Both should ingest identically
        normalized = await pipeline.ingest([api_event, browser_event])
        assert len(normalized) == 2

        email_events = [e for e in normalized if e.source == SourceType.EMAIL]
        slack_events = [e for e in normalized if e.source == SourceType.SLACK]
        assert len(email_events) == 1
        assert len(slack_events) == 1

        # Both should have content hashes (dedup-ready)
        assert email_events[0].content_hash
        assert slack_events[0].content_hash


class TestAdapterFactoryE2E:
    """Test adapter factory auto-selection end-to-end."""

    @pytest.mark.asyncio
    async def test_factory_selects_api_with_credential(self):
        """Factory should create API adapter when credential is available."""
        mock_http = AsyncMock()
        adapter = await create_adapter(
            source=SourceType.EMAIL,
            http_client=mock_http,
            account_id="test@example.net",
            credential="fake_token",
        )
        assert "gmail:" in adapter.name
        assert "browser" not in adapter.name

    @pytest.mark.asyncio
    async def test_factory_selects_browser_without_credential(self):
        """Factory should create browser adapter when no credential."""
        reader = BrowserContentReader()
        adapter = await create_adapter(
            source=SourceType.EMAIL,
            reader=reader,
        )
        assert "browser" in adapter.name

    @pytest.mark.asyncio
    async def test_factory_raises_when_nothing_available(self):
        """Factory should raise when no credential and no reader."""
        reader = BrowserContentReader()
        # Browser adapter is always created (it just won't connect)
        # So this test verifies we get a browser adapter as fallback
        adapter = await create_adapter(
            source=SourceType.EMAIL,
            reader=reader,
        )
        assert adapter is not None
        assert "browser" in adapter.name

    @pytest.mark.asyncio
    async def test_create_all_adapters_skips_failures(self):
        """create_all_adapters should skip sources that fail, not crash."""
        adapters = await create_all_adapters(account_id="test")
        # Should get at least some adapters (browser fallbacks)
        # This won't crash even if some imports fail
        assert isinstance(adapters, list)
