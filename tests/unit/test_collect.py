"""Collector tests: fake adapters in, briefing dict out — no LLM, no apps."""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from otto import paths
from otto.adapters.base import ConnectionStatus, RawEvent
from otto.storage.models import ConnectionState, ContentBlock, ContentType, SourceType
from otto.web import collect
from otto.web.collect import (
    capture_slack_screenshot,
    clean_ax_text,
    collect_briefing_data,
    is_noise,
    prune_screenshots,
)


def _event(text, *, source=SourceType.SLACK, title="#eng", sid=None, url="https://acme.slack.com/app_redirect?channel=eng",
           sender="Alice", bot=False, minutes_ago=5):
    return RawEvent(
        source=source,
        source_id=sid or f"id-{abs(hash(text))}",
        source_url=url,
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        title=title,
        content_blocks=[ContentBlock(type=ContentType.TEXT, text=text)],
        plain_text=text,
        sender_name=sender,
        is_auto_generated=bot,
        raw_metadata={"channel_name": title.lstrip("#")},
    )


class FakeAdapter:
    def __init__(self, events, *, name="browser_slack:test", fail=False, slow=0.0, state=ConnectionState.HEALTHY):
        self._events = events
        self.name = name
        self._fail = fail
        self._slow = slow
        self._state = state

    async def connect(self):
        if self._fail:
            raise RuntimeError("cannot connect")
        return ConnectionStatus(state=self._state)

    async def poll(self, since):
        if self._slow:
            await asyncio.sleep(self._slow)
        return list(self._events)


class NoLLM:
    def ensure_chat_provider(self):
        return False

    _providers: list = []


def test_end_to_end_without_llm():
    events = [
        _event("URGENT: production API is returning 500s, please investigate ASAP before the demo"),
        _event("Please review https://github.com/acme/audio-kit and see if it can be used in the next project",
               title="@alice", sender="You"),
        _event("Home", title="#eng", sid="noise-1"),  # UI noise → dropped
    ]
    data = collect_briefing_data(adapters=[FakeAdapter(events)], llm=NoLLM())

    assert data["ai_powered"] is False
    assert data["sources_polled"] == ["browser_slack:test"]
    assert data["total_items"] == 2
    assert data["refresh_seconds"] >= 0
    section = data["sections"][0]
    assert section["source"] == "slack"
    items = [i for ch in section["channels"] for i in ch["items"]]
    urgent = next(i for i in items if "production API" in i["text"])
    assert urgent["urgency"] in ("high", "critical")
    assert urgent["id"] and len(urgent["id"]) == 10
    assert urgent["source_url"].startswith("https://acme.slack.com/")
    repo = next(i for i in items if "audio-kit" in i["text"])
    assert repo["external_url"] == "https://github.com/acme/audio-kit"
    assert repo["link_intelligence"]["title"] == "acme/audio-kit"
    assert repo["action_items"]
    assert any(o["external_url"] == "https://github.com/acme/audio-kit" for o in data["opportunities"])
    # history was written to the isolated data dir
    assert (paths.history_dir() / "conversations.jsonl").exists()


def test_adapter_failure_and_timeout_are_contained(monkeypatch):
    monkeypatch.setattr(collect, "ADAPTER_POLL_TIMEOUT", 0.05)
    good = FakeAdapter([_event("Deploy finished successfully for the payments service, all green")], name="good")
    bad = FakeAdapter([], name="bad", fail=True)
    slow = FakeAdapter([_event("this should never arrive because the adapter is too slow")], name="slow", slow=1.0)
    started = time.monotonic()
    data = collect_briefing_data(adapters=[good, bad, slow], llm=NoLLM())
    assert time.monotonic() - started < 3
    assert data["total_items"] == 1
    assert set(data["sources_polled"]) == {"good", "bad", "slow"}
    # Per-source health so the UI can tell "nothing to report" from "couldn't read".
    by_name = {s["adapter"]: s for s in data["source_status"]}
    assert by_name["good"]["ok"] and by_name["good"]["items"] == 1
    assert not by_name["bad"]["ok"] and by_name["bad"]["state"] == "error"
    assert not by_name["slow"]["ok"] and by_name["slow"]["state"] == "timeout"
    assert "permission" in by_name["slow"]["error"]
    assert set(data["sources_failed"]) == {"bad", "slow"}


def test_source_status_explains_permission_failures():
    class Denied(FakeAdapter):
        async def connect(self):
            return ConnectionStatus(state=ConnectionState.FAILED, error="osascript is not allowed assistive access (-25211)")

    data = collect_briefing_data(adapters=[Denied([], name="browser_slack")], llm=NoLLM())
    st = data["source_status"][0]
    assert st["source"] == "slack"
    assert st["ok"] is False
    assert "Accessibility" in st["error"]
    assert data["sources_failed"] == ["slack"]


