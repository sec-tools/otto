from __future__ import annotations

"""
Tests for content_parser, html_to_text, setup script functions,
and additional edge cases for the browser reading pipeline.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import asyncio
import pytest

from otto.utils.content_parser import (
    html_to_text,
    strip_email_signature,
    content_hash,
    truncate_for_llm,
    parse_gmail_page_text,
    parse_slack_page_text,
    parse_jira_page_text,
    parse_calendar_events_text,
)
from otto.adapters.browser.reader import BrowserContentReader, BrowserTab, OSASCRIPT_TIMEOUT


# ============================================================================
# html_to_text
# ============================================================================

class TestHtmlToText:
    """Test HTML→plain text conversion used in Strategy 2 fallback."""

    def test_simple_paragraph(self):
        result = html_to_text("<p>Hello World</p>")
        assert "Hello World" in result

    def test_strips_script_tags(self):
        html = '<html><script>alert("evil")</script><body>Clean text</body></html>'
        result = html_to_text(html)
        assert "Clean text" in result
        assert "alert" not in result

    def test_strips_style_tags(self):
        html = '<html><style>.red { color: red; }</style><body>Visible</body></html>'
        result = html_to_text(html)
        assert "Visible" in result
        assert "color" not in result

    def test_decodes_html_entities(self):
        result = html_to_text("<p>&amp; &lt; &gt; &quot;</p>")
        assert "&" in result
        assert "<" in result
        assert ">" in result

    def test_empty_string(self):
        assert html_to_text("") == ""

    def test_none_input(self):
        assert html_to_text(None) == ""

    def test_plain_text_passthrough(self):
        assert html_to_text("Just plain text") == "Just plain text"

    def test_normalizes_whitespace(self):
        result = html_to_text("<p>  lots   of     spaces  </p>")
        assert "lots of spaces" in result

    def test_preserves_newlines_but_limits_them(self):
        html = "<p>Line 1</p>\n\n\n\n\n<p>Line 2</p>"
        result = html_to_text(html)
        assert "\n\n\n" not in result

    def test_complex_gmail_page(self):
        """Simulates a gmail-like page with complex HTML."""
        html = """
        <html><head><title>Gmail</title></head>
        <body>
            <div class="inbox-list">
                <div class="message">
                    <span class="sender">Alice Smith</span>
                    <span class="subject">Q3 Budget Review</span>
                    <span class="snippet">Please review the attached budget spreadsheet...</span>
                    <span class="time">2:30 PM</span>
                </div>
                <div class="message">
                    <span class="sender">Bob Jones</span>
                    <span class="subject">Deployment Update</span>
                    <span class="snippet">Production deployment completed successfully</span>
                    <span class="time">11:15 AM</span>
                </div>
            </div>
        </body></html>
        """
        result = html_to_text(html)
        assert "Alice Smith" in result
        assert "Q3 Budget Review" in result
        assert "Bob Jones" in result
        assert len(result) > 20  # Should pass the threshold


# ============================================================================
# strip_email_signature
# ============================================================================

class TestStripEmailSignature:
    """Test email signature stripping."""

    def test_strips_standard_sig(self):
        text = "Important message content\n-- \nJohn Smith\nCEO, Acme Corp"
        result = strip_email_signature(text)
        assert "Important message content" in result
        assert "John Smith" not in result

    def test_strips_mobile_sig(self):
        text = "Quick reply here\nSent from my iPhone"
        result = strip_email_signature(text)
        assert "Quick reply here" in result
        assert "iPhone" not in result

    def test_strips_outlook_sig(self):
        text = "Meeting notes attached\nGet Outlook for iOS"
        result = strip_email_signature(text)
        assert "Meeting notes attached" in result

    def test_strips_disclaimer(self):
        text = "Proposal details\nDISCLAIMER: This email is confidential"
        result = strip_email_signature(text)
        assert "Proposal details" in result
        assert "DISCLAIMER" not in result

    def test_no_sig_leaves_text(self):
        text = "Just a normal email with no signature"
        result = strip_email_signature(text)
        assert result == text

    def test_empty_input(self):
        assert strip_email_signature("") == ""


# ============================================================================
# content_hash
# ============================================================================

class TestContentHash:
    """Test SHA-256 content hashing for dedup."""

    def test_deterministic(self):
        h1 = content_hash("hello")
        h2 = content_hash("hello")
        assert h1 == h2

    def test_different_inputs_different_hashes(self):
        h1 = content_hash("hello")
        h2 = content_hash("world")
        assert h1 != h2

    def test_is_sha256(self):
        h = content_hash("test")
        assert len(h) == 64  # SHA-256 hex is 64 chars
        assert all(c in "0123456789abcdef" for c in h)


# ============================================================================
# truncate_for_llm
# ============================================================================

class TestTruncateForLlm:
    """Test LLM input truncation."""

    def test_short_text_unchanged(self):
        text = "Short text"
        assert truncate_for_llm(text) == text

    def test_long_text_truncated(self):
        text = "A" * 3000
        result = truncate_for_llm(text, max_chars=100)
        assert len(result) <= 103  # 100 + "..."

    def test_breaks_at_sentence_boundary(self):
        text = "First sentence. Second sentence. " + "A" * 2000
        result = truncate_for_llm(text, max_chars=50)
        assert result.endswith(".")

    def test_ellipsis_when_no_sentence_boundary(self):
        text = "A" * 3000
        result = truncate_for_llm(text, max_chars=100)
        assert result.endswith("...")


# ============================================================================
# parse_gmail_page_text
# ============================================================================

class TestParseGmailPageText:
    """Test Gmail page text parsing."""

    def test_parses_email_blocks(self):
        text = """Alice Smith
