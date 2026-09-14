from __future__ import annotations

"""
Failure journal — antifragile learning engine.

Catalogs every failure and feeds anti-patterns back into classification.
Otto gets MORE accurate BECAUSE it fails → antifragile.
"""

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("otto.intelligence.failure_journal")


@dataclass
class FailureRecord:
    """A single failure event."""
    failure_type: str       # "thumbs_down", "write_guard", "llm_timeout", "missed_item", "hallucination"
    source_id: str          # Event/conversation/source that failed
    context: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class AntiPattern:
    """A discovered anti-pattern from clustered failures."""
    pattern_id: str
    description: str        # Human-readable: "marketing@acme.example misclassified 73%"
    rule: str               # Machine-parseable: "demote_sender:marketing@acme.example"
    confidence: float       # 0.0 - 1.0
    evidence_count: int     # Number of failures supporting this
    discovered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class FailureJournal:
    """
    Catalogs every failure. Feeds anti-patterns back into classification.

    Records:
    - Every thumbs-down (with context: what was shown, why it was wrong)
    - Every LLM hallucination quarantined by write-intent detector
    - Every API timeout (with timestamp, source, endpoint)
    - Every missed item (user interacted with something Otto ranked low)
    - Every WriteGuard interception

    Anti-patterns feed into classification as negative-weight features.
    """

    def __init__(self) -> None:
        self._records: list[FailureRecord] = []
        self._anti_patterns: list[AntiPattern] = []
        self._max_records = 10_000

    @property
    def record_count(self) -> int:
        return len(self._records)

    @property
    def anti_patterns(self) -> list[AntiPattern]:
        return list(self._anti_patterns)

    def record(self, failure: FailureRecord) -> None:
        """Record a failure event."""
        self._records.append(failure)
        if len(self._records) > self._max_records:
            self._records = self._records[-self._max_records:]
        logger.info("Failure recorded: %s for %s", failure.failure_type, failure.source_id)

    def record_thumbs_down(self, conversation_id: str, reason: str = "", context: dict[str, Any] | None = None) -> None:
        """Record a user thumbs-down on a surfaced item."""
        self.record(FailureRecord(
            failure_type="thumbs_down",
            source_id=conversation_id,
            context={"reason": reason, **(context or {})},
        ))

    def record_missed_item(self, conversation_id: str, otto_rank: float) -> None:
        """Record that user found something Otto ranked low."""
        self.record(FailureRecord(
            failure_type="missed_item",
            source_id=conversation_id,
            context={"otto_rank": otto_rank},
        ))

    def record_api_timeout(self, source: str, endpoint: str) -> None:
        """Record an API timeout."""
        self.record(FailureRecord(
            failure_type="api_timeout",
            source_id=source,
            context={"endpoint": endpoint},
        ))

    def record_write_guard(self, method: str, url: str) -> None:
        """Record a WriteGuard interception."""
        self.record(FailureRecord(
            failure_type="write_guard",
            source_id=url,
            context={"method": method},
        ))

    def record_hallucination(self, llm_output: str, task: str) -> None:
        """Record a quarantined LLM hallucination."""
        self.record(FailureRecord(
            failure_type="hallucination",
            source_id=task,
            context={"output_snippet": llm_output[:200]},
        ))

    def analyze_weekly(self, now: datetime | None = None) -> list[AntiPattern]:
        """
        Cluster failure patterns from the last 7 days.

        Generates anti-pattern rules like:
        - "marketing@acme.example misclassified 73% → demote sender importance"
        - "#random 90% noise → auto-mute unless @-mention"
        - "Jira API times out 2-3pm daily → pre-fetch at 1:45pm"
        """
        if now is None:
            now = datetime.now(timezone.utc)

        cutoff = now - timedelta(days=7)
        recent = [r for r in self._records if r.timestamp > cutoff]

        if not recent:
            return []

        patterns: list[AntiPattern] = []

        # Cluster thumbs-downs by source
        thumbs_downs = [r for r in recent if r.failure_type == "thumbs_down"]
        source_failures: Counter = Counter()
        for td in thumbs_downs:
            sender = td.context.get("sender", td.source_id)
            source_failures[sender] += 1

        for sender, count in source_failures.most_common(10):
            if count >= 3:
                patterns.append(AntiPattern(
                    pattern_id=f"noisy_sender:{sender}",
                    description=f"{sender} marked as noise {count} times this week",
                    rule=f"demote_sender:{sender}",
                    confidence=min(1.0, count / 10.0),
                    evidence_count=count,
                ))

        # Cluster API timeouts by source + time
        timeouts = [r for r in recent if r.failure_type == "api_timeout"]
        timeout_by_source: dict[str, list[int]] = defaultdict(list)
        for t in timeouts:
            timeout_by_source[t.source_id].append(t.timestamp.hour)

        for source, hours in timeout_by_source.items():
            if len(hours) >= 3:
                hour_counter = Counter(hours)
                peak_hour, peak_count = hour_counter.most_common(1)[0]
                if peak_count >= 2:
                    prefetch_hour = (peak_hour - 1) % 24
                    patterns.append(AntiPattern(
                        pattern_id=f"timeout_pattern:{source}",
                        description=f"{source} times out around {peak_hour}:00 ({peak_count}x) → pre-fetch at {prefetch_hour}:45",
                        rule=f"prefetch:{source}:{prefetch_hour}:45",
                        confidence=min(1.0, peak_count / 5.0),
                        evidence_count=len(hours),
                    ))

        # Cluster missed items
        missed = [r for r in recent if r.failure_type == "missed_item"]
        if len(missed) >= 3:
            avg_rank = sum(m.context.get("otto_rank", 0) for m in missed) / len(missed)
            patterns.append(AntiPattern(
                pattern_id="missed_items_cluster",
                description=f"{len(missed)} items missed this week (avg Otto rank: {avg_rank:.2f})",
                rule=f"boost_threshold_down:{avg_rank:.2f}",
                confidence=min(1.0, len(missed) / 10.0),
                evidence_count=len(missed),
            ))

        self._anti_patterns = patterns
        logger.info("Weekly analysis: %d anti-patterns from %d failures", len(patterns), len(recent))
        return patterns

    def get_sender_penalty(self, sender: str) -> float:
        """Get the negative-weight penalty for a sender from anti-patterns."""
        for pattern in self._anti_patterns:
            if pattern.rule == f"demote_sender:{sender}":
                return -pattern.confidence * 0.5
        return 0.0

    def get_failures_since(self, since: datetime) -> list[FailureRecord]:
        """Get failures since a timestamp."""
        return [r for r in self._records if r.timestamp > since]

    def get_failure_rate(self, window_hours: int = 48) -> float:
        """Get the thumbs-down rate over the given window."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        recent = [r for r in self._records if r.timestamp > cutoff]
        if not recent:
            return 0.0
        thumbs_down = sum(1 for r in recent if r.failure_type == "thumbs_down")
        return thumbs_down / len(recent)