def test_denial_during_poll_is_not_reported_as_quiet():
    """Adapters swallow read errors and return []; the collector must still
    surface a permission denial recorded on the adapter as a failure."""
    class DeniedOnPoll(FakeAdapter):
        last_error = "osascript is not allowed assistive access (-1719)"

    class QuietButFine(FakeAdapter):
        last_error = ""

    class Closed(FakeAdapter):
        last_error = "Slack is not running"

    data = collect_briefing_data(
        adapters=[DeniedOnPoll([], name="browser_slack"), QuietButFine([], name="browser_gmail"), Closed([], name="browser_jira")],
        llm=NoLLM(),
    )
    by = {s["source"]: s for s in data["source_status"]}
    assert by["slack"]["ok"] is False and by["slack"]["state"] == "failed"
    assert "Accessibility" in by["slack"]["error"]
    assert by["gmail"]["ok"] is True and by["gmail"]["items"] == 0
    assert by["jira"]["ok"] is True  # closed ≠ denied; nothing to fix
    assert data["sources_failed"] == ["slack"]
    # ...and the denied source is backed off like any other permission failure
    assert collect._BACKOFF.should_skip("browser_slack")
    assert not collect._BACKOFF.should_skip("browser_jira")


def test_source_label_and_error_explanations():
    assert collect._source_label("browser_slack") == "slack"
    assert collect._source_label("native_calendar") == "calendar"
    assert collect._source_label("jira") == "jira"
    assert collect._source_label("browser_gmail:") == "gmail"
    assert collect._source_label("browser_slack:acme") == "slack"
    assert "Automation" in collect._explain_adapter_error("Not authorized to send Apple events (-1743)")
    assert "app not open" == collect._explain_adapter_error("No Slack tabs or native app found")
    assert collect._explain_adapter_error("") == "unavailable"


def test_duplicates_are_consolidated():
    events = [
        _event("Nightly scan complete — api Duration : 81.3s Raw leads: 33 Confirmed: 0 Qualified: 0", sid="s1", bot=True),
        _event("Nightly scan complete — api Duration : 69.3s Raw leads: 31 Confirmed: 1 Qualified: 0", sid="s2", bot=True),
    ]
    data = collect_briefing_data(adapters=[FakeAdapter(events)], llm=NoLLM())
    items = [i for ch in data["sections"][0]["channels"] for i in ch["items"]]
    assert len(items) == 1
    assert items[0]["occurrence_count"] == 2


def test_unsafe_source_urls_are_stripped():
    ev = _event("Click here for something interesting about the quarterly planning", url="javascript:alert(1)")
    data = collect_briefing_data(adapters=[FakeAdapter([ev])], llm=NoLLM())
    items = [i for ch in data["sections"][0]["channels"] for i in ch["items"]]
    assert items[0]["source_url"] == ""


def test_classification_cache_is_used_for_llm(monkeypatch):
    """With an LLM present, unchanged content must not trigger a second LLM call."""
    from otto.intelligence.classification_cache import ClassificationCache
    from otto.intelligence.classifier import ConversationClassifier

    calls = {"classify": 0, "actions": 0}

    async def fake_classify(self, conv, events, recent_context="", **kwargs):
        calls["classify"] += 1
        conv.summary = "LLM summary"
        conv.urgency = 0.9
        conv.llm_enriched = True
        return conv

    async def fake_actions(self, conv, events):
        calls["actions"] += 1
        return []

    monkeypatch.setattr(ConversationClassifier, "classify_llm", fake_classify)
    monkeypatch.setattr(ConversationClassifier, "extract_actions", fake_actions)

    class FakeLLM:
        _providers: list = []

        def ensure_chat_provider(self):
            return True

    cache = ClassificationCache()
    events = [_event("Please make sure the invoice batch runs before Friday, finance is waiting on it")]
    d1 = collect_briefing_data(adapters=[FakeAdapter(events)], llm=FakeLLM(), classification_cache=cache)
    assert d1["ai_powered"] is True
    assert calls["classify"] == 1 and d1["llm_calls"] >= 1
    d2 = collect_briefing_data(adapters=[FakeAdapter(events)], llm=FakeLLM(), classification_cache=cache)
    assert calls["classify"] == 1, "second refresh must hit the cache"
    assert d2["llm_calls"] == 0
    items = [i for ch in d2["sections"][0]["channels"] for i in ch["items"]]
    assert items[0]["summary"] == "LLM summary"