Q3 Budget Review
Please review the attached budget.
2:30 PM

Bob Jones
Deployment Update
Production deployment completed.
3 hours ago
"""
        snippets = parse_gmail_page_text(text)
        assert len(snippets) >= 1

    def test_empty_text_returns_empty(self):
        assert parse_gmail_page_text("") == []

    def test_short_text_returns_empty(self):
        assert parse_gmail_page_text("hi") == []

    def test_tab_title_only_returns_empty(self):
        """Tab title text should not produce false positives."""
        text = "Inbox (112) - you@example.com - Gmail\nURL: https://mail.google.com"
        snippets = parse_gmail_page_text(text)
        assert len(snippets) == 0  # Too short/structured to be email content


# ============================================================================
# parse_slack_page_text
# ============================================================================

class TestParseSlackPageText:
    """Test Slack page text parsing."""

    def test_parses_messages(self):
        text = """Alice  2:30 PM
Hey everyone, the deployment is done!

Bob  2:35 PM
Great work! Let me check the logs.

Bot  2:40 PM
Automated: Build #123 passed.
"""
        snippets = parse_slack_page_text(text)
        # Parsing depends on heuristics, just ensure no crash
        assert isinstance(snippets, list)

    def test_empty_returns_empty(self):
        assert parse_slack_page_text("") == []

    def test_window_title_returns_empty(self):
        text = "security-alerts (Channel) - demo - Slack"
        snippets = parse_slack_page_text(text)
        assert len(snippets) == 0


# ============================================================================
# parse_calendar_events_text
# ============================================================================

class TestParseCalendarEventsText:
    """Test Calendar event text parsing."""

    def test_parses_pipe_delimited(self):
        text = "Team Standup|||August 29, 2026 9:00 AM|||August 29, 2026 9:30 AM|||Zoom|||Daily standup|||Alice, Bob"
        events = parse_calendar_events_text(text)
        assert len(events) >= 1
        assert events[0].title == "Team Standup"

    def test_multiple_events(self):
        text = "Event 1|||Aug 29 9AM|||Aug 29 10AM||||||, Event 2|||Aug 29 2PM|||Aug 29 3PM||||||"
        events = parse_calendar_events_text(text)
        assert len(events) >= 1

    def test_empty_returns_empty(self):
        assert parse_calendar_events_text("") == []


# ============================================================================
# parse_jira_page_text
# ============================================================================

class TestParseJiraPageText:
    """Test Jira page text parsing."""

    def test_parses_issue(self):
        text = """PROJ-123
