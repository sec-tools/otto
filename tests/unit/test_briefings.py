from __future__ import annotations

"""
Tests for briefing generator — morning, catch-me-up, and meeting prep.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from otto.briefings.generator import BriefingGenerator
from otto.core.event_bus import EventBus
from otto.storage.models import (
    ActionItem,
    ActionStatus,
    BriefingReady,
    BriefingType,
    Conversation,
    Domain,
    SourceType,
)


class TestMorningBriefing:
    """Test morning briefing generation."""

    def _make_conv(self, **kwargs) -> Conversation:
        defaults = dict(
            source=SourceType.EMAIL, account_id="test",
            thread_id="t1", subject="Test Thread", summary="A test",
            domain=Domain.WORK, relevance_explanation="Important",
            urgency=0.5, importance=0.5,
            last_activity=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        defaults.update(kwargs)
        return Conversation(**defaults)

    @pytest.mark.asyncio
    async def test_generates_morning_briefing(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)
        conv = self._make_conv(urgency=0.9, open_actions=["Review proposal"])
        briefing = await gen.generate_morning_briefing([conv], [])

        assert briefing.type == BriefingType.MORNING
        assert len(briefing.sections) >= 1
        assert briefing.content_hash != ""

    @pytest.mark.asyncio
    async def test_urgent_items_section(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)
        conv = self._make_conv(
            subject="Q3 Budget", urgency=0.9,
            open_actions=["Submit budget"],
            relevance_explanation="You're the budget owner",
        )
        briefing = await gen.generate_morning_briefing([conv], [])

        assert any("Attention" in s.title for s in briefing.sections)

    @pytest.mark.asyncio
    async def test_opportunity_section(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)
        conv = self._make_conv(
            subject="New Role", opportunity_score=0.8,
        )
        briefing = await gen.generate_morning_briefing([conv], [])

        assert any("Opportunities" in s.title for s in briefing.sections)

    @pytest.mark.asyncio
    async def test_overnight_updates(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)
        now = datetime.now(timezone.utc)
        conv = self._make_conv(
            subject="Overnight Issue",
            importance=0.7,
            last_activity=now - timedelta(hours=5),
        )
        briefing = await gen.generate_morning_briefing([conv], [], now=now)

        assert any("Away" in s.title for s in briefing.sections)

    @pytest.mark.asyncio
    async def test_open_actions_section(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)
        action = ActionItem(
            description="Submit quarterly report",
            owner_id="user",
            domain=Domain.WORK,
            deadline=datetime.now(timezone.utc) + timedelta(days=2),
        )
        briefing = await gen.generate_morning_briefing([], [action])

        assert any("Actions" in s.title for s in briefing.sections)

    @pytest.mark.asyncio
    async def test_all_clear_when_empty(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)
        briefing = await gen.generate_morning_briefing([], [])

        assert any("Clear" in s.title for s in briefing.sections)

    @pytest.mark.asyncio
    async def test_publishes_briefing_ready(self):
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        await bus.subscribe(BriefingReady, handler)

        gen = BriefingGenerator(event_bus=bus)
        await gen.generate_morning_briefing([], [])
        await asyncio.sleep(0.05)

        assert len(received) == 1
        assert received[0].briefing_type == BriefingType.MORNING

    @pytest.mark.asyncio
    async def test_idempotent_hash(self):
        """Same input should produce same content hash."""
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)
        conv = self._make_conv(urgency=0.9, open_actions=["Do thing"])
        now = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)

        b1 = await gen.generate_morning_briefing([conv], [], now=now)
        b2 = await gen.generate_morning_briefing([conv], [], now=now)

        assert b1.content_hash == b2.content_hash

    @pytest.mark.asyncio
    async def test_sections_sorted_by_priority(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)
        now = datetime.now(timezone.utc)
        convs = [
            self._make_conv(
                thread_id="urgent", subject="Urgent", urgency=0.95,
                open_actions=["Do now"], last_activity=now - timedelta(hours=3),
                importance=0.6,
            ),
            self._make_conv(
                thread_id="opp", subject="Opportunity", opportunity_score=0.8,
                last_activity=now - timedelta(hours=5), importance=0.5,
            ),
        ]
        actions = [ActionItem(
            description="Task", owner_id="user", domain=Domain.WORK,
        )]
        briefing = await gen.generate_morning_briefing(convs, actions, now=now)

        priorities = [s.priority for s in briefing.sections]
        assert priorities == sorted(priorities)  # Ascending by priority


class TestCatchMeUp:
    """Test catch-me-up (delta) briefing."""

    @pytest.mark.asyncio
    async def test_shows_only_new(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=3)

        old_conv = Conversation(
            source=SourceType.EMAIL, account_id="t", thread_id="old",
            subject="Old", summary="Old", domain=Domain.WORK,
            relevance_explanation="", urgency=0.8, importance=0.8,
            last_activity=now - timedelta(hours=5),
        )
        new_conv = Conversation(
            source=SourceType.EMAIL, account_id="t", thread_id="new",
            subject="New Important", summary="New", domain=Domain.WORK,
            relevance_explanation="", urgency=0.8, importance=0.8,
            last_activity=now - timedelta(hours=1),
        )
        briefing = await gen.generate_catch_me_up([old_conv, new_conv], since, now=now)

        # Should show new_conv but not old_conv
        full_content = " ".join(s.content for s in briefing.sections)
        assert "New Important" in full_content

    @pytest.mark.asyncio
    async def test_nothing_new(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)
        since = datetime.now(timezone.utc) - timedelta(minutes=30)
        briefing = await gen.generate_catch_me_up([], since)

        assert any("Nothing" in s.title for s in briefing.sections)

    @pytest.mark.asyncio
    async def test_noise_filter_summary(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=2)

        convs = [
            Conversation(
                source=SourceType.EMAIL, account_id="t", thread_id=f"conv_{i}",
                subject=f"Conv {i}", summary="", domain=Domain.WORK,
                relevance_explanation="", urgency=0.1, importance=0.1,
                last_activity=now - timedelta(hours=1),
            )
            for i in range(5)
        ]
        briefing = await gen.generate_catch_me_up(convs, since, now=now)

        # Low priority items should be mentioned as filtered
        full_content = " ".join(s.content for s in briefing.sections)
        assert "filtered" in full_content.lower() or "Nothing" in full_content


class TestMeetingPrep:
    """Test meeting prep briefing."""

    @pytest.mark.asyncio
    async def test_gathers_participant_context(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)

        conv = Conversation(
            source=SourceType.EMAIL, account_id="t", thread_id="related",
            subject="Budget Discussion", summary="Budget review thread",
            domain=Domain.WORK, relevance_explanation="",
            participants=["alice@company.example", "bob@company.example"],
        )
        briefing = await gen.generate_meeting_prep(
            meeting_title="Q3 Review",
            participants=["alice@company.example"],
            conversations=[conv],
            actions=[],
        )

        full_content = " ".join(s.content for s in briefing.sections)
        assert "Budget Discussion" in full_content

    @pytest.mark.asyncio
    async def test_includes_related_actions(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)

        action = ActionItem(
            description="Prepare budget slides",
            owner_id="alice@company.example",
            domain=Domain.WORK,
            status=ActionStatus.IN_PROGRESS,
        )
        briefing = await gen.generate_meeting_prep(
            meeting_title="Q3 Review",
            participants=["alice@company.example"],
            conversations=[],
            actions=[action],
        )

        full_content = " ".join(s.content for s in briefing.sections)
        assert "budget slides" in full_content.lower()

    @pytest.mark.asyncio
    async def test_no_context_shows_empty(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)

        briefing = await gen.generate_meeting_prep(
            meeting_title="Standup",
            participants=["nobody@company.example"],
            conversations=[],
            actions=[],
        )

        full_content = " ".join(s.content for s in briefing.sections)
        assert "No related context" in full_content


class TestBriefingIdempotencyCache:
    """Test 5-minute idempotency cache from the original spec."""

    @pytest.mark.asyncio
    async def test_idempotency_cache_returns_same_instance(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)
        now = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)

        conv = Conversation(
            source=SourceType.EMAIL, account_id="t", thread_id="t1",
            subject="Same Subject", summary="Summary",
            domain=Domain.WORK, relevance_explanation="Rel",
            urgency=0.8, open_actions=["Action 1"],
        )

        b1 = await gen.generate_morning_briefing([conv], [], now=now)
        # Re-request 2 minutes later
        b2 = await gen.generate_morning_briefing([conv], [], now=now + timedelta(minutes=2))

        assert b1.id == b2.id
        assert b1.content_hash == b2.content_hash

    @pytest.mark.asyncio
    async def test_idempotency_cache_refreshes_after_5_minutes(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)
        now = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)

        conv = Conversation(
            source=SourceType.EMAIL, account_id="t", thread_id="t1",
            subject="Same Subject", summary="Summary",
            domain=Domain.WORK, relevance_explanation="Rel",
            urgency=0.8, open_actions=["Action 1"],
        )

        b1 = await gen.generate_morning_briefing([conv], [], now=now)
        # Re-request 6 minutes later
        b2 = await gen.generate_morning_briefing([conv], [], now=now + timedelta(minutes=6))

        assert b1.id != b2.id  # New briefing generated


class TestEODRecapBriefing:
    """Test End-of-Day recap briefings."""

    @pytest.mark.asyncio
    async def test_generate_eod_recap_with_completed_and_pending(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)
        now = datetime(2026, 1, 1, 17, 30, tzinfo=timezone.utc)

        actions = [
            ActionItem(
                description="Deployed service", owner_id="me",
                domain=Domain.WORK, status=ActionStatus.COMPLETED,
            ),
            ActionItem(
                description="Review security audit", owner_id="me",
                domain=Domain.WORK, status=ActionStatus.OPEN,
            ),
        ]
        convs = [
            Conversation(
                source=SourceType.SLACK, account_id="t", thread_id="s1",
                subject="#prod-deploy", summary="Release complete",
                domain=Domain.WORK, relevance_explanation="Rel",
                last_activity=now - timedelta(hours=2),
            )
        ]

        briefing = await gen.generate_eod_recap(convs, actions, now=now)
        assert briefing.type == BriefingType.EOD
        section_titles = [s.title for s in briefing.sections]
        assert any("Completed" in t for t in section_titles)
        assert any("Pending" in t for t in section_titles)


class TestWeeklyDigestBriefing:
    """Test Weekly digest briefings."""

    @pytest.mark.asyncio
    async def test_generate_weekly_digest(self):
        bus = EventBus()
        gen = BriefingGenerator(event_bus=bus)
        now = datetime(2026, 1, 7, 16, 0, tzinfo=timezone.utc)

        convs = [
            Conversation(
                source=SourceType.EMAIL, account_id="t", thread_id="e1",
                subject="Q3 Planning Kickoff", summary="Goals set",
                domain=Domain.WORK, relevance_explanation="Lead",
                importance=0.8, last_activity=now - timedelta(days=2),
            )
        ]
        actions = [
            ActionItem(
                description="Finalize roadmap", owner_id="me",
                domain=Domain.WORK, status=ActionStatus.IN_PROGRESS,
            )
        ]
        people = [MagicMock(display_name="Alice Smith"), MagicMock(display_name="Bob Jones")]

        briefing = await gen.generate_weekly_digest(convs, actions, top_people=people, now=now)
        assert briefing.type == BriefingType.WEEKLY
        full_content = " ".join(s.content for s in briefing.sections)
        assert "Alice Smith" in full_content
        assert "Q3 Planning Kickoff" in full_content
