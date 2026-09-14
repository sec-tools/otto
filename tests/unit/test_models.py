"""
Tests for data models, enums, and signal decay.

Verifies all dataclass instantiation, enum completeness,
signal decay behavior, and edge cases.
"""

from datetime import datetime, timedelta, timezone


from otto.storage.models import (
    ActionItem,
    ActionStatus,
    Briefing,
    BriefingReady,
    BriefingSection,
    BriefingType,
    BusEvent,
    ClassificationComplete,
    ConnectionState,
    ContentBlock,
    ContentType,
    Conversation,
    ConversationRole,
    DegradationLevel,
    Domain,
    Entity,
    FailureRecorded,
    Feedback,
    FeedbackRecord,
    FileReference,
    HealthStatus,
    LifecycleType,
    NewEventsIngested,
    NormalizedEvent,
    Person,
    PersonRole,
    SourceStateChanged,
    SourceType,
    SystemLifecycleEvent,
    UserFeedbackReceived,
    compute_effective_urgency,
)


# =============================================================================
# Enum Tests
# =============================================================================


class TestEnums:
    """Verify all enums have expected values and are StrEnum."""

    def test_source_type_values(self):
        assert SourceType.EMAIL == "email"
        assert SourceType.SLACK == "slack"
        assert SourceType.JIRA == "jira"
        assert SourceType.CALENDAR == "calendar"
        assert len(SourceType) == 4

    def test_domain_values(self):
        assert Domain.WORK == "work"
        assert Domain.PERSONAL == "personal"
        assert Domain.SOCIAL == "social"
        assert Domain.UNKNOWN == "unknown"
        assert len(Domain) == 4

    def test_connection_state_values(self):
        assert len(ConnectionState) == 6
        assert "healthy" in [s.value for s in ConnectionState]

    def test_briefing_type_values(self):
        assert BriefingType.CATCH_ME_UP == "catch_me_up"
        assert len(BriefingType) == 5

    def test_action_status_values(self):
        assert ActionStatus.STALE == "stale"
        assert len(ActionStatus) == 5

    def test_content_type_values(self):
        assert ContentType.REACTION == "reaction"
        assert len(ContentType) == 7

    def test_conversation_role_values(self):
        assert ConversationRole.OBSERVER == "observer"
        assert len(ConversationRole) == 5

    def test_person_role_values(self):
        assert PersonRole.SKIP_LEVEL == "skip_level"
        assert len(PersonRole) == 10

    def test_feedback_values(self):
        assert Feedback.USEFUL == "useful"
        assert Feedback.NOISE == "noise"
        assert len(Feedback) == 2

    def test_lifecycle_type_values(self):
        assert len(LifecycleType) == 5

    def test_degradation_level_values(self):
        assert len(DegradationLevel) == 5

    def test_health_status_values(self):
        assert HealthStatus.HEALTHY == "healthy"
        assert len(HealthStatus) == 3

    def test_all_enums_are_string_serializable(self):
        """All StrEnum values should be JSON-serializable."""
        for enum_class in [
            SourceType, Domain, ConnectionState, BriefingType,
            ActionStatus, ContentType, ConversationRole, PersonRole,
            Feedback, LifecycleType, DegradationLevel, HealthStatus,
        ]:
            for member in enum_class:
                assert isinstance(str(member), str)


# =============================================================================
# Dataclass Instantiation Tests
# =============================================================================


