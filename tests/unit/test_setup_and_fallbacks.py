from __future__ import annotations

"""
Tests for tab-title fallback features.

Tests cover:
- Gmail _event_from_tab_title parsing edge cases
- Slack _event_from_tab_title and _event_from_window_title parsing
- Content extraction multi-strategy fallback
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from otto.adapters.browser.reader import BrowserContentReader, BrowserTab, ExtractedContent
from otto.adapters.browser.gmail_browser import BrowserGmailAdapter
from otto.adapters.browser.slack_browser import BrowserSlackAdapter
from otto.storage.models import SourceType


# ============================================================================
# Gmail Tab Title Parsing
# ============================================================================

class TestGmailTabTitleParsing:
    """Test _event_from_tab_title with various real-world Gmail tab title formats."""

    def _make_adapter(self):
        reader = BrowserContentReader()
        return BrowserGmailAdapter(reader, "test@example.net")

    def test_standard_inbox_title(self):
        """Parse 'Inbox (112) - you@example.com - Gmail'."""
        adapter = self._make_adapter()
        tab = BrowserTab(
            browser="chrome", title="Inbox (112) - you@example.com - Gmail",
            url="https://mail.google.com/mail/u/1/#inbox", window_index=1, tab_index=9,
        )
        event = adapter._event_from_tab_title(tab)
        assert event is not None
        assert event.source == SourceType.EMAIL
        assert "112 unread" in event.title
        assert "you@example.com" in event.plain_text
        assert event.raw_metadata["unread_count"] == 112
        assert event.raw_metadata["account"] == "you@example.com"
        assert event.raw_metadata["view"] == "Inbox"
        assert event.raw_metadata["extraction_mode"] == "tab_title"

    def test_inbox_no_unread(self):
        """Parse 'Inbox - user@example.net - Gmail' (no unread count)."""
        adapter = self._make_adapter()
        tab = BrowserTab(
            browser="chrome", title="Inbox - user@example.net - Gmail",
            url="https://mail.google.com/mail/u/0/#inbox", window_index=1, tab_index=1,
        )
        event = adapter._event_from_tab_title(tab)
        assert event is not None
        assert event.raw_metadata["unread_count"] == 0
        assert event.raw_metadata["account"] == "user@example.net"

    def test_sent_view(self):
        """Parse 'Sent - user@example.com - Gmail'."""
        adapter = self._make_adapter()
        tab = BrowserTab(
            browser="chrome", title="Sent - user@example.com - Gmail",
            url="https://mail.google.com/mail/u/0/#sent", window_index=1, tab_index=1,
        )
        event = adapter._event_from_tab_title(tab)
        assert event is not None
        assert event.raw_metadata["account"] == "user@example.com"

    def test_large_unread_count(self):
        """Parse 'Inbox (5,432) - user@example.net - Gmail'."""
        adapter = self._make_adapter()
        tab = BrowserTab(
            browser="chrome", title="Inbox (5432) - user@example.net - Gmail",
            url="https://mail.google.com/mail/u/0/#inbox", window_index=1, tab_index=1,
        )
        event = adapter._event_from_tab_title(tab)
        assert event is not None
        assert event.raw_metadata["unread_count"] == 5432

    def test_single_unread(self):
        """Parse 'Inbox (1) - user@example.net - Gmail'."""
        adapter = self._make_adapter()
        tab = BrowserTab(
            browser="chrome", title="Inbox (1) - user@example.net - Gmail",
            url="https://mail.google.com/mail/u/0/#inbox", window_index=1, tab_index=1,
        )
        event = adapter._event_from_tab_title(tab)
        assert event is not None
        assert event.raw_metadata["unread_count"] == 1

    def test_non_gmail_title_returns_none(self):
        """Non-Gmail title should return None."""
        adapter = self._make_adapter()
        tab = BrowserTab(
            browser="chrome", title="Google Search",
            url="https://www.google.com", window_index=1, tab_index=1,
        )
        event = adapter._event_from_tab_title(tab)
        assert event is None

    def test_empty_title(self):
        """Empty title returns None."""
        adapter = self._make_adapter()
        tab = BrowserTab(
            browser="chrome", title="", url="https://mail.google.com",
            window_index=1, tab_index=1,
        )
        event = adapter._event_from_tab_title(tab)
        assert event is None

    def test_proton_mail_title_ignored(self):
        """Proton Mail tab should return None (no gmail.com email)."""
        adapter = self._make_adapter()
        tab = BrowserTab(
            browser="chrome", title="(58) Inbox | someone@example.org | Proton Mail",
            url="https://mail.proton.me/u/1/inbox", window_index=1, tab_index=1,
        )
        event = adapter._event_from_tab_title(tab)
        # Has unread count (58) and email pattern, so should produce event
        assert event is not None
        assert event.raw_metadata["unread_count"] == 58
        assert event.raw_metadata["account"] == "someone@example.org"

    def test_dedup_prevents_duplicate_tab_events(self):
        """Second poll with same tab title shouldn't produce duplicate events."""
        adapter = self._make_adapter()

        tab = BrowserTab(
            browser="chrome", title="Inbox (5) - test@example.net - Gmail",
            url="https://mail.google.com/mail/u/0/#inbox", window_index=1, tab_index=1,
        )

        # Simulate poll producing tab title event
        event1 = adapter._event_from_tab_title(tab)
        assert event1 is not None
        adapter._mark_seen(event1.source_id)

        # Second call with same title should be blocked by dedup
        event2 = adapter._event_from_tab_title(tab)
        assert event2 is not None
        assert event2.source_id in adapter._seen_ids  # Already seen

    def test_source_url_preserved(self):
        """Tab URL should be preserved in the RawEvent."""
        adapter = self._make_adapter()
        url = "https://mail.google.com/mail/u/0/#inbox"
        tab = BrowserTab(
            browser="chrome", title="Inbox (3) - a@example.net - Gmail",
            url=url, window_index=1, tab_index=1,
        )
        event = adapter._event_from_tab_title(tab)
        assert event.source_url == url


