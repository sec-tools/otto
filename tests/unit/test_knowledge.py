"""
Otto's memory (``otto.intelligence.knowledge``).

Hermetic: every store lives under the per-test ``OTTO_DATA_DIR`` (or an
explicit tmp path). Fixtures are anonymised (workspace ``acme``, channel
``#security-alerts``, senders ``scanner``/``alice``).
"""
from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from otto import paths
from otto.intelligence import knowledge
from otto.intelligence.knowledge import KnowledgeStore, keys_for, message_key


@dataclass
class Ev:
    """Stand-in for a NormalizedEvent — only the fields the store reads."""
    title: str
    sender: str
    plain_text_extract: str
    timestamp: datetime
    source: str = "slack"
    is_auto_generated: bool = False
    source_url: str = ""
    source_id: str = field(default_factory=lambda: os.urandom(4).hex())


def _ev(text, *, channel="#security-alerts", sender="alice", hours_ago=1.0, bot=False, now=None):
    now = now or time.time()
    return Ev(title=channel, sender=sender, plain_text_extract=text,
              timestamp=datetime.fromtimestamp(now - hours_ago * 3600, tz=timezone.utc), is_auto_generated=bot)


@pytest.fixture
def store(tmp_path):
    return KnowledgeStore(tmp_path / "knowledge.db")


class TestRemember:
    def test_new_messages_are_stored_once_and_reported_as_new(self, store):
        now = time.time()
        events = [_ev("first", now=now), _ev("second", now=now)]
        new = store.remember(events, now=now)
        assert len(new) == 2
        assert store.remember(events, now=now + 60) == []          # seen again → nothing new
        msgs = store.messages()
        assert [m.text for m in msgs] == ["first", "second"]
        assert all(m.seen == 2 for m in msgs)

    def test_same_message_via_two_readers_is_one_row(self, store):
        """Slack.app and a browser tab give different source_ids for the same message."""
        # A fixed midday: the key carries the UTC day, and two copies three
        # minutes apart must not straddle midnight just because of when the suite runs.
        now = datetime(2026, 3, 4, 12, 0, tzinfo=timezone.utc).timestamp()
        a = _ev("deploy finished", now=now)
        b = Ev(title="#security-alerts", sender="alice", plain_text_extract="deploy   finished ",
               timestamp=a.timestamp + timedelta(minutes=3), source="slack", source_id="other")
        assert len(store.remember([a], now=now)) == 1
        assert store.remember([b], now=now) == []
        assert store.stats()["messages"] == 1

    def test_screen_and_api_readers_agree_on_identity(self, store):
        """Slack.app shows a link's label; the API reader renders ``label (url)`` — same message."""
        now = time.time()
        screen = _ev("please review PR 7 by tomorrow", now=now)
        api = Ev(title="#security-alerts", sender="alice",
                 plain_text_extract="please review PR 7 (https://github.com/acme/x/pull/7) by tomorrow",
                 timestamp=screen.timestamp, source="slack", source_id="C1:1.0")
        assert len(store.remember([screen, api], now=now)) == 1
        assert message_key("slack", "#eng", "bob", now, "Ship it!") == message_key("slack", "#eng", "bob", now, "ship it")

    def test_the_message_link_wins_over_the_channel_link_whichever_reader_came_first(self, store):
        """The screen reader stores first with a channel link; the API copy of the same
        message brings the permalink, and memory keeps that one."""
        now = time.time()
        channel_link = "slack://open?team=T1"
        permalink = "https://acme.slack.com/archives/C024BE91L/p1726150000123456"
        screen = _ev("can you approve the deploy before 3pm?", now=now)
        screen.source_url = channel_link
        assert len(store.remember([screen], now=now)) == 1
        api = Ev(title="#security-alerts", sender="alice", plain_text_extract="can you approve the deploy before 3pm?",
                 timestamp=screen.timestamp + timedelta(seconds=40), source="slack", source_id="C1:1.0", source_url=permalink)
        assert store.remember([api], now=now + 60) == []                   # same message, not new
        assert [m.url for m in store.messages()] == [permalink]
        # and the other way round the permalink is simply kept
        again = _ev("can you approve the deploy before 3pm?", now=now)
        again.source_url = channel_link
        store.remember([again], now=now + 120)
        assert [m.url for m in store.messages()] == [permalink]

    def test_permalink_for_finds_the_exact_link_from_a_channel_only_copy(self, store):
        now = time.time()
        permalink = "https://acme.slack.com/archives/C024BE91L/p1726150000123456"
        api = Ev(title="@sam", sender="sam", plain_text_extract="note to self: rotate the staging key (https://vault.example/keys)",
                 timestamp=datetime.fromtimestamp(now - 20 * 3600, tz=timezone.utc), source="slack", source_id="D1:1.0", source_url=permalink)
        other = Ev(title="@sam", sender="sam", plain_text_extract="unrelated reminder", timestamp=api.timestamp,
                   source="slack", source_id="D1:2.0", source_url="https://acme.slack.com/archives/C024BE91L/p1726150000999999")
        store.remember([api, other], now=now)
        # the screen shows the link's label only, a day later, in the same DM
        assert store.permalink_for(channel="@sam", text="note to self: rotate the staging key", since=now - 7 * 86400) == permalink
        assert store.permalink_for(channel="@SAM", text="Note to self: rotate the staging key!") == permalink   # case, punctuation
        assert store.permalink_for(channel="#eng", text="note to self: rotate the staging key") == ""            # other channel
        assert store.permalink_for(channel="@sam", text="rotate") == ""                                          # too short to trust
        assert store.permalink_for(channel="@sam", text="note to self: rotate the staging key", since=now - 3600) == ""   # outside window
        # no exact copy → at least the conversation itself, derived from any permalink stored for it
        assert store.channel_link_for(channel="@sam") == "https://acme.slack.com/archives/C024BE91L"
        assert store.channel_link_for(channel="#eng") == "" and store.channel_link_for(channel="") == ""

    def test_keys_for_matches_what_remember_used(self, store):
        now = time.time()
        ev = _ev("hello there", now=now)
        (key,) = store.remember([ev], now=now)
        assert keys_for([ev], now=now) == [key]
        assert message_key("slack", "#security-alerts", "alice", ev.timestamp.timestamp(), "hello there") == key

    def test_empty_and_one_character_texts_are_ignored(self, store):
        assert store.remember([_ev(""), _ev("x")]) == []
        assert store.stats()["messages"] == 0

    def test_database_files_are_private(self, store):
        store.remember([_ev("secret-ish")])
        for suffix in ("", "-wal", "-shm"):
            p = store.path.parent / (store.path.name + suffix)
            if p.exists():
                assert stat.S_IMODE(p.stat().st_mode) == 0o600, p

    def test_text_is_capped(self, store):
        store.remember([_ev("y" * 10_000)])
        assert len(store.messages()[0].text) == knowledge.MAX_TEXT_CHARS


