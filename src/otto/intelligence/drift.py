from __future__ import annotations

"""
Drift detector — detects when the user's world has changed
and Otto's model is stale.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from otto.intelligence.failure_journal import FailureJournal

logger = logging.getLogger("otto.intelligence.drift")


@dataclass
class DriftSignal:
    """A detected drift in the user's world."""
    trigger: str           # e.g., "high_thumbs_down_rate"
    description: str       # Human-readable explanation
    severity: float        # 0.0 - 1.0
    recommendation: str    # What to do about it
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class DriftDetector:
    """
    Detects when the user's world has changed and Otto's model is stale.

    Triggers:
    - thumbs_down_rate > 30% over 48 hours
    - new high-frequency senders not seen before
    - domain distribution shift (work 70%→30%)
    - calendar pattern shift (meeting-heavy → meeting-free)
    - new source connected
    - many new channels/projects appeared
    """

    THUMBS_DOWN_THRESHOLD = 0.30
    NEW_SENDER_THRESHOLD = 5       # New high-freq senders in 48h
    DOMAIN_SHIFT_THRESHOLD = 0.25  # 25% change in domain distribution

    def __init__(self, failure_journal: FailureJournal | None = None) -> None:
        self._failure_journal = failure_journal or FailureJournal()
        self._domain_baseline: dict[str, float] = {}  # domain → proportion
        self._sender_baseline: set[str] = set()
        self._last_check: datetime | None = None

    def set_baseline(
        self,
        domain_distribution: dict[str, float],
        known_senders: set[str],
    ) -> None:
        """Set the baseline for drift detection."""
        self._domain_baseline = dict(domain_distribution)
        self._sender_baseline = set(known_senders)

    def to_dict(self) -> dict[str, Any]:
        """Serialize drift baseline state for persistence."""
        return {
            "domain_baseline": self._domain_baseline,
            "sender_baseline": list(self._sender_baseline),
            "last_check": self._last_check.isoformat() if self._last_check else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], failure_journal: FailureJournal | None = None) -> DriftDetector:
        """Restore DriftDetector from serialized state."""
        detector = cls(failure_journal=failure_journal)
        detector.set_baseline(
            domain_distribution=data.get("domain_baseline", {}),
            known_senders=set(data.get("sender_baseline", [])),
        )
        if data.get("last_check"):
            try:
                detector._last_check = datetime.fromisoformat(data["last_check"])
            except (ValueError, TypeError):
                pass
        return detector

    def check(
        self,
        current_domain_distribution: dict[str, float] | None = None,
        current_senders: set[str] | None = None,
        now: datetime | None = None,
    ) -> list[DriftSignal]:
        """
        Check for drift signals.

        Returns list of detected drift signals (empty = no drift).
        """
        if now is None:
            now = datetime.now(timezone.utc)

        signals: list[DriftSignal] = []

        # 1. Thumbs-down rate check
        td_rate = self._failure_journal.get_failure_rate(window_hours=48)
        if td_rate > self.THUMBS_DOWN_THRESHOLD:
            signals.append(DriftSignal(
                trigger="high_thumbs_down_rate",
                description=f"Thumbs-down rate is {td_rate:.0%} over the last 48 hours",
                severity=min(1.0, td_rate / 0.5),
                recommendation="Things look different. Quick recalibration recommended.",
            ))

        # 2. New high-frequency senders
        if current_senders and self._sender_baseline:
            new_senders = current_senders - self._sender_baseline
            if len(new_senders) >= self.NEW_SENDER_THRESHOLD:
                signals.append(DriftSignal(
                    trigger="new_senders",
                    description=f"{len(new_senders)} new frequent senders detected",
                    severity=min(1.0, len(new_senders) / 10.0),
                    recommendation="New people are appearing in your communications. Updating model.",
                ))

        # 3. Domain distribution shift
        if current_domain_distribution and self._domain_baseline:
            max_shift = 0.0
            shifted_domain = ""
            for domain, current_pct in current_domain_distribution.items():
                baseline_pct = self._domain_baseline.get(domain, 0.0)
                shift = abs(current_pct - baseline_pct)
                if shift > max_shift:
                    max_shift = shift
                    shifted_domain = domain

            if max_shift > self.DOMAIN_SHIFT_THRESHOLD:
                baseline_pct = self._domain_baseline.get(shifted_domain, 0)
                current_pct = current_domain_distribution.get(shifted_domain, 0)
                signals.append(DriftSignal(
                    trigger="domain_shift",
                    description=f"Domain '{shifted_domain}' shifted from {baseline_pct:.0%} to {current_pct:.0%}",
                    severity=min(1.0, max_shift / 0.5),
                    recommendation="Your activity distribution has changed. Adjusting priorities.",
                ))

        self._last_check = now
        if signals:
            logger.warning("Drift detected: %d signals", len(signals))
        return signals

    def check_new_source(self, source_name: str) -> DriftSignal:
        """Always triggers when a new source is connected."""
        return DriftSignal(
            trigger="new_source",
            description=f"New source connected: {source_name}",
            severity=0.5,
            recommendation=f"Integrating {source_name} data into your model.",
        )
