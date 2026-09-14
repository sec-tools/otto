from __future__ import annotations

"""
Statistical pattern extraction — volume makes Otto smarter.

Tracks sender precision, channel S/N ratio, temporal patterns,
and noise sources to auto-adjust classification weights.
"""

import logging
from collections import Counter
from dataclasses import dataclass

logger = logging.getLogger("otto.intelligence.patterns")


@dataclass
class SenderStats:
    """Statistics for a single sender."""
    sender_id: str
    total_count: int = 0
    useful_count: int = 0
    noise_count: int = 0

    @property
    def precision(self) -> float:
        """Percentage of messages from this sender rated useful."""
        total_rated = self.useful_count + self.noise_count
        if total_rated == 0:
            return 0.5  # Unknown → neutral
        return self.useful_count / total_rated


@dataclass
class ChannelStats:
    """Statistics for a Slack channel or email label."""
    channel_id: str
    channel_name: str
    total_count: int = 0
    useful_count: int = 0
    noise_count: int = 0

    @property
    def signal_to_noise(self) -> float:
        """Signal-to-noise ratio: useful / total rated."""
        total_rated = self.useful_count + self.noise_count
        if total_rated == 0:
            return 0.5
        return self.useful_count / total_rated


@dataclass
class TemporalPattern:
    """A discovered temporal pattern."""
    pattern_type: str       # "important_hours", "high_volume", "quiet_period"
    description: str
    hours: list[int]        # Which hours (0-23) this applies to
    weight: float           # How much to boost/dampen during these hours


class StatisticalPatternTracker:
    """
    Extracts statistical patterns from usage data.

    Tracked continuously:
    - Sender precision: "alice important 94%" → auto-boost
    - Channel S/N ratio: "#deploys 98% actionable" → boost. "#random 3%" → mute.
    - Temporal patterns: "Important emails 9-11am, 2-4pm" → poll more then
    - Noise sources: "noreply@github.com noise 99%" → auto-filter
    """

    def __init__(self) -> None:
        self._sender_stats: dict[str, SenderStats] = {}
        self._channel_stats: dict[str, ChannelStats] = {}
        self._hourly_importance: Counter = Counter()  # hour → count of important items
        self._hourly_total: Counter = Counter()        # hour → count of all items

    # ─── Sender Tracking ──────────────────────────────────────

    def record_sender_event(self, sender_id: str) -> None:
        """Record an event from a sender."""
        if sender_id not in self._sender_stats:
            self._sender_stats[sender_id] = SenderStats(sender_id=sender_id)
        self._sender_stats[sender_id].total_count += 1

    def record_sender_feedback(self, sender_id: str, useful: bool) -> None:
        """Record feedback for a sender's message."""
        if sender_id not in self._sender_stats:
            self._sender_stats[sender_id] = SenderStats(sender_id=sender_id)
        if useful:
            self._sender_stats[sender_id].useful_count += 1
        else:
            self._sender_stats[sender_id].noise_count += 1

    def get_sender_precision(self, sender_id: str) -> float:
        """Get the precision score for a sender."""
        stats = self._sender_stats.get(sender_id)
        return stats.precision if stats else 0.5

    def get_top_senders(self, n: int = 10) -> list[SenderStats]:
        """Get the most precise senders."""
        rated = [s for s in self._sender_stats.values() if s.useful_count + s.noise_count >= 3]
        return sorted(rated, key=lambda s: s.precision, reverse=True)[:n]

    def get_noise_senders(self, threshold: float = 0.1) -> list[SenderStats]:
        """Get senders with precision below threshold (noise sources)."""
        rated = [s for s in self._sender_stats.values() if s.useful_count + s.noise_count >= 5]
        return [s for s in rated if s.precision < threshold]

    # ─── Channel Tracking ─────────────────────────────────────

    def record_channel_event(self, channel_id: str, channel_name: str = "") -> None:
        """Record an event from a channel."""
        if channel_id not in self._channel_stats:
            self._channel_stats[channel_id] = ChannelStats(
                channel_id=channel_id, channel_name=channel_name or channel_id,
            )
        self._channel_stats[channel_id].total_count += 1

    def record_channel_feedback(self, channel_id: str, useful: bool) -> None:
        """Record feedback for a channel message."""
        if channel_id not in self._channel_stats:
            self._channel_stats[channel_id] = ChannelStats(
                channel_id=channel_id, channel_name=channel_id,
            )
        if useful:
            self._channel_stats[channel_id].useful_count += 1
        else:
            self._channel_stats[channel_id].noise_count += 1

    def get_channel_sn_ratio(self, channel_id: str) -> float:
        """Get signal-to-noise ratio for a channel."""
        stats = self._channel_stats.get(channel_id)
        return stats.signal_to_noise if stats else 0.5

    def get_noisy_channels(self, threshold: float = 0.1) -> list[ChannelStats]:
        """Get channels with poor S/N ratio."""
        rated = [c for c in self._channel_stats.values() if c.useful_count + c.noise_count >= 5]
        return [c for c in rated if c.signal_to_noise < threshold]

    # ─── Temporal Patterns ────────────────────────────────────

    def record_temporal_event(self, hour: int, is_important: bool) -> None:
        """Record an event's hour and importance for pattern learning."""
        self._hourly_total[hour] += 1
        if is_important:
            self._hourly_importance[hour] += 1

    def detect_temporal_patterns(self) -> list[TemporalPattern]:
        """
        Detect temporal patterns from accumulated data.

        Finds hours where important items concentrate.
        """
        patterns: list[TemporalPattern] = []

        if not self._hourly_total:
            return patterns

        total_events = sum(self._hourly_total.values())
        total_important = sum(self._hourly_importance.values())
        if total_events == 0 or total_important == 0:
            return patterns

        avg_importance_rate = total_important / total_events

        # Find hours with above-average importance
        hot_hours: list[int] = []
        for hour in range(24):
            count = self._hourly_total.get(hour, 0)
            important = self._hourly_importance.get(hour, 0)
            if count >= 5:  # Minimum sample
                rate = important / count
                if rate > avg_importance_rate * 1.3:  # 30% above average
                    hot_hours.append(hour)

        if hot_hours:
            patterns.append(TemporalPattern(
                pattern_type="important_hours",
                description=f"Important items concentrate at hours: {hot_hours}",
                hours=hot_hours,
                weight=0.2,  # Boost polling during these hours
            ))

        # Find quiet hours (very low activity)
        quiet_hours: list[int] = []
        for hour in range(24):
            count = self._hourly_total.get(hour, 0)
            if count <= total_events * 0.01:  # Less than 1% of traffic
                quiet_hours.append(hour)

        if quiet_hours:
            patterns.append(TemporalPattern(
                pattern_type="quiet_period",
                description=f"Low activity hours: {quiet_hours}",
                hours=quiet_hours,
                weight=-0.1,  # Reduce polling during these hours
            ))

        return patterns

    # ─── Composite Weight ─────────────────────────────────────

    def get_sender_weight(self, sender_id: str) -> float:
        """
        Get a sender's classification weight adjustment.

        High precision → positive boost
        Low precision → negative penalty
        """
        precision = self.get_sender_precision(sender_id)
        if precision > 0.8:
            return (precision - 0.5) * 0.4   # Up to +0.2
        elif precision < 0.2:
            return (precision - 0.5) * 0.4   # Down to -0.2
        return 0.0