class TestRecall:
    """The briefing is built from memory inside the window, not from what
    happens to be on screen this minute — no flicker when a tab closes."""

    def test_items_survive_the_source_going_out_of_view(self):
        ev = _event("Production deploy failed on step 4, rolling back now please check", title="#ops", sender="alice",
                    sid="ops-1", minutes_ago=30)
        live = collect_briefing_data(adapters=[FakeAdapter([ev])], llm=NoLLM())
        assert live["total_items"] == 1 and live["recalled"] == 0
        live_item = live["sections"][0]["channels"][0]["items"][0]

        gone = collect_briefing_data(adapters=[FakeAdapter([])], llm=NoLLM())     # Slack scrolled away / tab closed
        assert gone["recalled"] == 1 and gone["total_items"] == 1
        item = gone["sections"][0]["channels"][0]["items"][0]
        assert item["id"] == live_item["id"], "same id → a dismissal made while live still applies"
        assert item["text"] == live_item["text"] and item["sender"] == "alice"
        assert gone["sections"][0]["channels"][0]["name"] == "#ops"

    def test_recall_costs_no_llm_calls(self, monkeypatch):
        from otto.intelligence.classification_cache import ClassificationCache
        from otto.intelligence.classifier import ConversationClassifier

        calls = {"classify": 0}

        async def fake_classify(self, conv, events, recent_context="", **kwargs):
            calls["classify"] += 1
            conv.summary = "LLM summary"
            conv.llm_enriched = True
            return conv

        async def fake_actions(self, conv, events):
            return []

        monkeypatch.setattr(ConversationClassifier, "classify_llm", fake_classify)
        monkeypatch.setattr(ConversationClassifier, "extract_actions", fake_actions)

        class FakeLLM:
            _providers: list = []

            def ensure_chat_provider(self):
                return True

        cache = ClassificationCache()
        ev = _event("Please make sure the invoice batch runs before Friday, finance is waiting on it", sid="inv-1")
        collect_briefing_data(adapters=[FakeAdapter([ev])], llm=FakeLLM(), classification_cache=cache)
        assert calls["classify"] == 1
        recalled = collect_briefing_data(adapters=[FakeAdapter([])], llm=FakeLLM(), classification_cache=cache)
        assert recalled["recalled"] == 1 and recalled["llm_calls"] == 0 and calls["classify"] == 1
        items = [i for ch in recalled["sections"][0]["channels"] for i in ch["items"]]
        assert items[0]["summary"] == "LLM summary"

    def test_on_screen_but_older_than_the_window_is_remembered_not_shown(self):
        """A channel scrolled back to last week must not fill Important with old reports."""
        from otto.intelligence.knowledge import default_store

        old = _event("URGENT: production API is returning 500s, please investigate ASAP before the demo",
                     sid="old-500", minutes_ago=9 * 24 * 60)
        fresh = _event("Deploy window moved to 3pm today, please re-check the runbook", sid="fresh-2", minutes_ago=20)
        data = collect_briefing_data(adapters=[FakeAdapter([old, fresh])], llm=NoLLM())
        assert data["aged_out"] == 1 and data["total_items"] == 1
        assert "500s" not in data["sections"][0]["channels"][0]["items"][0]["text"]
        assert len(default_store().messages()) == 2          # both remembered (series, trends, context)

    def test_recall_respects_the_window_and_can_be_turned_off(self, monkeypatch):
        old = _event("Old thread about the Q2 budget review that nobody needs to see again today", sid="old-1",
                     minutes_ago=26 * 60)
        collect_briefing_data(adapters=[FakeAdapter([old])], llm=NoLLM())        # remembered
        again = collect_briefing_data(adapters=[FakeAdapter([])], llm=NoLLM())
        assert again["recalled"] == 0                                             # 26 h ago is outside a 24 h window
        fresh = _event("Fresh message worth recalling for the rest of the day", sid="fresh-1", minutes_ago=5)
        collect_briefing_data(adapters=[FakeAdapter([fresh])], llm=NoLLM())
        monkeypatch.setenv("OTTO_DISABLE_RECALL", "1")
        off = collect_briefing_data(adapters=[FakeAdapter([])], llm=NoLLM())
        assert off["recalled"] == 0 and off["total_items"] == 0


def test_llm_phase_budget(monkeypatch):
    from otto.intelligence.classifier import ConversationClassifier
    monkeypatch.setattr(collect, "LLM_PHASE_TIMEOUT_REMOTE", 0.05)

    async def slow_classify(self, conv, events, recent_context="", **kwargs):
        await asyncio.sleep(1)
        return conv

    monkeypatch.setattr(ConversationClassifier, "classify_llm", slow_classify)

    class FakeLLM:
        _providers: list = []

        def ensure_chat_provider(self):
            return True

    started = time.monotonic()
    data = collect_briefing_data(adapters=[FakeAdapter([_event("Please review the budget spreadsheet before Monday")])],
                                 llm=FakeLLM())
    assert time.monotonic() - started < 2
    assert data["total_items"] == 1  # still rendered with local heuristics


class TestHelpers:
    def test_clean_ax_text(self):
        assert clean_ax_text("Hello 12 characters remaining  world Loading 3 more replies") == "Hello world"

    def test_is_noise(self):
        assert is_noise("Home", "", [])
        assert is_noise("3 new messages", "", [])
        assert is_noise("This is your space. Draft messages, make to-do lists", "", [])
        assert not is_noise("Production deploy failed on step 4, rolling back now please check", "", [])
        assert is_noise("Some perfectly normal long message text here", "", ["slack-ui-noise"])

    def test_short_words_from_a_person_are_correspondence_not_chrome(self):
        assert is_noise("can you review #42?", "#eng", [])                    # unknown author: chrome-length
        assert not is_noise("can you review #42?", "#eng", [], from_person=True)
        assert is_noise("ok", "#eng", [], from_person=True)                  # one word is still nothing
        assert is_noise("Home", "#eng", [], from_person=True)
        assert is_noise("3 new messages", "#eng", [], from_person=True)

    def test_screenshots_disabled_in_tests(self):
        assert capture_slack_screenshot("#x") == ""


