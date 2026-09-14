from __future__ import annotations

"""
Notification engine — macOS UserNotifications integration.

Manages notification delivery, rate limiting, quiet hours,
Focus mode respect, and notification policies.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable

from otto.storage.models import Conversation

logger = logging.getLogger("otto.ui.notifications")


@dataclass
class NotificationRequest:
    """A request to show a notification."""
    conversation_id: str
    title: str                  # "[Source emoji] Subject"
    body: str                   # "One-line action summary"
    urgency: float              # 0.0 - 1.0
    source_emoji: str = "📧"
    action_url: str | None = None  # Deep link back to Otto panel
    action_required: bool = True
    image_path: str = ""           # Thumbnail of the source (the Slack window showing it), if one exists


@dataclass
class NotificationPolicy:
    """Notification delivery policy."""
    max_per_hour: int = 5
    quiet_hours_start: time = time(23, 0)   # 11 PM
    quiet_hours_end: time = time(7, 0)      # 7 AM
    bypass_threshold: float = 0.95          # Urgency to bypass quiet hours
    respect_focus_mode: bool = True
    min_urgency: float = 0.8               # Minimum urgency to notify
    require_action: bool = True            # Only notify if action_required
    # Time zone the quiet-hours window is expressed in. ``None`` means "use the
    # wall-clock of the datetime passed in" (handy for tests); production code
    # passes the local zone so 23:00–07:00 means the user's night, not UTC's.
    timezone: Any = None

    @classmethod
    def local(cls, **overrides: Any) -> "NotificationPolicy":
        """Policy whose quiet hours are interpreted in the machine's local time zone."""
        overrides.setdefault("timezone", datetime.now().astimezone().tzinfo)
        return cls(**overrides)


class NotificationEngine:
    """
    Manages notification delivery with rate limiting and policies.

    Policy enforcement:
    1. Urgency threshold: Only notify if urgency > min_urgency
    2. Rate limiting: Max N notifications per hour
    3. Quiet hours: Suppress during quiet hours (except bypass)
    4. Focus mode: Respect macOS Focus modes
    5. Deduplication: Don't re-notify for same conversation

    Queued notifications during quiet hours are delivered as a
    batch digest when the next active window starts.
    """

    def __init__(self, policy: NotificationPolicy | None = None) -> None:
        self._policy = policy or NotificationPolicy()
        self._delivered: list[tuple[str, datetime]] = []  # (conv_id, timestamp)
        self._queued: list[NotificationRequest] = []
        self._is_focus_mode = False
        self._on_deliver: Callable[[NotificationRequest], None] | None = None

    @property
    def policy(self) -> NotificationPolicy:
        return self._policy

    @property
    def queued_count(self) -> int:
        return len(self._queued)

    def set_deliver_callback(self, callback: Callable[[NotificationRequest], None]) -> None:
        """Set callback for actual notification delivery (macOS integration)."""
        self._on_deliver = callback

    def set_focus_mode(self, active: bool) -> None:
        """Update Focus mode state (from macOS)."""
        self._is_focus_mode = active

    def request(self, notification: NotificationRequest, now: datetime | None = None) -> str:
        """
        Process a notification request.

        Returns: "delivered", "queued", "suppressed", or "rate_limited"
        """
        if now is None:
            now = datetime.now(timezone.utc)

        # 1. Urgency threshold
        if notification.urgency < self._policy.min_urgency:
            return "suppressed"

        # 1b. Action required policy check
        if self._policy.require_action and not notification.action_required:
            if notification.urgency < self._policy.bypass_threshold:
                return "suppressed"

        # 2. Deduplication (don't re-notify same conversation within 1 hour)
        recent_convs = {
            conv_id
            for conv_id, ts in self._delivered
            if (now - ts).total_seconds() < 3600
        }
        if notification.conversation_id in recent_convs:
            return "suppressed"

        # 3. Quiet hours check
        if self._is_quiet_hours(now):
            if notification.urgency >= self._policy.bypass_threshold:
                pass  # Bypass quiet hours for emergency
            else:
                self._queued.append(notification)
                return "queued"

        # 4. Focus mode check
        if self._is_focus_mode and self._policy.respect_focus_mode:
            if notification.urgency >= self._policy.bypass_threshold:
                pass  # Bypass Focus mode for emergency
            else:
                self._queued.append(notification)
                return "queued"

        # 5. Rate limiting
        hour_ago = now - timedelta(hours=1)
        recent_count = sum(1 for _, ts in self._delivered if ts > hour_ago)
        if recent_count >= self._policy.max_per_hour:
            self._queued.append(notification)
            return "rate_limited"

        # All checks passed — deliver
        self._deliver(notification, now)
        return "delivered"

    def drain_queue(self, now: datetime | None = None) -> list[NotificationRequest]:
        """
        Drain queued notifications (called when quiet hours end or Focus off).

        Returns the notifications that were delivered.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        delivered = []
        remaining = []
        hour_ago = now - timedelta(hours=1)
        recent_count = sum(1 for _, ts in self._delivered if ts > hour_ago)

        for notification in self._queued:
            if recent_count < self._policy.max_per_hour:
                self._deliver(notification, now)
                delivered.append(notification)
                recent_count += 1
            else:
                remaining.append(notification)

        self._queued = remaining
        return delivered

    def _deliver(self, notification: NotificationRequest, now: datetime) -> None:
        """Actually deliver a notification."""
        self._delivered.append((notification.conversation_id, now))
        if self._on_deliver:
            self._on_deliver(notification)
        logger.info("Notification delivered: %s", notification.title)

        # Trim old delivery history
        cutoff = now - timedelta(hours=24)
        self._delivered = [(c, t) for c, t in self._delivered if t > cutoff]

    def _is_quiet_hours(self, now: datetime) -> bool:
        """Check if current time is in quiet hours."""
        tz = self._policy.timezone
        if tz is not None and now.tzinfo is not None:
            now = now.astimezone(tz)
        current_time = now.time()
        start = self._policy.quiet_hours_start
        end = self._policy.quiet_hours_end

        if start <= end:
            # Same-day range (e.g., 1:00 - 5:00)
            return start <= current_time <= end
        else:
            # Overnight range (e.g., 23:00 - 07:00)
            return current_time >= start or current_time <= end

    def build_notification(self, conversation: Conversation) -> NotificationRequest:
        """Build a notification request from a conversation."""
        source_emojis = {
            "email": "📧",
            "slack": "💬",
            "jira": "🎫",
            "calendar": "📅",
        }
        emoji = source_emojis.get(conversation.source.value, "📋")
        action_summary = ""
        if conversation.open_actions:
            action_summary = conversation.open_actions[0]
        elif conversation.summary:
            action_summary = conversation.summary[:80]
        else:
            action_summary = conversation.relevance_explanation or ""

        has_actions = bool(conversation.open_actions)
        return NotificationRequest(
            conversation_id=conversation.thread_id or conversation.id,
            title=f"{emoji} {conversation.subject}",
            body=action_summary,
            urgency=conversation.urgency,
            source_emoji=emoji,
            action_required=has_actions,
        )