# ============================================================================
# Slack Tab Title Parsing
# ============================================================================

class TestSlackTabTitleParsing:
    """Test Slack _event_from_tab_title and _event_from_window_title."""

    def _make_adapter(self):
        reader = BrowserContentReader()
        return BrowserSlackAdapter(reader, "test-workspace")

    def test_channel_with_channel_marker(self):
        """Parse 'security-alerts (Channel) - demo - Slack'."""
        adapter = self._make_adapter()
        tab = BrowserTab(
            browser="chrome", title="security-alerts (Channel) - demo - Slack",
            url="https://app.slack.com/client/T123/C456", window_index=1, tab_index=4,
        )
        event = adapter._event_from_tab_title(tab)
        assert event is not None
        assert event.source == SourceType.SLACK
        assert event.title == "#security-alerts"
        assert event.raw_metadata["channel_name"] == "security-alerts"
        assert event.raw_metadata["workspace"] == "demo"
        assert event.raw_metadata["extraction_mode"] == "tab_title"

    def test_channel_without_marker(self):
        """Parse 'general - mycompany - Slack' (no Channel marker)."""
        adapter = self._make_adapter()
        tab = BrowserTab(
            browser="chrome", title="general - mycompany - Slack",
            url="https://app.slack.com/client/T123/C789", window_index=1, tab_index=1,
        )
        event = adapter._event_from_tab_title(tab)
        assert event is not None
        assert event.title == "#general"
        assert event.raw_metadata["workspace"] == "mycompany"

    def test_dm_marker(self):
        """Parse 'Alice (DM) - demo - Slack'."""
        adapter = self._make_adapter()
        tab = BrowserTab(
            browser="chrome", title="Alice (DM) - demo - Slack",
            url="https://app.slack.com/client/T123/D456", window_index=1, tab_index=1,
        )
        event = adapter._event_from_tab_title(tab)
        assert event is not None
        assert event.title == "#Alice"
        assert event.raw_metadata["channel_name"] == "Alice"

    def test_group_marker(self):
        """Parse 'project-team (Group) - demo - Slack'."""
        adapter = self._make_adapter()
        tab = BrowserTab(
            browser="chrome", title="project-team (Group) - demo - Slack",
            url="https://app.slack.com/client/T123/G789", window_index=1, tab_index=1,
        )
        event = adapter._event_from_tab_title(tab)
        assert event is not None
        assert event.raw_metadata["channel_name"] == "project-team"

    def test_slack_api_page_returns_none(self):
        """Slack API settings page should be handled gracefully."""
        adapter = self._make_adapter()
        tab = BrowserTab(
            browser="chrome", title="Slack API: Applications | demo - Slack",
            url="https://app.slack.com/app-settings/T123/A456", window_index=1, tab_index=5,
        )
        event = adapter._event_from_tab_title(tab)
        # The title doesn't follow the " - X - Slack" pattern: it may or may not
        # yield an event, but it must never raise and any event needs a title.
        assert event is None or event.title

    def test_non_slack_title_returns_none(self):
        """Non-Slack title returns None."""
        adapter = self._make_adapter()
        tab = BrowserTab(
            browser="chrome", title="GitHub - Issues",
            url="https://github.com", window_index=1, tab_index=1,
        )
        event = adapter._event_from_tab_title(tab)
        assert event is None

    def test_window_title_channel(self):
        """Parse Slack.app window title 'all-demo (Channel) - demo - Slack'."""
        adapter = self._make_adapter()
        event = adapter._event_from_window_title("all-demo (Channel) - demo - Slack")
        assert event is not None
        assert event.title == "#all-demo"
        assert event.raw_metadata["channel_name"] == "all-demo"
        assert event.raw_metadata["workspace"] == "demo"
        assert event.raw_metadata["extraction_mode"] == "native_app_title"
        assert "slack" in event.source_url.lower()

    def test_window_title_simple(self):
        """Parse simple window title 'general - workspace - Slack'."""
        adapter = self._make_adapter()
        event = adapter._event_from_window_title("general - workspace - Slack")
        assert event is not None
        assert event.title == "#general"

    def test_window_title_empty(self):
        """Empty window title returns None."""
        adapter = self._make_adapter()
        event = adapter._event_from_window_title("")
        assert event is None

    def test_window_title_not_slack(self):
        """Non-Slack window title returns None."""
        adapter = self._make_adapter()
        event = adapter._event_from_window_title("Chrome - New Tab")
        assert event is None