class TestQueries:
    def test_filters_channel_sender_bots_and_time(self, store):
        now = time.time()
        store.remember([
            _ev("scan ok", sender="scanner", bot=True, hours_ago=30, now=now),
            _ev("hi", channel="#general", sender="alice", hours_ago=2, now=now),
            _ev("yo", channel="#general", sender="bob", hours_ago=1, now=now),
        ], now=now)
        assert [m.sender for m in store.messages(channel="#general")] == ["alice", "bob"]
        assert [m.sender for m in store.messages(channel="#GENERAL", newest_first=True)] == ["bob", "alice"]
        assert [m.text for m in store.messages(bots=True)] == ["scan ok"]
        assert [m.text for m in store.messages(since=now - 3 * 3600)] == ["hi", "yo"]
        assert store.messages(sender="nobody") == []

    def test_search_requires_all_terms(self, store):
        store.remember([_ev("the budget review is Friday"), _ev("budget approved"), _ev("review done")])
        assert [m.text for m in store.search(["budget", "review"])] == ["the budget review is Friday"]
        assert store.search([]) == []

    def test_channel_and_people_activity(self, store):
        now = time.time()
        store.remember([
            _ev("alpha", sender="alice", now=now), _ev("bravo", sender="bob", now=now),
            _ev("charlie", sender="scanner", bot=True, now=now), _ev("delta", channel="#general", sender="alice", now=now),
        ], now=now)
        chans = {c.channel: c for c in store.channels()}
        assert chans["#security-alerts"].total == 3
        assert chans["#security-alerts"].humans == 2 and chans["#security-alerts"].bots == 1
        assert chans["#security-alerts"].senders == 3
        people = {p.sender: p for p in store.people()}
        assert people["alice"].total == 2 and people["alice"].channels == 2
        assert "scanner" not in people

    def test_daily_counts_bucket_by_local_day(self, store):
        now = time.time()
        store.remember([_ev("today", hours_ago=0.1, now=now), _ev("also today", hours_ago=0.2, now=now),
                        _ev("long ago", hours_ago=24 * 10, now=now)], now=now)
        counts = store.daily_counts(days=3, now=now)
        assert len(counts) == 3 and counts[-1] == 2 and sum(counts) == 2

    def test_stats(self, store):
        now = time.time()
        assert store.stats(now=now)["messages"] == 0
        store.remember([_ev("alpha", sender="alice", hours_ago=0, now=now),
                        _ev("bravo", sender="Alice", channel="#x", hours_ago=24 * 9, now=now)], now=now)
        st = store.stats(now=now)
        assert st["messages"] == 2 and st["channels"] == 2 and st["people"] == 1
        assert st["days"] == 10 and st["messages_7d"] == 1


