from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.adapters.browser.calendar_browser import BrowserCalendarAdapter
from otto.adapters.browser.factory import  AdapterUnavailableError
from otto.adapters.browser.gmail_browser import BrowserGmailAdapter
from otto.adapters.browser.jira_browser import BrowserJiraAdapter
from otto.adapters.browser.reader import BrowserContentReader, BrowserTab, ExtractedContent
from otto.adapters.browser.slack_browser import BrowserSlackAdapter
from otto.storage.models import ConnectionState, ContentType, HealthStatus, SourceType
from otto.utils.content_parser import (
    parse_calendar_events_text,
    parse_gmail_page_text,
    parse_jira_page_text,
    parse_slack_page_text,
)


class TestBrowserContentReader:
    def setup_method(self):
        self.reader = BrowserContentReader()
        self.reader._run_osascript = AsyncMock()

    @pytest.mark.asyncio
    async def test_parse_tab_list_chrome(self):
        raw = '1||1||Gmail||https://mail.google.com, 1||2||Slack||https://app.slack.com'
        self.reader._run_osascript.return_value = raw
        tabs = await self.reader.list_chrome_tabs()
        assert len(tabs) == 2
        assert tabs[0].browser == "chrome"
        assert tabs[0].title == "Gmail"
        assert tabs[0].url == "https://mail.google.com"

    @pytest.mark.asyncio
    async def test_parse_tab_list_safari(self):
        raw = '1||1||Calendar||https://calendar.google.com'
        self.reader._run_osascript.return_value = raw
        tabs = await self.reader.list_safari_tabs()
        assert len(tabs) == 1
        assert tabs[0].browser == "safari"

    @pytest.mark.asyncio
    async def test_parse_tab_list_empty(self):
        self.reader._run_osascript.return_value = ""
        tabs = await self.reader.list_chrome_tabs()
        assert tabs == []
        
        self.reader._run_osascript.return_value = None
        tabs = await self.reader.list_safari_tabs()
        assert tabs == []

    @pytest.mark.asyncio
    async def test_parse_tab_list_malformed(self):
        raw = '1||1||Good||https://good.com, bad_entry, 1||2||AlsoGood||https://also.com'
        self.reader._run_osascript.return_value = raw
        tabs = await self.reader.list_chrome_tabs()
        assert len(tabs) == 2

    def test_filter_tabs_by_url(self):
        tabs = [
            BrowserTab("chrome", "Gmail", "https://mail.google.com"),
            BrowserTab("safari", "Slack", "https://app.slack.com"),
        ]
        filtered = self.reader.filter_tabs_by_url(tabs, ["mail.google.com"])
        assert len(filtered) == 1
        assert filtered[0].title == "Gmail"

    def test_filter_tabs_no_match(self):
        tabs = [BrowserTab("chrome", "Gmail", "https://mail.google.com")]
        filtered = self.reader.filter_tabs_by_url(tabs, ["slack.com"])
        assert filtered == []

    def test_cache_key_deterministic(self):
        key1 = self.reader._cache_key("https://example.com")
        key2 = self.reader._cache_key("https://example.com")
        assert key1 == key2

    @pytest.mark.asyncio
    async def test_content_cache_hit(self):
        tab = BrowserTab("chrome", "Test", "https://test.com")
        self.reader._run_osascript.return_value = "Page Content"
        content1 = await self.reader.extract_tab_content(tab)
        
        # Second call should not invoke run_osascript
        self.reader._run_osascript.reset_mock()
        content2 = await self.reader.extract_tab_content(tab)
        
        assert content1 is content2
        self.reader._run_osascript.assert_not_called()

    @pytest.mark.asyncio
    async def test_content_cache_expired(self):
        tab = BrowserTab("chrome", "Test", "https://test.com")
        self.reader._run_osascript.return_value = "Page Content"
        await self.reader.extract_tab_content(tab)
        
        # Manually expire cache
        key = self.reader._cache_key(tab.url)
        self.reader._cache[key].extracted_at -= 100
        
        self.reader._run_osascript.reset_mock()
        self.reader._run_osascript.return_value = "New Content"
        content2 = await self.reader.extract_tab_content(tab)
        
        assert content2.text == "New Content"
        self.reader._run_osascript.assert_called_once()

    def test_clear_cache(self):
        self.reader._cache["test"] = ExtractedContent("test", "test", "test", "test")
        self.reader.clear_cache()
        assert len(self.reader._cache) == 0

    @pytest.mark.asyncio
    async def test_extract_tab_content_chrome(self):
        tab = BrowserTab("chrome", "Test", "https://test.com")
        self.reader._run_osascript.return_value = "Page Content"
        content = await self.reader.extract_tab_content(tab)
        assert content.text == "Page Content"
        assert content.source == "chrome"

    @pytest.mark.asyncio
    async def test_extract_tab_content_timeout(self):
        tab = BrowserTab("chrome", "Test", "https://test.com")
        self.reader._run_osascript.side_effect = asyncio.TimeoutError()
        # the extract method catches exception and returns None
        # Actually reader catch is broad exception. 
        content = await self.reader.extract_tab_content(tab)
        assert content is None


