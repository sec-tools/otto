"""
Slack message-list parsing — the same header-based parser serves the
Slack.app Accessibility dump (one line per element) and the web client's
innerText (header on one line).

The fixture below mirrors what the native Accessibility reader (``ax_dump.py Slack``) really returns: window
title with Slack's unread marker, sidebar, toolbar, then messages laid out as
``sender / APP / time / body…``, then composer chrome.
"""
from datetime import datetime, timedelta, timezone

from otto.adapters.browser.slack_browser import BrowserSlackAdapter
from otto.utils.content_parser import (
    is_slack_chrome_line,
    parse_slack_message_lines,
    parse_slack_page_text,
)

AX_DUMP = """* security-alerts (Channel) - acme - Slack
Home
DMs
Activity
Files
Agents & tools
Admin
Upgrade Plan
Huddles
Directories
security-alerts
platform
alice
you
Slack
scanner
Connect apps
Messages
Add canvas
Canvas
List
Folder
Jump to first unread message (⌘J)
Mark as read (esc)
scanner
APP
8:19 AM
Scan complete — api
main
Duration : 380.2s
Raw leads: 34
Severity : 0 critical, 2 high, 0 medium
Report-eligible findings:
[HIGH 8.0]
Cross-audit pattern hit: Command Injection
Open on the platform
scanner
APP
12:03 PM
Scan complete — worker
main
Severity : 0 critical, 0 high, 0 medium
No report-eligible findings above the CVSS threshold.
Open the findings on the platform
Yesterday
alice
7:35 PM
Can someone look at the api findings before standup?
7:36 PM
Meeting moved to 3:00 PM by the way
Latest messages
80 characters remaining
Slack is trying to connect.
"""


class TestParseSlackMessageLines:
    def test_ax_dump_yields_clean_messages(self):
        lines = AX_DUMP.splitlines()[1:]          # the adapter strips the title line itself
        msgs = parse_slack_message_lines(lines, channel="#security-alerts")
        assert [m.sender for m in msgs] == ["scanner", "scanner", "alice", "alice"]
        # the "Yesterday" divider dates everything after it
        assert [m.time_str for m in msgs] == ["8:19 AM", "12:03 PM", "Yesterday at 7:35 PM", "Yesterday at 7:36 PM"]
        assert [m.is_bot for m in msgs] == [True, True, False, False]
        assert all(m.channel == "#security-alerts" for m in msgs)

    def test_body_keeps_short_lines_and_drops_chrome(self):
        msgs = parse_slack_message_lines(AX_DUMP.splitlines()[1:])
        first = msgs[0].text.splitlines()
        assert first[0] == "Scan complete — api"
        assert "main" in first                      # short lines inside a message survive
        assert "[HIGH 8.0]" in first
        assert "Open on the platform" in first
        assert "scanner" not in first and "APP" not in first
        last = msgs[-1].text
        assert "Latest messages" not in last and "characters remaining" not in last
        assert "Slack is trying to connect" not in last

    def test_sidebar_before_first_header_is_discarded(self):
        msgs = parse_slack_message_lines(AX_DUMP.splitlines()[1:])
        joined = "\n".join(m.text for m in msgs)
        for chrome in ("Home", "DMs", "Upgrade Plan", "Connect apps", "Mark as read"):
            assert chrome not in joined

    def test_collapsed_header_inherits_sender_and_prose_time_is_not_a_header(self):
        msgs = parse_slack_message_lines(AX_DUMP.splitlines()[1:])
        follow_up = msgs[-1]
        assert follow_up.sender == "alice"                   # only a time line — Slack collapsed the name
        assert follow_up.text == "Meeting moved to 3:00 PM by the way"

    def test_day_divider_is_not_content(self):
        msgs = parse_slack_message_lines(AX_DUMP.splitlines()[1:])
        assert "Yesterday" not in msgs[1].text

    def test_inline_headers_from_web_client(self):
        lines = ["Alice  10:30 AM", "Deployment is complete!", "Bob  10:32 AM", "Great work <@U123>!",
                 "GitHub APP 10:40 AM", "PR #12 merged"]
        msgs = parse_slack_message_lines(lines)
        assert [(m.sender, m.is_bot) for m in msgs] == [("Alice", False), ("Bob", False), ("GitHub", True)]
        assert msgs[0].text == "Deployment is complete!"

    def test_sentence_containing_time_is_not_a_sender(self):
        lines = ["Alice  10:30 AM", "Let's move the sync to 3:00 PM", "and grab coffee after"]
        msgs = parse_slack_message_lines(lines)
        assert len(msgs) == 1
        assert "3:00 PM" in msgs[0].text

    def test_no_headers_means_no_messages(self):
        assert parse_slack_message_lines(["security-alerts (Channel) - demo - Slack", "Home", "DMs"]) == []
        assert parse_slack_message_lines([]) == []

    def test_chrome_detection(self):
        assert is_slack_chrome_line("80 characters remaining")
        assert is_slack_chrome_line("Latest messages")
        assert is_slack_chrome_line("Message #general")
        assert not is_slack_chrome_line("Deployment is complete!")


