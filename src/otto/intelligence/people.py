from __future__ import annotations

"""
People intelligence — relationship tracking, sender importance,
and interaction pattern analysis.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from otto.storage.models import Domain, NormalizedEvent, Person, PersonRole

logger = logging.getLogger("otto.intelligence.people")


class PeopleTracker:
    """
    Tracks and enriches person records from event data.

    Learns:
    - How often the user interacts with each person
    - What topics they discuss
    - Sender importance (precision: % of their messages that user engages with)
    - Domain distribution per person
    - Sentiment trends over time
    """

    def __init__(self) -> None:
        self._people: dict[str, Person] = {}  # keyed by normalized email/id
        self._interaction_log: dict[str, list[datetime]] = defaultdict(list)

    def track_event(self, event: NormalizedEvent) -> Person:
        """
        Update person records from a normalized event.

        Creates the person if not seen before.
        Updates interaction count and topic tracking.
        """
        person_key = self._normalize_key(event.account_id)
        if not person_key:
            return Person(display_name="Unknown")

        person = self._people.get(person_key)
        if not person:
            person = Person(
                display_name=event.account_id,
                email=event.account_id if "@" in event.account_id else None,
            )
            self._people[person_key] = person

        # Update interaction tracking with 90-day pruning
        now = datetime.now(timezone.utc)
        cutoff_90d = now - timedelta(days=90)
        self._interaction_log[person_key] = [
            t for t in self._interaction_log[person_key] if t > cutoff_90d
        ]
        self._interaction_log[person_key].append(event.timestamp)

        # Update 30-day interaction count
        cutoff_30d = now - timedelta(days=30)
        recent = [t for t in self._interaction_log[person_key] if t > cutoff_30d]
        person.interaction_count_30d = len(recent)

        # Update last seen
        person.last_interaction = max(person.last_interaction or event.timestamp, event.timestamp)

        # Update domain tracking
        # (Domain inference happens in the classifier, but we track source type)
        if event.source.value in ("email", "calendar", "jira"):
            person.domains.add(Domain.WORK)

        # Bound people storage (max 10,000)
        if len(self._people) > 10000:
            least_important = sorted(self._people.keys(), key=lambda k: self._people[k].interaction_count_30d)[:1000]
            for k in least_important:
                del self._people[k]
                self._interaction_log.pop(k, None)

        return person

    def compute_importance(self, person_key: str) -> float:
        """
        Compute a person's importance score based on interaction patterns.

        Factors:
        - Interaction frequency (more = more important)
        - Recency (recent interactions weighted higher)
        - Role weight (manager > peer > external)
        """
        person = self._people.get(person_key)
        if not person:
            return 0.0

        # Frequency factor
        count = person.interaction_count_30d
        freq_score = min(1.0, count / 50.0)  # Cap at 50 interactions/month

        # Recency factor
        if person.last_interaction:
            hours_ago = (datetime.now(timezone.utc) - person.last_interaction).total_seconds() / 3600
            recency_score = max(0.0, 1.0 - (hours_ago / (24 * 30)))  # Decay over 30 days
        else:
            recency_score = 0.0

        # Role weight
        role_weights = {
            PersonRole.MANAGER: 1.0,
            PersonRole.CLIENT: 0.9,
            PersonRole.SKIP_LEVEL: 0.85,
            PersonRole.REPORT: 0.7,
            PersonRole.PEER: 0.6,
            PersonRole.VENDOR: 0.4,
            PersonRole.EXTERNAL: 0.3,
            PersonRole.FRIEND: 0.5,
            PersonRole.FAMILY: 0.8,
            PersonRole.UNKNOWN: 0.3,
        }
        role_weight = role_weights.get(person.role_to_user, 0.3)

        # Combined score
        score = (freq_score * 0.4 + recency_score * 0.3 + role_weight * 0.3)
        person.importance_score = min(1.0, score)

        return person.importance_score

    def get_person(self, key: str) -> Person | None:
        """Look up a person by normalized key."""
        return self._people.get(self._normalize_key(key))

    def get_top_people(self, n: int = 10) -> list[Person]:
        """Return the top N people by importance score."""
        for key in self._people:
            self.compute_importance(key)
        sorted_people = sorted(
            self._people.values(),
            key=lambda p: p.importance_score,
            reverse=True,
        )
        return sorted_people[:n]

    def set_role(self, person_key: str, role: PersonRole) -> None:
        """Explicitly set a person's role (from user input or inference)."""
        key = self._normalize_key(person_key)
        if key in self._people:
            self._people[key].role_to_user = role

    def _normalize_key(self, identifier: str) -> str:
        """Normalize person identifiers for dedup."""
        return identifier.lower().strip() if identifier else ""