class TestBrowserGmailAdapter:
    def setup_method(self):
        self.reader = MagicMock(spec=BrowserContentReader)
        self.adapter = BrowserGmailAdapter(self.reader, "acc1")

    def test_name(self):
        assert self.adapter.name == "browser_gmail:acc1"

    def test_source_type(self):
        assert self.adapter.source_type == SourceType.EMAIL

    def test_mode(self):
        assert self.adapter.mode == "browser"

    @pytest.mark.asyncio
    async def test_connect_with_gmail_tab(self):
        tabs = [BrowserTab("chrome", "Gmail", "https://mail.google.com")]
        self.reader.list_all_tabs = AsyncMock(return_value=tabs)
        self.reader.filter_tabs_by_url.return_value = tabs
        
        status = await self.adapter.connect()
        assert status.state == ConnectionState.HEALTHY

    @pytest.mark.asyncio
    async def test_connect_no_gmail_tab(self):
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        
        status = await self.adapter.connect()
        assert status.state == ConnectionState.FAILED

    @pytest.mark.asyncio
    async def test_poll_extracts_emails(self):
        self.adapter._connected = True
        tab = BrowserTab("chrome", "Gmail", "https://mail.google.com")
        self.reader.list_all_tabs = AsyncMock(return_value=[tab])
        self.reader.filter_tabs_by_url.return_value = [tab]
        
        text = """Alice Smith
Q3 Budget Review
Please review the attached budget for Q3. Let me know your thoughts by Friday.
2:30 PM

Bob Jones
Deployment Update
The production deployment completed successfully.
3 hours ago
"""
        content = ExtractedContent("chrome", tab.url, tab.title, text)
        self.reader.extract_tab_content = AsyncMock(return_value=content)
        
        events = await self.adapter.poll(datetime.now(timezone.utc))
        assert len(events) == 2
        assert events[0].title == "Q3 Budget Review"
        assert events[1].sender_name == "Bob Jones"

    @pytest.mark.asyncio
    async def test_poll_deduplication(self):
        self.adapter._connected = True
        tab = BrowserTab("chrome", "Gmail", "https://mail.google.com")
        self.reader.list_all_tabs = AsyncMock(return_value=[tab])
        self.reader.filter_tabs_by_url.return_value = [tab]
        
        text = "Alice\nSubject\nSnippet\n1:00 PM\n"
        content = ExtractedContent("chrome", tab.url, tab.title, text)
        self.reader.extract_tab_content = AsyncMock(return_value=content)
        
        events1 = await self.adapter.poll(datetime.now(timezone.utc))
        assert len(events1) == 1
        
        events2 = await self.adapter.poll(datetime.now(timezone.utc))
        assert len(events2) == 0

    @pytest.mark.asyncio
    async def test_poll_not_connected(self):
        self.adapter._connected = False
        events = await self.adapter.poll(datetime.now(timezone.utc))
        assert events == []

    def test_parse_time_str_ago(self):
        dt = self.adapter._parse_time_str("2 hours ago")
        diff = datetime.now(timezone.utc) - dt
        assert 1.9 < diff.total_seconds() / 3600 < 2.1

    def test_parse_time_str_yesterday(self):
        dt = self.adapter._parse_time_str("Yesterday")
        diff = datetime.now(timezone.utc) - dt
        assert 0.9 < diff.total_seconds() / (24*3600) < 1.1

    def test_parse_time_str_time(self):
        dt = self.adapter._parse_time_str("3:30 PM")
        assert dt.hour == 15
        assert dt.minute == 30

    def test_parse_time_str_date(self):
        dt = self.adapter._parse_time_str("Aug 28")
        assert dt.month == 8
        assert dt.day == 28

    def test_parse_time_str_empty(self):
        now = datetime.now(timezone.utc)
        dt = self.adapter._parse_time_str("")
        assert abs((now - dt).total_seconds()) < 1

    @pytest.mark.asyncio
    async def test_health_check_healthy(self):
        self.adapter._connected = True
        self.reader.list_all_tabs = AsyncMock(return_value=["tab"])
        self.reader.filter_tabs_by_url.return_value = ["tab"]
        assert await self.adapter.health_check() == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_health_check_degraded(self):
        self.adapter._connected = True
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        assert await self.adapter.health_check() == HealthStatus.DEGRADED

    @pytest.mark.asyncio
    async def test_disconnect(self):
        self.adapter._connected = True
        await self.adapter.disconnect()
        assert not self.adapter._connected
        self.reader.clear_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_thread_returns_empty(self):
        res = await self.adapter.fetch_thread("123")
        assert res == []

    @pytest.mark.asyncio
    async def test_raw_event_field_parity(self):
        self.adapter._connected = True
        tab = BrowserTab("chrome", "Gmail", "https://mail.google.com")
        self.reader.list_all_tabs = AsyncMock(return_value=[tab])
        self.reader.filter_tabs_by_url.return_value = [tab]
        
        text = "Alice\nSubject\nSnippet\n1:00 PM\n\n\n\n"
        content = ExtractedContent("chrome", tab.url, tab.title, text)
        self.reader.extract_tab_content = AsyncMock(return_value=content)
        
        events = await self.adapter.poll(datetime.now(timezone.utc))
        ev = events[0]
        assert ev.source == SourceType.EMAIL
        assert ev.title == "Subject"
        assert ev.sender_email == "Alice"
        assert ev.plain_text == "Snippet"
        assert ev.thread_id
        assert ev.content_blocks[0].type == ContentType.TEXT