class TestParseSlackPageText:
    def test_channel_line_is_not_a_sender(self):
        snippets = parse_slack_page_text("#engineering\nAlice  10:30 AM\nHello\nBob  10:32 AM\nWorld")
        assert [s.sender for s in snippets] == ["Alice", "Bob"]
        assert all(s.channel == "#engineering" for s in snippets)

    def test_legacy_name_containing_am_is_not_a_header(self):
        # "name" contains "am"; the old regex treated any such line as a header.
        text = "#sec\nnotify APP 9:00 AM\n• [HIGH 7.8] folder name traverses out of ./x via Join\nMore detail"
        snippets = parse_slack_page_text(text)
        assert len(snippets) == 1
        assert snippets[0].sender == "notify"
        assert snippets[0].text.startswith("• [HIGH 7.8] folder name")


class TestChannelFromTitle:
    def test_channel_with_unread_marker(self):
        assert BrowserSlackAdapter._channel_from_title("* security-alerts (Channel) - acme - Slack") == ("security-alerts", "acme")

    def test_dm_and_group(self):
        assert BrowserSlackAdapter._channel_from_title("alice (DM) - acme - Slack") == ("dm-alice", "acme")
        assert BrowserSlackAdapter._channel_from_title("• alice, bob (Group) - acme - Slack") == ("dm-alice, bob", "acme")

    def test_not_a_title(self):
        assert BrowserSlackAdapter._channel_from_title("Home") is None
        assert BrowserSlackAdapter._channel_from_title("Scan complete — api") is None


class TestTimestampFromTimeStr:
    def test_clock_time_earlier_today_is_today(self):
        now = datetime.now(timezone.utc)
        local_now = now.astimezone()
        earlier = (local_now - timedelta(hours=1)).strftime("%-I:%M %p")
        ts = BrowserSlackAdapter._timestamp_from_time_str(earlier, now)
        assert ts.astimezone().date() == local_now.date()
        assert abs((now - ts).total_seconds() - 3600) < 120

    def test_clock_time_later_than_now_is_yesterday(self):
        now = datetime.now(timezone.utc)
        local_now = now.astimezone()
        later = (local_now + timedelta(hours=2)).strftime("%-I:%M %p")
        ts = BrowserSlackAdapter._timestamp_from_time_str(later, now)
        assert ts < now
        assert ts.astimezone().date() == (local_now + timedelta(hours=2) - timedelta(days=1)).date()

    def test_month_day_format(self):
        now = datetime.now(timezone.utc)
        ts = BrowserSlackAdapter._timestamp_from_time_str("Aug 21st at 6:55:49 AM", now).astimezone()
        assert (ts.month, ts.day, ts.hour, ts.minute, ts.second) == (8, 21, 6, 55, 49)

    def test_yesterday_at(self):
        now = datetime.now(timezone.utc)
        ts = BrowserSlackAdapter._timestamp_from_time_str("Yesterday at 9:00 AM", now).astimezone()
        assert ts.date() == (now.astimezone() - timedelta(days=1)).date()
        assert (ts.hour, ts.minute) == (9, 0)

    def test_garbage_is_now(self):
        now = datetime.now(timezone.utc)
        assert BrowserSlackAdapter._timestamp_from_time_str("", now) == now
        assert BrowserSlackAdapter._timestamp_from_time_str("later today", now) == now
        assert BrowserSlackAdapter._timestamp_from_time_str("99:99 PM", now) == now


