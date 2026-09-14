from __future__ import annotations

"""
Opportunity detector — scans conversations for actionable opportunities
across work, personal, and social domains.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from otto.storage.models import Conversation, Domain, NormalizedEvent

logger = logging.getLogger("otto.intelligence.opportunity")


@dataclass
class Opportunity:
    """A detected opportunity for the user."""
    opportunity_type: str       # e.g., "collaboration_invite", "reconnection"
    description: str
    confidence: float           # 0.0 - 1.0
    domain: Domain
    conversation_id: str
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# Pattern definitions: keyword groups that signal opportunities
WORK_PATTERNS: dict[str, list[str]] = {
    "new_project_mention": [
        "new initiative", "new project", "starting a", "kicking off",
        "greenfield", "launching", "building a new",
    ],
    "resource_request": [
        "need someone who", "looking for help", "could use expertise",
        "anyone available", "who can help with",
    ],
    "role_opening": [
        "hiring for", "opening on", "role is open", "position available",
        "we're looking for", "job posting",
    ],
    "collaboration_invite": [
        "would you be interested", "want to join", "let's work together",
        "partnership", "collaborate on", "team up",
    ],
    "praise_recognition": [
        "great job on", "impressed by", "amazing work", "well done",
        "shout out to", "kudos", "fantastic", "excellent work",
    ],
}

PERSONAL_PATTERNS: dict[str, list[str]] = {
    "event_of_interest": [
        "you might like", "thought of you", "check this out",
        "tickets available", "event next",
    ],
    "deal_or_discount": [
        "discount", "sale ends", "limited time", "promo code",
        "special offer", "flash sale",
    ],
}

SOCIAL_PATTERNS: dict[str, list[str]] = {
    "reconnection_opportunity": [
        "long time", "been a while", "miss you", "catch up",
        "how have you been", "reconnect",
    ],
    "group_activity_invite": [
        "we're planning", "come join", "group trip", "get together",
        "let's all", "party", "celebration",
    ],
}


class OpportunityDetector:
    """
    Scans conversations for opportunity patterns across domains.

    Uses keyword matching as a fast first pass. When LLM is available,
    can be enhanced with semantic opportunity detection.
    """

    def detect(
        self,
        conversation: Conversation,
        events: list[NormalizedEvent],
    ) -> list[Opportunity]:
        """
        Scan a conversation for opportunities.

        Returns list of detected opportunities sorted by confidence.
        """
        opportunities: list[Opportunity] = []
        conv_id = conversation.thread_id or conversation.id

        # Combine all text for scanning
        full_text = ""
        for event in events:
            full_text += f" {event.title} {event.plain_text_extract}"
        text_lower = full_text.lower()

        # Check work patterns
        for opp_type, keywords in WORK_PATTERNS.items():
            hits = sum(1 for kw in keywords if kw in text_lower)
            if hits > 0:
                confidence = min(1.0, hits * 0.3)
                opportunities.append(Opportunity(
                    opportunity_type=opp_type,
                    description=self._describe(opp_type, keywords, text_lower),
                    confidence=confidence,
                    domain=Domain.WORK,
                    conversation_id=conv_id,
                ))

        # Check personal patterns
        for opp_type, keywords in PERSONAL_PATTERNS.items():
            hits = sum(1 for kw in keywords if kw in text_lower)
            if hits > 0:
                confidence = min(1.0, hits * 0.3)
                opportunities.append(Opportunity(
                    opportunity_type=opp_type,
                    description=self._describe(opp_type, keywords, text_lower),
                    confidence=confidence,
                    domain=Domain.PERSONAL,
                    conversation_id=conv_id,
                ))

        # Check social patterns
        for opp_type, keywords in SOCIAL_PATTERNS.items():
            hits = sum(1 for kw in keywords if kw in text_lower)
            if hits > 0:
                confidence = min(1.0, hits * 0.3)
                opportunities.append(Opportunity(
                    opportunity_type=opp_type,
                    description=self._describe(opp_type, keywords, text_lower),
                    confidence=confidence,
                    domain=Domain.SOCIAL,
                    conversation_id=conv_id,
                ))

        # Sort by confidence descending
        opportunities.sort(key=lambda o: o.confidence, reverse=True)

        if opportunities:
            logger.info(
                "Detected %d opportunities in conversation %s",
                len(opportunities), conv_id,
            )

        return opportunities

    def _describe(self, opp_type: str, keywords: list[str], text: str) -> str:
        """Generate a human-readable description of the opportunity."""
        matched = [kw for kw in keywords if kw in text]
        type_label = opp_type.replace("_", " ").title()
        return f"{type_label}: matched keywords [{', '.join(matched[:3])}]"