def test_a_short_human_message_reaches_the_briefing():
    events = [_event("can you review #42 today?", sender="Alice", minutes_ago=2)]
    data = collect_briefing_data(adapters=[FakeAdapter(events)], llm=NoLLM())
    assert data["total_items"] == 1
    bot = [_event("can you review #42 today?", sender="notify", bot=True, minutes_ago=2)]
    data = collect_briefing_data(adapters=[FakeAdapter(bot)], llm=NoLLM())
    assert data["total_items"] == 0


def test_thread_replies_are_one_conversation_and_the_briefing_reads_the_thread():
    """A reply carries the parent's id as its thread; the item shows the newest message with the thread behind it."""
    parent = _event("Report 10 generated with 0 findings", sender="notify", bot=True, minutes_ago=60, sid="parent-1")
    reply = _event("build failed twice this morning, could be an upstream issue with the runner image", sender="Carol", minutes_ago=3, sid="reply-1")
    reply.thread_id = parent.source_id
    data = collect_briefing_data(adapters=[FakeAdapter([parent, reply])], llm=NoLLM())
    items = [i for s in data["sections"] for ch in s["channels"] for i in ch["items"]]
    assert len(items) == 1
    assert items[0]["text"].startswith("build failed twice this morning")
    assert items[0]["sender"] == "Carol"


def test_items_carry_provable_reasons_and_the_model_is_told_them(monkeypatch):
    """Every item says why it is for you, from the data; the same evidence goes to the LLM."""
    from otto.intelligence.classification_cache import ClassificationCache
    from otto.intelligence.classifier import ConversationClassifier
    from otto.utils import identity
    identity.remember_self_name("Sam")
    monkeypatch.setattr(identity, "profile", lambda: {"role": "security engineer", "focus": ["runner image"]})
    seen: dict = {}

    async def fake_classify(self, conv, events, recent_context="", **kwargs):
        seen["evidence"] = kwargs.get("evidence", "")
        conv.for_you = "Alice is waiting on your review of the runner image PR."
        conv.llm_enriched = True
        return conv

    async def fake_actions(self, conv, events):
        return []

    monkeypatch.setattr(ConversationClassifier, "classify_llm", fake_classify)
    monkeypatch.setattr(ConversationClassifier, "extract_actions", fake_actions)

    class FakeLLM:
        _providers: list = []

        def ensure_chat_provider(self):
            return True

    ev = _event("Sam can you review the runner image PR by Friday? it blocks the release", sender="Alice")
    data = collect_briefing_data(adapters=[FakeAdapter([ev])], llm=FakeLLM(), classification_cache=ClassificationCache())
    (item,) = [i for s in data["sections"] for ch in s["channels"] for i in ch["items"]]
    kinds = [r["kind"] for r in item["why"]]
    assert kinds[0] == "asked" and "focus" in kinds
    assert item["why"][0]["label"] == "Asked of you" and item["why"][0]["tone"] == "you"
    assert item["for_you"] == "Alice is waiting on your review of the runner image PR."
    assert seen["evidence"].startswith("- Asked of you: review the runner image PR")


def test_merging_duplicates_keeps_every_distinct_reason():
    from otto.web.collect import merge_duplicate_items
    a = {"urgency": "high", "why": [{"kind": "asked", "label": "Asked of you"}], "for_you": ""}
    b = {"urgency": "high", "why": [{"kind": "asked", "label": "Asked of you"}, {"kind": "severity", "label": "2 high"}],
         "for_you": "Two high findings in a system you own."}
    merge_duplicate_items(a, b)
    assert [r["kind"] for r in a["why"]] == ["asked", "severity"]
    assert a["for_you"] == "Two high findings in a system you own."