class TestProcessNativeContent:
    def _events(self):
        adapter = BrowserSlackAdapter(reader=None, workspace_id="t")
        events: list = []
        adapter._process_native_content(AX_DUMP, events)
        return adapter, events

    def test_channel_sender_and_bot_flag_come_from_the_dump(self):
        _, events = self._events()
        assert len(events) == 4
        assert {e.title for e in events} == {"#security-alerts"}
        assert [e.sender_name for e in events] == ["scanner", "scanner", "alice", "alice"]
        assert [e.is_auto_generated for e in events] == [True, True, False, False]
        assert all(e.raw_metadata["channel_name"] == "security-alerts" for e in events)
        assert all(e.raw_metadata["workspace"] == "acme" for e in events)
        assert events[0].raw_metadata["time"] == "8:19 AM"

    def test_window_title_never_becomes_a_message(self):
        _, events = self._events()
        assert not any("(Channel)" in e.plain_text for e in events)
        assert not any(e.plain_text.startswith("*") for e in events)

    def test_message_text_is_the_body_only(self):
        _, events = self._events()
        assert events[0].plain_text.startswith("Scan complete — api")
        assert "8:19 AM" not in events[0].plain_text
        assert "scanner" not in events[0].plain_text

    def test_timestamps_follow_the_header_time(self):
        _, events = self._events()
        assert events[0].timestamp.astimezone().strftime("%H:%M") == "08:19"
        yesterday = (datetime.now(timezone.utc).astimezone() - timedelta(days=1)).date()
        assert events[2].timestamp.astimezone().strftime("%H:%M") == "19:35"
        assert events[2].timestamp.astimezone().date() == yesterday

    def test_second_read_of_same_window_adds_nothing(self):
        adapter, events = self._events()
        again: list = []
        adapter._process_native_content(AX_DUMP, again)
        assert again == []

    def test_legacy_layout_still_parses(self):
        adapter = BrowserSlackAdapter(reader=None, workspace_id="t")
        events: list = []
        adapter._process_native_content(
            "Channel eng\nAug 21st at 10:00:00 AM\nGithub notify APP: PR merged into main - Added by integration bot",
            events,
        )
        assert len(events) == 1
        assert events[0].raw_metadata["channel_name"] == "eng"
        assert events[0].is_auto_generated

    def test_full_timestamps_date_older_messages_on_their_own_day(self):
        """Slack.app's day dividers have no text in the Accessibility tree (a
        "Jump to date" pill); the reader emits each time link's description
        instead ("Sep 7th at 10:47:28 AM"). A DM scrolled back a week must not
        be dated today and resurface as new every morning."""
        adapter = BrowserSlackAdapter(reader=None, workspace_id="t")
        events: list = []
        dump = (
            "* alice (DM) - acme - Slack\n"
            "alice\nSep 7th at 10:47:28 AM\nDo not touch the payments migration until Priya signs off\n"
            "Sep 7th at 10:47:37 AM\nMake sure to always look at the read side first\n"
            "alice\nYesterday at 5:36:50 PM\nIts retry logic is neat, worth a look\n"
            "alice\nToday at 9:38:16 AM\nPriya is the PM for billing, she might know\n"
        )
        adapter._process_native_content(dump, events)
        assert len(events) == 4
        local = [e.timestamp.astimezone() for e in events]
        year = datetime.now(timezone.utc).astimezone().year
        assert [(t.month, t.day, t.hour, t.minute, t.second) for t in local[:2]] == [
            (9, 7, 10, 47, 28), (9, 7, 10, 47, 37)]
        assert all(t.year == year for t in local[:2])
        today = datetime.now(timezone.utc).astimezone().date()
        assert local[2].date() == today - timedelta(days=1) and (local[2].hour, local[2].minute) == (17, 36)
        assert local[3].date() == today and (local[3].hour, local[3].minute, local[3].second) == (9, 38, 16)
        # the collapsed second message keeps alice as its sender
        assert [e.sender_name for e in events] == ["alice", "alice", "alice", "alice"]
        assert events[0].raw_metadata["time"] == "Sep 7th at 10:47:28 AM"

    def test_link_preview_controls_are_not_a_sender(self):
        """The unfurl under a shared link ends in a "Remove preview" button; the
        message after it is still the same person's, not one from "Remove preview"."""
        lines = [
            "alice", "Yesterday at 5:36:50 PM", "See if this can be used for the importer",
            "acme/csv-kit", "A small, dependency-free CSV toolkit",
            "Remove preview",
            "Yesterday at 5:37:17 PM", "Its retry logic is neat",
        ]
        msgs = parse_slack_message_lines(lines, channel="@alice")
        assert [m.sender for m in msgs] == ["alice", "alice"]
        assert msgs[1].text == "Its retry logic is neat"
        assert "Remove preview" not in msgs[0].text


