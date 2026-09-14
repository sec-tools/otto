from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import sys
if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum
    class StrEnum(str, Enum):
        """Polyfill for Python < 3.11."""
        def __str__(self) -> str:
            return str(self.value)


def _now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(timezone.utc)


def _uuid_str() -> str:
    """Return a new UUID4 string."""
    return str(uuid.uuid4())


# ==============================================================================
# 1. Enums
# ==============================================================================

class SourceType(StrEnum):
    EMAIL = "email"
    SLACK = "slack"
    JIRA = "jira"
    CALENDAR = "calendar"


class AdapterMode(StrEnum):
    API = "api"
    BROWSER = "browser"
    NATIVE_APP = "native_app"


class Domain(StrEnum):
    WORK = "work"
    PERSONAL = "personal"
    SOCIAL = "social"
    UNKNOWN = "unknown"


class ConnectionState(StrEnum):
    UNSET = "unset"
    CONNECTING = "connecting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    REAUTHENTICATING = "reauthenticating"
    FAILED = "failed"


class BriefingType(StrEnum):
    MORNING = "morning"
    MEETING_PREP = "meeting_prep"
    EOD = "eod"
    WEEKLY = "weekly"
    CATCH_ME_UP = "catch_me_up"


class ActionStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    OVERDUE = "overdue"
    STALE = "stale"


class ContentType(StrEnum):
    TEXT = "text"
    HTML = "html"
    CODE = "code"
    IMAGE = "image"
    FILE = "file"
    LINK = "link"
    REACTION = "reaction"


class ConversationRole(StrEnum):
    INITIATOR = "initiator"
    PARTICIPANT = "participant"
    CC = "cc"
    MENTIONED = "mentioned"
    OBSERVER = "observer"


class PersonRole(StrEnum):
    MANAGER = "manager"
    REPORT = "report"
    PEER = "peer"
    SKIP_LEVEL = "skip_level"
    EXTERNAL = "external"
    CLIENT = "client"
    VENDOR = "vendor"
    FRIEND = "friend"
    FAMILY = "family"
    UNKNOWN = "unknown"


class Feedback(StrEnum):
    USEFUL = "useful"
    NOISE = "noise"


class LifecycleType(StrEnum):
    WAKE = "wake"
    SLEEP = "sleep"
    NETWORK_CHANGE = "network_change"
    MEMORY_PRESSURE = "memory_pressure"
    POWER_CHANGE = "power_change"


class DegradationLevel(StrEnum):
    FULLY_OPERATIONAL = "fully_operational"
    SOURCE_DEGRADED = "source_degraded"
    LLM_DEGRADED = "llm_degraded"
    OFFLINE = "offline"
    CRITICAL_FAILURE = "critical_failure"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


# ==============================================================================
# 2. Content Models
# ==============================================================================

@dataclass
class FileReference:
    filename: str
    mime_type: str
    size_bytes: int
    source_url: str


@dataclass
class ContentBlock:
    type: ContentType
    text: str | None = None
    html: str | None = None
    file_ref: FileReference | None = None
    language: str | None = None
    reaction_emoji: str | None = None
    reaction_count: int | None = None


# ==============================================================================
# 3. Core Entity Models
# ==============================================================================

@dataclass
class Entity:
    """Represents an extracted entity (People, projects, dates, links, etc.)."""
    name: str
    entity_type: str
    source_ref: str | None = None


@dataclass
class NormalizedEvent:
    """Individual Message"""
    source: SourceType
    account_id: str
    source_id: str
    source_url: str
    timestamp: datetime
    title: str
    plain_text_extract: str
    content_hash: str
    content_language: str
    is_auto_generated: bool
    conversation_id: str = ""
    id: str = field(default_factory=_uuid_str)
    ingested_at: datetime = field(default_factory=_now)
    content_blocks: list[ContentBlock] = field(default_factory=list)
    sender: str | None = None
    recipients: list[str] = field(default_factory=list)
    has_attachments: bool = False
    attachment_summaries: list[str] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    embedding: list[float] | None = None
    # What the reader knew beyond the text: Slack reactions, reply counts,
    # whether you were mentioned, the channel's purpose, the sender's title…
    # (see ``otto.adapters.slack`` and ``otto.intelligence.ingestion``).
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Conversation:
    """Primary Intelligence Unit"""
    source: SourceType
    account_id: str
    thread_id: str
    subject: str
    summary: str
    domain: Domain
    relevance_explanation: str
    id: str = field(default_factory=_uuid_str)
    started: datetime = field(default_factory=_now)
    last_activity: datetime = field(default_factory=_now)
    message_count: int = 1
    is_active: bool = True
    participants: list[str] = field(default_factory=list)
    initiator: str = ""
    user_role: ConversationRole = ConversationRole.PARTICIPANT
    topic_arc: list[str] = field(default_factory=list)
    urgency: float = 0.0
    importance: float = 0.0
    opportunity_score: float = 0.0
    open_actions: list[str] = field(default_factory=list)
    resolved_actions: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    related_conversation_ids: list[str] = field(default_factory=list)
    related_ticket_ids: list[str] = field(default_factory=list)
    related_calendar_event_ids: list[str] = field(default_factory=list)
    source_url: str = ""
    topics: list[str] = field(default_factory=list)
    opportunity_type: str = ""
    opportunity_description: str = ""
    ai_analysis: str = ""
    action_items: list[str] = field(default_factory=list)
    user_feedback: Feedback | None = None
    is_dismissed: bool = False
    dismissed_at: datetime | None = None
    matched_directive: str = ""
    # One sentence on why this matters to *this* user (their role, focus,
    # a directive, an ask aimed at them) — grounded in verified evidence.
    for_you: str = ""
    # True once an LLM classification was actually parsed into this object
    # (local heuristics alone leave it False), so callers can tell
    # "AI-analyzed" from "the LLM was configured but every call failed".
    llm_enriched: bool = False