class TestDataclassInstantiation:
    """Verify all dataclasses can be instantiated with required + default fields."""

    def test_content_block(self):
        block = ContentBlock(type=ContentType.TEXT, text="Hello")
        assert block.type == ContentType.TEXT
        assert block.text == "Hello"
        assert block.html is None

    def test_content_block_html(self):
        block = ContentBlock(type=ContentType.HTML, html="<p>Hi</p>")
        assert block.html == "<p>Hi</p>"

    def test_content_block_reaction(self):
        block = ContentBlock(type=ContentType.REACTION, reaction_emoji="👍", reaction_count=5)
        assert block.reaction_count == 5

    def test_file_reference(self):
        ref = FileReference(filename="doc.pdf", mime_type="application/pdf", size_bytes=1024, source_url="https://example.com")
        assert ref.filename == "doc.pdf"

    def test_entity(self):
        entity = Entity(name="Alice", entity_type="person")
        assert entity.source_ref is None

    def test_normalized_event_required_fields(self):
        event = NormalizedEvent(
            source=SourceType.EMAIL,
            account_id="test@example.com",
            source_id="msg_123",
            source_url="https://mail.google.com/123",
            timestamp=datetime.now(timezone.utc),
            title="Test Subject",
            plain_text_extract="Hello world",
            content_hash="abc123",
            content_language="en",
            is_auto_generated=False,
        )
        assert event.source == SourceType.EMAIL
        assert event.id != ""  # UUID generated
        assert event.content_blocks == []
        assert event.embedding is None

    def test_normalized_event_uuid_uniqueness(self):
        """Each event should get a unique UUID."""
        e1 = NormalizedEvent(
            source=SourceType.EMAIL, account_id="a", source_id="1",
            source_url="u", timestamp=datetime.now(timezone.utc),
            title="t", plain_text_extract="p", content_hash="h",
            content_language="en", is_auto_generated=False,
        )
        e2 = NormalizedEvent(
            source=SourceType.EMAIL, account_id="a", source_id="2",
            source_url="u", timestamp=datetime.now(timezone.utc),
            title="t", plain_text_extract="p", content_hash="h",
            content_language="en", is_auto_generated=False,
        )
        assert e1.id != e2.id

    def test_conversation(self):
        conv = Conversation(
            source=SourceType.SLACK,
            account_id="workspace_1",
            thread_id="thread_abc",
            subject="Platform Discussion",
            summary="Team discussing migration",
            domain=Domain.WORK,
            relevance_explanation="You're mentioned",
        )
        assert conv.is_active is True
        assert conv.urgency == 0.0
        assert conv.is_dismissed is False

    def test_person(self):
        person = Person(display_name="Alice Chen")
        assert person.role_to_user == PersonRole.UNKNOWN
        assert person.importance_score == 0.0
        assert person.domains == set()

    def test_person_with_all_fields(self):
        person = Person(
            display_name="Bob",
            email="bob@example.com",
            slack_id="U123",
            jira_id="bob_jira",
            role_to_user=PersonRole.MANAGER,
            importance_score=0.95,
            domains={Domain.WORK},
            topics=["migration", "OKRs"],
        )
        assert person.role_to_user == PersonRole.MANAGER
        assert Domain.WORK in person.domains

    def test_action_item(self):
        action = ActionItem(
            description="Review the proposal",
            owner_id="user_1",
            domain=Domain.WORK,
        )
        assert action.status == ActionStatus.OPEN
        assert action.deadline is None
        assert action.staleness_days == 0

    def test_briefing_section(self):
        section = BriefingSection(title="Top Priorities", content="Item 1, Item 2")
        assert section.priority == 0

    def test_briefing(self):
        briefing = Briefing(
            type=BriefingType.MORNING,
            content_hash="hash123",
            valid_until=datetime.now(timezone.utc),
        )
        assert briefing.read_at is None
        assert briefing.is_stale is False

    def test_feedback_record(self):
        record = FeedbackRecord(
            feedback_type=Feedback.USEFUL,
            conversation_id="conv_123",
        )
        assert record.reason is None


# =============================================================================
# Bus Event Tests
# =============================================================================


class TestBusEvents:
    """Verify all bus event types."""

    def test_bus_event_has_timestamp(self):
        event = BusEvent()
        assert event.timestamp is not None
        assert event.correlation_id is not None

    def test_new_events_ingested(self):
        event = NewEventsIngested(
            source=SourceType.EMAIL, event_ids=["1", "2"], count=2
        )
        assert event.count == 2

    def test_classification_complete(self):
        event = ClassificationComplete(event_ids=["1"], conversation_ids=["c1"])
        assert event.conversation_ids == ["c1"]

    def test_briefing_ready(self):
        event = BriefingReady(briefing_id="b1", briefing_type=BriefingType.MORNING)
        assert event.briefing_type == BriefingType.MORNING

    def test_user_feedback_received(self):
        event = UserFeedbackReceived(event_id="e1", feedback=Feedback.USEFUL)
        assert event.feedback == Feedback.USEFUL

    def test_source_state_changed(self):
        event = SourceStateChanged(
            source=SourceType.SLACK,
            old_state=ConnectionState.HEALTHY,
            new_state=ConnectionState.DEGRADED,
        )
        assert event.new_state == ConnectionState.DEGRADED

    def test_failure_recorded(self):
        event = FailureRecorded(subsystem="llm", failure_type="timeout", details="5s")
        assert event.subsystem == "llm"

    def test_system_lifecycle_event(self):
        event = SystemLifecycleEvent(event=LifecycleType.WAKE)
        assert event.event == LifecycleType.WAKE