class TestRetention:
    def test_prune_drops_old_rows_and_caps_total(self, tmp_path):
        store = KnowledgeStore(tmp_path / "k.db", retention_days=7, max_messages=100)
        now = time.time()
        store._last_prune = now                       # keep the write itself from pruning
        store.remember([_ev("ancient", hours_ago=24 * 30, now=now), _ev("fresh", now=now)], now=now)
        assert store.prune(now=now) == 1
        assert [m.text for m in store.messages()] == ["fresh"]

        many = [_ev(f"msg {i}", hours_ago=i / 60, now=now) for i in range(150)]
        store.remember(many, now=now)
        assert store.stats()["messages"] > 100
        store.prune(now=now)
        assert store.stats()["messages"] == 100

    def test_prune_runs_on_write_only_every_few_minutes(self, tmp_path):
        store = KnowledgeStore(tmp_path / "k.db", retention_days=1)
        now = time.time()
        store.remember([_ev("old", hours_ago=48, now=now)], now=now)      # first write prunes immediately…
        assert store.stats()["messages"] == 0                                # …so the old row never lands
        store._last_prune = now
        store.remember([_ev("old2", hours_ago=48, now=now)], now=now + 10)  # within the interval: kept for now
        assert store.stats()["messages"] == 1


class TestContext:
    def test_context_for_gives_earlier_channel_messages_and_sender_elsewhere(self, store):
        now = time.time()
        store.remember([
            _ev("earlier one", sender="bob", hours_ago=5, now=now),
            _ev("earlier two", sender="alice", hours_ago=4, now=now),
            _ev("the conversation itself", sender="alice", hours_ago=1, now=now),
            _ev("alice in general", channel="#general", sender="alice", hours_ago=2, now=now),
        ], now=now)
        conv_key = message_key("slack", "#security-alerts", "alice", now - 3600, "the conversation itself")
        ctx = store.context_for(channel="#security-alerts", sender="alice", before=now - 3600 + 1,
                                exclude_keys=[conv_key], now=now)
        assert "Earlier in #security-alerts:" in ctx
        assert "bob: earlier one" in ctx and "alice: earlier two" in ctx
        assert "the conversation itself" not in ctx
        assert "alice elsewhere this week:" in ctx and "alice in general" in ctx

    def test_context_respects_budget_and_is_empty_without_history(self, store):
        assert store.context_for(channel="#nothing") == ""
        now = time.time()
        store.remember([_ev("x" * 300, hours_ago=i + 1, now=now) for i in range(8)], now=now)
        ctx = store.context_for(channel="#security-alerts", budget_chars=400, now=now)
        assert 0 < len(ctx) <= 400


class TestCommitmentsTable:
    def test_upsert_list_and_status(self, store):
        row = {"id": "c1", "kind": "promise", "who": "alice", "what": "send the deck", "channel": "#x",
               "due": time.time() + 3600, "due_text": "Friday", "created": time.time(), "confidence": 0.7, "for_you": False}
        assert store.upsert_commitment(row) is True
        assert store.upsert_commitment({**row, "confidence": 0.9}) is False     # existing: refreshed, not duplicated
        (item,) = store.commitments()
        assert item["confidence"] == 0.9 and item["status"] == "open"
        assert store.set_commitment_status("c1", "done")
        assert store.commitments() == []
        assert store.commitments(status="done")[0]["id"] == "c1"
        assert store.set_commitment_status("nope", "done") is False

    def test_meta_notes(self, store):
        assert store.get_meta("k", "dflt") == "dflt"
        store.set_meta("k", "v")
        assert store.get_meta("k") == "v"


