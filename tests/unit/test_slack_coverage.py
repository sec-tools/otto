"""
Slack without a token: every window is read, and the sidebar tells Otto what
is waiting in conversations it cannot see.

Fixtures are anonymised (workspace ``acme``, channels ``ops``/``leads``/``eng``,
people ``alice``/``bob``/``carol``).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.adapters.browser import slack_browser
from otto.adapters.browser.reader import BrowserContentReader, SlackAppRead
from otto.adapters.browser.slack_browser import (
    BrowserSlackAdapter,
    clear_slack_channel_cache,
    coverage_sentence,
    slack_coverage,
    visible_channel,
)

MAIN_WINDOW = """ops (Channel) - acme - Slack
Home
DMs
ops
leads
alice
Message ops
Today
alice
9:00 AM
deploy is done, please check the dashboards
"""

THREAD_WINDOW = """Thread - eng - acme - Slack
Thread
bob
9:05 AM
the runner image needs a rebuild before friday
"""

SIDEBAR = [
    {"name": "ops", "section": "Channels", "dm": False, "unread": False, "badge": False, "selected": True, "muted": False, "self": False},
    {"name": "leads", "section": "Channels", "dm": False, "unread": True, "badge": 2, "selected": False, "muted": False, "self": False},
    {"name": "random", "section": "Channels", "dm": False, "unread": True, "badge": False, "selected": False, "muted": True, "self": False},
    {"name": "alice", "section": "Direct messages", "dm": True, "unread": True, "badge": True, "selected": False, "muted": False, "self": False},
    {"name": "carol", "section": "Direct messages", "dm": True, "unread": False, "badge": False, "selected": False, "muted": False, "self": False},
    {"name": "Sam", "section": "Direct messages", "dm": True, "unread": True, "badge": True, "selected": False, "muted": False, "self": True},
    {"name": "notify", "section": "Agents & apps", "dm": False, "unread": True, "badge": False, "selected": False, "muted": False, "self": False},
]


def _payload(**over) -> dict:
    payload = {
        "app": "Slack",
        "windows": [{"title": "ops (Channel) - acme - Slack", "text": MAIN_WINDOW},
                    {"title": "Thread - eng - acme - Slack", "text": THREAD_WINDOW}],
        "conversations": SIDEBAR,
        "announced": False, "truncated": False, "nodes": 300, "ms": 120,
    }
    payload.update(over)
    return payload


@pytest.fixture(autouse=True)
def _fresh():
    clear_slack_channel_cache()
    yield
    clear_slack_channel_cache()


# ---------------------------------------------------------------------------
# Payload → SlackAppRead
# ---------------------------------------------------------------------------

class TestSlackAppRead:
    def test_from_payload_normalises(self):
        read = SlackAppRead.from_payload(_payload(conversations=SIDEBAR + [{"name": "  ", "unread": True}, "junk"]))
        assert [t for t, _ in read.windows] == ["ops (Channel) - acme - Slack", "Thread - eng - acme - Slack"]
        assert read.text.startswith("ops (Channel)")
        names = [c["name"] for c in read.conversations]
        assert names == ["ops", "leads", "random", "alice", "carol", "Sam", "notify"]   # nameless / non-dict dropped
        leads = read.conversations[1]
        assert leads["badge"] == 2 and leads["unread"] is True and leads["dm"] is False
        alice = read.conversations[3]
        assert alice["badge"] is True and alice["dm"] is True

    def test_empty_read(self):
        assert SlackAppRead().text == "" and SlackAppRead().windows == []


# ---------------------------------------------------------------------------
# The reader's structured read
# ---------------------------------------------------------------------------

class TestReaderStructuredRead:
    FAKE = ["/usr/bin/true", "ax_dump.py", "Slack", "--max-ms", "3000"]

    @staticmethod
    def _proc(code: int, out: bytes = b"", err: bytes = b""):
        proc = AsyncMock()
        proc.returncode = code
        proc.communicate = AsyncMock(return_value=(out, err))
        proc.kill = lambda: None
        return proc

    @pytest.mark.asyncio
    async def test_unavailable_reader_returns_none(self):
        reader = BrowserContentReader()          # conftest: OTTO_NATIVE_AX=0
        assert await reader.extract_slack_app() is None

    @pytest.mark.asyncio
    async def test_json_is_parsed_cached_and_seeds_the_text_cache(self, monkeypatch):
        monkeypatch.setattr("otto.adapters.browser.reader.ax_reader_command", lambda app: list(self.FAKE))
        reader = BrowserContentReader()
        body = json.dumps(_payload(announced=True)).encode("utf-8")
        with patch("asyncio.create_subprocess_exec", return_value=self._proc(0, body)) as spawn:
            read = await reader.extract_slack_app()
        assert list(spawn.call_args.args) == self.FAKE + ["--json"]
        assert isinstance(read, SlackAppRead) and len(read.windows) == 2 and read.announced is True
        assert len(read.conversations) == 7
        assert reader.last_error == ""
        # the text read is served from the same result: nothing is spawned twice
        with patch("asyncio.create_subprocess_exec") as spawn:
            assert (await reader.extract_slack_app_content()).startswith("ops (Channel)")
            assert await reader.extract_slack_app() is read
            spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_closed_or_denied_is_an_empty_read_not_none(self, monkeypatch):
        monkeypatch.setattr("otto.adapters.browser.reader.ax_reader_command", lambda app: list(self.FAKE))
        reader = BrowserContentReader()
        with patch("asyncio.create_subprocess_exec", return_value=self._proc(1, b"", b"Slack is not running")):
            read = await reader.extract_slack_app()
        assert isinstance(read, SlackAppRead) and read.windows == []
        assert "not running" in reader.last_error
        with patch("asyncio.create_subprocess_exec", return_value=self._proc(2, b"", b"not allowed assistive access")):
            read = await reader.extract_slack_app()
        assert read.windows == [] and "assistive" in reader.last_error

    @pytest.mark.asyncio
    async def test_garbage_output_is_an_empty_read(self, monkeypatch):
        monkeypatch.setattr("otto.adapters.browser.reader.ax_reader_command", lambda app: list(self.FAKE))
        reader = BrowserContentReader()
        with patch("asyncio.create_subprocess_exec", return_value=self._proc(0, b"not json")):
            read = await reader.extract_slack_app()
        assert read.windows == [] and "unreadable" in reader.last_error


# ---------------------------------------------------------------------------
# Coverage from the sidebar
# ---------------------------------------------------------------------------

class TestSlackCoverage:
    def test_unseen_is_unread_minus_on_screen_muted_and_self(self):
        read = SlackAppRead.from_payload(_payload())
        cov = slack_coverage(read, {"ops", "dm-carol"})
        titles = [u["title"] for u in cov["unseen"]]
        # mentions first (leads has 2, alice a badge), then plain unread; muted #random and the self-DM never appear
        assert titles == ["#leads", "@alice", "#notify"]
        assert cov["unseen"][0]["mention"] is True and cov["unseen"][0]["badge"] == 2
        assert cov["unseen"][1]["mention"] is True and cov["unseen"][1]["badge"] == 0
        assert cov["unseen"][2]["mention"] is False
        assert all(u["url"].startswith(("slack://", "https://")) for u in cov["unseen"])
        assert cov["listed"] == 6                 # the self-DM is not a conversation with anyone
        assert cov["read"] == ["#ops", "@carol"]
        assert cov["unread"] == 3 and cov["mentions"] == 2
        assert cov["windows"] == 2

    def test_dm_by_section_when_the_dm_flag_is_missing(self):
        read = SlackAppRead(conversations=[{"name": "bob", "section": "Direct messages", "unread": True}])
        cov = slack_coverage(read, set())
        assert cov["unseen"][0]["title"] == "@bob" and cov["unseen"][0]["key"] == "dm-bob"

    def test_sentence(self):
        read = SlackAppRead.from_payload(_payload())
        assert coverage_sentence(slack_coverage(read, {"ops"})) == "1 of 6 conversations read · 3 unread not opened (2 mention you)"
        assert coverage_sentence(slack_coverage(SlackAppRead(), {"ops", "eng"})) == "2 conversations read"
        assert coverage_sentence({}) == "" and coverage_sentence(None) == ""
        assert coverage_sentence({"listed": 0, "read": [], "unseen": []}) == ""


# ---------------------------------------------------------------------------
# The adapter: every window, then coverage
# ---------------------------------------------------------------------------

class TestAdapterReadsEveryWindow:
    def _adapter(self, read):
        reader = MagicMock(spec=BrowserContentReader)
        reader.last_error = ""
        reader.extract_slack_app = AsyncMock(return_value=read)
        reader.extract_slack_app_content = AsyncMock(return_value="")
        reader.list_all_tabs = AsyncMock(return_value=[])
        reader.filter_tabs_by_url.return_value = []
        reader.app_is_running = AsyncMock(return_value=True)
        adapter = BrowserSlackAdapter(reader, "acme")
        adapter._connected = True
        return adapter, reader

    @pytest.mark.asyncio
    async def test_messages_from_both_windows_and_coverage(self):
        read = SlackAppRead.from_payload(_payload())
        adapter, reader = self._adapter(read)
        events = await adapter.poll(datetime.now(timezone.utc))
        texts = {e.plain_text for e in events}
        assert any("dashboards" in t for t in texts)          # main window
        assert any("runner image" in t for t in texts)        # popped-out thread window
        channels = {e.raw_metadata.get("channel_name") for e in events}
        assert "ops" in channels
        assert visible_channel() == "#ops"                     # thumbnails follow the main window
        reader.extract_slack_app_content.assert_not_called()
        assert [u["title"] for u in adapter.coverage["unseen"]] == ["#leads", "@alice", "#notify"]
        assert "#ops" in adapter.coverage["read"]

    @pytest.mark.asyncio
    async def test_text_only_reader_still_works(self):
        """A reader without the structured read (or a mock of one) falls back to the text read."""
        reader = MagicMock(spec=BrowserContentReader)
        reader.last_error = ""
        reader.extract_slack_app_content = AsyncMock(return_value=MAIN_WINDOW)
        reader.list_all_tabs = AsyncMock(return_value=[])
        reader.filter_tabs_by_url.return_value = []
        adapter = BrowserSlackAdapter(reader, "acme")
        adapter._connected = True
        events = await adapter.poll(datetime.now(timezone.utc))
        assert any("dashboards" in e.plain_text for e in events)
        assert adapter.coverage["read"] == ["#ops"] and adapter.coverage["unseen"] == []

    @pytest.mark.asyncio
    async def test_remembered_channels_count_as_read(self):
        read = SlackAppRead.from_payload(_payload())
        adapter, _reader = self._adapter(read)
        await adapter.poll(datetime.now(timezone.utc))
        # a second refresh showing only the thread window: #ops is remembered, still "read"
        adapter2, _r = self._adapter(SlackAppRead.from_payload(_payload(windows=[{"title": "Thread - eng - acme - Slack", "text": THREAD_WINDOW}])))
        await adapter2.poll(datetime.now(timezone.utc))
        assert "#ops" in adapter2.coverage["read"]


# ---------------------------------------------------------------------------
# Into the briefing: radar rows + a note, page and panel
# ---------------------------------------------------------------------------

class TestUnreadInTheBriefing:
    def _status(self, coverage, api_ok=False):
        st = [{"source": "slack", "ok": True, "items": 3, "error": "", "coverage": coverage}]
        if api_ok:
            st.append({"source": "slack api", "ok": True, "items": 40, "error": ""})
        return st

    def _coverage(self):
        return slack_coverage(SlackAppRead.from_payload(_payload()), {"ops"})

    def test_rows_and_note(self):
        from otto.web.collect import _add_unread_slack
        radar = {"attention": ["#eng is busier than usual: 40 messages this week vs 12 last week."]}
        _add_unread_slack(radar, self._status(self._coverage()))
        rows = radar["unread"]
        assert [r["what"] for r in rows] == ["#leads", "@alice", "#notify"]
        assert rows[0]["channel"] == "2 mentions" and rows[1]["channel"] == "mentions you" and rows[2]["channel"] == "unread"
        assert all(r["id"].startswith("unread:") and r["url"] for r in rows)
        assert radar["attention"][0] == "Slack shows unread in #leads, @alice, #notify — not opened yet, so nothing from there is in this briefing."
        # stable ids: the same conversation dismisses the same row next refresh
        again = {}
        _add_unread_slack(again, self._status(self._coverage()))
        assert [r["id"] for r in again["unread"]] == [r["id"] for r in rows]

    def test_nothing_added_with_the_api_reader_or_without_unseen(self):
        from otto.web.collect import _add_unread_slack
        radar: dict = {}
        _add_unread_slack(radar, self._status(self._coverage(), api_ok=True))
        assert radar == {}
        _add_unread_slack(radar, self._status(slack_coverage(SlackAppRead(), {"ops"})))
        assert radar == {}
        _add_unread_slack(radar, [])
        assert radar == {}

    def test_more_than_three_are_summarised(self):
        from otto.web.collect import _add_unread_slack
        many = [dict(c, name=f"chan{i}", unread=True, selected=False) for i, c in enumerate(SIDEBAR[:1] * 6)]
        cov = slack_coverage(SlackAppRead(conversations=many), set())
        radar: dict = {}
        _add_unread_slack(radar, self._status(cov))
        assert len(radar["unread"]) == 6
        assert radar["attention"][0].startswith("Slack shows unread in #chan0, #chan1, #chan2 and 3 more —")

    def test_page_and_panel_render_the_section(self):
        from otto.web.collect import _add_unread_slack
        from otto.web.items import build_items
        from otto.web.render import render_worth_knowing
        radar: dict = {}
        _add_unread_slack(radar, self._status(self._coverage()))
        data = {"radar": radar, "sections": [], "source_status": self._status(self._coverage())}
        html = render_worth_knowing(data)              # the Worth knowing drawer: the note, then the section
        assert "Unread in Slack" in html and "#leads" in html and 'class="rlink"' in html
        assert html.count("Slack shows unread in") == 1 and 'class="rr note n-quiet"' in html
        payload = build_items(data)
        section = next(s for s in payload["radar"]["sections"] if s["key"] == "unread")
        assert section["heading"] == "Unread in Slack"
        assert [r["what"] for r in section["rows"]] == ["#leads", "@alice", "#notify"]
        assert payload["radar"]["attention"][0].startswith("Slack shows unread in")
        assert [n["kind"] for n in payload["worth_knowing"]["notes"]] == ["attention"]
        # dismissed rows stay dismissed
        html_hidden = render_worth_knowing(data, hidden=[section["rows"][0]["id"]])
        assert html.count('class="rr"') == 3 and html_hidden.count('class="rr"') == 2
        assert ">#leads</a>" not in html_hidden and ">@alice</a>" in html_hidden


class TestProcessAppReadDirect:
    def test_empty_windows_are_skipped_and_main_channel_noted(self):
        reader = MagicMock(spec=BrowserContentReader)
        adapter = BrowserSlackAdapter(reader, "acme")
        read = SlackAppRead(windows=[("", ""), ("ops (Channel) - acme - Slack", MAIN_WINDOW)], conversations=SIDEBAR)
        events: list = []
        adapter._process_app_read(read, events)
        assert events and visible_channel() == "#ops"
        assert adapter.coverage["read"] == ["#ops"]

    def test_process_native_content_returns_the_channel_key(self):
        reader = MagicMock(spec=BrowserContentReader)
        adapter = BrowserSlackAdapter(reader, "acme")
        assert adapter._process_native_content(MAIN_WINDOW, []) == "ops"
        assert adapter._process_native_content("", []) == ""
        assert adapter._process_native_content("alice (DM) - acme - Slack\nalice\n9:00 AM\nhey, got a minute?", []) == "dm-alice"


def test_sync_helpers():
    assert slack_browser._sidebar_key("#ops", False) == "ops"
    assert slack_browser._sidebar_key("alice", True) == "dm-alice"
    assert slack_browser._display("ops") == "#ops" and slack_browser._display("dm-alice") == "@alice"
    asyncio.run(asyncio.sleep(0))