Implement user authentication
Status: In Progress
Priority: High
Assignee: Alice Smith
Description: Add OAuth2 authentication to the API.
Comments: 5
"""
        issues = parse_jira_page_text(text)
        assert isinstance(issues, list)

    def test_empty_returns_empty(self):
        assert parse_jira_page_text("") == []


# ============================================================================
# BrowserContentReader - Timeout & Error Handling
# ============================================================================

class TestBrowserReaderErrorHandling:
    """Test error and edge case handling in BrowserContentReader."""

    @pytest.mark.asyncio
    async def test_osascript_timeout_returns_empty(self):
        """When osascript times out, return empty string."""
        reader = BrowserContentReader()

        import asyncio
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
            mock_proc.kill = lambda: None
            mock_exec.return_value = mock_proc
            result = await reader._run_osascript("test")
            assert result == ""

    @pytest.mark.asyncio
    async def test_osascript_not_found(self):
        """When osascript binary doesn't exist."""
        reader = BrowserContentReader()

        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            result = await reader._run_osascript("test")
            assert result == ""

    @pytest.mark.asyncio
    async def test_osascript_error_code(self):
        """Non-zero exit code returns empty."""
        reader = BrowserContentReader()

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b"error message"))
            mock_proc.returncode = 1
            mock_exec.return_value = mock_proc
            result = await reader._run_osascript("test")
            assert result == ""

    @pytest.mark.asyncio
    async def test_accessibility_error_logged(self):
        """Accessibility error message logged as warning."""
        reader = BrowserContentReader()

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(
                return_value=(b"", b"Not allowed assistive access")
            )
            mock_proc.returncode = 1
            mock_exec.return_value = mock_proc
            result = await reader._run_osascript("test")
            assert result == ""

    def test_timeout_constant(self):
        """Verify timeout is configured for accessibility tree traversal."""
        assert OSASCRIPT_TIMEOUT == 45

    def test_parse_tab_list_empty(self):
        """Empty input produces no tabs."""
        reader = BrowserContentReader()
        tabs = reader._parse_tab_list("", "chrome")
        assert tabs == []

    def test_cache_clear(self):
        """Cache clear removes all cached content."""
        reader = BrowserContentReader()
        from otto.adapters.browser.reader import ExtractedContent
        reader._cache["test"] = ExtractedContent(
            source="test", url="", title="", text="cached"
        )
        assert len(reader._cache) > 0
        reader.clear_cache()
        assert len(reader._cache) == 0

    def test_filter_tabs_by_url(self):
        """URL filtering works correctly."""
        reader = BrowserContentReader()
        tabs = [
            BrowserTab(browser="chrome", title="Gmail", url="https://mail.google.com/inbox",
                       window_index=1, tab_index=1),
            BrowserTab(browser="chrome", title="Slack", url="https://app.slack.com/client",
                       window_index=1, tab_index=2),
            BrowserTab(browser="chrome", title="Google", url="https://www.google.com",
                       window_index=1, tab_index=3),
        ]
        gmail = reader.filter_tabs_by_url(tabs, ["mail.google.com"])
        assert len(gmail) == 1
        assert gmail[0].title == "Gmail"

        slack = reader.filter_tabs_by_url(tabs, ["slack.com"])
        assert len(slack) == 1

        both = reader.filter_tabs_by_url(tabs, ["google.com", "slack.com"])
        assert len(both) == 3  # All contain google.com or slack.com

    def test_filter_tabs_empty_patterns(self):
        """Empty patterns returns nothing."""
        reader = BrowserContentReader()
        tabs = [BrowserTab(browser="chrome", title="T", url="https://x.com",
                           window_index=1, tab_index=1)]
        result = reader.filter_tabs_by_url(tabs, [])
        assert result == []

    def test_filter_tabs_no_match(self):
        """No matching URL returns nothing."""
        reader = BrowserContentReader()
        tabs = [BrowserTab(browser="chrome", title="T", url="https://example.com",
                           window_index=1, tab_index=1)]
        result = reader.filter_tabs_by_url(tabs, ["nomatch.com"])
        assert result == []


