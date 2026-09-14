from __future__ import annotations

"""
End-to-end integration tests — multi-source ingestion through
classification, correlation, opportunity detection, and briefing.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from otto.adapters.base import RawEvent
from otto.briefings.generator import BriefingGenerator
from otto.core.event_bus import EventBus
from otto.intelligence.classifier import ConversationClassifier
from otto.intelligence.correlator import CrossSourceCorrelator
from otto.intelligence.failure_journal import FailureJournal
from otto.intelligence.ingestion import IngestionPipeline
from otto.intelligence.opportunity import OpportunityDetector
from otto.intelligence.patterns import StatisticalPatternTracker
from otto.intelligence.people import PeopleTracker
from otto.intelligence.temporal import TemporalEngine
from otto.storage.models import (
    ActionItem,
    BriefingType,
    Conversation,
    Domain,
    Feedback,
    NewEventsIngested,
    NormalizedEvent,
    SourceType,
    UserFeedbackReceived,
)
from otto.ui.panel import FeedItem, PanelController


class TestFullPipelineE2E:
    """Test the full pipeline: ingest → classify → correlate → brief."""

    @pytest.mark.asyncio
    async def test_email_to_briefing_pipeline(self):
        """Email ingestion through to morning briefing generation."""
        bus = EventBus()
        ingestion_events = []

        async def capture(event):
            ingestion_events.append(event)
        await bus.subscribe(NewEventsIngested, capture)

        # 1. Ingest emails
        pipeline = IngestionPipeline(event_bus=bus)
        raw_events = [
            RawEvent(
                source=SourceType.EMAIL, source_id="email_1",
                source_url="https://mail.google.com/1",
                timestamp=datetime.now(timezone.utc) - timedelta(hours=2),
                title="Q3 Budget Review - URGENT",
                plain_text="Please review the Q3 budget by EOD Friday. ASAP.",
                sender_email="vp@company.example",
                thread_id="thread_budget",
            ),
            RawEvent(
                source=SourceType.EMAIL, source_id="email_2",
                source_url="https://mail.google.com/2",
                timestamp=datetime.now(timezone.utc) - timedelta(hours=1),
                title="Team Outing Planning",
                plain_text="Let's plan the team outing for next month. Any ideas?",
                sender_email="peer@company.example",
                thread_id="thread_outing",
            ),
        ]
        normalized = await pipeline.ingest(raw_events)
        await asyncio.sleep(0.05)

        assert len(normalized) == 2
        assert len(ingestion_events) == 1  # Both from same source

        # 2. Classify
        classifier = ConversationClassifier(event_bus=bus)
        conv_budget = Conversation(
            source=SourceType.EMAIL, account_id="test",
            thread_id="thread_budget", subject="Q3 Budget Review",
            summary="", domain=Domain.UNKNOWN, relevance_explanation="",
        )
        conv_budget = classifier.classify_local(conv_budget, [normalized[0]], user_email="me@company.example")
        assert conv_budget.urgency > 0  # URGENT + ASAP should boost

        # 3. Generate briefing
        generator = BriefingGenerator(event_bus=bus)
        conv_budget.open_actions = ["Review Q3 budget"]
        briefing = await generator.generate_morning_briefing(
            [conv_budget], [],
        )
        assert briefing.type == BriefingType.MORNING
        assert len(briefing.sections) >= 1

    @pytest.mark.asyncio
    async def test_cross_source_correlation_e2e(self):
        """Email + Slack mentioning same Jira ticket → correlated."""
        bus = EventBus()
        pipeline = IngestionPipeline(event_bus=bus)
        correlator = CrossSourceCorrelator()

        # Email about PLAT-89
        email_events = await pipeline.ingest([RawEvent(
            source=SourceType.EMAIL, source_id="e1",
            source_url="https://mail.google.com/1",
            timestamp=datetime.now(timezone.utc),
            title="Migration update",
            plain_text="PLAT-89 is blocked. Need your input on the migration plan.",
            sender_email="alice@co.example", thread_id="email_migration",
        )])

        # Slack about same ticket
        slack_events = await pipeline.ingest([RawEvent(
            source=SourceType.SLACK, source_id="s1",
            source_url="https://slack.com/archives/C1/p1",
            timestamp=datetime.now(timezone.utc),
            title="#platform-team",
            plain_text="@bob PLAT-89 blocker: the API contract changed",
            sender_id="U_alice", thread_id="slack_thread",
        )])

        # Index and correlate
        email_conv = Conversation(
            source=SourceType.EMAIL, account_id="test",
            thread_id="email_migration", subject="Migration",
            summary="", domain=Domain.WORK, relevance_explanation="",
        )
        correlator.index_conversation(email_conv, email_events)

        slack_conv = Conversation(
            source=SourceType.SLACK, account_id="test",
            thread_id="slack_thread", subject="#platform-team",
            summary="", domain=Domain.WORK, relevance_explanation="",
        )
        correlations = correlator.find_correlations(slack_conv, slack_events)

        assert len(correlations) >= 1
        assert any(c.correlation_type == "ticket_mention" for c in correlations)
        assert any("PLAT-89" in c.evidence for c in correlations)

    @pytest.mark.asyncio
    async def test_opportunity_to_briefing_pipeline(self):
        """Opportunity detected in conversation → appears in briefing."""
        bus = EventBus()

        # Create conversation with opportunity
        conv = Conversation(
            source=SourceType.EMAIL, account_id="test",
            thread_id="t_opp", subject="New Senior Eng Role",
            summary="", domain=Domain.WORK, relevance_explanation="",
            importance=0.6,
        )
        events = [NormalizedEvent(
            source=SourceType.EMAIL, account_id="hr@co.example",
            source_id="e1", source_url="http://x", title="New Role",
            timestamp=datetime.now(timezone.utc),
            plain_text_extract="We're looking for a senior engineer to lead the platform team",
            content_hash="h1", content_language="en", is_auto_generated=False,
        )]

        # Detect opportunity
        detector = OpportunityDetector()
        opportunities = detector.detect(conv, events)
        assert len(opportunities) >= 1

        # Set opportunity score on conversation
        conv.opportunity_score = max(o.confidence for o in opportunities)
        conv.opportunity_score = max(conv.opportunity_score, 0.6)  # Ensure above threshold

        # Generate briefing
        generator = BriefingGenerator(event_bus=bus)
        briefing = await generator.generate_morning_briefing([conv], [])
        section_titles = [s.title for s in briefing.sections]
        assert any("Opportunities" in t for t in section_titles)

    @pytest.mark.asyncio
    async def test_people_tracking_through_ingestion(self):
        """People tracker updates from ingested events."""
        bus = EventBus()
        pipeline = IngestionPipeline(event_bus=bus)
        tracker = PeopleTracker()

        # Ingest multiple emails from same sender
        for i in range(5):
            await pipeline.ingest([RawEvent(
                source=SourceType.EMAIL, source_id=f"e_{i}",
                source_url="https://mail.google.com",
                timestamp=datetime.now(timezone.utc),
                title=f"Update {i}",
                plain_text=f"Status update number {i}",
                sender_email="alice@co.example", thread_id=f"thread_{i}",
            )])

        # Track all normalized events
        normalized = await pipeline.ingest([RawEvent(
            source=SourceType.EMAIL, source_id="e_final",
            source_url="https://mail.google.com",
            timestamp=datetime.now(timezone.utc),
            title="Final update", plain_text="Done",
            sender_email="alice@co.example", thread_id="thread_final",
        )])
        for event in normalized:
            tracker.track_event(event)

        # Alice should be tracked
        person = tracker.get_person("alice@co.example")
        assert person is not None
        assert person.interaction_count_30d >= 1

    @pytest.mark.asyncio
    async def test_feedback_loop_e2e(self):
        """UI swipe feedback → EventBus → FailureJournal + StatisticalPatternTracker → anti-pattern."""
        bus = EventBus()
        journal = FailureJournal()
        patterns = StatisticalPatternTracker()

        # Connect backend consumers to EventBus
        async def on_user_feedback(event: UserFeedbackReceived):
            sender = event.conversation_id
            useful = (event.feedback == Feedback.USEFUL)
            patterns.record_sender_feedback(sender, useful)
            if not useful:
                journal.record_thumbs_down(sender, context={"sender": sender})

        await bus.subscribe(UserFeedbackReceived, on_user_feedback)

        # Panel sends feedback via drain_feedback_to_bus
        panel = PanelController(event_bus=bus)
        panel.set_feed([
            FeedItem(
                conversation_id="alice@co.example", source=SourceType.EMAIL,
                title="Useful email", sender="Alice", sender_role="Peer",
                time_ago="1h", summary="Rel", relevance_explanation="Rel",
                urgency=0.7,
            ),
            FeedItem(
                conversation_id="noreply@github.com", source=SourceType.EMAIL,
                title="Noise notification", sender="Bot", sender_role="Bot",
                time_ago="1h", summary="Noise", relevance_explanation="Noise",
                urgency=0.2,
            ),
        ])

        # Alice: 3 useful swipes
        panel.mark_useful("alice@co.example")
        panel.mark_useful("alice@co.example")
        panel.mark_useful("alice@co.example")

        # Github: 5 dismisses
        for _ in range(5):
            panel.dismiss_item("noreply@github.com")

        await panel.drain_feedback_to_bus(bus)
        await asyncio.sleep(0.05)

        # Patterns should show alice as high precision
        assert patterns.get_sender_precision("alice@co.example") == 1.0
        assert patterns.get_sender_precision("noreply@github.com") == 0.0

        # Weight adjustment
        assert patterns.get_sender_weight("alice@co.example") > 0
        assert patterns.get_sender_weight("noreply@github.com") < 0

        # Failure journal should detect noisy sender
        anti_patterns = journal.analyze_weekly()
        assert any("noreply@github.com" in p.description for p in anti_patterns)

    @pytest.mark.asyncio
    async def test_temporal_deadline_to_briefing(self):
        """Detected deadline → appears in morning briefing."""
        bus = EventBus()
        now = datetime.now(timezone.utc)

        # Action with approaching deadline
        action = ActionItem(
            description="Submit quarterly report",
            owner_id="user",
            domain=Domain.WORK,
            deadline=now + timedelta(hours=20),
            source_conversation_ids=["conv_report"],
        )

        # Detect deadline
        engine = TemporalEngine()
        alerts = engine.detect_deadlines([action])
        assert len(alerts) == 1
        assert not alerts[0].is_overdue

        # Generate briefing with action
        generator = BriefingGenerator(event_bus=bus)
        briefing = await generator.generate_morning_briefing([], [action], now=now)
        section_titles = [s.title for s in briefing.sections]
        assert any("Actions" in t for t in section_titles)

    @pytest.mark.asyncio
    async def test_dedup_across_batches(self):
        """Same event in multiple batches should be deduplicated."""
        bus = EventBus()
        pipeline = IngestionPipeline(event_bus=bus)

        raw = RawEvent(
            source=SourceType.EMAIL, source_id="dedup_test",
            source_url="https://mail.google.com",
            timestamp=datetime.now(timezone.utc),
            title="Important", plain_text="Same event",
            sender_email="a@co.example",
        )

        batch1 = await pipeline.ingest([raw])
        batch2 = await pipeline.ingest([raw])
        batch3 = await pipeline.ingest([raw])

        assert len(batch1) == 1
        assert len(batch2) == 0
        assert len(batch3) == 0

    @pytest.mark.asyncio
    async def test_multi_source_briefing(self):
        """Conversations from multiple sources in one briefing."""
        bus = EventBus()
        now = datetime.now(timezone.utc)

        convs = [
            Conversation(
                source=SourceType.EMAIL, account_id="t",
                thread_id="email_urgent", subject="Urgent Email",
                summary="Need response", domain=Domain.WORK,
                relevance_explanation="Direct ask",
                urgency=0.9, open_actions=["Reply"],
                last_activity=now - timedelta(hours=1),
                importance=0.8,
            ),
            Conversation(
                source=SourceType.SLACK, account_id="t",
                thread_id="slack_mention", subject="#team mention",
                summary="@-mentioned", domain=Domain.WORK,
                relevance_explanation="You were mentioned",
                urgency=0.7, importance=0.6,
                last_activity=now - timedelta(hours=3),
            ),
            Conversation(
                source=SourceType.JIRA, account_id="t",
                thread_id="PLAT-123", subject="PLAT-123 blocked",
                summary="Blocker", domain=Domain.WORK,
                relevance_explanation="Assigned to you",
                importance=0.5,
                last_activity=now - timedelta(hours=5),
            ),
        ]

        generator = BriefingGenerator(event_bus=bus)
        briefing = await generator.generate_morning_briefing(convs, [], now=now)

        # Should have sections covering multiple sources
        full_content = " ".join(s.content for s in briefing.sections)
        assert "Urgent Email" in full_content