class TestOneMessageReadTwice:
    """The screen copy knows the channel; the API copy knows the exact message.

    Folded into one item, the link must land on the message and a dismissal
    of either copy must hold — whichever copy the next refresh happens to see.
    """

    CHANNEL = "https://acme.slack.com/app_redirect?channel=eng"
    MESSAGE = "https://acme.slack.com/archives/C024BE91L/p1726150000123456"
    THREAD = MESSAGE + "?thread_ts=1726149000.000100&cid=C024BE91L"

    def test_link_specificity_orders_message_over_channel_over_app(self):
        from otto.web.collect import link_specificity
        assert link_specificity(self.MESSAGE) == 2
        assert link_specificity(self.THREAD) == 2
        assert link_specificity(self.CHANNEL) == 1
        assert link_specificity("https://acme.slack.com/archives/C024BE91L") == 1
        assert link_specificity("slack://channel?team=T1&id=C1") == 1
        assert link_specificity("slack://open") == 0
        assert link_specificity("") == 0

    def test_merge_prefers_the_message_link_either_way_round(self):
        from otto.web.collect import merge_duplicate_items
        screen = {"id": "aaa", "source_url": self.CHANNEL, "screenshot": "shot.png", "urgency": "high"}
        api = {"id": "bbb", "source_url": self.MESSAGE, "screenshot": "", "urgency": "high"}
        merge_duplicate_items(screen, api)
        assert screen["source_url"] == self.MESSAGE
        assert screen["screenshot"] == "shot.png"          # the picture only the screen copy has
        assert screen["ids"] == ["aaa", "bbb"]

        api2 = {"id": "bbb", "source_url": self.MESSAGE, "screenshot": "", "urgency": "high"}
        screen2 = {"id": "aaa", "source_url": self.CHANNEL, "screenshot": "shot.png", "urgency": "high"}
        merge_duplicate_items(api2, screen2)
        assert api2["source_url"] == self.MESSAGE             # not downgraded to the channel
        assert api2["screenshot"] == "shot.png"
        assert api2["ids"] == ["aaa", "bbb"]

    def test_a_third_copy_keeps_every_id(self):
        from otto.web.collect import merge_duplicate_items
        a = {"id": "aaa", "source_url": self.CHANNEL, "urgency": "high"}
        merge_duplicate_items(a, {"id": "bbb", "source_url": self.MESSAGE, "urgency": "high"})
        merge_duplicate_items(a, {"id": "ccc", "source_url": "", "urgency": "high"})
        assert a["ids"] == ["aaa", "bbb", "ccc"] and a["occurrence_count"] == 3

    def test_a_screen_only_item_gets_its_permalink_from_memory(self):
        """Yesterday the API read the message (permalink into memory); today only the
        screen shows it, scrolled back. The item still links to the exact message."""
        text = "URGENT: production API is returning 500s, please investigate ASAP before the demo"
        api_copy = _event(text, url=self.MESSAGE, sid="C1:1.0", minutes_ago=30)
        first = collect_briefing_data(adapters=[FakeAdapter([api_copy], name="slack_api")], llm=NoLLM())
        assert [it["source_url"] for ch in first["sections"][0]["channels"] for it in ch["items"]] == [self.MESSAGE]
        screen_copy = _event(text, url="slack://open?team=T1", sid="ax-7", minutes_ago=30)
        second = collect_briefing_data(adapters=[FakeAdapter([screen_copy])], llm=NoLLM())
        items = [it for ch in second["sections"][0]["channels"] for it in ch["items"]]
        assert len(items) == 1 and items[0]["source_url"] == self.MESSAGE
        # a message memory has only ever seen on screen, in a channel the API has read:
        # no exact link exists, so it opens that conversation rather than just the app
        lone = _event("Deploy blocked: migration failed on staging, needs a decision", url="slack://open?team=T1", sid="ax-8")
        third = collect_briefing_data(adapters=[FakeAdapter([lone])], llm=NoLLM())
        lone_items = [it for ch in third["sections"][0]["channels"] for it in ch["items"] if it["text"].startswith("Deploy blocked")]
        assert lone_items and lone_items[0]["source_url"] == "https://acme.slack.com/archives/C024BE91L"
        # …and in a channel the API has never read, the screen's own link stands
        elsewhere = _event("Deploy blocked: migration failed on staging, needs a decision", title="#ops",
                           url="slack://open?team=T1", sid="ax-9")
        fourth = collect_briefing_data(adapters=[FakeAdapter([elsewhere])], llm=NoLLM())
        ops = [it for ch in fourth["sections"][0]["channels"] if ch["name"] == "#ops" for it in ch["items"]]
        assert ops and ops[0]["source_url"] == "slack://open?team=T1"

    def test_hidden_by_any_copy_id(self):
        from otto.web.render import flatten_items
        from otto.web.state import is_hidden, item_ids
        item = {"id": "aaa", "ids": ["aaa", "bbb"], "text": "x", "urgency": "high"}
        assert item_ids(item) == {"aaa", "bbb"}
        assert is_hidden(item, {"bbb"}) and is_hidden(item, {"aaa"}) and not is_hidden(item, {"zzz"})
        assert not is_hidden(item, set())
        data = {"sections": [{"source": "slack", "channels": [{"name": "#eng", "items": [item]}]}]}
        assert flatten_items(data, {"bbb"}) == []          # dismissed under the API copy's id
        assert len(flatten_items(data, {"other"})) == 1
        lone = {"id": "solo", "text": "y", "urgency": "low"}
        assert item_ids(lone) == {"solo"} and not is_hidden(lone, {"aaa"})