class TestResilience:
    def test_reads_and_writes_degrade_to_empty_when_the_db_is_unusable(self, tmp_path):
        bad = tmp_path / "dir-not-file"
        bad.mkdir()
        store = KnowledgeStore(bad)            # sqlite cannot open a directory
        assert store.remember([_ev("alpha")]) == []
        assert store.messages() == []
        assert store.stats()["messages"] == 0
        assert store.commitments() == []

    def test_schema_is_recreated_if_the_file_disappears(self, store):
        store.remember([_ev("alpha")])
        store.path.unlink()
        for side in ("-wal", "-shm"):
            p = store.path.parent / (store.path.name + side)
            if p.exists():
                p.unlink()
        assert store.remember([_ev("bravo")]) != []
        assert [m.text for m in store.messages()] == ["bravo"]

    def test_default_store_follows_the_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OTTO_DATA_DIR", str(tmp_path / "one"))
        first = knowledge.default_store()
        assert first.path == paths.knowledge_db()
        monkeypatch.setenv("OTTO_DATA_DIR", str(tmp_path / "two"))
        second = knowledge.default_store()
        assert second is not first and second.path.parent == tmp_path / "two"

    def test_corrupt_file_is_set_aside_and_memory_starts_fresh(self, tmp_path, caplog):
        path = tmp_path / "knowledge.db"
        path.write_bytes(b"this is not a sqlite database at all" * 40)
        store = KnowledgeStore(path)
        with caplog.at_level("WARNING"):
            assert store.remember([_ev("alpha")]) != []
        assert [m.text for m in store.messages()] == ["alpha"]
        kept = list(tmp_path.glob("knowledge.db.corrupt-*"))
        assert len(kept) == 1 and kept[0].read_bytes().startswith(b"this is not")   # nothing deleted
        assert "unreadable" in caplog.text

    def test_damage_found_mid_session_recovers_on_the_next_call(self, store):
        store.remember([_ev("alpha")])
        assert store.messages()                      # store is open and ready
        # Overwrite the file body while the store believes the schema is in place.
        store.path.write_bytes(b"garbage" * 200)
        for side in ("-wal", "-shm"):
            p = store.path.parent / (store.path.name + side)
            if p.exists():
                p.unlink()
        assert store.messages() == []                # this call degrades …
        assert store.remember([_ev("bravo")]) != []  # … the next one runs on a fresh file
        assert [m.text for m in store.messages()] == ["bravo"]
        assert list(store.path.parent.glob("knowledge.db.corrupt-*"))


class TestUpgradeAndRecall:
    def test_old_schema_gains_the_new_columns(self, tmp_path):
        import sqlite3

        path = tmp_path / "knowledge.db"
        old = knowledge._SCHEMA
        with sqlite3.connect(str(path)) as conn:
            conn.executescript(old)
            conn.execute(
                "INSERT INTO messages (key, source, channel, sender, is_bot, ts, text, url, first_seen, last_seen, seen) "
                "VALUES ('k1','slack','#eng','alice',0,?, 'older row', '', ?, ?, 1)", (time.time(),) * 3,
            )
        store = KnowledgeStore(path)
        (m,) = store.messages()
        assert m.text == "older row" and m.conv == "" and m.source_id == ""
        store.remember([_ev("new row")])
        assert len(store.messages()) == 2

    def test_reader_ids_are_kept_and_events_rebuild_identically(self, store):
        now = time.time()
        ev = _ev("can you review the rollout plan?", now=now, channel="#eng", sender="bob")
        ev.source_id = "abc123"
        ev.__dict__["conversation_id"] = "thread-9"
        ev.source_url = "slack://channel?team=T1&id=C1"
        store.remember([ev], now=now)
        (m,) = store.messages()
        assert (m.conv, m.source_id) == ("thread-9", "abc123")
        rebuilt = m.to_event()
        assert rebuilt.conversation_id == "thread-9" and rebuilt.source_id == "abc123"
        assert rebuilt.title == "#eng" and rebuilt.sender == "bob" and rebuilt.source_url == ev.source_url
        assert rebuilt.plain_text_extract == ev.plain_text_extract and rebuilt.timestamp == ev.timestamp
        assert rebuilt.source.value == "slack" and rebuilt.is_auto_generated is False
        assert keys_for([rebuilt]) == [m.key]        # same identity → the live copy excludes it

    def test_recall_returns_the_window_minus_what_is_live_and_skips_snapshots(self, store):
        now = time.time()
        live = _ev("on screen now", now=now, hours_ago=0.1)
        gone = _ev("scrolled away an hour ago", now=now, hours_ago=1.0)
        mail = _ev("an email seen earlier", now=now, hours_ago=2.0)
        mail.source = "email"
        cal = _ev("📅 Standup — 09:00 AM", now=now, hours_ago=3.0)
        cal.source = "calendar"
        old = _ev("last week", now=now, hours_ago=30.0)
        store.remember([live, gone, mail, cal, old], now=now)
        got = store.recall(since=now - 24 * 3600, exclude_keys=keys_for([live]))
        assert [m.text for m in got] == ["an email seen earlier", "scrolled away an hour ago"]