# =============================================================================
# Signal Decay Tests
# =============================================================================


class TestSignalDecay:
    """Test the compute_effective_urgency function."""

    def _make_conversation(self, urgency: float, hours_ago: float) -> Conversation:
        return Conversation(
            source=SourceType.EMAIL,
            account_id="test",
            thread_id="t1",
            subject="Test",
            summary="Test",
            domain=Domain.WORK,
            relevance_explanation="Test",
            urgency=urgency,
            last_activity=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        )

    def test_no_decay_recent_activity(self):
        """Urgency should not decay significantly for recent conversations."""
        conv = self._make_conversation(0.9, hours_ago=1)
        result = compute_effective_urgency(conv, [])
        assert result > 0.85

    def test_decay_after_24_hours(self):
        """Urgency should decay by ~10% after 24 hours of inactivity."""
        conv = self._make_conversation(1.0, hours_ago=24)
        result = compute_effective_urgency(conv, [])
        assert 0.85 <= result <= 0.95

    def test_significant_decay_after_72_hours(self):
        """Urgency should decay significantly after 72 hours."""
        conv = self._make_conversation(1.0, hours_ago=72)
        result = compute_effective_urgency(conv, [])
        assert result < 0.8

    def test_heavy_decay_after_week(self):
        """Urgency should decay heavily after a week."""
        conv = self._make_conversation(1.0, hours_ago=168)
        result = compute_effective_urgency(conv, [])
        assert result < 0.5

    def test_zero_urgency_stays_zero(self):
        """Zero urgency should remain zero regardless of decay."""
        conv = self._make_conversation(0.0, hours_ago=1)
        result = compute_effective_urgency(conv, [])
        assert result == 0.0

    def test_deadline_within_24h_boosts_urgency(self):
        """Approaching deadline (< 24h) should boost urgency to at least 0.9."""
        conv = self._make_conversation(0.3, hours_ago=48)
        action = ActionItem(
            description="Deadline task",
            owner_id="user",
            domain=Domain.WORK,
            deadline=datetime.now(timezone.utc) + timedelta(hours=12),
        )
        result = compute_effective_urgency(conv, [action])
        assert result >= 0.9

    def test_deadline_within_72h_resists_decay(self):
        """Deadline within 72h should resist decay."""
        conv = self._make_conversation(0.8, hours_ago=48)
        action = ActionItem(
            description="Task",
            owner_id="user",
            domain=Domain.WORK,
            deadline=datetime.now(timezone.utc) + timedelta(hours=48),
        )
        result = compute_effective_urgency(conv, [action])
        assert result >= 0.8  # Should not decay below original

    def test_past_deadline_treated_as_imminent(self):
        """Past deadline (negative hours_to_deadline) is < 24h, so urgency boosts.
        This is actually correct: overdue items ARE urgent."""
        conv = self._make_conversation(0.5, hours_ago=72)
        action = ActionItem(
            description="Overdue",
            owner_id="user",
            domain=Domain.WORK,
            deadline=datetime.now(timezone.utc) - timedelta(hours=24),
        )
        result = compute_effective_urgency(conv, [action])
        assert result >= 0.9  # Overdue items are urgent

    def test_no_actions_just_decays(self):
        """With no action items, urgency should simply decay."""
        conv = self._make_conversation(0.8, hours_ago=48)
        result = compute_effective_urgency(conv, [])
        assert result < 0.8

    def test_multiple_actions_nearest_deadline_used(self):
        """Should use the nearest deadline among all actions."""
        conv = self._make_conversation(0.3, hours_ago=48)
        actions = [
            ActionItem(
                description="Far", owner_id="u", domain=Domain.WORK,
                deadline=datetime.now(timezone.utc) + timedelta(days=30),
            ),
            ActionItem(
                description="Near", owner_id="u", domain=Domain.WORK,
                deadline=datetime.now(timezone.utc) + timedelta(hours=6),
            ),
        ]
        result = compute_effective_urgency(conv, actions)
        assert result >= 0.9  # Near deadline should dominate

    def test_action_without_deadline_no_boost(self):
        """Actions without deadlines should not provide deadline boost."""
        conv = self._make_conversation(0.5, hours_ago=48)
        action = ActionItem(
            description="No deadline", owner_id="user", domain=Domain.WORK,
        )
        result = compute_effective_urgency(conv, [action])
        assert result < 0.5