class TestScreenshotBudget:
    """Thumbnails must never cost seconds every minute or show stale pictures."""

    @pytest.fixture
    def shots(self, monkeypatch):
        from otto.web import collect

        monkeypatch.setenv("OTTO_DISABLE_SCREENSHOTS", "0")
        collect.reset_screenshot_state()
        monkeypatch.setattr(collect, "_get_slack_window_id", lambda: 42)
        # Screen Recording granted (the preflight is never asked for real in tests).
        monkeypatch.setattr(collect, "_screen_recording_granted", lambda: True)
        # Slack is showing whatever the test says it shows.
        from otto.adapters.browser import slack_browser
        showing = {"channel": "#eng"}
        monkeypatch.setattr(slack_browser, "visible_channel", lambda: showing["channel"])
        calls: list[list[str]] = []
        behaviour = {"write": b"PNG" * 1000, "rc": 0, "showing": showing}

        def fake_run(cmd, **kw):
            calls.append(cmd)
            path = Path(cmd[-1])
            if behaviour["write"] is not None:
                path.write_bytes(behaviour["write"])
            return SimpleNamespace(returncode=behaviour["rc"], stderr=b"could not create image from window")

        monkeypatch.setattr(collect.subprocess, "run", fake_run)
        sdir = paths.screenshots_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        yield collect, calls, behaviour, sdir
        collect.reset_screenshot_state()

    def test_one_capture_per_refresh_and_ttl_reuse(self, shots):
        collect, calls, behaviour, _sdir = shots
        collect._SCREENSHOT_STATE["captured_this_refresh"] = False
        a = capture_slack_screenshot("#eng")
        assert a.endswith(".png") and len(calls) == 1
        behaviour["showing"]["channel"] = "#ops"
        assert capture_slack_screenshot("#ops") == ""            # budget spent this refresh
        assert len(calls) == 1
        assert capture_slack_screenshot("#eng") == a             # fresh file → no capture
        collect._SCREENSHOT_STATE["captured_this_refresh"] = False
        behaviour["write"] = b"OTHER" * 1000
        b = capture_slack_screenshot("#ops")
        assert b and b != a and len(calls) == 2

    def test_only_the_conversation_on_screen_is_captured(self, shots):
        """A picture of #eng must never be filed as #ops: capture only while Slack shows the item's conversation."""
        collect, calls, behaviour, _sdir = shots
        behaviour["showing"]["channel"] = "#eng"
        assert capture_slack_screenshot("#ops") == "" and calls == []
        assert capture_slack_screenshot("@alice") == "" and calls == []
        assert collect._SCREENSHOT_STATE["captured_this_refresh"] is False   # budget untouched
        assert capture_slack_screenshot("#eng") and len(calls) == 1
        # DM naming across the reader ("@alice") and the briefing ("dm-alice") is the same conversation.
        collect._SCREENSHOT_STATE["captured_this_refresh"] = False
        behaviour["showing"]["channel"] = "@alice"
        behaviour["write"] = b"DM" * 1000
        assert capture_slack_screenshot("dm-alice") and len(calls) == 2
        behaviour["showing"]["channel"] = ""                                  # reader could not tell → no capture
        collect._SCREENSHOT_STATE["captured_this_refresh"] = False
        assert capture_slack_screenshot("#ops") == "" and len(calls) == 2

    def test_no_screen_recording_means_no_attempt_and_a_clear_status(self, shots, monkeypatch, caplog):
        collect, calls, _behaviour, _sdir = shots
        monkeypatch.setattr(collect, "_screen_recording_granted", lambda: False)
        with caplog.at_level("INFO"):
            assert capture_slack_screenshot("#eng") == ""
        assert calls == []                                                   # screencapture never run
        assert "Screen Recording" in caplog.text and "otto permissions" in caplog.text
        st = collect.screenshot_status()
        assert st["enabled"] and st["paused_seconds"] > 0 and "Screen Recording" in st["reason"]

    def test_status_reports_the_preflight_result(self, monkeypatch):
        import otto.utils.screen as screen
        collect.reset_screenshot_state()
        monkeypatch.setattr(screen, "screen_recording_granted", lambda: False)
        assert collect._screen_recording_granted() is False
        assert collect.screenshot_status()["granted"] is False
        # cached: a later grant is noticed on the next re-check, not every call
        monkeypatch.setattr(screen, "screen_recording_granted", lambda: True)
        assert collect._screen_recording_granted() is False
        collect._SCREENSHOT_STATE["granted_checked"] = 0.0
        assert collect._screen_recording_granted() is True
        collect.reset_screenshot_state()

    def test_failed_capture_pauses_the_feature(self, shots, caplog):
        collect, calls, behaviour, _sdir = shots
        behaviour["write"] = None
        behaviour["rc"] = 1
        with caplog.at_level("INFO"):
            assert capture_slack_screenshot("#eng") == ""
        assert len(calls) == 1
        assert collect._SCREENSHOT_STATE["paused_until"] > time.time()
        assert "Screen Recording" in caplog.text and "could not create image" in caplog.text
        for _ in range(3):
            collect._SCREENSHOT_STATE["captured_this_refresh"] = False
            assert capture_slack_screenshot("#eng") == ""
        assert len(calls) == 1                                    # no retries while paused

    def test_blank_identical_captures_are_rejected(self, shots):
        collect, calls, behaviour, sdir = shots
        collect._SCREENSHOT_STATE["captured_this_refresh"] = False
        assert capture_slack_screenshot("#eng")
        collect._SCREENSHOT_STATE["captured_this_refresh"] = False
        behaviour["showing"]["channel"] = "#ops"
        assert capture_slack_screenshot("#ops") == ""            # same bytes as #eng → blank window
        assert not any(p.name.startswith(hashlib.sha256(b"#ops").hexdigest()[:12]) for p in sdir.glob("*.png"))
        assert collect._SCREENSHOT_STATE["paused_until"] > time.time()

    def test_an_unchanged_window_is_not_mistaken_for_a_blank_one(self, shots):
        """Re-capturing the *same* conversation after the TTL may well give identical bytes — that is fine."""
        collect, calls, _behaviour, sdir = shots
        import os
        collect._SCREENSHOT_STATE["captured_this_refresh"] = False
        a = capture_slack_screenshot("#eng")
        assert a
        old = time.time() - collect.SCREENSHOT_TTL_SECONDS - 5
        os.utime(sdir / a, (old, old))
        collect._SCREENSHOT_STATE["captured_this_refresh"] = False
        assert capture_slack_screenshot("#eng") == a and len(calls) == 2
        assert collect._SCREENSHOT_STATE["paused_until"] == 0.0

    def test_stale_thumbnails_are_not_shown(self, shots):
        collect, _calls, _behaviour, sdir = shots
        import os

        name = hashlib.sha256(b"#eng").hexdigest()[:12] + ".png"
        old = sdir / name
        old.write_bytes(b"x" * 2000)
        stale = time.time() - collect.SCREENSHOT_STALE_SECONDS - 60
        os.utime(old, (stale, stale))
        collect._SCREENSHOT_STATE["paused_until"] = time.time() + 600      # cannot recapture right now
        assert capture_slack_screenshot("#eng") == ""                       # stale → nothing rather than wrong
        recent = time.time() - collect.SCREENSHOT_TTL_SECONDS - 60          # older than TTL, younger than stale
        os.utime(old, (recent, recent))
        assert capture_slack_screenshot("#eng") == name

    def test_prune_screenshots(self):
        sdir = paths.screenshots_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        import os
        for i in range(5):
            p = sdir / f"s{i}.png"
            p.write_bytes(b"x")
            os.utime(p, (time.time() - i, time.time() - i))
        removed = prune_screenshots(max_files=2, keep={"s4.png"})
        assert removed == 2
        remaining = {p.name for p in sdir.glob("*.png")}
        assert "s4.png" in remaining and len(remaining) == 3
        removed = prune_screenshots(max_files=100, max_age=timedelta(seconds=0))
        assert removed == 3


