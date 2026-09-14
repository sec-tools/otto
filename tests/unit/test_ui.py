from __future__ import annotations

"""
Tests for UI layer — menu bar, panel controller, notification engine,
keyboard shortcuts, and onboarding flow.
"""

import asyncio
from datetime import datetime, time, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from otto.onboarding.manager import (
    AdaptivePhase,
    OnboardingManager,
    OnboardingState,
    OnboardingStep,
)
from otto.storage.models import (
    Briefing,
    BriefingSection,
    BriefingType,
    Conversation,
    Domain,
    Feedback,
    SourceType,
)
from otto.ui.menubar import MenuBarApp
from otto.ui.notifications import (
    NotificationEngine,
    NotificationPolicy,
    NotificationRequest,
)
from otto.ui.panel import FeedItem, PanelController, Tab
from otto.ui.shortcuts import ShortcutManager


# =============================================================================
# Menu Bar Tests
# =============================================================================


class TestMenuBarApp:
    """Test menu bar status item."""

    def test_default_state(self):
        app = MenuBarApp()
        assert app.title == "○"
        assert app.badge_count == 0
        assert app.is_panel_visible is False

    def test_set_badge(self):
        app = MenuBarApp()
        app.set_badge(3)
        assert app.badge_count == 3
        assert "3" in app.title

    def test_clear_badge(self):
        app = MenuBarApp()
        app.set_badge(5)
        app.set_badge(0)
        assert app.badge_count == 0
        assert app.title == "○"

    def test_negative_badge_clamped(self):
        app = MenuBarApp()
        app.set_badge(-1)
        assert app.badge_count == 0

    def test_toggle_panel(self):
        app = MenuBarApp()
        app.toggle_panel()
        assert app.is_panel_visible is True
        app.toggle_panel()
        assert app.is_panel_visible is False

    def test_show_hide_panel(self):
        app = MenuBarApp()
        app.show_panel()
        assert app.is_panel_visible is True
        app.hide_panel()
        assert app.is_panel_visible is False

    def test_ai_tier_setting(self):
        app = MenuBarApp()
        app.set_ai_tier("full")
        app.set_ai_tier("backup")
        app.set_ai_tier("local")
        app.set_ai_tier("heuristic")
        # Should not raise

    def test_menu_items(self):
        app = MenuBarApp()
        items = app.get_menu_items()
        actions = [i.get("action") for i in items if i.get("action")]
        assert "toggle_panel" in actions
        assert "show_briefing" in actions
        assert "catch_me_up" in actions
        assert "quit" in actions

    def test_event_callback(self):
        app = MenuBarApp()
        received = []
        app.on("panel_toggled", lambda v: received.append(v))
        app.toggle_panel()
        assert received == [True]

    def test_panel_delegates(self):
        panel = MagicMock()
        app = MenuBarApp(panel_controller=panel)
        app.show_panel()
        panel.show.assert_called_once()
        app.hide_panel()
        panel.hide.assert_called_once()


# =============================================================================
# Panel Controller Tests
# =============================================================================


class TestPanelController:
    """Test panel state machine."""

    def test_default_state(self):
        panel = PanelController()
        assert panel.is_visible is False
        assert panel.active_tab == Tab.BRIEFING

    def test_show_auto_selects_tab(self):
        panel = PanelController()
        panel.show()
        assert panel.is_visible is True
        # No briefing → Feed tab
        assert panel.active_tab == Tab.FEED

    def test_show_selects_briefing_when_available(self):
        panel = PanelController()
        panel.set_briefing(Briefing(
            type=BriefingType.MORNING,
            content_hash="abc",
            valid_until=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            sections=[BriefingSection(title="Test", content="Test", priority=1)],
        ))
        panel.show()
        assert panel.active_tab == Tab.BRIEFING

    def test_switch_tab(self):
        panel = PanelController()
        panel.switch_tab(Tab.SEARCH)
        assert panel.active_tab == Tab.SEARCH

    def test_pin_prevents_hide(self):
        panel = PanelController()
        panel.show()
        panel.toggle_pin()
        panel.hide()
        assert panel.is_visible is True  # Still visible because pinned

    def test_unpin_allows_hide(self):
        panel = PanelController()
        panel.show()
        panel.toggle_pin()
        panel.toggle_pin()
        panel.hide()
        assert panel.is_visible is False