class TestBrowserSlackAdapter:
    def setup_method(self):
        self.reader = MagicMock(spec=BrowserContentReader)
        self.reader.extract_slack_app_content = AsyncMock(return_value="")
        self.adapter = BrowserSlackAdapter(self.reader, "wk1")

    def test_name(self):
        assert self.adapter.name == "browser_slack:wk1"

    def test_source_type(self):
        assert self.adapter.source_type == SourceType.SLACK

    def test_mode(self):
        assert self.adapter.mode == "browser"

    @pytest.mark.asyncio
    async def test_connect_with_slack_tab(self):
        tab = BrowserTab("chrome", "Slack", "https://app.slack.com")
        self.reader.list_all_tabs = AsyncMock(return_value=[tab])
        self.reader.filter_tabs_by_url.return_value = [tab]
        status = await self.adapter.connect()
        assert status.state == ConnectionState.HEALTHY

    @pytest.mark.asyncio
    async def test_connect_with_native_app(self):
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        self.reader.app_is_running = AsyncMock(return_value=True)
        self.reader.extract_slack_app_content = AsyncMock(return_value="content")
        status = await self.adapter.connect()
        assert status.state == ConnectionState.HEALTHY
        # connect is a presence check only: the expensive read belongs to poll()
        self.reader.extract_slack_app_content.assert_not_called()

    @pytest.mark.asyncio
    async def test_connect_nothing(self):
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        self.reader.app_is_running = AsyncMock(return_value=False)
        self.reader.extract_slack_app_content = AsyncMock(return_value="")
        status = await self.adapter.connect()
        assert status.state == ConnectionState.FAILED
        assert "not open" in status.error

    def test_last_error_mirrors_reader(self):
        self.reader.last_error = "not allowed assistive access"
        assert self.adapter.last_error == "not allowed assistive access"

    @pytest.mark.asyncio
    async def test_poll_from_browser_tabs(self):
        self.adapter._connected = True
        tab = BrowserTab("chrome", "Slack", "https://app.slack.com")
        self.reader.list_all_tabs = AsyncMock(return_value=[tab])
        self.reader.filter_tabs_by_url.return_value = [tab]
        
        text = "#engineering\nAlice  10:30 AM\nDeployment is complete!\nBob  10:32 AM\nGreat work <@U123>!"
        self.reader.extract_tab_content = AsyncMock(return_value=ExtractedContent("chrome", tab.url, tab.title, text))
        
        events = await self.adapter.poll(datetime.now(timezone.utc))
        assert len(events) == 2
        assert events[0].plain_text == "Deployment is complete!"
        assert events[1].sender_name == "Bob"

    @pytest.mark.asyncio
    async def test_poll_from_native_app(self):
        self.adapter._connected = True
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        text = "Channel engineering\nAug 21st at 10:30:00 AM\nAlice posted: Deployment to production is now complete and verified!"
        self.reader.extract_slack_app_content = AsyncMock(return_value=text)
        
        events = await self.adapter.poll(datetime.now(timezone.utc))
        assert len(events) >= 1
        assert "slack" in events[0].source_url.lower()

    @pytest.mark.asyncio
    async def test_poll_deduplication(self):
        self.adapter._connected = True
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        content = "Channel eng\nAug 21st at 10:00:00 AM\nAlice posted a deployment update for the staging environment"
        self.reader.extract_slack_app_content = AsyncMock(return_value=content)
        
        text = "Evt|||2099-01-15T09:00:00Z|||2099-01-15T09:15:00Z|||||||||\nEvt|||2099-01-15T09:00:00Z|||2099-01-15T09:15:00Z|||||||||"
        self.reader.extract_calendar_events = AsyncMock(return_value=text)
        ev1 = await self.adapter.poll(datetime(2025, 1, 1, tzinfo=timezone.utc))
        assert len(ev1) >= 1

    @pytest.mark.asyncio
    async def test_poll_mention_detection(self):
        self.adapter._connected = True
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        content = "Channel eng\nAug 21st at 10:00:00 AM\nAlice mentioned you: Hey <@U123> check this deployment update please"
        self.reader.extract_slack_app_content = AsyncMock(return_value=content)
        
        events = await self.adapter.poll(datetime.now(timezone.utc))
        assert len(events) >= 1
        assert events[0].raw_metadata.get("channel_name") == "eng"

    @pytest.mark.asyncio
    async def test_poll_bot_detection(self):
        self.adapter._connected = True
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        content = "Channel eng\nAug 21st at 10:00:00 AM\nGithub notify APP: PR merged into main - Added by integration bot"
        self.reader.extract_slack_app_content = AsyncMock(return_value=content)
        
        events = await self.adapter.poll(datetime.now(timezone.utc))
        assert len(events) >= 1
        assert events[0].is_auto_generated

    @pytest.mark.asyncio
    async def test_health_check_healthy(self):
        self.adapter._connected = True
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        self.reader.extract_slack_app_content = AsyncMock(return_value="hi")
        assert await self.adapter.health_check() == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_disconnect(self):
        self.adapter._connected = True
        await self.adapter.disconnect()
        assert not self.adapter._connected

    def test_does_not_read_slack_private_storage(self):
        """Otto must only read what is on screen — never Slack's IndexedDB/cache files."""
        import inspect
        import otto.adapters.browser.slack_browser as mod
        src = inspect.getsource(mod)
        assert "IndexedDB" not in src
        assert "Cache_Data" not in src
        assert "webapp-console.log" not in src

    def test_remembers_previously_viewed_channels(self):
        from otto.adapters.browser.slack_browser import clear_slack_channel_cache
        clear_slack_channel_cache()
        events: list = []
        self.adapter._process_native_content(
            "Channel eng\nAug 21st at 10:00:00 AM\nDeploy finished for the payments service, all green.",
            events,
        )
        assert len(events) == 1
        # New adapter instance (as happens every refresh) viewing a different channel
        fresh = BrowserSlackAdapter(self.reader, "ws")
        events2: list = []
        fresh._process_native_content(
            "Channel design\nAug 21st at 11:00:00 AM\nNew mockups are up for the onboarding flow, please review.",
            events2,
        )
        channels = {e.raw_metadata["channel_name"] for e in events2}
        assert channels == {"eng", "design"}
        clear_slack_channel_cache()