class TestSourceBackoff:
    def test_schedule_doubles_and_caps(self):
        b = collect.SourceBackoff()
        assert not b.should_skip("slack", now=0)
        b.record_failure("slack", "timed out", now=0)
        assert b.should_skip("slack", now=100)
        assert b.retry_in("slack", now=0) == 120
        assert not b.should_skip("slack", now=121)
        b.record_failure("slack", "timed out", now=121)
        assert b.retry_in("slack", now=121) == 240
        for _ in range(10):
            b.record_failure("slack", "timed out", now=0)
        assert b.retry_in("slack", now=0) == 15 * 60
        assert b.last_error("slack") == "timed out"

    def test_success_and_reset_clear_state(self):
        b = collect.SourceBackoff()
        b.record_failure("slack", "denied", now=0)
        b.record_success("slack")
        assert not b.should_skip("slack", now=1)
        b.record_failure("jira", "denied", now=0)
        b.reset()
        assert not b.should_skip("jira", now=1)

    def test_poll_skips_source_in_backoff_and_reports_it(self, monkeypatch):
        monkeypatch.setattr(collect, "ADAPTER_POLL_TIMEOUT", 0.05)
        slow = FakeAdapter([_event("never arrives because the adapter is far too slow today")], name="browser_slack", slow=1.0)
        first = collect_briefing_data(adapters=[slow], llm=NoLLM())
        assert first["source_status"][0]["state"] == "timeout"
        # Second refresh: the source is skipped without touching the adapter.
        calls = {"n": 0}
        real_connect = slow.connect

        async def counting_connect():
            calls["n"] += 1
            return await real_connect()

        slow.connect = counting_connect
        second = collect_briefing_data(adapters=[slow], llm=NoLLM())
        st = second["source_status"][0]
        assert st["state"] == "backoff" and st["retry_in"] > 0 and "permission" in st["error"]
        assert calls["n"] == 0
        # A forced (manual) refresh tries again.
        collect_briefing_data(adapters=[slow], llm=NoLLM(), force=True)
        assert calls["n"] == 1

    def test_app_not_open_is_not_backed_off(self):
        class Closed(FakeAdapter):
            async def connect(self):
                return ConnectionStatus(state=ConnectionState.FAILED, error="No Slack tabs or native app found")

        a = Closed([], name="browser_slack")
        collect_briefing_data(adapters=[a], llm=NoLLM())
        second = collect_briefing_data(adapters=[a], llm=NoLLM())
        assert second["source_status"][0]["state"] != "backoff"
        assert second["source_status"][0]["error"] == "app not open"


def test_all_clear_bot_notices_are_noise():
    from otto.web.collect import is_noise
    text = "Scan complete — worker main Severity : 0 critical, 0 high, 0 medium No report-eligible findings above the CVSS threshold."
    assert is_noise(text, "#security-alerts", ["all-clear"])
    assert not is_noise(text, "#security-alerts", [])