class TestFeedView:
    """Test feed item management."""

    def _make_item(self, conv_id: str, urgency: float = 0.5) -> FeedItem:
        return FeedItem(
            conversation_id=conv_id, source=SourceType.EMAIL,
            title="Test", sender="Alice", sender_role="Manager",
            time_ago="2h", summary="Test summary",
            relevance_explanation="Important", urgency=urgency,
        )

    def test_feed_sorted_by_urgency(self):
        panel = PanelController()
        panel.set_feed([
            self._make_item("low", 0.3),
            self._make_item("high", 0.9),
            self._make_item("mid", 0.6),
        ])
        items = panel.get_visible_feed()
        assert items[0].conversation_id == "high"
        assert items[-1].conversation_id == "low"

    def test_dismiss_item(self):
        panel = PanelController()
        panel.set_feed([self._make_item("c1"), self._make_item("c2")])
        panel.dismiss_item("c1")
        visible = panel.get_visible_feed()
        assert len(visible) == 1
        assert visible[0].conversation_id == "c2"

    def test_dismiss_creates_feedback(self):
        panel = PanelController()
        panel.set_feed([self._make_item("c1")])
        panel.dismiss_item("c1")
        feedback = panel.drain_feedback()
        assert len(feedback) == 1
        assert feedback[0].feedback_type == Feedback.NOISE

    def test_mark_useful_creates_feedback(self):
        panel = PanelController()
        panel.mark_useful("c1")
        feedback = panel.drain_feedback()
        assert len(feedback) == 1
        assert feedback[0].feedback_type == Feedback.USEFUL

    def test_undo_dismiss(self):
        panel = PanelController()
        panel.set_feed([self._make_item("c1")])
        panel.dismiss_item("c1")
        assert len(panel.get_visible_feed()) == 0
        result = panel.undo_last()
        assert result is True
        assert len(panel.get_visible_feed()) == 1

    def test_undo_clears_feedback(self):
        panel = PanelController()
        panel.set_feed([self._make_item("c1")])
        panel.dismiss_item("c1")
        panel.undo_last()
        feedback = panel.drain_feedback()
        assert len(feedback) == 0

    def test_undo_empty_stack(self):
        panel = PanelController()
        assert panel.undo_last() is False

    def test_show_dismissed(self):
        panel = PanelController()
        panel.set_feed([self._make_item("c1")])
        panel.dismiss_item("c1")
        visible = panel.get_visible_feed(show_dismissed=True)
        assert len(visible) == 1


class TestDrillDown:
    """Test detail view navigation."""

    def test_drill_down(self):
        panel = PanelController()
        panel.drill_down("conv_1")
        assert panel.is_detail_view is True
        assert panel.current_detail_id == "conv_1"

    def test_go_back(self):
        panel = PanelController()
        panel.drill_down("conv_1")
        panel.go_back()
        assert panel.is_detail_view is False

    def test_nested_drill_down(self):
        panel = PanelController()
        panel.drill_down("conv_1")
        panel.drill_down("conv_2")
        assert panel.current_detail_id == "conv_2"
        panel.go_back()
        assert panel.current_detail_id == "conv_1"

    def test_go_back_at_root(self):
        panel = PanelController()
        assert panel.go_back() is False

    def test_tab_switch_clears_nav(self):
        panel = PanelController()
        panel.drill_down("conv_1")
        panel.switch_tab(Tab.SEARCH)
        assert panel.is_detail_view is False