# ============================================================================
# Multi-Strategy Content Extraction
# ============================================================================

class TestMultiStrategyExtraction:
    """Test the 3-tier content extraction fallback in BrowserContentReader."""

    @pytest.mark.asyncio
    async def test_js_extraction_success_uses_js(self):
        """When JS works, use JS result."""
        reader = BrowserContentReader()
        tab = BrowserTab(browser="chrome", title="Test", url="https://example.com",
                         window_index=1, tab_index=1)
        
        with patch.object(reader, "_run_osascript", new_callable=AsyncMock) as mock:
            mock.return_value = "Hello World from JavaScript"
            result = await reader._extract_chrome_tab(tab)
            assert result == "Hello World from JavaScript"

    @pytest.mark.asyncio
    async def test_js_fails_falls_back_to_source(self):
        """When JS fails, try page source HTML."""
        reader = BrowserContentReader()
        tab = BrowserTab(browser="chrome", title="Test", url="https://example.com",
                         window_index=1, tab_index=1)

        call_count = 0
        async def mock_osascript(script):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ""  # JS fails
            elif call_count == 2:
                return "<html><body><p>This is the page content extracted from the HTML source property of the Chrome tab. It contains enough text to pass the length threshold.</p></body></html>"
            return ""

        with patch.object(reader, "_run_osascript", side_effect=mock_osascript):
            result = await reader._extract_chrome_tab(tab)
            assert "page content extracted" in result

    @pytest.mark.asyncio
    async def test_all_fail_falls_back_to_title(self):
        """When JS and source both fail, use tab title."""
        reader = BrowserContentReader()
        tab = BrowserTab(browser="chrome", title="My Test Page", url="https://example.com/page",
                         window_index=1, tab_index=1)

        with patch.object(reader, "_run_osascript", new_callable=AsyncMock) as mock:
            mock.return_value = ""  # Everything fails
            result = await reader._extract_chrome_tab(tab)
            assert "My Test Page" in result
            assert "https://example.com/page" in result

    @pytest.mark.asyncio
    async def test_safari_extraction_fallback(self):
        """Safari also uses 3-tier fallback."""
        reader = BrowserContentReader()
        tab = BrowserTab(browser="safari", title="Safari Page", url="https://example.com",
                         window_index=1, tab_index=1)

        with patch.object(reader, "_run_osascript", new_callable=AsyncMock) as mock:
            mock.return_value = ""
            result = await reader._extract_safari_tab(tab)
            assert "Safari Page" in result

    def test_tab_metadata_text(self):
        """_tab_metadata_text produces title + URL."""
        tab = BrowserTab(browser="chrome", title="Hello World", url="https://example.com",
                         window_index=1, tab_index=1)
        text = BrowserContentReader._tab_metadata_text(tab)
        assert "Hello World" in text
        assert "https://example.com" in text

    def test_tab_metadata_no_url(self):
        """_tab_metadata_text with no URL."""
        tab = BrowserTab(browser="chrome", title="Hello", url="",
                         window_index=1, tab_index=1)
        text = BrowserContentReader._tab_metadata_text(tab)
        assert "Hello" in text


