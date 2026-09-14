from __future__ import annotations

"""
Temporal anticipation engine — proactive time-aware intelligence.

Detects deadlines, recurring events, conflicts, and schedule shifts.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from otto.storage.models import ActionItem

logger = logging.getLogger("otto.intelligence.temporal")


@dataclass
class DeadlineAlert:
    """An approaching or passed deadline."""
    description: str
    deadline: datetime
    source_conversation_id: str
    hours_until: float
    is_overdue: bool = False


@dataclass
class ConflictAlert:
    """A conflict between calendar and commitments."""
    description: str
    commitment: str           # What was promised
    calendar_event: str       # What's blocking
    conflict_date: datetime
    severity: float           # 0.0 - 1.0


@dataclass
class RecurringPrediction:
    """A predicted recurring event."""
    event_name: str
    predicted_next: datetime
    confidence: float
    pattern: str              # "weekly", "biweekly", "monthly", "quarterly"


# Deadline keywords
DEADLINE_PATTERNS = [
    re.compile(r"(?:by|before|due|deadline)\s+(\w+day)", re.IGNORECASE),
    re.compile(r"(?:by|before|due)\s+(tomorrow|today|tonight|eod|eow|end of (?:day|week))", re.IGNORECASE),
    re.compile(r"need\s+(?:this|it)\s+by\s+(\w+)", re.IGNORECASE),
]

# Commitment keywords
COMMITMENT_PATTERNS = [
    re.compile(r"I(?:'ll| will)\s+(?:have|get|send|finish|complete)\s+(?:it|that|this)\s+(?:by|before)\s+(\w+)", re.IGNORECASE),
    re.compile(r"I(?:'ll| will)\s+(\w+)\s+(?:by|before)\s+(\w+day)", re.IGNORECASE),
]


class TemporalEngine:
    """
    Proactive time-aware intelligence.

    Capabilities:
    - Detect approaching deadlines from email/Jira/Slack
    - Predict recurring events from calendar patterns
    - Detect conflicts between calendar and commitments
    - Travel awareness (timezone shifts)
    """

    def detect_deadlines(
        self,
        actions: list[ActionItem],
        now: datetime | None = None,
    ) -> list[DeadlineAlert]:
        """Find approaching or overdue deadlines."""
        if now is None:
            now = datetime.now(timezone.utc)

        alerts: list[DeadlineAlert] = []

        for action in actions:
            if not action.deadline:
                continue

            hours_until = (action.deadline - now).total_seconds() / 3600

            # Alert if within 48 hours or overdue
            if hours_until < 48:
                alerts.append(DeadlineAlert(
                    description=action.description,
                    deadline=action.deadline,
                    source_conversation_id=(
                        action.source_conversation_ids[0]
                        if action.source_conversation_ids else ""
                    ),
                    hours_until=hours_until,
                    is_overdue=hours_until < 0,
                ))

        # Sort: overdue first, then by urgency
        alerts.sort(key=lambda a: a.hours_until)
        return alerts

    def detect_deadline_mentions(
        self,
        text: str,
        conversation_id: str = "",
    ) -> list[str]:
        """Extract deadline mentions from text."""
        mentions: list[str] = []
        for pattern in DEADLINE_PATTERNS:
            for match in pattern.finditer(text):
                mentions.append(match.group(0))
        return mentions

    def detect_commitments(self, text: str) -> list[str]:
        """Extract user commitments from text ("I'll have it by...")."""
        commitments: list[str] = []
        for pattern in COMMITMENT_PATTERNS:
            for match in pattern.finditer(text):
                commitments.append(match.group(0))
        return commitments

    def detect_conflicts(
        self,
        commitments: list[dict[str, Any]],
        calendar_events: list[dict[str, Any]],
        now: datetime | None = None,
    ) -> list[ConflictAlert]:
        """
        Detect conflicts between commitments and calendar.

        E.g., "I'll have that to you by Thursday" + Thursday is packed.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        alerts: list[ConflictAlert] = []

        for commitment in commitments:
            commit_date = commitment.get("date")
            commit_desc = commitment.get("description", "")
            if not commit_date:
                continue

            # Check if that day is packed
            day_events = [
                e for e in calendar_events
                if e.get("date") and e["date"].date() == commit_date.date()
            ]

            if len(day_events) >= 5:  # Packed day
                event_names = [e.get("title", "") for e in day_events[:3]]
                alerts.append(ConflictAlert(
                    description=f"Commitment on a packed day ({len(day_events)} events)",
                    commitment=commit_desc,
                    calendar_event=", ".join(event_names),
                    conflict_date=commit_date,
                    severity=min(1.0, len(day_events) / 8.0),
                ))

        return alerts

    def detect_recurring_patterns(
        self,
        event_dates: list[tuple[str, datetime]],
    ) -> list[RecurringPrediction]:
        """
        Detect recurring event patterns from historical dates.

        Finds weekly, biweekly, monthly, and quarterly patterns.
        """
        # Group events by name
        events_by_name: dict[str, list[datetime]] = {}
        for name, dt in event_dates:
            events_by_name.setdefault(name, []).append(dt)

        predictions: list[RecurringPrediction] = []

        for name, dates in events_by_name.items():
            if len(dates) < 3:
                continue  # Need at least 3 occurrences

            dates_sorted = sorted(dates)
            gaps = [
                (dates_sorted[i + 1] - dates_sorted[i]).days
                for i in range(len(dates_sorted) - 1)
            ]

            if not gaps:
                continue

            avg_gap = sum(gaps) / len(gaps)
            gap_variance = sum((g - avg_gap) ** 2 for g in gaps) / len(gaps)

            # Classify pattern
            pattern = ""
            confidence = 0.0

            if 5 <= avg_gap <= 9 and gap_variance < 4:
                pattern = "weekly"
                confidence = max(0.5, 1.0 - gap_variance / 4)
            elif 12 <= avg_gap <= 16 and gap_variance < 8:
                pattern = "biweekly"
                confidence = max(0.5, 1.0 - gap_variance / 8)
            elif 26 <= avg_gap <= 35 and gap_variance < 20:
                pattern = "monthly"
                confidence = max(0.5, 1.0 - gap_variance / 20)
            elif 85 <= avg_gap <= 100 and gap_variance < 50:
                pattern = "quarterly"
                confidence = max(0.5, 1.0 - gap_variance / 50)

            if pattern:
                predicted_next = dates_sorted[-1] + timedelta(days=int(avg_gap))
                predictions.append(RecurringPrediction(
                    event_name=name,
                    predicted_next=predicted_next,
                    confidence=confidence,
                    pattern=pattern,
                ))

        return predictions