class TestEmptyStates:
    """Test empty state messages."""

    def test_empty_briefing(self):
        panel = PanelController()
        panel.switch_tab(Tab.BRIEFING)
        state = panel.get_empty_state()
        assert "briefing" in state["title"].lower() or "briefing" in state["subtitle"].lower()

    def test_empty_feed(self):
        panel = PanelController()
        panel.switch_tab(Tab.FEED)
        state = panel.get_empty_state()
        assert "clear" in state["title"].lower()

    def test_empty_search_no_query(self):
        panel = PanelController()
        panel.switch_tab(Tab.SEARCH)
        state = panel.get_empty_state()
        assert "search" in state["title"].lower()

    def test_empty_search_with_query(self):
        panel = PanelController()
        panel.switch_tab(Tab.SEARCH)
        panel.set_search_query("migration")
        state = panel.get_empty_state()
        assert "migration" in state["subtitle"]

    def test_degradation_banners(self):
        panel = PanelController()
        assert panel.get_degradation_banner(0) is None
        assert "⚠️" in panel.get_degradation_banner(1)
        assert "🟡" in panel.get_degradation_banner(2)
        assert "📴" in panel.get_degradation_banner(3)
        assert "🔴" in panel.get_degradation_banner(4)


# =============================================================================
# Notification Engine Tests
# =============================================================================


class TestNotificationEngine:
    """Test notification delivery, rate limiting, and policies."""

    def _make_request(self, urgency: float = 0.9, conv_id: str = "c1") -> NotificationRequest:
        return NotificationRequest(
            conversation_id=conv_id,
            title="📧 Test",
            body="Action needed",
            urgency=urgency,
        )

    def test_deliver_above_threshold(self):
        engine = NotificationEngine()
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        result = engine.request(self._make_request(0.9), now)
        assert result == "delivered"

    def test_suppress_below_threshold(self):
        engine = NotificationEngine()
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        result = engine.request(self._make_request(0.3), now)
        assert result == "suppressed"

    def test_rate_limiting(self):
        policy = NotificationPolicy(max_per_hour=3)
        engine = NotificationEngine(policy=policy)
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

        for i in range(3):
            result = engine.request(self._make_request(0.9, f"c{i}"), now)
            assert result == "delivered"

        result = engine.request(self._make_request(0.9, "c_extra"), now)
        assert result == "rate_limited"

    def test_quiet_hours_queue(self):
        policy = NotificationPolicy(quiet_hours_start=time(23, 0), quiet_hours_end=time(7, 0))
        engine = NotificationEngine(policy=policy)
        midnight = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        result = engine.request(self._make_request(0.85), midnight)
        assert result == "queued"

    def test_quiet_hours_bypass_emergency(self):
        policy = NotificationPolicy(
            quiet_hours_start=time(23, 0), quiet_hours_end=time(7, 0),
            bypass_threshold=0.95,
        )
        engine = NotificationEngine(policy=policy)
        midnight = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        result = engine.request(self._make_request(0.96), midnight)
        assert result == "delivered"

    def test_focus_mode_queues(self):
        engine = NotificationEngine()
        engine.set_focus_mode(True)
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        result = engine.request(self._make_request(0.85), now)
        assert result == "queued"

    def test_focus_mode_bypass_emergency(self):
        engine = NotificationEngine()
        engine.set_focus_mode(True)
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        result = engine.request(self._make_request(0.96), now)
        assert result == "delivered"

    def test_deduplication(self):
        engine = NotificationEngine()
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        engine.request(self._make_request(0.9, "same"), now)
        result = engine.request(self._make_request(0.9, "same"), now)
        assert result == "suppressed"

    def test_drain_queue(self):
        policy = NotificationPolicy(max_per_hour=10)
        engine = NotificationEngine(policy=policy)
        engine.set_focus_mode(True)
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

        engine.request(self._make_request(0.9, "q1"), now)
        engine.request(self._make_request(0.9, "q2"), now)
        assert engine.queued_count == 2

        engine.set_focus_mode(False)
        delivered = engine.drain_queue(now)
        assert len(delivered) == 2
        assert engine.queued_count == 0

    def test_deliver_callback(self):
        engine = NotificationEngine()
        delivered = []
        engine.set_deliver_callback(lambda n: delivered.append(n))
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        engine.request(self._make_request(0.9), now)
        assert len(delivered) == 1

    def test_build_notification_from_conversation(self):
        engine = NotificationEngine()
        conv = Conversation(
            source=SourceType.EMAIL, account_id="t", thread_id="t1",
            subject="Q3 Budget Review", summary="Budget needs approval",
            domain=Domain.WORK, relevance_explanation="You're the approver",
            urgency=0.9,
        )
        notif = engine.build_notification(conv)
        assert "📧" in notif.title
        assert "Q3 Budget Review" in notif.title
        assert notif.urgency == 0.9

    def test_quiet_hours_overnight(self):
        """Quiet hours spanning midnight should work correctly."""
        policy = NotificationPolicy(
            quiet_hours_start=time(23, 0),
            quiet_hours_end=time(7, 0),
        )
        engine = NotificationEngine(policy=policy)

        # 11:30 PM — should be quiet
        late = datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc)
        assert engine.request(self._make_request(0.85), late) == "queued"

        # 6:30 AM — should still be quiet
        early = datetime(2026, 1, 2, 6, 30, tzinfo=timezone.utc)
        assert engine.request(self._make_request(0.85, "c2"), early) == "queued"

    def test_outside_quiet_hours(self):
        policy = NotificationPolicy(
            quiet_hours_start=time(23, 0),
            quiet_hours_end=time(7, 0),
        )
        engine = NotificationEngine(policy=policy)
        noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        assert engine.request(self._make_request(0.85), noon) == "delivered"