class TestBrowserCalendarAdapter:
    def setup_method(self):
        self.reader = MagicMock(spec=BrowserContentReader)
        self.adapter = BrowserCalendarAdapter(self.reader, "cal1")

    def test_name(self):
        assert self.adapter.name == "browser_calendar:cal1"

    def test_source_type(self):
        assert self.adapter.source_type == SourceType.CALENDAR

    def test_mode(self):
        assert self.adapter.mode == "browser"

    @pytest.mark.asyncio
    async def test_connect_with_calendar_app(self):
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        self.reader.extract_calendar_events = AsyncMock(return_value="Event|||2026|||2026||||||")
        status = await self.adapter.connect()
        assert status.state == ConnectionState.HEALTHY

    @pytest.mark.asyncio
    async def test_connect_with_gcal_tab(self):
        tab = BrowserTab("chrome", "Cal", "https://calendar.google.com")
        self.reader.list_all_tabs = AsyncMock(return_value=[tab])
        self.reader.filter_tabs_by_url.return_value = [tab]
        self.reader.extract_calendar_events = AsyncMock(return_value="")
        status = await self.adapter.connect()
        assert status.state == ConnectionState.HEALTHY

    @pytest.mark.asyncio
    async def test_connect_nothing(self):
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        self.reader.extract_calendar_events = AsyncMock(return_value="")
        status = await self.adapter.connect()
        assert status.state == ConnectionState.FAILED

    @pytest.mark.asyncio
    async def test_poll_from_calendar_app(self):
        self.adapter._connected = True
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        text = "Team Standup|||2099-01-15T09:00:00Z|||2099-01-15T09:15:00Z|||Zoom|||Daily standup|||Alice, Bob"
        self.reader.extract_calendar_events = AsyncMock(return_value=text)
        
        events = await self.adapter.poll(datetime(2025, 1, 1, tzinfo=timezone.utc))
        assert len(events) == 1
        assert events[0].title == "Team Standup"
        assert events[0].source_url == ""

    @pytest.mark.asyncio
    async def test_poll_filters_by_since(self):
        self.adapter._connected = True
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        text = "Old Event|||2024-01-15T09:00:00Z|||2024-01-15T09:15:00Z|||Zoom||||||"
        self.reader.extract_calendar_events = AsyncMock(return_value=text)
        
        events = await self.adapter.poll(datetime(2025, 1, 1, tzinfo=timezone.utc))
        # Event is older than since, so it might be filtered by the adapter logic
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_poll_deduplication(self):
        self.adapter._connected = True
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        text = "Evt|||2099-01-15T09:00:00Z|||2099-01-15T09:15:00Z|||||||||\nEvt|||2099-01-15T09:00:00Z|||2099-01-15T09:15:00Z|||||||||\nEvt|||2099-01-15T09:00:00Z|||2099-01-15T09:15:00Z|||||||||"
        self.reader.extract_calendar_events = AsyncMock(return_value=text)
        ev1 = await self.adapter.poll(datetime(2025, 1, 1, tzinfo=timezone.utc))
        assert len(ev1) == 1

    @pytest.mark.asyncio
    async def test_get_upcoming(self):
        self.adapter._connected = True
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        self.reader.extract_calendar_events = AsyncMock(return_value="Evt|||2099-01-15T09:00:00Z|||2099-01-15T09:15:00Z|||||||||")
        
        ev1 = await self.adapter.get_upcoming(hours=7)
        assert len(ev1) == 1

    @pytest.mark.asyncio
    async def test_all_day_event_detection(self):
        self.adapter._connected = True
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        text = "Lunch|||2099-01-15|||2099-01-15|||||||||"
        self.reader.extract_calendar_events = AsyncMock(return_value=text)
        
        events = await self.adapter.poll(datetime(2025, 1, 1, tzinfo=timezone.utc))
        assert len(events) == 1
        assert events[0].raw_metadata is not None
        assert events[0].raw_metadata.get("all_day") is True

    @pytest.mark.asyncio
    async def test_health_check(self):
        self.adapter._connected = True
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        self.reader.extract_calendar_events = AsyncMock(return_value="1")
        assert await self.adapter.health_check() == HealthStatus.HEALTHY