# ---------------------------------------------------------------------------
# Slack *web client* (Chrome tab innerText) — different layout, same parser
# ---------------------------------------------------------------------------

WEB_TEXT = """Search acme
A
Home
1
DMs
2
Activity
Files
Agents & tools
Admin
acme
Upgrade Plan
Huddles
Directories
Starred
Drag and drop important stuff here
Channels
security-alerts
platform
Direct messages
aliceyou
Agents & apps
Slack
1
scanner
Connect apps
Slack works better when you use it together.
Invite teammates
security-alerts
Invite teammates
Messages
Add canvas
Canvas
List
Folder
Wednesday, September 2nd
7:36
New finding: Empty dashboard token fail-open (CVSS 8.7) in https://github.com/example/repo
GitHub
GitHub - example/repo: Example project.
GitHub | Added by scanner
7:37
 Scan complete — repo (main)
Duration : 1406.8s
Severity : 0 critical, 11 high, 0 medium

Report-eligible findings:
• [HIGH 8.7] Empty dashboard token fail-open
• [HIGH 8.2] Arbitrary Class Instantiation via Unsafe Filter Dispatch
 Open the full report notebook
scanner
APP\xa0\xa010:57 AM

 Scan complete — sandbox (main)
Duration : 182.0s
Severity : 4 critical, 2 high, 0 medium

Report-eligible findings:
• [CRITICAL 9.8] Remote Code Execution via Unauthenticated Runtime Spec Execution
 Open the full report notebook




Message security-alerts
Shift + Return to add a new line
loading…
Slack is trying to connect.
"""


class TestSlackWebLayout:
    def test_three_messages_with_dates_and_badge_time_line(self):
        msgs = parse_slack_page_text(WEB_TEXT)
        assert len(msgs) == 3
        assert [m.time_str for m in msgs] == [
            "September 2nd at 7:36", "September 2nd at 7:37", "September 2nd at 10:57 AM",
        ]
        assert msgs[2].sender == "scanner" and msgs[2].is_bot       # "APP  10:57 AM" on one line
        assert msgs[0].sender == "" and msgs[1].sender == ""          # header scrolled out of view

    def test_workspace_initial_and_badges_are_not_a_channel(self):
        msgs = parse_slack_page_text(WEB_TEXT)
        assert all(m.channel == "Unknown" for m in msgs)              # no "#D"-style channel from the avatar letter

    def test_composer_and_footer_chrome_excluded(self):
        msgs = parse_slack_page_text(WEB_TEXT)
        last = msgs[-1].text
        for chrome in ("Message security-alerts", "Shift + Return", "loading", "trying to connect"):
            assert chrome not in last
        assert last.rstrip().endswith("Open the full report notebook")

    def test_bodies_are_intact(self):
        msgs = parse_slack_page_text(WEB_TEXT)
        assert msgs[1].text.startswith("Scan complete — repo (main)")
        assert "[HIGH 8.2] Arbitrary Class Instantiation" in msgs[1].text
        assert "[CRITICAL 9.8]" in msgs[2].text

    def test_month_day_time_resolves_to_that_date(self):
        ts = BrowserSlackAdapter._timestamp_from_time_str("September 2nd at 7:36").astimezone()
        assert (ts.month, ts.day, ts.hour, ts.minute) == (9, 2, 7, 36)


