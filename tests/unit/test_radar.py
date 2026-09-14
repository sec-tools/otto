"""
Radar (``otto.intelligence.radar``): memory → open loops, upcoming dates,
patterns — and its wiring into the collector, the page and the status API.

Hermetic: the store lives in the per-test ``OTTO_DATA_DIR``; fixtures are
anonymised (channel ``#eng``, people ``alice``/``carol``, bot ``scanner``).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from otto.intelligence import radar
from otto.intelligence.knowledge import KnowledgeStore


@dataclass
class Ev:
    title: str
    sender: str
    plain_text_extract: str
    timestamp: datetime
    source: str = "slack"
    is_auto_generated: bool = False
    source_url: str = ""
    conversation_id: str = ""
    source_id: str = field(default_factory=lambda: "x")


NOW = time.time()


def _ev(text, *, channel="#eng", sender="alice", hours_ago=1.0, bot=False, url=""):
    return Ev(title=channel, sender=sender, plain_text_extract=text, is_auto_generated=bot, source_url=url,
              timestamp=datetime.fromtimestamp(NOW - hours_ago * 3600, tz=timezone.utc))


SCAN = "Scanner scan complete — api\nmain\nDuration : {dur}s\nSeverity : 0 critical, {high} high, 0 medium"


def _scans(highs, *, last_hours_ago=2.0):
    out = []
    for i, h in enumerate(reversed(highs)):
        out.append(_ev(SCAN.format(dur=370 + i, high=h), channel="#security-alerts", sender="scanner", bot=True,
                       hours_ago=last_hours_ago + i * 24))
    return out


@pytest.fixture
def store(tmp_path):
    return KnowledgeStore(tmp_path / "knowledge.db")


@pytest.fixture
def you(monkeypatch):
    monkeypatch.setattr(radar, "self_names", lambda: ["robin"])


class TestBuckets:
    def test_open_loops_land_in_the_right_lists(self, store, you):
        events = [
            _ev("@robin can you review the PR by tomorrow? I'll send the deck Friday.", hours_ago=5),
            _ev("I'll take a look at the sandbox findings this afternoon.", sender="robin", hours_ago=4),
            _ev("Can someone take the on-call swap next week?", sender="carol", hours_ago=3),
            _ev("Certificate for api.example.com expires in 5 days.", sender="dave", hours_ago=2),
            _ev("could you send me the numbers by EOD?", channel="@alice", hours_ago=1, url="https://acme.slack.com/x"),
        ]
        r = radar.update(events, store=store, now=NOW)
        assert [x["what"] for x in r["todo"]] == [
            "take a look at the sandbox findings", "send me the numbers", "review the PR",
        ] or {x["what"] for x in r["todo"]} == {"take a look at the sandbox findings", "send me the numbers", "review the PR"}
        assert r["todo"][0]["who"] == "you" or any(x["who"] == "you" for x in r["todo"])
        assert [x["what"] for x in r["waiting"]] == ["send the deck"]
        assert [x["what"] for x in r["open_calls"]] == ["take the on-call swap"]
        assert [x["what"] for x in r["upcoming"]] == ["Certificate for api.example.com expires"]
        assert r["upcoming"][0]["kind"] == "deadline"
        dm = next(x for x in r["todo"] if x["what"] == "send me the numbers")
        assert dm["url"] == "https://acme.slack.com/x" and dm["channel"] == "@alice"
        assert all(x["id"].startswith("radar:") for x in r["todo"])
        assert r["memory"]["messages"] == 5 and r["self_names"] == ["robin"]
        assert radar.count(r) == 6

    def test_rereading_the_same_messages_adds_nothing(self, store, you):
        events = [_ev("@robin please update the runbook by Friday", hours_ago=2)]
        first = radar.update(events, store=store, now=NOW)
        second = radar.update(events, store=store, now=NOW + 60)
        assert len(first["todo"]) == len(second["todo"]) == 1
        assert store.stats()["open_commitments"] == 1

    def test_asks_between_other_people_are_not_your_todo(self, store, you):
        r = radar.update([_ev("Bob, can you review the PR by tomorrow?", sender="alice")], store=store, now=NOW)
        assert r["todo"] == [] and r["waiting"] == []

    def test_low_confidence_items_are_kept_out(self, store, you):
        r = radar.update([_ev("Any update on the vendor contract?", sender="ivan")], store=store, now=NOW)
        assert radar.count(r) == 0

    def test_old_messages_do_not_create_open_loops(self, store, you):
        r = radar.update([_ev("@robin can you send the report by Friday?", hours_ago=24 * 20)], store=store, now=NOW)
        assert r["todo"] == []

    def test_hidden_ids_are_filtered(self, store, you):
        r = radar.update([_ev("@robin can you send the report by Friday?")], store=store, now=NOW)
        (row,) = r["todo"]
        again = radar.build(store=store, hidden=[row["id"]], now=NOW, names=["robin"])
        assert again["todo"] == []


class TestLifecycle:
    def test_promise_closes_when_its_owner_says_done(self, store, you):
        radar.update([_ev("I'll send the deck by Friday.", hours_ago=5)], store=store, now=NOW)
        assert len(store.commitments()) == 1
        r = radar.update([_ev("deck sent, let me know what you think", hours_ago=1)], store=store, now=NOW)
        assert r["waiting"] == []
        assert store.commitments(status="done")[0]["what"] == "send the deck"

    def test_someone_else_saying_done_does_not_close_a_promise(self, store, you):
        radar.update([_ev("I'll send the deck by Friday.", hours_ago=5)], store=store, now=NOW)
        r = radar.update([_ev("deck sent? great", sender="carol", hours_ago=1)], store=store, now=NOW)
        assert [x["what"] for x in r["waiting"]] == ["send the deck"]

    def test_ask_for_you_closes_when_you_answer(self, store, you):
        radar.update([_ev("@robin can you update the runbook by Friday?", hours_ago=5)], store=store, now=NOW)
        r = radar.update([_ev("runbook updated", sender="robin", hours_ago=1)], store=store, now=NOW)
        assert r["todo"] == []

    def test_overdue_items_stay_visible_for_a_grace_period_then_fade(self, store, you):
        radar.update([_ev("@robin can you send the report by tomorrow?", hours_ago=2)], store=store, now=NOW)
        (row,) = radar.build(store=store, now=NOW, names=["robin"])["todo"]
        due = datetime.fromisoformat(row["due"]).timestamp()
        late = radar.update([], store=store, now=due + 3600)
        assert late["todo"][0]["overdue"] and late["todo"][0]["due_label"].startswith("overdue")
        gone = radar.update([], store=store, now=due + (radar.DEADLINE_GRACE_DAYS + 1) * 86400)
        assert gone["todo"] == []
        assert store.commitments(status="expired")

    def test_undated_items_fade_after_a_while(self, store, you):
        radar.update([_ev("Let me check with legal and get back to you.", hours_ago=1)], store=store, now=NOW)
        assert store.commitments()
        radar.update([], store=store, now=NOW + (radar.UNDATED_TTL_DAYS + 1) * 86400)
        assert store.commitments() == []

    def test_events_are_over_once_they_happened(self, store, you):
        radar.update([_ev("Team offsite kickoff tomorrow at 9am", sender="pm")], store=store, now=NOW)
        (row,) = radar.build(store=store, now=NOW, names=["robin"])["upcoming"]
        when = datetime.fromisoformat(row["due"]).timestamp()
        assert radar.update([], store=store, now=when + radar.EVENT_OVER_AFTER_S + 60)["upcoming"] == []


class TestPatterns:
    def test_recurring_bot_series_with_trend_and_attention(self, store, you):
        r = radar.update(_scans([2, 2, 0, 2, 2, 5]), store=store, now=NOW)
        (p,) = r["patterns"]
        assert p["label"] == "Scanner scan complete — api" and p["n"] == 6 and p["cadence"].startswith("daily")
        assert p["next_expected"] and not p["late_by"]
        assert p["metrics"][0]["name"] == "high" and p["metrics"][0]["direction"] == "up"
        assert any("high is up — 5, usually 2" in a for a in r["attention"])
        assert p["id"].startswith("pattern:")

    def test_thin_evidence_is_a_pattern_row_not_an_alert(self, store, you):
        """Three runs where the last is a little off → shown in Patterns, no attention note."""
        r = radar.update(_scans([10, 10, 13]), store=store, now=NOW)
        (p,) = r["patterns"]
        assert p["metrics"] and p["metrics"][0]["direction"] == "up"
        assert not any("high is up" in a for a in r["attention"])
        # … but a dramatic move on three runs still deserves a note (9× the usual).
        from otto.intelligence.knowledge import KnowledgeStore
        store2 = KnowledgeStore(store.path.parent / "k2.db")
        r2 = radar.update(_scans([3, 3, 30]), store=store2, now=NOW)
        assert any("high is up — 30, usually 3" in a for a in r2["attention"])

    def test_late_series_raises_attention(self, store, you):
        r = radar.update(_scans([1, 1, 1, 1], last_hours_ago=60), store=store, now=NOW)
        (p,) = r["patterns"]
        assert p["late_by"]
        assert any("usually arrives" in a for a in r["attention"])

    def test_pattern_ids_are_stable_across_builds(self, store, you):
        radar.update(_scans([1, 1, 1]), store=store, now=NOW)
        a = radar.build(store=store, now=NOW, names=[])["patterns"][0]["id"]
        b = radar.build(store=store, now=NOW + 5, names=[])["patterns"][0]["id"]
        assert a == b
        assert radar.build(store=store, hidden=[a], now=NOW, names=[])["patterns"] == []


class TestResilience:
    def test_broken_store_gives_an_empty_radar(self, tmp_path, you):
        bad = tmp_path / "not-a-file"
        bad.mkdir()
        r = radar.update([_ev("@robin can you send the report by Friday?")], store=KnowledgeStore(bad), now=NOW)
        assert r == radar.empty_radar() or radar.count(r) == 0

    def test_self_names_merge_config_and_learned(self, monkeypatch, tmp_path):
        (tmp_path / "config.toml").write_text('[user]\nname = "Alex Kim"\naliases = ["alex", "Alex Kim"]\n')
        monkeypatch.setenv("OTTO_DATA_DIR", str(tmp_path))
        from otto.utils import identity
        assert identity.remember_self_name("akim") and not identity.remember_self_name("AKIM")
        assert not identity.remember_self_name("Messages") and not identity.remember_self_name("x")
        assert radar.self_names() == ["Alex Kim", "alex", "akim"]

    def test_slack_app_sidebar_teaches_the_self_name(self):
        from otto.adapters.browser.slack_browser import _learn_self_name
        from otto.utils import identity

        _learn_self_name(["Home", "DMs", "security-alerts", "alice", "you", "Slack"])
        assert identity.learned_self_names() == ("alice",)
        _learn_self_name(["Messages", "(you)"])              # chrome words are never names
        assert identity.learned_self_names() == ("alice",)

    def test_due_labels(self):
        now = NOW
        assert radar.due_label(None, now=now) == ("", False, False)
        label, overdue, soon = radar.due_label(now - 7200, now=now)
        assert overdue and soon and label == "overdue 2 h"
        local_now = datetime.fromtimestamp(now, tz=timezone.utc).astimezone()
        eod_today = local_now.replace(hour=17, minute=0, second=0, microsecond=0)
        if eod_today > local_now:
            assert radar.due_label(eod_today.timestamp(), now=now) == ("today EOD", False, True)
        tomorrow_ten = (local_now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        assert radar.due_label(tomorrow_ten.timestamp(), now=now)[0] == "tomorrow 10:00"
        far = (local_now + timedelta(days=20)).replace(hour=17, minute=0, second=0, microsecond=0)
        assert radar.due_label(far.timestamp(), now=now) == (far.strftime("%b %d"), False, False)


class TestWiring:
    def test_collector_attaches_the_radar_and_remembers_messages(self):
        from otto.intelligence.knowledge import default_store
        from otto.web.collect import collect_briefing_data
        from tests.unit.test_collect import FakeAdapter, NoLLM, _event

        events = [_event("@robin can you review the budget by Friday?", sender="alice"),
                  _event("I'll send the deck tomorrow.", sender="alice", sid="deck")]
        data = collect_briefing_data(adapters=[FakeAdapter(events)], llm=NoLLM())
        r = data["radar"]
        assert r["memory"]["messages"] == 2
        assert [x["what"] for x in r["waiting"]] == ["send the deck"]
        assert default_store().stats()["messages"] == 2

    def test_page_renders_radar_rows_and_partial_hides_dismissed(self):
        from otto.web.render import render_body, render_radar

        data = {
            "sections": [], "generated_at_human": "now",
            "radar": {
                "todo": [{"id": "radar:abc", "kind": "ask", "who": "alice", "what": "review <the> PR", "channel": "#eng",
                          "due": "2026-01-01T00:00:00+00:00", "due_label": "tomorrow", "overdue": False, "soon": True,
                          "url": "https://acme.slack.com/x"}],
                "waiting": [], "open_calls": [],
                "upcoming": [{"id": "radar:def", "kind": "deadline", "who": "", "what": "cert expires", "channel": "#ops",
                              "due": "2026-01-02T00:00:00+00:00", "due_label": "Sep 20", "overdue": False, "soon": False}],
                "patterns": [{"id": "pattern:1", "label": "Scanner scan complete — api", "channel": "#security-alerts",
                              "n": 6, "span": "5 d", "cadence": "daily ~08:20", "next_expected": "today ~08:20",
                              "late_by": "", "metrics": [{"name": "high", "last": "5", "usual": "2", "direction": "up",
                                                          "spark": "▄▄▁▄▄█"}]}],
                "attention": ["#security-alerts · “Scanner scan complete — api”: high ↑ 5 (usually 2, 6 runs)."],
                "memory": {"messages": 1204, "channels": 6, "people": 12, "days": 9},
            },
        }
        html = render_radar(data)
        assert 'data-id="radar:abc"' in html and "review &lt;the&gt; PR" in html and "<the>" not in html
        assert 'class="rwho">alice' in html and 'rdue soon">tomorrow' in html
        assert 'href="https://acme.slack.com/x"' in html
        assert "Coming up" in html and "cert expires" in html
        assert "Keeps coming back" in html and "Patterns" not in html and "▄▄▁▄▄█" in html and "daily ~08:20" in html
        assert "1,204 messages" in html
        assert "high ↑ 5" not in html          # the attention line is a note, drawn once above the rows, not here
        # The radar is "worth knowing", not a notification: it renders in the
        # drawer (`wk`), never among the items in `main`.
        body = render_body(data, hidden=["radar:abc"])
        assert body["wk"].count("high ↑ 5") == 1 and body["wk"].index("high ↑ 5") < body["wk"].index("cert expires")
        assert "review &lt;the&gt; PR" not in body["wk"] and "cert expires" in body["wk"]
        assert "cert expires" not in body["main"] and body["wk_count"] == "3"       # 1 note + upcoming + pattern
        assert render_radar({"sections": [], "radar": radar.empty_radar()}) == ""

    def test_status_summary_counts_visible_rows(self):
        from otto.web.server import _radar_summary

        r = {"todo": [{"id": "a", "overdue": True}, {"id": "b"}], "waiting": [{"id": "c"}], "open_calls": [],
             "upcoming": [], "patterns": [{"id": "p"}], "attention": ["x"], "memory": {"messages": 3}}
        s = _radar_summary(r, hidden={"b"})
        assert s["todo"] == 1 and s["overdue"] == 1 and s["waiting"] == 1 and s["patterns"] == 1
        assert s["attention"] == ["x"] and s["memory"]["messages"] == 3

    def test_dismissing_a_radar_row_closes_it_in_memory(self, monkeypatch):
        from otto.intelligence.knowledge import default_store
        from otto.web import server

        store = default_store()
        store.upsert_commitment({"id": "c1", "kind": "ask", "who": "alice", "what": "review", "channel": "#eng",
                                 "created": NOW, "confidence": 0.9, "for_you": True})
        calls = []
        handler = server.OttoRequestHandler.__new__(server.OttoRequestHandler)
        monkeypatch.setattr(handler, "_json", lambda payload, **kw: calls.append(payload), raising=False)
        monkeypatch.setattr(handler, "_error", lambda *a, **kw: calls.append(("error", a)), raising=False)
        handler._dismiss({"id": "radar:c1"})
        assert calls == [{"status": "ok"}]
        assert store.commitments(status="dismissed")[0]["id"] == "c1"
        assert store.commitments(status="open") == []

    def test_status_line_describes_the_radar(self):
        from otto import cli

        assert cli._describe_radar({"todo": 2, "overdue": 1, "waiting": 1, "patterns": 3, "attention": ["a"]}) == \
            "2 to do (1 overdue) · waiting on 1 · 3 things that keep coming back · 1 needs attention"
        assert cli._describe_radar({"patterns": 1}) == "1 thing that keeps coming back"
        assert cli._describe_radar({}) == "nothing open"

