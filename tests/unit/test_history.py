"""Tests for otto.intelligence.history — conversation history persistence."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from otto import paths
from otto.intelligence.history import (
    _conversation_to_entry,
    _relative_time,
    detect_directives_in_text,
    get_recent_context,
    get_standing_directives_text,
    load_directives,
    remove_directive,
    save_conversations,
    save_directive,
)
from otto.storage.models import Conversation, Domain, SourceType


def _history_path():
    return paths.history_dir() / "conversations.jsonl"


def _make_conv(
    subject: str = "Test Subject",
    urgency: float = 0.5,
    importance: float = 0.5,
    opportunity_score: float = 0.0,
    topics: list | None = None,
    ai_analysis: str = "",
    source_url: str = "",
    thread_id: str = "",
) -> Conversation:
    """Create a test Conversation."""
    return Conversation(
        source=SourceType.SLACK,
        account_id="user@test.example",
        thread_id=thread_id or f"thread-{subject.lower().replace(' ', '-')}",
        subject=subject,
        summary="Test summary",
        domain=Domain.WORK,
        relevance_explanation="Test relevance",
        urgency=urgency,
        importance=importance,
        opportunity_score=opportunity_score,
        topics=topics or [],
        ai_analysis=ai_analysis,
        source_url=source_url,
    )


class TestIsolation:
    def test_history_lives_in_isolated_data_dir(self, _isolate_user_data):
        save_conversations([_make_conv("Isolated")])
        assert _history_path().exists()
        assert str(_history_path()).startswith(str(_isolate_user_data))

    def test_history_dir_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OTTO_HISTORY_DIR", str(tmp_path / "custom-history"))
        save_conversations([_make_conv("Custom")])
        assert (tmp_path / "custom-history" / "conversations.jsonl").exists()


class TestSaveConversations:
    """Tests for save_conversations()."""

    def test_save_empty_list(self):
        assert save_conversations([]) == 0

    def test_save_single_conversation(self):
        conv = _make_conv("Security Alert", urgency=0.9)
        assert save_conversations([conv]) == 1

        fpath = _history_path()
        assert fpath.exists()
        lines = fpath.read_text().strip().split("\n")
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert entry["subject"] == "Security Alert"
        assert entry["urgency"] == 0.9
        assert "_saved_at" in entry

    def test_save_multiple_conversations(self):
        convs = [
            _make_conv("Alert 1", urgency=0.8),
            _make_conv("Alert 2", urgency=0.5),
            _make_conv("Update", urgency=0.2),
        ]
        assert save_conversations(convs) == 3
        lines = _history_path().read_text().strip().split("\n")
        assert len(lines) == 3

    def test_deduplication_by_thread_id(self):
        save_conversations([_make_conv("Alert V1", urgency=0.5, thread_id="same-thread")])
        save_conversations([_make_conv("Alert V2", urgency=0.9, thread_id="same-thread")])

        lines = _history_path().read_text().strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0])["subject"] == "Alert V2"  # Latest wins

    def test_pruning_old_entries(self):
        fpath = _history_path()
        fpath.parent.mkdir(parents=True, exist_ok=True)
        old_time = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        fpath.write_text(json.dumps({
            "thread_id": "old-thread", "subject": "Old Entry", "_saved_at": old_time,
        }) + "\n")

        save_conversations([_make_conv("New Entry")], retention_hours=72)

        subjects = [json.loads(line)["subject"] for line in fpath.read_text().strip().split("\n")]
        assert "Old Entry" not in subjects
        assert "New Entry" in subjects

    def test_corrupt_line_resilience(self):
        fpath = _history_path()
        fpath.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        fpath.write_text(
            '{"thread_id": "good", "subject": "Good", "_saved_at": "' + now + '"}\n'
            "not valid json\n"
            '{"thread_id": "also-good", "subject": "Also Good", "_saved_at": "' + now + '"}\n'
        )

        save_conversations([_make_conv("Newer")])

        subjects = [json.loads(line)["subject"] for line in fpath.read_text().strip().split("\n")]
        assert {"Good", "Also Good", "Newer"} <= set(subjects)

    def test_does_not_auto_create_directives_from_message_text(self):
        """A message that merely contains 'make sure to' is not a user directive."""
        save_conversations([_make_conv("make sure to always review payments migration bugs")])
        assert load_directives() == []


class TestGetRecentContext:
    """Tests for get_recent_context()."""

    def test_no_history_file(self):
        result = get_recent_context()
        assert "No previous" in result or "No recent" in result

    def test_with_conversations(self):
        save_conversations([
            _make_conv("Sprint Planning", urgency=0.3, importance=0.5, topics=["planning"]),
            _make_conv("Security CVE", urgency=0.9, importance=0.9, ai_analysis="Critical vuln"),
        ])
        result = get_recent_context()
        assert "Sprint Planning" in result
        assert "Security CVE" in result
        assert "urgency=" in result
        assert "importance=" in result

    def test_max_entries_limit(self):
        save_conversations([_make_conv(f"Conv {i}") for i in range(30)])
        result = get_recent_context(max_entries=5)
        lines = [line for line in result.strip().split("\n") if line.strip()]
        assert len(lines) <= 5

    def test_context_includes_topics(self):
        save_conversations([_make_conv("Design Review", topics=["design", "ui", "frontend"])])
        assert "design" in get_recent_context()


class TestConversationToEntry:
    """Tests for _conversation_to_entry()."""

    def test_dataclass_serialization(self):
        conv = _make_conv(
            "Test Conv", urgency=0.7, importance=0.8, opportunity_score=0.6,
            topics=["security"], ai_analysis="Important finding",
            source_url="https://example.com",
        )
        now = datetime.now(timezone.utc)
        entry = _conversation_to_entry(conv, now)

        assert entry["subject"] == "Test Conv"
        assert entry["urgency"] == 0.7
        assert entry["importance"] == 0.8
        assert entry["opportunity_score"] == 0.6
        assert entry["topics"] == ["security"]
        assert entry["ai_analysis"] == "Important finding"
        assert entry["source_url"] == "https://example.com"
        assert entry["_saved_at"] == now.isoformat()

    def test_dict_serialization(self):
        now = datetime.now(timezone.utc)
        entry = _conversation_to_entry({"subject": "Dict Conv", "urgency": 0.5}, now)
        assert entry["subject"] == "Dict Conv"
        assert entry["_saved_at"] == now.isoformat()

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError):
            _conversation_to_entry("not a conv", datetime.now(timezone.utc))


class TestRelativeTime:
    """Tests for _relative_time()."""

    def test_just_now(self):
        now = datetime.now(timezone.utc)
        assert _relative_time(now.isoformat(), now) == "just now"

    def test_minutes_ago(self):
        now = datetime.now(timezone.utc)
        assert _relative_time((now - timedelta(minutes=15)).isoformat(), now) == "15m ago"

    def test_hours_ago(self):
        now = datetime.now(timezone.utc)
        assert _relative_time((now - timedelta(hours=3)).isoformat(), now) == "3h ago"

    def test_days_ago(self):
        now = datetime.now(timezone.utc)
        assert _relative_time((now - timedelta(days=2)).isoformat(), now) == "2d ago"

    def test_invalid_input(self):
        now = datetime.now(timezone.utc)
        assert _relative_time("not-a-date", now) == "?"
        assert _relative_time(None, now) == "?"


class TestStandingDirectives:
    """Tests for standing user directives."""

    def test_no_directives_by_default(self):
        """Otto ships with zero directives — they are the user's, not ours."""
        assert load_directives() == []
        assert "No standing user directives" in get_standing_directives_text()

    def test_save_new_directive(self):
        assert save_directive("Always verify database migrations in staging first") is True
        texts = [d["directive"] for d in load_directives()]
        assert "Always verify database migrations in staging first" in texts

    def test_save_directive_normalises_whitespace_and_dedupes(self):
        assert save_directive("  Flag anything   about payments  ") is True
        assert save_directive("flag anything about PAYMENTS") is False
        assert load_directives()[0]["directive"] == "Flag anything about payments"

    def test_save_directive_rejects_trivial_text(self):
        assert save_directive("   ") is False
        assert save_directive("abc") is False

    def test_remove_directive_by_text_and_index(self):
        save_directive("First rule for otto")
        save_directive("Second rule for otto")
        assert remove_directive("FIRST rule for otto") is True
        assert [d["directive"] for d in load_directives()] == ["Second rule for otto"]
        assert remove_directive(1) is True
        assert load_directives() == []
        assert remove_directive(1) is False
        assert remove_directive("nope") is False

    def test_detect_directives_in_text_is_suggestion_only(self):
        text = (
            "Hey team,\n"
            "make sure to always review payments migration bugs\n"
            "random chat line\n"
            "Identified as directly relevant to standing directive: \"make sure to x\"\n"
            "Directive: make sure to y\n"
        )
        detected = detect_directives_in_text(text)
        assert detected == ["make sure to always review payments migration bugs"]
        assert load_directives() == []  # nothing saved

    def test_get_standing_directives_text(self):
        save_directive("Always review payments PRs", category="reviews")
        text = get_standing_directives_text()
        assert "- Always review payments PRs [reviews]" in text

    def test_corrupt_directive_lines_are_skipped(self):
        path = paths.history_dir() / "directives.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"directive": "Good one here"}\nnot json\n')
        assert [d["directive"] for d in load_directives()] == ["Good one here"]