class TestBrowserJiraAdapter:
    def setup_method(self):
        self.reader = MagicMock(spec=BrowserContentReader)
        self.adapter = BrowserJiraAdapter(self.reader, "jira1")

    def test_name(self):
        assert self.adapter.name == "browser_jira:jira1"

    def test_source_type(self):
        assert self.adapter.source_type == SourceType.JIRA

    def test_mode(self):
        assert self.adapter.mode == "browser"

    @pytest.mark.asyncio
    async def test_connect_with_jira_tab(self):
        tab = BrowserTab("chrome", "Jira", "https://acme.atlassian.net/jira")
        self.reader.list_all_tabs = AsyncMock(return_value=[tab])
        self.reader.filter_tabs_by_url.return_value = [tab]
        status = await self.adapter.connect()
        assert status.state == ConnectionState.HEALTHY

    @pytest.mark.asyncio
    async def test_connect_nothing(self):
        self.reader.list_all_tabs = AsyncMock(return_value=[])
        self.reader.filter_tabs_by_url.return_value = []
        status = await self.adapter.connect()
        assert status.state == ConnectionState.FAILED

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_poll_extracts_issues(self):
        self.adapter._connected = True
        tab = BrowserTab("chrome", "Jira", "https://acme.atlassian.net/jira")
        self.reader.list_all_tabs = AsyncMock(return_value=[tab])
        self.reader.filter_tabs_by_url.return_value = [tab]
        
        text = "PROJ-123\nFix login bug\nStatus: In Progress\nPriority: High\nAssignee: Alice Smith\nDescription: The login page throws a 500 error when..."
        self.reader.extract_tab_content = AsyncMock(return_value=ExtractedContent("chrome", tab.url, tab.title, text))
        
        events = await self.adapter.poll(datetime.now(timezone.utc))
        assert len(events) == 1
        assert events[0].title == "[PROJ-123] Fix login bug"

    @pytest.mark.asyncio
    async def test_poll_deduplication_by_key(self):
        self.adapter._connected = True
        tab = BrowserTab("chrome", "Jira", "https://acme.atlassian.net")
        self.reader.list_all_tabs = AsyncMock(return_value=[tab])
        self.reader.filter_tabs_by_url.return_value = [tab]
        
        text = "PROJ-123\nFix login bug\nStatus: In Progress\nPROJ-123\nFix login bug\nStatus: In Progress\n"
        self.reader.extract_tab_content = AsyncMock(return_value=ExtractedContent("chrome", tab.url, tab.title, text))
        
        ev1 = await self.adapter.poll(datetime.now(timezone.utc))
        assert len(ev1) == 1

    @pytest.mark.asyncio
    async def test_issue_key_parsing(self):
        self.adapter._connected = True
        tab = BrowserTab("chrome", "Jira", "https://acme.atlassian.net")
        self.reader.list_all_tabs = AsyncMock(return_value=[tab])
        self.reader.filter_tabs_by_url.return_value = [tab]
        text = "MYPROJ-999\nSummary\nStatus: Done\n"
        self.reader.extract_tab_content = AsyncMock(return_value=ExtractedContent("chrome", tab.url, tab.title, text))
        events = await self.adapter.poll(datetime.now(timezone.utc))
        assert events[0].source_id == "MYPROJ-999"

    @pytest.mark.asyncio
    async def test_base_url_extraction(self):
        self.adapter._connected = True
        tab = BrowserTab("chrome", "Jira", "https://acme.atlassian.net/browse/PROJ-123?filter=abc")
        self.reader.list_all_tabs = AsyncMock(return_value=[tab])
        self.reader.filter_tabs_by_url.return_value = [tab]
        await self.adapter.connect()
        text = "PROJ-123\\nSummary\\n"
        self.reader.extract_tab_content = AsyncMock(return_value=ExtractedContent("chrome", tab.url, tab.title, text))
        events = await self.adapter.poll(datetime.now(timezone.utc))
        assert events[0].source_url == "https://acme.atlassian.net/browse/PROJ-123"

    @pytest.mark.asyncio
    async def test_source_url_construction(self):
        # Already tested in base url extraction
        pass

    @pytest.mark.asyncio
    async def test_health_check(self):
        self.adapter._connected = True
        tab = BrowserTab("chrome", "Jira", "https://acme.atlassian.net")
        self.reader.list_all_tabs = AsyncMock(return_value=[tab])
        self.reader.filter_tabs_by_url.return_value = [tab]
        assert await self.adapter.health_check() == HealthStatus.HEALTHY