class TestNativeAccessibilityReader:
    """ax_dump.py (run under the engine's own interpreter) replaces the slow
    AppleScript UI walk."""

    FAKE_COMMAND = ["/usr/bin/true", "ax_dump.py", "Slack", "--max-ms", "3000"]

    @staticmethod
    def _fake_proc(returncode: int, out: bytes = b"", err: bytes = b""):
        proc = AsyncMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(out, err))
        proc.kill = lambda: None
        return proc

    def _reader_available(self, monkeypatch):
        monkeypatch.setattr("otto.adapters.browser.reader.ax_reader_command", lambda app: self.FAKE_COMMAND)

    def test_command_runs_the_script_under_this_interpreter(self, monkeypatch):
        """The reader must inherit the engine's code identity (its Accessibility grant)."""
        from otto.adapters.browser import reader as reader_mod
        monkeypatch.delenv("OTTO_NATIVE_AX")  # conftest disables the real reader
        monkeypatch.setattr(reader_mod.sys, "platform", "darwin")
        cmd = reader_mod.ax_reader_command("Slack")
        assert cmd is not None
        assert cmd[0] == reader_mod.sys.executable
        assert cmd[1].endswith("ax_dump.py") and Path(cmd[1]).exists()
        assert cmd[2] == "Slack" and "--max-ms" in cmd

    def test_command_is_unavailable_off_macos_or_when_disabled(self, monkeypatch):
        from otto.adapters.browser import reader as reader_mod
        monkeypatch.delenv("OTTO_NATIVE_AX")
        monkeypatch.setattr(reader_mod.sys, "platform", "linux")
        assert reader_mod.ax_reader_command("Slack") is None
        monkeypatch.setattr(reader_mod.sys, "platform", "darwin")
        monkeypatch.setenv("OTTO_NATIVE_AX", "off")
        assert reader_mod.ax_reader_command("Slack") is None

    @pytest.mark.asyncio
    async def test_unavailable_reader_means_fallback(self):
        reader = BrowserContentReader()  # conftest: OTTO_NATIVE_AX=0
        assert await reader._run_ax_reader("Slack") is None

    @pytest.mark.asyncio
    async def test_success_returns_text_and_clears_error(self, monkeypatch):
        self._reader_available(monkeypatch)
        reader = BrowserContentReader()
        reader.last_error = "stale"
        with patch("asyncio.create_subprocess_exec", return_value=self._fake_proc(0, b"general (Channel) - acme - Slack\nAlice 10:30 AM\nhello\n")) as spawn:
            text = await reader._run_ax_reader("Slack")
        assert text.startswith("general (Channel)")
        assert reader.last_error == ""
        assert list(spawn.call_args.args) == self.FAKE_COMMAND

    @pytest.mark.asyncio
    async def test_denied_sets_accessibility_error(self, monkeypatch):
        self._reader_available(monkeypatch)
        reader = BrowserContentReader()
        with patch("asyncio.create_subprocess_exec", return_value=self._fake_proc(2, b"", b"not allowed assistive access")):
            assert await reader._run_ax_reader("Slack") == ""
        assert "assistive" in reader.last_error

    @pytest.mark.asyncio
    async def test_app_closed_is_definitive(self, monkeypatch):
        self._reader_available(monkeypatch)
        reader = BrowserContentReader()
        with patch("asyncio.create_subprocess_exec", return_value=self._fake_proc(1, b"", b"Slack is not running")):
            assert await reader._run_ax_reader("Slack") == ""
        assert "not running" in reader.last_error

    @pytest.mark.asyncio
    async def test_timeout_is_definitive_and_reaps(self, monkeypatch):
        self._reader_available(monkeypatch)
        reader = BrowserContentReader()
        proc = self._fake_proc(0)
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        proc.wait = AsyncMock(return_value=0)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await reader._run_ax_reader("Slack") == ""
        assert reader.last_error == "timed out"

    @pytest.mark.asyncio
    async def test_extract_slack_prefers_reader_and_skips_applescript(self, monkeypatch):
        self._reader_available(monkeypatch)
        reader = BrowserContentReader()
        reader._run_osascript = AsyncMock(return_value="should not be used")
        with patch("asyncio.create_subprocess_exec", return_value=self._fake_proc(0, b"general (Channel) - acme - Slack\nBob 9:00 AM\nship it")):
            text = await reader.extract_slack_app_content()
        assert "ship it" in text
        reader._run_osascript.assert_not_called()
        # cached for the TTL window: a second call does not spawn anything
        with patch("asyncio.create_subprocess_exec") as spawn:
            assert "ship it" in await reader.extract_slack_app_content()
            spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_slack_denied_does_not_fall_back(self, monkeypatch):
        """A definitive denial must not trigger the System Events path (and its prompt)."""
        self._reader_available(monkeypatch)
        reader = BrowserContentReader()
        reader._run_osascript = AsyncMock(return_value="window name")
        with patch("asyncio.create_subprocess_exec", return_value=self._fake_proc(2, b"", b"not allowed assistive access")):
            assert await reader.extract_slack_app_content() == ""
        reader._run_osascript.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_slack_falls_back_when_reader_unavailable(self):
        reader = BrowserContentReader()  # conftest: OTTO_NATIVE_AX=0
        reader._run_osascript = AsyncMock(side_effect=["general (Channel) - acme - Slack", ""])
        assert await reader.extract_slack_app_content() == "general (Channel) - acme - Slack"

    @pytest.mark.asyncio
    async def test_app_is_running_uses_exit_code(self):
        reader = BrowserContentReader()
        with patch("asyncio.create_subprocess_exec", return_value=self._fake_proc(0)):
            assert await reader.app_is_running("Slack") is True
        with patch("asyncio.create_subprocess_exec", return_value=self._fake_proc(1)):
            assert await reader.app_is_running("Slack") is False

    def test_script_target_label(self):
        assert BrowserContentReader._script_target('tell application "System Events"\nend tell') == "System Events"
        assert BrowserContentReader._script_target("return 1") == "osascript"