# =============================================================================
# Keyboard Shortcut Tests
# =============================================================================


class TestShortcutManager:
    """Test keyboard shortcut handling."""

    def test_default_shortcuts_exist(self):
        manager = ShortcutManager()
        shortcuts = manager.get_all_shortcuts()
        assert len(shortcuts) > 10

    def test_handle_registered_key(self):
        manager = ShortcutManager()
        called = []
        manager.register_handler("toggle_panel", lambda: called.append("toggled"))
        result = manager.handle_key("⌥O")
        assert result is True
        assert called == ["toggled"]

    def test_handle_unregistered_key(self):
        manager = ShortcutManager()
        result = manager.handle_key("⌘X")  # Not bound
        assert result is False

    def test_scope_filtering(self):
        manager = ShortcutManager()
        feed_shortcuts = manager.get_shortcuts_for_scope("feed")
        actions = [s.action for s in feed_shortcuts]
        assert "navigate_up" in actions
        assert "navigate_down" in actions

    def test_global_shortcuts_in_all_scopes(self):
        manager = ShortcutManager()
        for scope in ["panel", "feed", "briefing"]:
            shortcuts = manager.get_shortcuts_for_scope(scope)
            actions = [s.action for s in shortcuts]
            assert "toggle_panel" in actions

    def test_customize_shortcut(self):
        manager = ShortcutManager()
        result = manager.customize("toggle_panel", "⌥P")
        assert result is True
        called = []
        manager.register_handler("toggle_panel", lambda: called.append(True))
        manager.handle_key("⌥P")
        assert called == [True]

    def test_customize_nonexistent(self):
        manager = ShortcutManager()
        result = manager.customize("nonexistent_action", "⌘X")
        assert result is False

    def test_all_prd_shortcuts_defined(self):
        """All shortcuts from the original spec should be defined."""
        manager = ShortcutManager()
        expected_actions = [
            "toggle_panel", "show_briefing", "catch_me_up",
            "focus_search", "tab_briefing", "tab_feed", "tab_search",
            "show_health", "back_or_dismiss",
            "navigate_up", "navigate_down", "expand_item",
            "mark_useful", "mark_noise", "undo_last",
        ]
        all_actions = {s.action for s in manager.get_all_shortcuts()}
        for action in expected_actions:
            assert action in all_actions, f"Missing shortcut: {action}"