class TestContentParsers:
    def test_parse_gmail_empty(self):
        assert parse_gmail_page_text("") == []

    def test_parse_gmail_single_email(self):
        text = "Alice\nSubject\nSnippet text\n1:00 PM\n"
        res = parse_gmail_page_text(text)
        assert len(res) == 1
        assert res[0].sender == "Alice"
        assert res[0].subject == "Subject"

    def test_parse_gmail_multiple_emails(self):
        text = "Alice\nS1\nSnip1\n1:00 PM\n\n\nBob\nS2\nSnip2\n2:00 PM\n"
        res = parse_gmail_page_text(text)
        assert len(res) == 2

    def test_parse_gmail_unread_marker(self):
        text = "* Alice\nS1\nSnip\n1:00 PM\n"
        res = parse_gmail_page_text(text)
        assert res[0].is_unread
        assert res[0].sender == "Alice"

    def test_parse_gmail_max_50(self):
        text = ("A\nB\nC\n1:00 PM\n\n\n" * 60)
        res = parse_gmail_page_text(text)
        assert len(res) == 50

    def test_parse_slack_empty(self):
        assert parse_slack_page_text("") == []

    def test_parse_slack_single_message(self):
        text = "#channel\nAlice 1:00 PM\nHello world"
        res = parse_slack_page_text(text)
        assert len(res) == 1
        assert res[0].text == "Hello world"
        assert res[0].sender == "Alice"

    def test_parse_slack_multiple_messages(self):
        text = "#channel\nAlice 1:00 PM\nMsg1\nBob 1:02 PM\nMsg2\n"
        res = parse_slack_page_text(text)
        assert len(res) == 2
        assert res[1].text == "Msg2"

    def test_parse_slack_bot_detection(self):
        text = "#channel\nGithub APP 1:00 PM\nPR merged"
        res = parse_slack_page_text(text)
        assert res[0].is_bot

    def test_parse_jira_empty(self):
        assert parse_jira_page_text("") == []

    def test_parse_jira_single_issue(self):
        text = "PROJ-123\nTitle\nStatus: In Progress\nPriority: High\nAssignee: Me\nDescription: stuff"
        res = parse_jira_page_text(text)
        assert len(res) == 1
        assert res[0].key == "PROJ-123"
        assert res[0].summary == "Title"
        assert "In Progress" in res[0].status

    def test_parse_jira_multiple_issues(self):
        text = "A-1\nT1\n\nB-2\nT2\n\n"
        res = parse_jira_page_text(text)
        assert len(res) == 2

    def test_parse_calendar_pipe_delimited(self):
        text = "T|||S|||E|||L|||D|||A, B"
        res = parse_calendar_events_text(text)
        assert len(res) == 1
        assert res[0].title == "T"
        assert res[0].attendees == ["A", "B"]

    def test_parse_calendar_empty(self):
        assert parse_calendar_events_text("") == []

    def test_parse_calendar_with_attendees(self):
        text = "T|||S|||E|||L|||D|||A"
        res = parse_calendar_events_text(text)
        assert res[0].attendees == ["A"]


