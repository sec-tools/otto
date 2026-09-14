from __future__ import annotations

"""
Tests for Phase 4 — failure journal, drift detector, statistical patterns,
temporal engine, and Jira adapter.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.adapters.jira import JiraAdapter
from otto.intelligence.drift import DriftDetector
from otto.intelligence.failure_journal import FailureJournal, FailureRecord
from otto.intelligence.patterns import StatisticalPatternTracker
from otto.intelligence.temporal import TemporalEngine
from otto.storage.models import ActionItem, ConnectionState, Domain, HealthStatus, SourceType


# =============================================================================
# Failure Journal Tests
# =============================================================================


class TestFailureJournal:
    """Test antifragile failure recording and pattern analysis."""

    def test_record_failure(self):
        journal = FailureJournal()
        journal.record(FailureRecord(
            failure_type="thumbs_down", source_id="conv_1",
        ))
        assert journal.record_count == 1

    def test_record_thumbs_down(self):
        journal = FailureJournal()
        journal.record_thumbs_down("conv_1", reason="not relevant")
        assert journal.record_count == 1

    def test_record_missed_item(self):
        journal = FailureJournal()
        journal.record_missed_item("conv_2", otto_rank=0.1)
        records = journal.get_failures_since(
            datetime.now(timezone.utc) - timedelta(hours=1)
        )
        assert len(records) == 1
        assert records[0].context["otto_rank"] == 0.1

    def test_record_api_timeout(self):
        journal = FailureJournal()
        journal.record_api_timeout("jira", "/rest/api/2/search")
        assert journal.record_count == 1

    def test_record_write_guard(self):
        journal = FailureJournal()
        journal.record_write_guard("POST", "https://api.slack.com/chat.postMessage")
        assert journal.record_count == 1

    def test_record_hallucination(self):
        journal = FailureJournal()
        journal.record_hallucination("Here's a draft email...", "classify")
        assert journal.record_count == 1

    def test_max_records_cap(self):
        journal = FailureJournal()
        journal._max_records = 100
        for i in range(150):
            journal.record(FailureRecord(failure_type="test", source_id=str(i)))
        assert journal.record_count == 100

    def test_weekly_analysis_noisy_sender(self):
        """Repeated thumbs-downs from same sender should produce anti-pattern."""
        journal = FailureJournal()
        for _ in range(5):
            journal.record(FailureRecord(
                failure_type="thumbs_down",
                source_id="noreply@github.com",
                context={"sender": "noreply@github.com"},
            ))
        patterns = journal.analyze_weekly()
        assert len(patterns) >= 1
        assert any("noreply@github.com" in p.description for p in patterns)

    def test_weekly_analysis_timeout_pattern(self):
        """Clustered timeouts should detect temporal pattern."""
        journal = FailureJournal()
        for _ in range(3):
            journal.record(FailureRecord(
                failure_type="api_timeout",
                source_id="jira",
                context={"endpoint": "/search"},
                timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc),
            ))
        patterns = journal.analyze_weekly(
            now=datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)
        )
        timeout_patterns = [p for p in patterns if "timeout" in p.pattern_id]
        assert len(timeout_patterns) >= 1

    def test_weekly_analysis_missed_items(self):
        """Clustered missed items should produce anti-pattern."""
        journal = FailureJournal()
        for _ in range(4):
            journal.record_missed_item("conv", otto_rank=0.15)
        patterns = journal.analyze_weekly()
        assert any("missed" in p.pattern_id for p in patterns)

    def test_sender_penalty(self):
        """After analysis, noisy sender should get penalty."""
        journal = FailureJournal()
        for _ in range(5):
            journal.record(FailureRecord(
                failure_type="thumbs_down", source_id="spam@co.example",
                context={"sender": "spam@co.example"},
            ))
        journal.analyze_weekly()
        penalty = journal.get_sender_penalty("spam@co.example")
        assert penalty < 0

    def test_no_penalty_unknown_sender(self):
        journal = FailureJournal()
        assert journal.get_sender_penalty("unknown@co.example") == 0.0

    def test_failure_rate(self):
        journal = FailureJournal()
        journal.record_thumbs_down("c1")
        journal.record_thumbs_down("c2")
        journal.record_api_timeout("jira", "/api")
        journal.record_api_timeout("jira", "/api")
        rate = journal.get_failure_rate(window_hours=48)
        assert rate == 0.5  # 2 thumbs-down out of 4

    def test_empty_analysis(self):
        journal = FailureJournal()
        patterns = journal.analyze_weekly()
        assert patterns == []


# =============================================================================
# Drift Detector Tests
# =============================================================================


class TestDriftDetector:
    """Test drift detection."""

    def test_no_drift_clean(self):
        detector = DriftDetector()
        signals = detector.check()
        assert signals == []

    def test_high_thumbs_down_triggers(self):
        journal = FailureJournal()
        # Record many thumbs-downs and a few other failures
        for i in range(8):
            journal.record_thumbs_down(f"c{i}")
        for i in range(2):
            journal.record_api_timeout("jira", "/api")

        detector = DriftDetector(failure_journal=journal)
        signals = detector.check()
        assert any(s.trigger == "high_thumbs_down_rate" for s in signals)

    def test_new_senders_triggers(self):
        detector = DriftDetector()
        detector.set_baseline(
            domain_distribution={"work": 0.7, "personal": 0.3},
            known_senders={"alice@co.example", "bob@co.example"},
        )
        new_senders = {f"new{i}@co.example" for i in range(6)}
        signals = detector.check(current_senders=new_senders | {"alice@co.example"})
        assert any(s.trigger == "new_senders" for s in signals)

    def test_domain_shift_triggers(self):
        detector = DriftDetector()
        detector.set_baseline(
            domain_distribution={"work": 0.7, "personal": 0.3},
            known_senders=set(),
        )
        signals = detector.check(
            current_domain_distribution={"work": 0.3, "personal": 0.7},
        )
        assert any(s.trigger == "domain_shift" for s in signals)

    def test_no_domain_shift_small(self):
        detector = DriftDetector()
        detector.set_baseline(
            domain_distribution={"work": 0.7, "personal": 0.3},
            known_senders=set(),
        )
        signals = detector.check(
            current_domain_distribution={"work": 0.65, "personal": 0.35},
        )
        domain_shifts = [s for s in signals if s.trigger == "domain_shift"]
        assert len(domain_shifts) == 0

    def test_new_source_always_triggers(self):
        detector = DriftDetector()
        signal = detector.check_new_source("slack")
        assert signal.trigger == "new_source"
        assert "slack" in signal.description

    def test_drift_has_recommendation(self):
        journal = FailureJournal()
        for i in range(10):
            journal.record_thumbs_down(f"c{i}")
        detector = DriftDetector(failure_journal=journal)
        signals = detector.check()
        assert all(s.recommendation for s in signals)


# =============================================================================
# Statistical Pattern Tests
# =============================================================================


class TestStatisticalPatterns:
    """Test sender/channel/temporal pattern tracking."""

    def test_sender_precision_default(self):
        tracker = StatisticalPatternTracker()
        assert tracker.get_sender_precision("unknown") == 0.5

    def test_sender_precision_high(self):
        tracker = StatisticalPatternTracker()
        for _ in range(9):
            tracker.record_sender_feedback("alice@co.example", useful=True)
        tracker.record_sender_feedback("alice@co.example", useful=False)
        assert tracker.get_sender_precision("alice@co.example") == 0.9

    def test_sender_precision_low(self):
        tracker = StatisticalPatternTracker()
        tracker.record_sender_feedback("spam@co.example", useful=True)
        for _ in range(9):
            tracker.record_sender_feedback("spam@co.example", useful=False)
        assert tracker.get_sender_precision("spam@co.example") == 0.1

    def test_top_senders(self):
        tracker = StatisticalPatternTracker()
        # Alice: 90% precision
        for _ in range(9):
            tracker.record_sender_feedback("alice", useful=True)
        tracker.record_sender_feedback("alice", useful=False)
        # Bob: 30% precision
        for _ in range(3):
            tracker.record_sender_feedback("bob", useful=True)
        for _ in range(7):
            tracker.record_sender_feedback("bob", useful=False)

        top = tracker.get_top_senders(5)
        assert len(top) == 2
        assert top[0].sender_id == "alice"

    def test_noise_senders(self):
        tracker = StatisticalPatternTracker()
        for _ in range(10):
            tracker.record_sender_feedback("noreply@gh.example", useful=False)
        noise = tracker.get_noise_senders()
        assert any(s.sender_id == "noreply@gh.example" for s in noise)

    def test_channel_sn_ratio(self):
        tracker = StatisticalPatternTracker()
        for _ in range(8):
            tracker.record_channel_feedback("C_deploys", useful=True)
        for _ in range(2):
            tracker.record_channel_feedback("C_deploys", useful=False)
        assert tracker.get_channel_sn_ratio("C_deploys") == 0.8

    def test_noisy_channels(self):
        tracker = StatisticalPatternTracker()
        for _ in range(10):
            tracker.record_channel_feedback("C_random", useful=False)
        noisy = tracker.get_noisy_channels()
        assert any(c.channel_id == "C_random" for c in noisy)

    def test_temporal_patterns(self):
        tracker = StatisticalPatternTracker()
        # Simulate important items at 9-11am
        for _ in range(20):
            tracker.record_temporal_event(9, is_important=True)
            tracker.record_temporal_event(10, is_important=True)
            tracker.record_temporal_event(11, is_important=True)
        # And noise at other hours
        for hour in range(24):
            for _ in range(10):
                tracker.record_temporal_event(hour, is_important=False)

        patterns = tracker.detect_temporal_patterns()
        hot = [p for p in patterns if p.pattern_type == "important_hours"]
        if hot:
            assert any(9 in p.hours or 10 in p.hours for p in hot)

    def test_sender_weight_boost(self):
        tracker = StatisticalPatternTracker()
        for _ in range(9):
            tracker.record_sender_feedback("vp@co.example", useful=True)
        tracker.record_sender_feedback("vp@co.example", useful=False)
        weight = tracker.get_sender_weight("vp@co.example")
        assert weight > 0

    def test_sender_weight_penalty(self):
        tracker = StatisticalPatternTracker()
        tracker.record_sender_feedback("spam@co.example", useful=True)
        for _ in range(9):
            tracker.record_sender_feedback("spam@co.example", useful=False)
        weight = tracker.get_sender_weight("spam@co.example")
        assert weight < 0

    def test_sender_weight_neutral(self):
        tracker = StatisticalPatternTracker()
        for _ in range(5):
            tracker.record_sender_feedback("avg@co.example", useful=True)
        for _ in range(5):
            tracker.record_sender_feedback("avg@co.example", useful=False)
        weight = tracker.get_sender_weight("avg@co.example")
        assert weight == 0.0


# =============================================================================
# Temporal Engine Tests
# =============================================================================


class TestTemporalEngine:
    """Test deadline detection, conflicts, and recurring patterns."""

    def _make_action(self, hours_until: float = 24, desc: str = "Review PR") -> ActionItem:
        now = datetime.now(timezone.utc)
        return ActionItem(
            description=desc,
            owner_id="user",
            domain=Domain.WORK,
            deadline=now + timedelta(hours=hours_until),
            source_conversation_ids=["conv_1"],
        )

    def test_detect_approaching_deadline(self):
        engine = TemporalEngine()
        action = self._make_action(hours_until=12)
        alerts = engine.detect_deadlines([action])
        assert len(alerts) == 1
        assert not alerts[0].is_overdue

    def test_detect_overdue_deadline(self):
        engine = TemporalEngine()
        action = self._make_action(hours_until=-5)
        alerts = engine.detect_deadlines([action])
        assert len(alerts) == 1
        assert alerts[0].is_overdue

    def test_no_alert_far_deadline(self):
        engine = TemporalEngine()
        action = self._make_action(hours_until=100)
        alerts = engine.detect_deadlines([action])
        assert len(alerts) == 0

    def test_no_alert_no_deadline(self):
        engine = TemporalEngine()
        action = ActionItem(
            description="No deadline task", owner_id="user", domain=Domain.WORK,
        )
        alerts = engine.detect_deadlines([action])
        assert len(alerts) == 0

    def test_sorted_overdue_first(self):
        engine = TemporalEngine()
        alerts = engine.detect_deadlines([
            self._make_action(hours_until=40, desc="later"),
            self._make_action(hours_until=-2, desc="overdue"),
            self._make_action(hours_until=10, desc="soon"),
        ])
        assert alerts[0].is_overdue
        assert alerts[0].description == "overdue"

    def test_detect_deadline_mentions(self):
        engine = TemporalEngine()
        text = "Please have this done by Friday. Need this by tomorrow."
        mentions = engine.detect_deadline_mentions(text)
        assert len(mentions) >= 2

    def test_detect_commitments(self):
        engine = TemporalEngine()
        text = "I'll have it by Thursday. I will send the report by Friday."
        commitments = engine.detect_commitments(text)
        assert len(commitments) >= 1

    def test_detect_no_deadlines_in_normal_text(self):
        engine = TemporalEngine()
        text = "Here are the meeting notes from our standup."
        mentions = engine.detect_deadline_mentions(text)
        assert len(mentions) == 0

    def test_detect_conflicts(self):
        engine = TemporalEngine()
        thursday = datetime(2026, 3, 5, 10, 0, tzinfo=timezone.utc)
        commitments = [{"description": "Submit report", "date": thursday}]
        calendar = [
            {"title": f"Meeting {i}", "date": thursday}
            for i in range(6)
        ]
        conflicts = engine.detect_conflicts(commitments, calendar)
        assert len(conflicts) == 1
        assert conflicts[0].severity > 0

    def test_no_conflict_on_free_day(self):
        engine = TemporalEngine()
        thursday = datetime(2026, 3, 5, 10, 0, tzinfo=timezone.utc)
        commitments = [{"description": "Submit report", "date": thursday}]
        calendar = [{"title": "Standup", "date": thursday}]
        conflicts = engine.detect_conflicts(commitments, calendar)
        assert len(conflicts) == 0

    def test_detect_weekly_pattern(self):
        engine = TemporalEngine()
        base = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
        dates = [
            ("Sprint Retro", base + timedelta(weeks=i))
            for i in range(5)
        ]
        patterns = engine.detect_recurring_patterns(dates)
        assert len(patterns) >= 1
        assert any(p.pattern == "weekly" for p in patterns)

    def test_detect_monthly_pattern(self):
        engine = TemporalEngine()
        dates = [
            ("Board Meeting", datetime(2026, i, 15, 10, 0, tzinfo=timezone.utc))
            for i in range(1, 6)
        ]
        patterns = engine.detect_recurring_patterns(dates)
        assert len(patterns) >= 1
        assert any(p.pattern == "monthly" for p in patterns)

    def test_no_pattern_insufficient_data(self):
        engine = TemporalEngine()
        dates = [("Event", datetime(2026, 1, 1, tzinfo=timezone.utc))]
        patterns = engine.detect_recurring_patterns(dates)
        assert len(patterns) == 0


# =============================================================================
# Jira Adapter Tests
# =============================================================================


class TestJiraAdapter:
    """Test read-only Jira adapter."""

    def setup_method(self):
        self.mock_http = AsyncMock()
        self.adapter = JiraAdapter(self.mock_http, "https://myco.atlassian.net", "user@co.example")

    def test_name(self):
        assert self.adapter.name == "jira:user@co.example"

    def test_source_type(self):
        assert self.adapter.source_type == SourceType.JIRA

    def test_no_write_methods(self):
        methods = [m for m in dir(self.adapter) if not m.startswith("_")]
        write_words = {"send", "post", "write", "update", "delete", "modify", "create", "transition"}
        for method in methods:
            for word in write_words:
                assert word not in method.lower(), f"Write method found: {method}"

    @pytest.mark.asyncio
    async def test_connect_success(self):
        self.mock_http.get = AsyncMock(return_value=MagicMock(status_code=200))
        result = await self.adapter.connect()
        assert result.state == ConnectionState.HEALTHY

    @pytest.mark.asyncio
    async def test_connect_auth_failure(self):
        self.mock_http.get = AsyncMock(return_value=MagicMock(status_code=401))
        result = await self.adapter.connect()
        assert result.state == ConnectionState.FAILED

    @pytest.mark.asyncio
    async def test_poll_when_not_connected(self):
        result = await self.adapter.poll(datetime.now(timezone.utc))
        assert result == []

    @pytest.mark.asyncio
    async def test_poll_with_issues(self):
        self.mock_http.get = AsyncMock(return_value=MagicMock(status_code=200))
        await self.adapter.connect()

        issues_resp = MagicMock(
            status_code=200,
            json=MagicMock(return_value={
                "issues": [
                    {
                        "key": "PLAT-123",
                        "fields": {
                            "summary": "Fix login timeout",
                            "status": {"name": "In Progress"},
                            "priority": {"name": "High"},
                            "issuetype": {"name": "Bug"},
                            "project": {"key": "PLAT"},
                            "updated": "2026-01-01T10:00:00+00:00",
                            "reporter": {"displayName": "Alice", "emailAddress": "alice@co.example"},
                            "assignee": {"displayName": "Bob"},
                            "description": "Users report login timeout after 30s",
                            "comment": {
                                "comments": [
                                    {"body": "Investigating the root cause", "author": {"displayName": "Bob"}},
                                ],
                            },
                        },
                    },
                ],
            }),
        )
        self.mock_http.get = AsyncMock(return_value=issues_resp)
        events = await self.adapter.poll(datetime(2026, 1, 1, tzinfo=timezone.utc))

        assert len(events) == 1
        assert events[0].source == SourceType.JIRA
        assert "PLAT-123" in events[0].title
        assert events[0].raw_metadata["status"] == "In Progress"
        assert events[0].raw_metadata["priority"] == "High"

    def test_issue_parsing(self):
        issue = {
            "key": "DEV-42",
            "fields": {
                "summary": "Implement caching",
                "status": {"name": "Open"},
                "priority": {"name": "Medium"},
                "issuetype": {"name": "Story"},
                "project": {"key": "DEV"},
                "updated": "2026-03-01T09:00:00Z",
                "reporter": {"displayName": "Alice", "emailAddress": "alice@co.example"},
                "assignee": {"displayName": "Bob"},
                "description": "Add Redis caching layer",
                "comment": {"comments": []},
            },
        }
        event = self.adapter._issue_to_event(issue)
        assert event is not None
        assert event.title == "[DEV-42] Implement caching"
        assert event.thread_id == "DEV-42"
        assert event.raw_metadata["comment_count"] == 0

    def test_issue_with_comments(self):
        issue = {
            "key": "BUG-99",
            "fields": {
                "summary": "Memory leak",
                "status": {"name": "Reopened"},
                "priority": {"name": "Critical"},
                "issuetype": {"name": "Bug"},
                "project": {"key": "BUG"},
                "updated": "2026-06-15T12:00:00Z",
                "reporter": {},
                "assignee": {},
                "description": "",
                "comment": {
                    "comments": [
                        {"body": "First comment"},
                        {"body": "Latest update: still leaking"},
                    ],
                },
            },
        }
        event = self.adapter._issue_to_event(issue)
        assert event is not None
        assert "Latest update" in event.plain_text
        assert event.raw_metadata["comment_count"] == 2

    @pytest.mark.asyncio
    async def test_health_check(self):
        self.adapter._connected = True
        self.mock_http.get = AsyncMock(return_value=MagicMock(status_code=200))
        status = await self.adapter.health_check()
        assert status == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_disconnect(self):
        self.adapter._connected = True
        await self.adapter.disconnect()
        assert self.adapter._connected is False

    @pytest.mark.asyncio
    async def test_jira_poll_pagination(self):
        self.adapter._connected = True
        # First page has total=2, returns issue 1; second page returns issue 2
        page1 = MagicMock(status_code=200, json=MagicMock(return_value={
            "total": 2,
            "issues": [{
                "key": "P-1", "fields": {
                    "summary": "Issue 1", "status": {"name": "Open"},
                    "priority": {"name": "P1"}, "issuetype": {"name": "Bug"},
                    "project": {"key": "P"}, "updated": "2026-01-01T00:00:00Z",
                    "reporter": {}, "assignee": {}, "comment": {"comments": []}
                }
            }]
        }))
        page2 = MagicMock(status_code=200, json=MagicMock(return_value={
            "total": 2,
            "issues": [{
                "key": "P-2", "fields": {
                    "summary": "Issue 2", "status": {"name": "Closed"},
                    "priority": {"name": "P2"}, "issuetype": {"name": "Task"},
                    "project": {"key": "P"}, "updated": "2026-01-01T01:00:00Z",
                    "reporter": {}, "assignee": {}, "comment": {"comments": []}
                }
            }]
        }))
        self.mock_http.get = AsyncMock(side_effect=[page1, page2])

        events = await self.adapter.poll(datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert len(events) == 2
        assert events[0].source_id == "P-1"
        assert events[1].source_id == "P-2"


class TestDriftSerialization:
    """Test drift detector state persistence."""

    def test_drift_to_dict_and_from_dict(self):
        detector = DriftDetector()
        detector.set_baseline(
            domain_distribution={"work": 0.8, "personal": 0.2},
            known_senders={"boss@co.example", "colleague@co.example"},
        )
        data = detector.to_dict()
        assert "work" in data["domain_baseline"]
        assert "boss@co.example" in data["sender_baseline"]

        restored = DriftDetector.from_dict(data)
        assert restored._domain_baseline["work"] == 0.8
        assert "colleague@co.example" in restored._sender_baseline