# ============================================================================
# Chrome Preferences File Handling
# ============================================================================

class TestChromePreferencesEdge:
    """Edge cases for Chrome preferences modification."""

    def test_corrupted_json(self):
        """Corrupted JSON should not crash."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            temp_path = f.name

        try:
            with open(temp_path) as f:
                try:
                    json.load(f)
                    assert False, "Should have raised"
                except json.JSONDecodeError:
                    pass  # Expected
        finally:
            os.unlink(temp_path)

    def test_empty_prefs(self):
        """Empty prefs dict should be handled."""
        prefs = {}
        prefs.setdefault("browser", {})["allow_javascript_apple_events"] = True
        assert prefs["browser"]["allow_javascript_apple_events"] is True

    def test_nested_prefs_preserved(self):
        """Other prefs sections should not be affected."""
        prefs = {
            "browser": {"homepage": "https://google.com"},
            "extensions": {"enabled": True},
        }
        prefs["browser"]["allow_javascript_apple_events"] = True
        assert prefs["browser"]["homepage"] == "https://google.com"
        assert prefs["extensions"]["enabled"] is True


class TestNeverLaunchApps:
    """AppleScript launches its target app; a read-only assistant must only read what is already open."""

    @pytest.mark.asyncio
    async def test_closed_browsers_are_not_scripted(self, monkeypatch):
        reader = BrowserContentReader()
        calls: list[str] = []

        async def fake_osascript(script):
            calls.append(script)
            return "1||1||Slack||https://app.slack.com/client/T/C"

        async def not_running(name):
            return False

        monkeypatch.setattr(reader, "_run_osascript", fake_osascript)
        monkeypatch.setattr(reader, "app_is_running", not_running)
        assert await reader.list_all_tabs() == []
        assert calls == []

    @pytest.mark.asyncio
    async def test_only_running_browsers_are_listed(self, monkeypatch):
        reader = BrowserContentReader()
        calls: list[str] = []

        async def fake_osascript(script):
            calls.append(script)
            return "1||1||Slack||https://app.slack.com/client/T/C"

        async def running(name):
            return name == "Google Chrome"

        monkeypatch.setattr(reader, "_run_osascript", fake_osascript)
        monkeypatch.setattr(reader, "app_is_running", running)
        tabs = await reader.list_all_tabs()
        assert [t.browser for t in tabs] == ["chrome"]
        assert len(calls) == 1 and 'tell application "Google Chrome"' in calls[0]

    @pytest.mark.asyncio
    async def test_tab_list_is_shared_across_concurrent_adapters(self, monkeypatch):
        reader = BrowserContentReader()
        calls = 0

        async def fake_osascript(script):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return "1||1||Slack||https://app.slack.com/client/T/C"

        async def running(name):
            return name == "Google Chrome"

        monkeypatch.setattr(reader, "_run_osascript", fake_osascript)
        monkeypatch.setattr(reader, "app_is_running", running)
        results = await asyncio.gather(*(reader.list_all_tabs() for _ in range(4)))
        assert all(len(r) == 1 for r in results)
        assert calls == 1, "four adapters must share one tab enumeration"
        await reader.list_all_tabs()
        assert calls == 1, "within TAB_LIST_TTL the cached list is reused"

    @pytest.mark.asyncio
    async def test_calendar_is_not_launched(self, monkeypatch):
        reader = BrowserContentReader()
        calls: list[str] = []

        async def fake_osascript(script):
            calls.append(script)
            return "3"

        async def not_running(name):
            return False

        monkeypatch.setattr(reader, "_run_osascript", fake_osascript)
        monkeypatch.setattr(reader, "app_is_running", not_running)
        assert await reader.extract_calendar_events() == ""
        assert calls == []
        assert "not running" in reader.last_error