class TestTabPathUsesTitle:
    def test_channel_comes_from_tab_title_and_bot_from_text(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from otto.adapters.browser.reader import BrowserTab, ExtractedContent

        reader = MagicMock()
        tab = BrowserTab("chrome", "security-alerts (Channel) - acme - Slack", "https://app.slack.com/client/T1/C1")
        reader.list_all_tabs = AsyncMock(return_value=[tab])
        reader.filter_tabs_by_url = MagicMock(return_value=[tab])
        reader.extract_tab_content = AsyncMock(return_value=ExtractedContent("chrome", tab.url, tab.title, WEB_TEXT))
        reader.extract_slack_app_content = AsyncMock(return_value="")
        reader.last_error = ""

        adapter = BrowserSlackAdapter(reader=reader, workspace_id="t")
        adapter._connected = True
        events = asyncio.run(adapter.poll(datetime.now(timezone.utc)))
        assert len(events) == 3
        assert {e.title for e in events} == {"#security-alerts"}
        assert all(e.raw_metadata["channel_name"] == "security-alerts" for e in events)
        assert all(e.raw_metadata["extraction_mode"] == "browser_tab" for e in events)
        assert all(e.is_auto_generated for e in events)              # "Added by scanner" / "Scan complete" / APP badge
        assert events[0].timestamp.astimezone().month == 9 and events[0].timestamp.astimezone().day == 2


# ---------------------------------------------------------------------------
# Thread panel: parent, "1 reply", then replies with relative stamps. The
# channel view shows the same parent with a "1 reply / <last reply time> /
# View thread" summary underneath.
# ---------------------------------------------------------------------------

THREAD_DUMP = """security-alerts (Channel) - acme - Slack
Home
DMs
security-alerts
carol
you
scanner
Message security-alerts
Today
scanner
APP
4:54 PM
Report 10 generated with 0 findings
1 reply
Today at 7:15 PM
View thread
Thread
Close
scanner
APP
Today at 4:54 PM
Report 10 generated with 0 findings
1 reply
carol
4 minutes ago
build failed twice this morning, could be an upstream issue with the runner image
carol
Just now
second thought: it is the runner image, not us
Reply…
Also send to
security-alerts
"""


class TestThreadPanel:
    def test_thread_composer_chrome_never_becomes_message_text(self):
        """The "Also send to #channel" checkbox and its AX label sit right under the last reply."""
        lines = ["scanner", "APP", "9:40 AM", "Report 10 generated with 0 findings", "1 reply",
                 "carol", "4 minutes ago", "build failed twice this morning, could be an upstream issue",
                 "Also send to security-alerts", "Channel security-alerts", "Reply…"]
        msgs = parse_slack_message_lines(lines, channel="#security-alerts")
        assert [m.text for m in msgs] == ["Report 10 generated with 0 findings",
                                          "build failed twice this morning, could be an upstream issue"]
        assert msgs[1].reply_to == 0
        from otto.utils.content_parser import is_slack_chrome_line
        assert is_slack_chrome_line("Also send to #eng") and is_slack_chrome_line("Channel eng")
        assert not is_slack_chrome_line("channel strategy for next quarter")

    def test_relative_stamps_are_headers_and_replies_point_at_the_parent(self):
        lines = THREAD_DUMP.splitlines()[1:]
        msgs = parse_slack_message_lines(lines, channel="#security-alerts")
        texts = [m.text for m in msgs]
        assert texts == [
            "Report 10 generated with 0 findings",
            "Report 10 generated with 0 findings",
            "build failed twice this morning, could be an upstream issue with the runner image",
            "second thought: it is the runner image, not us",
        ]
        assert [m.sender for m in msgs] == ["scanner", "scanner", "carol", "carol"]
        assert [m.time_str for m in msgs] == ["Today at 4:54 PM", "Today at 4:54 PM", "4 minutes ago", "Just now"]
        assert [m.is_bot for m in msgs] == [True, True, False, False]
        # channel-view copy: not a thread parent (summary + View thread); panel copy: parent of both replies
        assert [m.reply_to for m in msgs] == [-1, -1, 1, 1]

    def test_last_reply_time_and_composer_echo_are_not_content(self):
        lines = THREAD_DUMP.splitlines()[1:]
        msgs = parse_slack_message_lines(lines, channel="#security-alerts")
        joined = "\n".join(m.text for m in msgs)
        assert "7:15 PM" not in joined and "View thread" not in joined
        assert "Also send to" not in joined and "\nsecurity-alerts" not in joined
        assert "Reply…" not in joined

    def test_channel_view_summary_does_not_open_a_thread(self):
        lines = [
            "alice", "9:00 AM", "Anyone free to pair on the migration?",
            "3 replies", "Last reply today at 9:40 AM",
            "bob", "10:00 AM", "Deploy is done.",
        ]
        msgs = parse_slack_message_lines(lines, channel="#eng")
        assert [m.reply_to for m in msgs] == [-1, -1]

    def test_loading_placeholder_is_chrome(self):
        assert is_slack_chrome_line("Loading messages for eng…")
        assert is_slack_chrome_line("Loading more replies")
        assert not is_slack_chrome_line("Loading the new dataset took 3 hours")

    def test_prose_ending_in_ago_is_not_a_header(self):
        lines = ["alice", "9:00 AM", "Build finished 10 minutes ago", "and it is green", "bob", "9:05 AM", "nice"]
        msgs = parse_slack_message_lines(lines, channel="#eng")
        assert [m.text for m in msgs] == ["Build finished 10 minutes ago\nand it is green", "nice"]


class TestRelativeTimes:
    def test_minutes_ago_is_floored_to_the_minute(self):
        now = datetime(2026, 9, 11, 19, 19, 35, tzinfo=timezone.utc)
        ts = BrowserSlackAdapter._timestamp_from_time_str("4 minutes ago", now)
        assert ts == datetime(2026, 9, 11, 19, 15, 0, tzinfo=timezone.utc)
        assert BrowserSlackAdapter._timestamp_from_time_str("Just now", now) == now.replace(second=0)
        assert BrowserSlackAdapter._timestamp_from_time_str("an hour ago", now) == datetime(2026, 9, 11, 18, 19, tzinfo=timezone.utc)
        assert BrowserSlackAdapter._timestamp_from_time_str("2 days ago", now).day == 9

    def test_same_message_keeps_one_id_as_slack_re_renders_its_time(self):
        """"Just now" → "4 minutes ago" → "7:15 PM": one message, one id, one timestamp."""
        from otto.adapters.browser import slack_browser as sb

        def read(stamp: str):
            adapter = BrowserSlackAdapter(reader=None, workspace_id="t")   # a fresh adapter, like every refresh
            events: list = []
            adapter._process_native_content(
                f"eng (Channel) - acme - Slack\nMessage eng\ncarol\n{stamp}\nrunner image is the culprit, fix incoming",
                events,
            )
            return events

        first = read("Just now")
        sb._GLOBAL_SLACK_CHANNEL_CACHE.clear()
        later = read("4 minutes ago")
        sb._GLOBAL_SLACK_CHANNEL_CACHE.clear()
        clock = read(datetime.now().astimezone().strftime("%-I:%M %p"))
        assert len(first) == len(later) == len(clock) == 1
        assert first[0].source_id == later[0].source_id == clock[0].source_id
        assert first[0].timestamp == later[0].timestamp == clock[0].timestamp

    def test_channel_view_and_panel_copies_of_the_parent_are_one_event(self):
        adapter = BrowserSlackAdapter(reader=None, workspace_id="t")
        events: list = []
        adapter._process_native_content(THREAD_DUMP, events)
        texts = [e.plain_text for e in events]
        assert texts.count("Report 10 generated with 0 findings") == 1
        assert len(events) == 3

    def test_replies_join_the_parent_conversation(self):
        adapter = BrowserSlackAdapter(reader=None, workspace_id="t")
        events: list = []
        adapter._process_native_content(THREAD_DUMP, events)
        parent, reply1, reply2 = events
        assert parent.thread_id is None
        assert reply1.thread_id == parent.source_id and reply2.thread_id == parent.source_id
        assert reply1.raw_metadata["reply_to"] == parent.source_id
        assert reply1.sender_name == "carol" and not reply1.is_auto_generated
        assert reply1.timestamp > parent.timestamp

    def test_visible_channel_is_remembered_for_thumbnails(self):
        from otto.adapters.browser import slack_browser as sb

        adapter = BrowserSlackAdapter(reader=None, workspace_id="t")
        adapter._process_native_content(THREAD_DUMP, [])
        assert sb.visible_channel() == "#security-alerts"
        sb.clear_slack_channel_cache()
        assert sb.visible_channel() == ""
