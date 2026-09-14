from __future__ import annotations

"""
Panel controller — manages the main Otto panel UI state.

Panel is a 380×600 dropdown from the menu bar with three tabs:
Briefing, Feed, and Search. Supports drill-down navigation.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from otto.storage.models import (
    Briefing,
    BriefingSection,
    Feedback,
    FeedbackRecord,
    SourceType,
    UserFeedbackReceived,
)

logger = logging.getLogger("otto.ui.panel")


class Tab:
    """Tab identifiers."""
    BRIEFING = "briefing"
    FEED = "feed"
    SEARCH = "search"
    SETTINGS = "settings"


@dataclass
class FeedItem:
    """A single item in the Feed view."""
    conversation_id: str
    source: SourceType
    title: str
    sender: str
    sender_role: str
    time_ago: str
    summary: str
    relevance_explanation: str
    urgency: float
    is_dismissed: bool = False
    linked_tickets: list[str] = field(default_factory=list)


@dataclass
class SearchResult:
    """A search result."""
    conversation_id: str
    title: str
    snippet: str
    source: SourceType
    timestamp: datetime
    relevance_score: float


class PanelController:
    """
    Controls the Otto panel UI state machine.

    Panel architecture:
    ┌────────────────────────────────────┐
    │  [Briefing]  [Feed]  [Search]  ⚙️  │  ← Tab bar
    ├────────────────────────────────────┤
    │                                    │
    │       Active view content          │
    │                                    │
    │  Item → Detail → "Open in Source"  │  ← Drill-down
    │                                    │
    └────────────────────────────────────┘

    Position: Drops from menu bar icon
    Size: 380w × 600h (resizable)
    Dismiss: Click-outside or Escape
    """

    def __init__(self, event_bus: Any = None) -> None:
        self._active_tab = Tab.BRIEFING
        self._is_visible = False
        self._pinned = False
        self._event_bus = event_bus

        # Content state
        self._current_briefing: Briefing | None = None
        self._feed_items: list[FeedItem] = []
        self._search_query: str = ""
        self._search_results: list[SearchResult] = []

        # Navigation stack for drill-down
        self._nav_stack: list[str] = []  # conversation IDs
        self._detail_view: dict[str, Any] | None = None

        # Feedback queue
        self._feedback_queue: list[FeedbackRecord] = []
        self._undo_stack: list[dict[str, Any]] = []

    @property
    def active_tab(self) -> str:
        return self._active_tab

    @property
    def is_visible(self) -> bool:
        return self._is_visible

    def show(self) -> None:
        """Show the panel, selecting the most relevant tab."""
        self._is_visible = True
        # Auto-select tab based on state
        if self._current_briefing:
            self._active_tab = Tab.BRIEFING
        else:
            self._active_tab = Tab.FEED
        logger.debug("Panel shown, tab=%s", self._active_tab)

    def hide(self) -> None:
        """Hide the panel unless pinned."""
        if not self._pinned:
            self._is_visible = False
            self._nav_stack.clear()
            self._detail_view = None
            logger.debug("Panel hidden")

    def toggle_pin(self) -> None:
        """Toggle pin state — pinned panels don't dismiss on click-outside."""
        self._pinned = not self._pinned

    def switch_tab(self, tab: str) -> None:
        """Switch to a different tab."""
        if tab in (Tab.BRIEFING, Tab.FEED, Tab.SEARCH, Tab.SETTINGS):
            self._active_tab = tab
            self._nav_stack.clear()
            self._detail_view = None

    # ─── Briefing View ─────────────────────────────────────────

    def set_briefing(self, briefing: Briefing) -> None:
        """Display a new briefing."""
        self._current_briefing = briefing

    def get_briefing_sections(self) -> list[BriefingSection]:
        """Get sections to render in the briefing view."""
        if not self._current_briefing:
            return []
        return sorted(self._current_briefing.sections, key=lambda s: s.priority)

    # ─── Feed View ─────────────────────────────────────────────

    def set_feed(self, items: list[FeedItem]) -> None:
        """Update the feed with new items."""
        self._feed_items = items

    def get_visible_feed(self, show_dismissed: bool = False) -> list[FeedItem]:
        """Get visible feed items, sorted by urgency."""
        items = self._feed_items
        if not show_dismissed:
            items = [i for i in items if not i.is_dismissed]
        return sorted(items, key=lambda i: i.urgency, reverse=True)

    def dismiss_item(self, conversation_id: str) -> None:
        """Dismiss a feed item (swipe left / noise feedback)."""
        for item in self._feed_items:
            if item.conversation_id == conversation_id:
                item.is_dismissed = True
                self._undo_stack.append({
                    "action": "dismiss",
                    "conversation_id": conversation_id,
                    "timestamp": datetime.now(timezone.utc),
                })
                record = FeedbackRecord(
                    feedback_type=Feedback.NOISE,
                    conversation_id=conversation_id,
                )
                self._feedback_queue.append(record)
                self._publish_feedback(record)
                break

    def mark_useful(self, conversation_id: str) -> None:
        """Mark a feed item as useful (swipe right)."""
        record = FeedbackRecord(
            feedback_type=Feedback.USEFUL,
            conversation_id=conversation_id,
        )
        self._feedback_queue.append(record)
        self._publish_feedback(record)

    def _publish_feedback(self, record: FeedbackRecord) -> None:
        """Publish feedback to EventBus if attached and loop is running."""
        if not self._event_bus:
            return
        import asyncio
        event = UserFeedbackReceived(
            conversation_id=record.conversation_id or "",
            event_id=record.event_id or "",
            feedback=record.feedback_type,
            reason=record.reason or "",
        )
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._event_bus.publish(event))
        except RuntimeError:
            pass

    async def drain_feedback_to_bus(self, event_bus: Any | None = None) -> list[FeedbackRecord]:
        """Drain accumulated feedback and publish all to EventBus."""
        bus = event_bus or self._event_bus
        records = self.drain_feedback()
        if bus:
            for record in records:
                await bus.publish(UserFeedbackReceived(
                    conversation_id=record.conversation_id or "",
                    event_id=record.event_id or "",
                    feedback=record.feedback_type,
                    reason=record.reason or "",
                ))
        return records

    def undo_last(self) -> bool:
        """Undo the last dismiss action. Returns True if undo was possible."""
        if not self._undo_stack:
            return False

        action = self._undo_stack.pop()
        if action["action"] == "dismiss":
            for item in self._feed_items:
                if item.conversation_id == action["conversation_id"]:
                    item.is_dismissed = False
                    # Remove the feedback too
                    self._feedback_queue = [
                        f for f in self._feedback_queue
                        if f.conversation_id != action["conversation_id"]
                        or f.feedback_type != Feedback.NOISE
                    ]
                    return True
        return False

    # ─── Search View ───────────────────────────────────────────

    def set_search_query(self, query: str) -> None:
        self._search_query = query

    def set_search_results(self, results: list[SearchResult]) -> None:
        self._search_results = results

    def get_search_results(self) -> list[SearchResult]:
        return sorted(self._search_results, key=lambda r: r.relevance_score, reverse=True)

    # ─── Detail View (drill-down) ──────────────────────────────

    def drill_down(self, conversation_id: str) -> None:
        """Navigate into a conversation's detail view."""
        self._nav_stack.append(conversation_id)
        self._detail_view = {"conversation_id": conversation_id}

    def go_back(self) -> bool:
        """Navigate back. Returns False if already at root."""
        if self._nav_stack:
            self._nav_stack.pop()
            if self._nav_stack:
                self._detail_view = {"conversation_id": self._nav_stack[-1]}
            else:
                self._detail_view = None
            return True
        return False

    @property
    def is_detail_view(self) -> bool:
        return self._detail_view is not None

    @property
    def current_detail_id(self) -> str | None:
        if self._detail_view:
            return self._detail_view.get("conversation_id")
        return None

    # ─── Feedback ──────────────────────────────────────────────

    def drain_feedback(self) -> list[FeedbackRecord]:
        """Drain accumulated feedback for persistence."""
        feedback = list(self._feedback_queue)
        self._feedback_queue.clear()
        return feedback

    # ─── Degradation Display ───────────────────────────────────

    def get_degradation_banner(self, level: int, detail: str = "") -> str | None:
        """
        Get degradation banner text.

        Level 0: None (fully operational, calm default)
        Level 1: Source degraded
        Level 2: LLM degraded
        Level 3: Offline
        Level 4: Critical failure
        """
        banners = {
            0: None,
            1: f"⚠️ {detail or 'Source connection issue — showing cached data'}",
            2: "🟡 Simpler analysis active",
            3: "📴 Offline — showing cached data",
            4: "🔴 Critical error. Check Health Dashboard.",
        }
        return banners.get(level)

    # ─── Empty States ──────────────────────────────────────────

    def get_empty_state(self) -> dict[str, str]:
        """Get the appropriate empty state message."""
        if self._active_tab == Tab.BRIEFING and not self._current_briefing:
            return {
                "title": "No briefing yet",
                "subtitle": "Otto is preparing your briefing...",
            }
        elif self._active_tab == Tab.FEED and not self._feed_items:
            return {
                "title": "All clear ✅",
                "subtitle": "Nothing needs your attention right now.",
            }
        elif self._active_tab == Tab.SEARCH and not self._search_results:
            if self._search_query:
                return {
                    "title": "No results",
                    "subtitle": f'No matches for "{self._search_query}"',
                }
            return {
                "title": "Search",
                "subtitle": "Search across all your conversations.",
            }
        elif self._active_tab == Tab.SETTINGS:
            return {
                "title": "Settings ⚙️",
                "subtitle": "Configure sources, models, quiet hours, and privacy.",
            }
        return {"title": "", "subtitle": ""}