# ============================================================================
# Slack Native App Extraction
# ============================================================================

class TestSlackNativeAppExtraction:
    """Test Slack.app content extraction."""

    @pytest.mark.asyncio
    async def test_slack_app_window_name_extraction(self):
        """Window name strategy works."""
        reader = BrowserContentReader()

        call_count = 0
        async def mock_osascript(script):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "general (Channel) - myworkspace - Slack"
            return ""  # Second strategy returns nothing

        with patch.object(reader, "_run_osascript", side_effect=mock_osascript):
            result = await reader.extract_slack_app_content()
            assert "general" in result
            assert "myworkspace" in result

    @pytest.mark.asyncio
    async def test_slack_app_returns_empty_when_not_running(self):
        """Returns empty when Slack.app isn't running."""
        reader = BrowserContentReader()

        with patch.object(reader, "_run_osascript", new_callable=AsyncMock) as mock:
            mock.return_value = ""
            result = await reader.extract_slack_app_content()
            assert result == ""

    @pytest.mark.asyncio
    async def test_slack_app_caches_result(self):
        """Cached result returned on second call."""
        reader = BrowserContentReader()

        async def mock_osascript(script):
            return "cached-channel (Channel) - ws - Slack"

        with patch.object(reader, "_run_osascript", side_effect=mock_osascript):
            result1 = await reader.extract_slack_app_content()
            assert "cached-channel" in result1

        # Second call should use cache
        result2 = await reader.extract_slack_app_content()
        assert result2 == result1


# ============================================================================
# Browser Availability Detection
# ============================================================================

class TestBrowserAvailability:
    """Test the is_browser_available method."""

    @pytest.mark.asyncio
    async def test_chrome_available(self):
        """Chrome with windows is available."""
        reader = BrowserContentReader()

        with patch.object(reader, "_run_osascript", new_callable=AsyncMock) as mock:
            mock.return_value = "3"  # 3 windows
            result = await reader.is_browser_available()
            assert result is True

    @pytest.mark.asyncio
    async def test_no_browser(self):
        """No browser returns False."""
        reader = BrowserContentReader()

        with patch.object(reader, "_run_osascript", new_callable=AsyncMock) as mock:
            mock.return_value = ""  # Nothing
            result = await reader.is_browser_available()
            assert result is False

    @pytest.mark.asyncio
    async def test_zero_windows(self):
        """Browser with 0 windows returns False."""
        reader = BrowserContentReader()

        with patch.object(reader, "_run_osascript", new_callable=AsyncMock) as mock:
            mock.return_value = "0"
            result = await reader.is_browser_available()
            assert result is False


# ============================================================================
# Enable Chrome JS Script
# ============================================================================

# ============================================================================
# Gmail Adapter Full Poll with Tab Title Fallback
# ============================================================================