def test_repo_named_in_a_finding_is_not_a_tool_to_evaluate(monkeypatch):
    """Seen live: a CVSS 8.7 finding "in https://github.com/acme/tool" grew a
    "check the README before adopting" card and a process-improvement opportunity."""
    from otto.web import collect as collect_mod

    finding = _event(
        "New finding: Empty dashboard token fail-open (frp-class) (CVSS 8.7) in https://github.com/acme/tool\n"
        "[HIGH 8.7]\nEmpty dashboard token fail-open\nOpen on the platform",
        title="#security-alerts", sender="scanner", bot=True,
    )
    recommendation = _event(
        "Please review https://github.com/acme/audio-kit and see if it can be used in the next project",
        title="@alice", sender="You",
    )
    seen: list[str] = []

    def fake_analyze(url):
        seen.append(url)
        return {"title": "acme/audio-kit", "url": url, "summary": "GitHub repository", "why_useful": ["evaluate"], "key_features": []}

    monkeypatch.setattr("otto.intelligence.link_intelligence.analyze_url", fake_analyze)
    data = collect_briefing_data(adapters=[FakeAdapter([finding, recommendation])], llm=NoLLM())

    items = [it for sec in data["sections"] for ch in sec["channels"] for it in ch["items"]]
    finding_item = next(it for it in items if "fail-open" in it["text"])
    tool_item = next(it for it in items if "audio-kit" in it["text"])
    assert seen == ["https://github.com/acme/audio-kit"], "link intelligence must skip the repo in a finding"
    assert finding_item["link_intelligence"] is None
    assert finding_item["external_url"] == "https://github.com/acme/tool"  # still one click away
    assert finding_item["opportunity_type"] == "" and finding_item["opportunity_score"] <= 0.4
    assert tool_item["link_intelligence"]["title"] == "acme/audio-kit"
    assert all(o["subject"] != "#security-alerts" for o in data["opportunities"])

    # the detector holds on LLM-worded conversations too (no raw finding lines)
    class Conv:
        summary = "A new high-severity vulnerability (CVSS 8.7) was identified in the tool project."
        relevance_explanation = ""
        action_items = ["Address the finding"]
        topics = ["security_bug"]

    assert collect_mod.is_security_finding(Conv(), "") is True

    class Chat:
        summary = "Alice suggests trying a new audio library for the next project."
        relevance_explanation = ""
        action_items = ["Evaluate the library"]
        topics = ["tool-evaluation", "github"]

    assert collect_mod.is_security_finding(Chat(), "Please review https://github.com/acme/audio-kit") is False


def test_cached_llm_topics_keep_local_structural_tags():
    """A cache entry written before the all-clear tag existed must not un-mute the notice."""
    from otto.intelligence.classification_cache import ClassificationCache
    from otto.storage.models import Conversation

    from otto.storage.models import Domain
    conv = Conversation(source=SourceType.SLACK, account_id="acme", thread_id="t1", subject="#security-alerts",
                        summary="Scan complete, nothing found.", domain=Domain.WORK, relevance_explanation="")
    conv.topics = ["all-clear", "security"]
    cache = ClassificationCache()
    cache.apply(conv, {"topics": ["security audit", "no findings"], "summary": "Routine scan.", "llm_enriched": True})
    assert conv.topics == ["all-clear", "security audit", "no findings"]
    assert conv.summary == "Routine scan."


def test_failed_llm_is_not_ai_powered_and_not_cached(monkeypatch):
    """Configured-but-failing providers: heuristics only, honest flag, retry next refresh."""
    from otto.intelligence.classification_cache import ClassificationCache
    from otto.intelligence.classifier import ConversationClassifier

    calls = {"classify": 0}

    async def failing_classify(self, conv, events, recent_context="", **kwargs):
        calls["classify"] += 1
        return conv                      # gateway returned None → nothing applied, llm_enriched stays False

    monkeypatch.setattr(ConversationClassifier, "classify_llm", failing_classify)

    class FakeLLM:
        _providers: list = []

        def ensure_chat_provider(self):
            return True

        def provider_status(self):
            return [{"name": "openai", "model": "gpt-4o-mini", "local": False, "state": "API key rejected (HTTP 401)"}]

    cache = ClassificationCache()
    events = [_event("Please make sure the invoice batch runs before Friday, finance is waiting on it")]
    d1 = collect_briefing_data(adapters=[FakeAdapter(events)], llm=FakeLLM(), classification_cache=cache)
    assert d1["ai_powered"] is False
    assert d1["llm"]["configured"] is True and d1["llm"]["enriched"] == 0
    assert d1["llm"]["providers"][0]["state"].startswith("API key rejected")
    assert d1["total_items"] == 1                       # local heuristics still render the item
    d2 = collect_briefing_data(adapters=[FakeAdapter(events)], llm=FakeLLM(), classification_cache=cache)
    assert calls["classify"] == 2, "a failed enrichment must not be cached"
    assert d2["ai_powered"] is False


def test_legacy_cache_entries_without_enrichment_flag_are_misses(tmp_path):
    from otto.intelligence.classification_cache import ClassificationCache, content_key
    c = ClassificationCache(tmp_path / "cache.json")
    key = content_key("slack", "t", "text")
    c.put(key, {"urgency": 0.4, "summary": "heuristic"})   # no llm_enriched → pre-flag entry
    assert c.get(key) is None
    c.put(key, {"urgency": 0.9, "summary": "real", "llm_enriched": True})
    assert c.get(key)["summary"] == "real"