@dataclass
class Person:
    """Relationship Intelligence"""
    display_name: str
    id: str = field(default_factory=_uuid_str)
    email: str | None = None
    slack_id: str | None = None
    jira_id: str | None = None
    role_to_user: PersonRole = PersonRole.UNKNOWN
    interaction_count_30d: int = 0
    last_interaction: datetime = field(default_factory=_now)
    domains: set[Domain] = field(default_factory=set)
    importance_score: float = 0.0
    topics: list[str] = field(default_factory=list)
    last_discussed: list[tuple[str, datetime]] = field(default_factory=list)
    sentiment_trend: float = 0.0
    response_time_avg_hours: float = 0.0
    blockers_involving: list[str] = field(default_factory=list)
    context_embedding: list[float] | None = None


@dataclass
class ActionItem:
    """Cross-source action item"""
    description: str
    owner_id: str
    domain: Domain
    id: str = field(default_factory=_uuid_str)
    source_conversation_ids: list[str] = field(default_factory=list)
    first_seen: datetime = field(default_factory=_now)
    last_mentioned: datetime = field(default_factory=_now)
    status: ActionStatus = ActionStatus.OPEN
    deadline: datetime | None = None
    completion_evidence: list[str] = field(default_factory=list)
    staleness_days: int = 0
    urgency: float = 0.0


@dataclass
class BriefingSection:
    title: str
    content: str
    conversation_ids: list[str] = field(default_factory=list)
    priority: int = 0


@dataclass
class Briefing:
    type: BriefingType
    content_hash: str
    valid_until: datetime
    id: str = field(default_factory=_uuid_str)
    generated_at: datetime = field(default_factory=_now)
    sections: list[BriefingSection] = field(default_factory=list)
    calendar_event_id: str | None = None
    time_range: tuple[datetime, datetime] | None = None
    source_conversation_ids: list[str] = field(default_factory=list)
    source_action_item_ids: list[str] = field(default_factory=list)
    read_at: datetime | None = None
    time_spent_seconds: float | None = None
    feedback: Feedback | None = None
    is_stale: bool = False


# ==============================================================================
# 4. Event Bus Event Types
# ==============================================================================

@dataclass
class BusEvent:
    """Base event type for the internal typed publish/subscribe bus."""
    timestamp: datetime = field(default_factory=_now)
    correlation_id: str = field(default_factory=_uuid_str)


@dataclass
class NewEventsIngested(BusEvent):
    source: SourceType = SourceType.EMAIL
    event_ids: list[str] = field(default_factory=list)
    count: int = 0


@dataclass
class ClassificationComplete(BusEvent):
    event_ids: list[str] = field(default_factory=list)
    conversation_ids: list[str] = field(default_factory=list)


@dataclass
class BriefingReady(BusEvent):
    briefing_id: str = ""
    briefing_type: BriefingType = BriefingType.MORNING


@dataclass
class UserFeedbackReceived(BusEvent):
    event_id: str = ""
    conversation_id: str = ""
    feedback: Feedback = Feedback.USEFUL
    reason: str = ""


@dataclass
class SourceStateChanged(BusEvent):
    source: SourceType = SourceType.EMAIL
    old_state: ConnectionState = ConnectionState.UNSET
    new_state: ConnectionState = ConnectionState.UNSET


@dataclass
class FailureRecorded(BusEvent):
    subsystem: str = ""
    failure_type: str = ""
    details: str = ""


@dataclass
class SystemLifecycleEvent(BusEvent):
    event: LifecycleType = LifecycleType.WAKE


# ==============================================================================
# 5. Feedback Model
# ==============================================================================

@dataclass
class FeedbackRecord:
    feedback_type: Feedback
    id: str = field(default_factory=_uuid_str)
    event_id: str | None = None
    conversation_id: str | None = None
    reason: str | None = None
    timestamp: datetime = field(default_factory=_now)


# ==============================================================================
# 6. Helper
# ==============================================================================

def compute_effective_urgency(
    conversation: Conversation,
    open_action_items: list[ActionItem],
    now: datetime | None = None
) -> float:
    """
    Urgency decays unless reinforced by new activity or approaching deadlines.
    
    Args:
        conversation: The conversation to compute urgency for.
        open_action_items: The resolved ActionItem instances for this conversation.
        now: The current datetime (used for testing).
        
    Returns:
        The decayed effective urgency as a float.
    """
    if now is None:
        now = _now()

    def _as_utc(dt: datetime) -> datetime:
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

    now_utc = _as_utc(now)
    last_act_utc = _as_utc(conversation.last_activity)

    base = conversation.urgency
    hours_since_activity = max(0.0, (now_utc - last_act_utc).total_seconds() / 3600)

    # Decay: lose 10% per 24 hours of inactivity
    decay = 0.9 ** (hours_since_activity / 24)
    decayed = base * decay

    # Boost: approaching deadline reverses decay
    if open_action_items:
        nearest_deadline = min(
            (a.deadline for a in open_action_items if a.deadline),
            default=None
        )
        if nearest_deadline:
            deadline_utc = _as_utc(nearest_deadline)
            hours_to_deadline = (deadline_utc - now_utc).total_seconds() / 3600
            if -720 <= hours_to_deadline < 24:
                decayed = max(decayed, 0.9)  # Deadlines within 24h and overdue items are urgent
            elif 24 <= hours_to_deadline < 72:
                decayed = max(decayed, base)  # Approaching deadlines resist decay

    return min(1.0, max(0.0, decayed))