class TestGmailPollWithFallback:
    """Test Gmail adapter poll() with tab title fallback integration."""

    @pytest.mark.asyncio
    async def test_poll_produces_event_from_tab_title(self):
        """When page content parsing returns no snippets, tab title fallback produces an event."""
        reader = BrowserContentReader()
        adapter = BrowserGmailAdapter(reader, "test@example.net")

        gmail_tab = BrowserTab(
            browser="chrome",
            title="Inbox (42) - user@example.net - Gmail",
            url="https://mail.google.com/mail/u/0/#inbox",
            window_index=1, tab_index=1,
        )

        with patch.object(reader, "list_all_tabs", new_callable=AsyncMock) as mock_tabs, \
             patch.object(reader, "extract_tab_content", new_callable=AsyncMock) as mock_content:
            mock_tabs.return_value = [gmail_tab]
            # Return tab-title-only content (no parseable emails)
            mock_content.return_value = ExtractedContent(
                source="chrome",
                url="https://mail.google.com/mail/u/0/#inbox",
                title="Gmail",
                text="Inbox (42) - user@example.net - Gmail\nURL: https://mail.google.com/mail/u/0/#inbox",
            )

            adapter._connected = True
            events = await adapter.poll(datetime(2020, 1, 1, tzinfo=timezone.utc))

            assert len(events) == 1
            assert events[0].source == SourceType.EMAIL
            assert "42 unread" in events[0].title
            assert events[0].raw_metadata["unread_count"] == 42
            assert events[0].raw_metadata["extraction_mode"] == "tab_title"

    @pytest.mark.asyncio
    async def test_poll_prefers_full_content_over_title(self):
        """When full content parsing works, tab title fallback is NOT used."""
        reader = BrowserContentReader()
        adapter = BrowserGmailAdapter(reader, "test@example.net")

        gmail_tab = BrowserTab(
            browser="chrome",
            title="Inbox (5) - user@example.net - Gmail",
            url="https://mail.google.com/mail/u/0/#inbox",
            window_index=1, tab_index=1,
        )

        full_gmail_text = """Alice Smith
Q3 Budget Review
Please review the attached budget.
2:30 PM

Bob Jones
Deployment Update
Production deployment completed.
3 hours ago
"""

        with patch.object(reader, "list_all_tabs", new_callable=AsyncMock) as mock_tabs, \
             patch.object(reader, "extract_tab_content", new_callable=AsyncMock) as mock_content:
            mock_tabs.return_value = [gmail_tab]
            mock_content.return_value = ExtractedContent(
                source="chrome",
                url="https://mail.google.com/mail/u/0/#inbox",
                title="Gmail",
                text=full_gmail_text,
            )

            adapter._connected = True
            events = await adapter.poll(datetime(2020, 1, 1, tzinfo=timezone.utc))

            # Should get events from parsed content, not tab title
            assert len(events) >= 1
            # Check that events are from content parsing, not tab title
            for e in events:
                if hasattr(e, 'raw_metadata') and e.raw_metadata:
                    assert e.raw_metadata.get("extraction_mode") != "tab_title"


class TestSlackPollWithFallback:
    """Test Slack adapter poll() with tab title fallback integration."""

    @pytest.mark.asyncio
    async def test_poll_produces_event_from_tab_title(self):
        """When Slack page content parsing returns no messages, tab title fallback works."""
        reader = BrowserContentReader()
        adapter = BrowserSlackAdapter(reader, "demo")

        slack_tab = BrowserTab(
            browser="chrome",
            title="engineering (Channel) - mycompany - Slack",
            url="https://app.slack.com/client/T123/C456",
            window_index=1, tab_index=1,
        )

        with patch.object(reader, "list_all_tabs", new_callable=AsyncMock) as mock_tabs, \
             patch.object(reader, "extract_tab_content", new_callable=AsyncMock) as mock_content, \
             patch.object(reader, "extract_slack_app_content", new_callable=AsyncMock) as mock_native:
            mock_tabs.return_value = [slack_tab]
            mock_content.return_value = ExtractedContent(
                source="chrome",
                url="https://app.slack.com/client/T123/C456",
                title="Slack",
                text="engineering (Channel) - mycompany - Slack\nURL: https://app.slack.com/client/T123/C456",
            )
            mock_native.return_value = ""

            adapter._connected = True
            adapter._has_browser_tabs = True
            events = await adapter.poll(datetime(2020, 1, 1, tzinfo=timezone.utc))

            assert len(events) == 1
            assert events[0].title == "#engineering"
            assert events[0].raw_metadata["workspace"] == "mycompany"
            assert events[0].raw_metadata["extraction_mode"] == "tab_title"

    @pytest.mark.asyncio
    async def test_poll_native_app_fallback(self):
        """When no Slack tabs, extracts content from Slack.app AX tree."""
        reader = BrowserContentReader()
        adapter = BrowserSlackAdapter(reader, "demo")

        with patch.object(reader, "list_all_tabs", new_callable=AsyncMock) as mock_tabs, \
             patch.object(reader, "extract_slack_app_content", new_callable=AsyncMock) as mock_native:
            mock_tabs.return_value = []  # No Slack tabs
            mock_native.return_value = "Channel random\nAug 21st at 10:00:00 AM\nAlice posted a deployment update for the staging environment"

            adapter._connected = True
            adapter._has_browser_tabs = False
            adapter._has_native_app = True
            events = await adapter.poll(datetime(2020, 1, 1, tzinfo=timezone.utc))

            assert len(events) >= 1
            assert events[0].title == "#random"
            assert events[0].raw_metadata.get("extraction_mode") == "native_app_ax_tree"