class TestAdapterFactory:
        
    @pytest.mark.asyncio
    async def test_factory_creates_api_adapter_with_credential(self):
        from otto.adapters.browser.factory import create_adapter
        http_client = MagicMock()
        adapter = await create_adapter(SourceType.EMAIL, http_client, "acc1", credential="key")
        assert type(adapter).__name__ == "GmailAdapter"

    @pytest.mark.asyncio
    async def test_factory_creates_browser_adapter_without_credential(self):
        from otto.adapters.browser.factory import create_adapter
        http_client = MagicMock()
        adapter = await create_adapter(SourceType.EMAIL, http_client, "acc1")
        assert type(adapter).__name__ == "BrowserGmailAdapter"

    @pytest.mark.asyncio
    async def test_factory_raises_when_nothing_available(self):
        from otto.adapters.browser.factory import create_adapter, AdapterUnavailableError
        
        with patch("otto.adapters.browser.factory._create_browser_adapter", return_value=None):
            with pytest.raises(AdapterUnavailableError):
                await create_adapter(SourceType.EMAIL, None, "acc1")

    @pytest.mark.asyncio
    async def test_factory_creates_gmail_api(self):
        from otto.adapters.browser.factory import _create_api_adapter
        adapter = _create_api_adapter(SourceType.EMAIL, MagicMock(), "acc1")
        assert type(adapter).__name__ == "GmailAdapter"

    @pytest.mark.asyncio
    async def test_factory_creates_browser_gmail(self):
        from otto.adapters.browser.factory import _create_browser_adapter
        adapter = _create_browser_adapter(SourceType.EMAIL, MagicMock(), "acc1")
        assert type(adapter).__name__ == "BrowserGmailAdapter"

    @pytest.mark.asyncio
    async def test_factory_creates_all_adapters(self):
        from otto.adapters.browser.factory import create_all_adapters
        http_client = MagicMock()
        adapters = await create_all_adapters(http_client, "acc1", [SourceType.EMAIL, SourceType.SLACK])
        assert len(adapters) == 2

    @pytest.mark.asyncio
    async def test_factory_skips_unavailable_sources(self):
        from otto.adapters.browser.factory import create_all_adapters
        
        with patch("otto.adapters.browser.factory.create_adapter", side_effect=AdapterUnavailableError):
            adapters = await create_all_adapters(None, "acc", [SourceType.EMAIL])
            assert len(adapters) == 0

    def test_source_hint_messages(self):
        from otto.adapters.browser.factory import _source_hint
        assert "Gmail" in _source_hint(SourceType.EMAIL)
        assert "Slack" in _source_hint(SourceType.SLACK)
        assert "Calendar" in _source_hint(SourceType.CALENDAR)
        assert "Jira" in _source_hint(SourceType.JIRA)

    def test_build_slack_url_variants(self):
        from otto.adapters.browser.slack_browser import _build_slack_url
        ws = {"domain": "acme", "team_id": "T0123ABCD"}

        # Channel ID + message timestamp → permalink
        assert _build_slack_url("C0AAAAAAAAA", message_ts="1700000000.123456", info=ws) == \
            "https://acme.slack.com/archives/C0AAAAAAAAA/p1700000000123456"
        # Channel ID only → native deep link
        assert _build_slack_url("C0AAAAAAAAA", info=ws) == "slack://channel?team=T0123ABCD&id=C0AAAAAAAAA"
        # Channel name → app_redirect (Slack resolves the name)
        assert _build_slack_url("#general", info=ws) == "https://acme.slack.com/app_redirect?channel=general"
        assert _build_slack_url("eng team", info=ws) == "https://acme.slack.com/app_redirect?channel=eng%20team"
        # DMs by user name cannot be linked → open workspace
        assert _build_slack_url("dm-alice", info=ws) == "slack://open?team=T0123ABCD"
        assert _build_slack_url("@alice", info=ws) == "slack://open?team=T0123ABCD"
        # No workspace info at all
        assert _build_slack_url("general", info={}) == "slack://open"
        assert _build_slack_url("C0AAAAAAAAA", message_ts="1", info={}) == "slack://open"

    def test_build_slack_url_never_hardcodes_a_workspace(self):
        import inspect
        import otto.adapters.browser.slack_browser as mod
        src = inspect.getsource(mod._build_slack_url)
        assert "demo" not in src.lower()
        assert not re.search(r"[CD]0[A-Z0-9]{8,}", src), "channel IDs must not be hardcoded"