# =============================================================================
# Onboarding Tests
# =============================================================================


class TestOnboarding:
    """Test onboarding flow."""

    def test_initial_state(self):
        manager = OnboardingManager()
        assert manager.state.current_step == OnboardingStep.WELCOME
        assert manager.is_complete is False

    def test_advance_step(self):
        manager = OnboardingManager()
        next_step = manager.advance()
        assert next_step == OnboardingStep.API_KEY
        assert OnboardingStep.WELCOME.value in manager.state.completed_steps

    def test_skip_step(self):
        manager = OnboardingManager()
        manager.advance()  # → API_KEY
        skipped = manager.skip()  # Skip API_KEY → SOURCE_EMAIL
        assert skipped == OnboardingStep.SOURCE_EMAIL
        assert OnboardingStep.API_KEY.value in manager.state.skipped_steps

    def test_complete_api_key(self):
        manager = OnboardingManager()
        manager.complete_api_key("openai")
        assert manager.state.api_key_configured is True

    def test_complete_source(self):
        manager = OnboardingManager()
        manager.complete_source("email")
        assert "email" in manager.state.connected_sources

    def test_complete_multiple_sources(self):
        manager = OnboardingManager()
        manager.complete_source("email")
        manager.complete_source("slack")
        assert len(manager.state.connected_sources) == 2

    def test_source_dedup(self):
        manager = OnboardingManager()
        manager.complete_source("email")
        manager.complete_source("email")
        assert len(manager.state.connected_sources) == 1

    def test_mark_first_value(self):
        manager = OnboardingManager()
        manager.mark_first_value()
        assert manager.state.first_value_delivered is True

    def test_adaptive_phase_observation(self):
        manager = OnboardingManager()
        phase = manager.update_adaptive_phase()
        assert phase == AdaptivePhase.OBSERVATION

    def test_adaptive_phase_calibration(self):
        state = OnboardingState(
            started_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        manager = OnboardingManager(state=state)
        phase = manager.update_adaptive_phase()
        assert phase == AdaptivePhase.CALIBRATION

    def test_adaptive_phase_stabilization(self):
        state = OnboardingState(
            started_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        manager = OnboardingManager(state=state)
        phase = manager.update_adaptive_phase()
        assert phase == AdaptivePhase.STABILIZATION

    def test_adaptive_phase_steady_state(self):
        state = OnboardingState(
            started_at=datetime.now(timezone.utc) - timedelta(weeks=2),
        )
        manager = OnboardingManager(state=state)
        phase = manager.update_adaptive_phase()
        assert phase == AdaptivePhase.STEADY_STATE

    def test_confidence_threshold_decreases(self):
        """Confidence threshold should decrease as phase progresses."""
        manager = OnboardingManager()
        thresholds = []
        for phase in AdaptivePhase:
            manager.state.adaptive_phase = phase
            thresholds.append(manager.get_confidence_threshold())
        # Should be decreasing
        for i in range(len(thresholds) - 1):
            assert thresholds[i] >= thresholds[i + 1]

    def test_suggest_source_with_evidence(self):
        manager = OnboardingManager()
        assert manager.should_suggest_source("slack", "Found Slack threads in emails") is True

    def test_no_suggest_connected_source(self):
        manager = OnboardingManager()
        manager.complete_source("slack")
        assert manager.should_suggest_source("slack", "evidence") is False

    def test_no_suggest_skipped_source(self):
        manager = OnboardingManager()
        manager.state.skipped_steps.append("slack")
        assert manager.should_suggest_source("slack", "evidence") is False

    def test_no_suggest_without_evidence(self):
        manager = OnboardingManager()
        assert manager.should_suggest_source("slack", "") is False

    def test_resume_message(self):
        manager = OnboardingManager()
        manager.complete_source("email")
        msg = manager.get_resume_message()
        assert "email" in msg

    def test_current_message(self):
        manager = OnboardingManager()
        msg = manager.current_message
        assert "Welcome" in msg["title"]

    def test_serialization(self):
        manager = OnboardingManager()
        manager.complete_source("email")
        manager.complete_api_key("openai")
        data = manager.to_dict()
        assert data["api_key_configured"] is True
        assert "email" in data["connected_sources"]

    def test_full_flow_to_steady_state(self):
        """Walk through the complete onboarding flow."""
        manager = OnboardingManager()
        # Walk through all steps
        while not manager.is_complete:
            manager.advance()
        assert manager.state.current_step == OnboardingStep.STEADY_STATE

    def test_onboarding_checkpoint_file_persistence(self, tmp_path):
        """Test checkpoint saving and loading to/from disk."""
        chk_file = tmp_path / "onboarding.json"
        manager = OnboardingManager(checkpoint_path=chk_file)
        manager.complete_source("gmail")
        manager.complete_api_key("anthropic")
        manager.advance()

        assert chk_file.exists()

        restored = OnboardingManager.load_checkpoint(chk_file)
        assert "gmail" in restored.state.connected_sources
        assert restored.state.api_key_configured is True
        assert restored.state.current_step == OnboardingStep.API_KEY

    def test_onboarding_from_dict_and_to_dict(self):
        manager = OnboardingManager()
        manager.complete_source("slack")
        manager.record_feedback()
        data = manager.to_dict()

        restored = OnboardingManager.from_dict(data)
        assert "slack" in restored.state.connected_sources
        assert restored.state.feedback_count == 1


class TestPanelEnhancements:
    """Test settings tab and event bus integration."""

    def test_switch_to_settings_tab(self):
        panel = PanelController()
        panel.switch_tab(Tab.SETTINGS)
        assert panel.active_tab == Tab.SETTINGS
        empty = panel.get_empty_state()
        assert "Settings" in empty["title"]

    @pytest.mark.asyncio
    async def test_panel_drain_feedback_to_bus(self):
        from otto.core.event_bus import EventBus
        from otto.storage.models import UserFeedbackReceived

        bus = EventBus()
        received_events = []

        async def handler(event):
            received_events.append(event)

        await bus.subscribe(UserFeedbackReceived, handler)

        panel = PanelController(event_bus=bus)
        panel.set_feed([
            FeedItem(
                conversation_id="conv_10", source=SourceType.EMAIL,
                title="Test", sender="Alice", sender_role="Peer",
                time_ago="1h", summary="Sum", relevance_explanation="Rel",
                urgency=0.6,
            )
        ])
        panel.dismiss_item("conv_10")
        panel.mark_useful("conv_20")

        # Drain to bus
        await panel.drain_feedback_to_bus()
        await asyncio.sleep(0.05)

        assert len(received_events) >= 2
        conv_ids = [e.conversation_id for e in received_events]
        assert "conv_10" in conv_ids
        assert "conv_20" in conv_ids


class TestNotificationActionRequired:
    """Test require_action enforcement."""

    def test_suppress_when_action_not_required(self):
        policy = NotificationPolicy(require_action=True, min_urgency=0.8)
        engine = NotificationEngine(policy=policy)
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

        req = NotificationRequest(
            conversation_id="c1",
            title="Update",
            body="FYI update",
            urgency=0.85,
            action_required=False,
        )
        assert engine.request(req, now) == "suppressed"

    def test_deliver_when_action_required(self):
        policy = NotificationPolicy(require_action=True, min_urgency=0.8)
        engine = NotificationEngine(policy=policy)
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

        req = NotificationRequest(
            conversation_id="c1",
            title="Action",
            body="Please reply",
            urgency=0.85,
            action_required=True,
        )
        assert engine.request(req, now) == "delivered"

    def test_emergency_bypasses_action_required(self):
        policy = NotificationPolicy(require_action=True, bypass_threshold=0.95)
        engine = NotificationEngine(policy=policy)
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

        req = NotificationRequest(
            conversation_id="c1",
            title="Server Down",
            body="Prod outage",
            urgency=0.98,
            action_required=False,
        )
        assert engine.request(req, now) == "delivered"
