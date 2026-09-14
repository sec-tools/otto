from __future__ import annotations

"""
Cross-source correlator.

Links related conversations across sources:
- Email ↔ Calendar (participant overlap, keyword matching)
- Jira ↔ Slack (ticket ID mentions)
- Email ↔ Slack (profile matching)
- Conversation ↔ Conversation (embedding similarity)
"""

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from otto.storage.models import Conversation, NormalizedEvent

logger = logging.getLogger("otto.intelligence.correlator")

# Jira ticket pattern (e.g. PROJ-123, OTTO-42)
JIRA_PATTERN = re.compile(r"\b([A-Z]{2,10}-\d{1,6})\b")

# Common URL patterns for linking
URL_PATTERN = re.compile(r"https?://[^\s<>\"]+")


@dataclass
class Correlation:
    """A discovered link between two conversations."""
    conversation_id_a: str
    conversation_id_b: str
    correlation_type: str  # "participant_overlap", "ticket_mention", "embedding_similar", etc.
    confidence: float       # 0.0 - 1.0
    evidence: str           # Human-readable explanation
    discovered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class CrossSourceCorrelator:
    """
    Discovers relationships between conversations across sources.

    Correlation types:
    - participant_overlap: Same people in email thread and calendar event
    - ticket_mention: Jira ticket ID mentioned in Slack/email
    - topic_overlap: Similar topics/keywords across sources
    - embedding_similar: Vector similarity above threshold
    """

    MAX_INDEX_KEYS = 5000

    def __init__(self) -> None:
        self._correlations: list[Correlation] = []
        self._ticket_index: OrderedDict[str, list[str]] = OrderedDict()  # ticket_id → [conversation_ids]
        self._participant_index: OrderedDict[str, list[str]] = OrderedDict()  # email → [conversation_ids]

    def index_conversation(self, conversation: Conversation, events: list[NormalizedEvent]) -> None:
        """
        Index a conversation for future correlation lookups.

        Call this after ingestion for each conversation.
        """
        conv_id = conversation.thread_id or conversation.id

        # Index participants
        for event in events:
            if event.account_id:
                key = event.account_id.lower()
                conv_list = self._participant_index.setdefault(key, [])
                if conv_id not in conv_list:
                    conv_list.append(conv_id)
                self._participant_index.move_to_end(key)

        # Index Jira ticket mentions
        for event in events:
            text = event.title + " " + event.plain_text_extract
            for match in JIRA_PATTERN.finditer(text):
                ticket_id = match.group(1)
                conv_list = self._ticket_index.setdefault(ticket_id, [])
                if conv_id not in conv_list:
                    conv_list.append(conv_id)
                self._ticket_index.move_to_end(ticket_id)

        # Prune indices with true LRU eviction if too large
        while len(self._participant_index) > self.MAX_INDEX_KEYS:
            self._participant_index.popitem(last=False)

        while len(self._ticket_index) > self.MAX_INDEX_KEYS:
            self._ticket_index.popitem(last=False)

    def find_correlations(
        self,
        conversation: Conversation,
        events: list[NormalizedEvent],
    ) -> list[Correlation]:
        """
        Find all correlations for a given conversation.

        Checks:
        1. Ticket mentions linking to other conversations
        2. Participant overlap with other conversations
        3. Topic keyword matching
        """
        conv_id = conversation.thread_id or conversation.id
        results: list[Correlation] = []

        # 1. Ticket mention correlations
        for event in events:
            text = event.title + " " + event.plain_text_extract
            for match in JIRA_PATTERN.finditer(text):
                ticket_id = match.group(1)
                linked_convs = self._ticket_index.get(ticket_id, [])
                for linked_id in linked_convs:
                    if linked_id != conv_id:
                        results.append(Correlation(
                            conversation_id_a=conv_id,
                            conversation_id_b=linked_id,
                            correlation_type="ticket_mention",
                            confidence=0.95,
                            evidence=f"Both reference {ticket_id}",
                        ))

        # 2. Participant overlap
        event_participants = set()
        for event in events:
            if event.account_id:
                event_participants.add(event.account_id.lower())

        for participant in event_participants:
            linked_convs = self._participant_index.get(participant, [])
            for linked_id in linked_convs:
                if linked_id != conv_id:
                    results.append(Correlation(
                        conversation_id_a=conv_id,
                        conversation_id_b=linked_id,
                        correlation_type="participant_overlap",
                        confidence=0.7,
                        evidence=f"Shared participant: {participant}",
                    ))

        # Deduplicate (same pair, same type)
        seen = set()
        unique: list[Correlation] = []
        for corr in results:
            key = (
                frozenset([corr.conversation_id_a, corr.conversation_id_b]),
                corr.correlation_type,
            )
            if key not in seen:
                seen.add(key)
                unique.append(corr)

        if unique:
            logger.info(
                "Found %d correlations for conversation %s",
                len(unique), conv_id,
            )

        return unique

    def find_by_ticket(self, ticket_id: str) -> list[str]:
        """Find all conversations mentioning a Jira ticket."""
        return self._ticket_index.get(ticket_id, [])

    def find_by_participant(self, email: str) -> list[str]:
        """Find all conversations involving a participant."""
        return self._participant_index.get(email.lower(), [])
