from __future__ import annotations

"""
Tests for the intelligence layer — ingestion, classifier, correlator,
opportunity detector, and people tracker.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from otto.adapters.base import RawEvent
from otto.core.event_bus import EventBus
from otto.intelligence.classifier import ConversationClassifier
from otto.intelligence.correlator import CrossSourceCorrelator
from otto.intelligence.ingestion import IngestionPipeline
from otto.intelligence.opportunity import OpportunityDetector
from otto.intelligence.people import PeopleTracker
from otto.storage.models import (
    ClassificationComplete,
    Conversation,
    ContentBlock,
    ContentType,
    Domain,
    NewEventsIngested,
    NormalizedEvent,
    PersonRole,
    SourceType,
)


# =============================================================================
# Ingestion Pipeline Tests
# =============================================================================


class TestIngestionPipeline:
    """Test raw event normalization, dedup, and publishing."""

    def _make_raw_event(
        self, source_id: str = "msg_1", text: str = "Hello world",
        source: SourceType = SourceType.EMAIL,
    ) -> RawEvent:
        return RawEvent(
            source=source,
            source_id=source_id,
            source_url=f"https://example.com/{source_id}",
            timestamp=datetime.now(timezone.utc),
            title="Test Subject",
            plain_text=text,
            sender_email="sender@example.com",
            thread_id="thread_1",
        )

    @pytest.mark.asyncio
    async def test_ingest_single_event(self):
        bus = EventBus()
        pipeline = IngestionPipeline(event_bus=bus)
        raw = self._make_raw_event()
        result = await pipeline.ingest([raw])
        assert len(result) == 1
        assert result[0].source == SourceType.EMAIL

    @pytest.mark.asyncio
    async def test_normalized_event_has_content_hash(self):
        bus = EventBus()
        pipeline = IngestionPipeline(event_bus=bus)
        raw = self._make_raw_event()
        result = await pipeline.ingest([raw])
        assert result[0].content_hash != ""
        assert len(result[0].content_hash) == 64  # SHA-256

    @pytest.mark.asyncio
    async def test_normalized_event_has_language(self):
        bus = EventBus()
        pipeline = IngestionPipeline(event_bus=bus)
        raw = self._make_raw_event(text="This is English text")
        result = await pipeline.ingest([raw])
        assert result[0].content_language == "en"

    @pytest.mark.asyncio
    async def test_deduplication(self):
        """Same event ingested twice should be deduplicated."""
        bus = EventBus()
        pipeline = IngestionPipeline(event_bus=bus)
        raw = self._make_raw_event(source_id="dup_1")

        r1 = await pipeline.ingest([raw])
        r2 = await pipeline.ingest([raw])
        assert len(r1) == 1
        assert len(r2) == 0  # Duplicate

    @pytest.mark.asyncio
    async def test_different_events_not_deduped(self):
        bus = EventBus()
        pipeline = IngestionPipeline(event_bus=bus)
        raw1 = self._make_raw_event(source_id="a", text="First message")
        raw2 = self._make_raw_event(source_id="b", text="Second message")
        result = await pipeline.ingest([raw1, raw2])
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_publishes_new_events_ingested(self):
        """Should publish NewEventsIngested to the event bus."""
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        await bus.subscribe(NewEventsIngested, handler)

        pipeline = IngestionPipeline(event_bus=bus)
        raw = self._make_raw_event()
        await pipeline.ingest([raw])
        await asyncio.sleep(0.05)

        assert len(received) == 1
        assert received[0].count == 1

    @pytest.mark.asyncio
    async def test_html_content_extraction(self):
        """Should extract text from HTML content blocks."""
        bus = EventBus()
        pipeline = IngestionPipeline(event_bus=bus)
        raw = RawEvent(
            source=SourceType.EMAIL,
            source_id="html_msg",
            source_url="https://example.com",
            timestamp=datetime.now(timezone.utc),
            title="HTML Email",
            plain_text="",
            content_blocks=[
                ContentBlock(type=ContentType.HTML, html="<p>Important info</p>")
            ],
        )
        result = await pipeline.ingest([raw])
        assert "Important info" in result[0].plain_text_extract

    @pytest.mark.asyncio
    async def test_empty_batch(self):
        bus = EventBus()
        pipeline = IngestionPipeline(event_bus=bus)
        result = await pipeline.ingest([])
        assert result == []

    @pytest.mark.asyncio
    async def test_failed_normalization_skipped(self):
        """Malformed events should be skipped without crashing."""
        bus = EventBus()
        pipeline = IngestionPipeline(event_bus=bus)
        good = self._make_raw_event(source_id="good")
        # Bad event with None source
        bad = RawEvent(
            source=None,  # type: ignore
            source_id="bad",
            source_url="",
            timestamp=datetime.now(timezone.utc),
            title="",
            plain_text="",
        )
        result = await pipeline.ingest([good, bad])
        assert len(result) >= 1  # Good event should succeed

    @pytest.mark.asyncio
    async def test_auto_generated_preserved(self):
        """Auto-generated flag should be preserved through normalization."""
        bus = EventBus()
        pipeline = IngestionPipeline(event_bus=bus)
        raw = self._make_raw_event()
        raw.is_auto_generated = True
        result = await pipeline.ingest([raw])
        assert result[0].is_auto_generated is True


# =============================================================================
# Classifier Tests
# =============================================================================


class TestConversationClassifier:
    """Test two-stage classification pipeline."""

    def _make_conversation(self, **kwargs) -> Conversation:
        defaults = dict(
            source=SourceType.EMAIL, account_id="test",
            thread_id="t1", subject="Test", summary="Test",
            domain=Domain.UNKNOWN, relevance_explanation="",
        )
        defaults.update(kwargs)
        return Conversation(**defaults)

    def _make_event(self, text: str = "Hello", **kwargs) -> NormalizedEvent:
        defaults = dict(
            source=SourceType.EMAIL, account_id="sender@test.example",
            source_id="e1", source_url="http://x", title="Test",
            timestamp=datetime.now(timezone.utc),
            plain_text_extract=text, content_hash="h",
            content_language="en", is_auto_generated=False,
        )
        defaults.update(kwargs)
        return NormalizedEvent(**defaults)

    def test_urgency_keyword_boost(self):
        """Events with urgency keywords should boost conversation urgency."""
        bus = EventBus()
        classifier = ConversationClassifier(event_bus=bus)
        conv = self._make_conversation()
        events = [self._make_event(text="This is URGENT please respond ASAP")]
        result = classifier.classify_local(conv, events)
        assert result.urgency >= 0.3

    def test_no_urgency_keywords_no_boost(self):
        """Events without urgency keywords should not boost urgency."""
        bus = EventBus()
        classifier = ConversationClassifier(event_bus=bus)
        conv = self._make_conversation()
        events = [self._make_event(text="Here are the meeting notes from Tuesday")]
        result = classifier.classify_local(conv, events)
        assert result.urgency < 0.3

    def test_sender_importance_boost(self):
        """Known important sender should boost scores."""
        bus = EventBus()
        classifier = ConversationClassifier(event_bus=bus)
        classifier.learn_sender_weight("vp@company.example", 0.8)
        conv = self._make_conversation()
        events = [self._make_event(text="Hello", account_id="vp@company.example")]
        result = classifier.classify_local(conv, events)
        assert result.urgency >= 0.8 or result.importance > 0

    def test_auto_generated_penalty(self):
        """Auto-generated messages should receive urgency penalty."""
        bus = EventBus()
        classifier = ConversationClassifier(event_bus=bus)
        conv = self._make_conversation()
        events = [
            self._make_event(text="Build #123 passed", is_auto_generated=True),
            self._make_event(text="Deploy complete", is_auto_generated=True),
        ]
        result = classifier.classify_local(conv, events)
        # Auto-generated should not result in high urgency
        assert result.urgency <= 0.1

    def test_domain_detection_work(self):
        """Work-related keywords should detect work domain."""
        bus = EventBus()
        classifier = ConversationClassifier(event_bus=bus)
        conv = self._make_conversation()
        events = [self._make_event(text="Sprint planning for the project deployment")]
        result = classifier.classify_local(conv, events)
        assert result.domain == Domain.WORK

    def test_domain_detection_personal(self):
        """Personal keywords should detect personal domain."""
        bus = EventBus()
        classifier = ConversationClassifier(event_bus=bus)
        conv = self._make_conversation()
        events = [self._make_event(text="Happy birthday! Family vacation next week")]
        result = classifier.classify_local(conv, events)
        assert result.domain == Domain.PERSONAL

    def test_empty_events_no_crash(self):
        """Empty event list should not crash."""
        bus = EventBus()
        classifier = ConversationClassifier(event_bus=bus)
        conv = self._make_conversation()
        result = classifier.classify_local(conv, [])
        assert result is conv

    def test_existing_urgency_preserved(self):
        """Classification should only increase urgency, never decrease."""
        bus = EventBus()
        classifier = ConversationClassifier(event_bus=bus)
        conv = self._make_conversation(urgency=0.9)
        events = [self._make_event(text="Just a note")]
        result = classifier.classify_local(conv, events)
        assert result.urgency >= 0.9

    @pytest.mark.asyncio
    async def test_llm_classification_without_llm(self):
        """LLM classification should gracefully return conversation when no LLM."""
        bus = EventBus()
        classifier = ConversationClassifier(event_bus=bus, llm_gateway=None)
        conv = self._make_conversation()
        events = [self._make_event()]
        result = await classifier.classify_llm(conv, events)
        assert result is conv

    @pytest.mark.asyncio
    async def test_action_extraction_without_llm(self):
        """Action extraction without LLM should return empty list."""
        bus = EventBus()
        classifier = ConversationClassifier(event_bus=bus, llm_gateway=None)
        conv = self._make_conversation()
        events = [self._make_event()]
        result = await classifier.extract_actions(conv, events)
        assert result == []


# =============================================================================
# Correlator Tests
# =============================================================================


class TestCrossSourceCorrelator:
    """Test cross-source conversation correlation."""

    def _make_conv(self, thread_id: str = "t1") -> Conversation:
        return Conversation(
            source=SourceType.EMAIL, account_id="test",
            thread_id=thread_id, subject="Test", summary="Test",
            domain=Domain.WORK, relevance_explanation="",
        )

    def _make_event(self, text: str, account_id: str = "alice@test.example") -> NormalizedEvent:
        return NormalizedEvent(
            source=SourceType.EMAIL, account_id=account_id,
            source_id="e1", source_url="http://x", title="Test",
            timestamp=datetime.now(timezone.utc),
            plain_text_extract=text, content_hash="h",
            content_language="en", is_auto_generated=False,
        )

    def test_ticket_correlation(self):
        """Conversations mentioning the same Jira ticket should correlate."""
        correlator = CrossSourceCorrelator()

        # Index first conversation
        conv1 = self._make_conv("email_thread")
        events1 = [self._make_event("Working on PLAT-123 today")]
        correlator.index_conversation(conv1, events1)

        # Correlate second conversation
        conv2 = self._make_conv("slack_thread")
        events2 = [self._make_event("PLAT-123 is blocked")]
        correlations = correlator.find_correlations(conv2, events2)

        assert len(correlations) >= 1
        assert any(c.correlation_type == "ticket_mention" for c in correlations)
        assert any("PLAT-123" in c.evidence for c in correlations)

    def test_participant_correlation(self):
        """Conversations with shared participants should correlate."""
        correlator = CrossSourceCorrelator()

        conv1 = self._make_conv("thread_1")
        events1 = [self._make_event("Message 1", account_id="alice@company.example")]
        correlator.index_conversation(conv1, events1)

        conv2 = self._make_conv("thread_2")
        events2 = [self._make_event("Message 2", account_id="alice@company.example")]
        correlations = correlator.find_correlations(conv2, events2)

        assert len(correlations) >= 1
        assert any(c.correlation_type == "participant_overlap" for c in correlations)

    def test_no_self_correlation(self):
        """A conversation should not correlate with itself."""
        correlator = CrossSourceCorrelator()

        conv = self._make_conv("same_thread")
        events = [self._make_event("PLAT-456 update")]
        correlator.index_conversation(conv, events)
        correlations = correlator.find_correlations(conv, events)

        assert len(correlations) == 0

    def test_multiple_ticket_correlations(self):
        """Multiple ticket mentions should create multiple correlations."""
        correlator = CrossSourceCorrelator()

        conv1 = self._make_conv("t1")
        events1 = [self._make_event("Working on PROJ-1")]
        correlator.index_conversation(conv1, events1)

        conv2 = self._make_conv("t2")
        events2 = [self._make_event("PROJ-2 is ready")]
        correlator.index_conversation(conv2, events2)

        conv3 = self._make_conv("t3")
        events3 = [self._make_event("PROJ-1 and PROJ-2 are related")]
        correlations = correlator.find_correlations(conv3, events3)

        assert len(correlations) >= 2

    def test_find_by_ticket(self):
        """Should find conversations by Jira ticket ID."""
        correlator = CrossSourceCorrelator()
        conv = self._make_conv("t1")
        events = [self._make_event("See FEAT-789")]
        correlator.index_conversation(conv, events)

        result = correlator.find_by_ticket("FEAT-789")
        assert "t1" in result

    def test_find_by_participant(self):
        """Should find conversations by participant email."""
        correlator = CrossSourceCorrelator()
        conv = self._make_conv("t1")
        events = [self._make_event("Hi", account_id="bob@test.example")]
        correlator.index_conversation(conv, events)

        result = correlator.find_by_participant("bob@test.example")
        assert "t1" in result

    def test_case_insensitive_participant(self):
        """Participant lookup should be case-insensitive."""
        correlator = CrossSourceCorrelator()
        conv = self._make_conv("t1")
        events = [self._make_event("Hi", account_id="Alice@Company.EXAMPLE")]
        correlator.index_conversation(conv, events)

        result = correlator.find_by_participant("alice@company.example")
        assert "t1" in result

    def test_dedup_correlations(self):
        """Same pair + same type should be deduplicated."""
        correlator = CrossSourceCorrelator()
        conv1 = self._make_conv("t1")
        events1 = [
            self._make_event("PROJ-1 first", account_id="a@test.example"),
            self._make_event("PROJ-1 again", account_id="a@test.example"),
        ]
        correlator.index_conversation(conv1, events1)

        conv2 = self._make_conv("t2")
        events2 = [self._make_event("PROJ-1 update", account_id="a@test.example")]
        correlations = correlator.find_correlations(conv2, events2)

        # Should have at most 1 ticket_mention and 1 participant_overlap
        ticket_mentions = [c for c in correlations if c.correlation_type == "ticket_mention"]
        participant_overlaps = [c for c in correlations if c.correlation_type == "participant_overlap"]
        assert len(ticket_mentions) <= 1
        assert len(participant_overlaps) <= 1


# =============================================================================
# Opportunity Detector Tests
# =============================================================================


class TestOpportunityDetector:
    """Test opportunity pattern detection."""

    def _make_conv_events(self, text: str):
        conv = Conversation(
            source=SourceType.EMAIL, account_id="test",
            thread_id="t1", subject="Test", summary="Test",
            domain=Domain.WORK, relevance_explanation="",
        )
        events = [NormalizedEvent(
            source=SourceType.EMAIL, account_id="test",
            source_id="e1", source_url="http://x", title="Test",
            timestamp=datetime.now(timezone.utc),
            plain_text_extract=text, content_hash="h",
            content_language="en", is_auto_generated=False,
        )]
        return conv, events

    def test_detects_new_project(self):
        detector = OpportunityDetector()
        conv, events = self._make_conv_events("We're kicking off a new project next quarter")
        results = detector.detect(conv, events)
        assert any(o.opportunity_type == "new_project_mention" for o in results)

    def test_detects_role_opening(self):
        detector = OpportunityDetector()
        conv, events = self._make_conv_events("We're looking for a senior engineer")
        results = detector.detect(conv, events)
        assert any(o.opportunity_type == "role_opening" for o in results)

    def test_detects_collaboration(self):
        detector = OpportunityDetector()
        conv, events = self._make_conv_events("Would you be interested in collaborating on this?")
        results = detector.detect(conv, events)
        assert any(o.opportunity_type == "collaboration_invite" for o in results)

    def test_detects_praise(self):
        detector = OpportunityDetector()
        conv, events = self._make_conv_events("Great job on the migration! Amazing work")
        results = detector.detect(conv, events)
        assert any(o.opportunity_type == "praise_recognition" for o in results)

    def test_detects_reconnection(self):
        detector = OpportunityDetector()
        conv, events = self._make_conv_events("It's been a while! Let's catch up soon")
        results = detector.detect(conv, events)
        assert any(o.opportunity_type == "reconnection_opportunity" for o in results)

    def test_detects_deal(self):
        detector = OpportunityDetector()
        conv, events = self._make_conv_events("Special offer: 50% discount ends Friday")
        results = detector.detect(conv, events)
        assert any(o.opportunity_type == "deal_or_discount" for o in results)

    def test_no_opportunity_in_mundane_text(self):
        detector = OpportunityDetector()
        conv, events = self._make_conv_events("Here are the meeting notes from yesterday")
        results = detector.detect(conv, events)
        assert len(results) == 0

    def test_sorted_by_confidence(self):
        detector = OpportunityDetector()
        conv, events = self._make_conv_events(
            "Great job on kicking off the new project! Amazing work starting a new initiative."
        )
        results = detector.detect(conv, events)
        if len(results) > 1:
            for i in range(len(results) - 1):
                assert results[i].confidence >= results[i + 1].confidence

    def test_opportunity_has_description(self):
        detector = OpportunityDetector()
        conv, events = self._make_conv_events("We're looking for a team lead")
        results = detector.detect(conv, events)
        assert all(o.description for o in results)

    def test_multiple_domains_detected(self):
        """Should detect opportunities across multiple domains in same text."""
        detector = OpportunityDetector()
        conv, events = self._make_conv_events(
            "New project launching next month. Also, special discount on conference tickets."
        )
        results = detector.detect(conv, events)
        domains = {o.domain for o in results}
        assert len(domains) >= 2


# =============================================================================
# People Tracker Tests
# =============================================================================


class TestPeopleTracker:
    """Test relationship intelligence."""

    def _make_event(self, account_id: str = "alice@test.example") -> NormalizedEvent:
        return NormalizedEvent(
            source=SourceType.EMAIL, account_id=account_id,
            source_id="e1", source_url="http://x", title="Test",
            timestamp=datetime.now(timezone.utc),
            plain_text_extract="Hello", content_hash="h",
            content_language="en", is_auto_generated=False,
        )

    def test_track_new_person(self):
        tracker = PeopleTracker()
        person = tracker.track_event(self._make_event("alice@test.example"))
        assert person.display_name == "alice@test.example"
        assert person.email == "alice@test.example"

    def test_track_increments_interaction(self):
        tracker = PeopleTracker()
        tracker.track_event(self._make_event("alice@test.example"))
        tracker.track_event(self._make_event("alice@test.example"))
        tracker.track_event(self._make_event("alice@test.example"))
        person = tracker.get_person("alice@test.example")
        assert person is not None
        assert person.interaction_count_30d == 3

    def test_importance_increases_with_frequency(self):
        tracker = PeopleTracker()
        for _ in range(20):
            tracker.track_event(self._make_event("active@test.example"))
        tracker.track_event(self._make_event("rare@test.example"))

        active_score = tracker.compute_importance("active@test.example")
        rare_score = tracker.compute_importance("rare@test.example")
        assert active_score > rare_score

    def test_role_affects_importance(self):
        tracker = PeopleTracker()
        tracker.track_event(self._make_event("manager@test.example"))
        tracker.track_event(self._make_event("peer@test.example"))
        tracker.set_role("manager@test.example", PersonRole.MANAGER)
        tracker.set_role("peer@test.example", PersonRole.PEER)

        manager_score = tracker.compute_importance("manager@test.example")
        peer_score = tracker.compute_importance("peer@test.example")
        assert manager_score > peer_score

    def test_get_top_people(self):
        tracker = PeopleTracker()
        for i in range(15):
            for _ in range(i + 1):
                tracker.track_event(self._make_event(f"person{i}@test.example"))

        top = tracker.get_top_people(5)
        assert len(top) == 5
        # Most interactions should be first
        assert top[0].interaction_count_30d >= top[4].interaction_count_30d

    def test_domain_tracking(self):
        tracker = PeopleTracker()
        person = tracker.track_event(self._make_event("alice@test.example"))
        assert Domain.WORK in person.domains

    def test_case_insensitive_lookup(self):
        tracker = PeopleTracker()
        tracker.track_event(self._make_event("Alice@Test.EXAMPLE"))
        person = tracker.get_person("alice@test.example")
        assert person is not None

    def test_unknown_person_returns_none(self):
        tracker = PeopleTracker()
        assert tracker.get_person("nobody@test.example") is None

    def test_empty_identifier_handled(self):
        tracker = PeopleTracker()
        event = self._make_event("")
        person = tracker.track_event(event)
        assert person.display_name == "Unknown"

    def test_people_interaction_log_pruning(self):
        tracker = PeopleTracker()
        now = datetime.now(timezone.utc)
        # Old event 100 days ago
        old_event = NormalizedEvent(
            source=SourceType.EMAIL, account_id="bob@test.example",
            source_id="old1", source_url="", title="",
            timestamp=now - timedelta(days=100),
            plain_text_extract="old", content_hash="h1",
            content_language="en", is_auto_generated=False,
        )
        new_event = self._make_event("bob@test.example")
        tracker.track_event(old_event)
        tracker.track_event(new_event)

        # The 100-day old event should be pruned from interaction log
        assert len(tracker._interaction_log["bob@test.example"]) == 1


class TestClassifierEvents:
    """Test classifier event bus subscriptions."""

    @pytest.mark.asyncio
    async def test_classifier_publishes_classification_complete(self):
        bus = EventBus()
        classifier = ConversationClassifier(event_bus=bus)
        await classifier.start()

        completed_events = []
        async def on_complete(event):
            completed_events.append(event)

        await bus.subscribe(ClassificationComplete, on_complete)

        await bus.publish(NewEventsIngested(
            source=SourceType.EMAIL,
            event_ids=["e100", "e101"],
            count=2,
        ))
        await asyncio.sleep(0.05)

        assert len(completed_events) == 1
        assert "e100" in completed_events[0].event_ids
        assert "e101" in completed_events[0].event_ids
        await classifier.stop()
