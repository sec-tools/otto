from __future__ import annotations

"""
Menu bar app — Otto's macOS menu bar presence.

Uses rumps for the status bar icon and menu, delegates to the
panel controller for the main UI.
"""

import logging
from typing import Any, Callable

logger = logging.getLogger("otto.ui.menubar")


class MenuBarApp:
    """
    Otto's macOS menu bar status item.

    Displays a status icon with:
    - AI tier indicator (🟢 Full / 🟡 Backup / 🟠 Local / 🔴 Heuristics)
    - Unread badge count
    - Click to summon/dismiss panel
    - Menu items for common actions

    Note: This class abstracts the rumps dependency so tests can run
    without a GUI environment.
    """

    def __init__(self, panel_controller: Any = None) -> None:
        self._panel = panel_controller
        self._title = "○"  # Otto default icon (calm, inactive)
        self._badge_count = 0
        self._ai_tier = "full"
        self._is_panel_visible = False
        self._callbacks: dict[str, Callable] = {}

    @property
    def title(self) -> str:
        return self._title

    @property
    def badge_count(self) -> int:
        return self._badge_count

    @property
    def is_panel_visible(self) -> bool:
        return self._is_panel_visible

    def set_badge(self, count: int) -> None:
        """Update the unread badge count."""
        self._badge_count = max(0, count)
        if count > 0:
            self._title = f"● {count}"
        else:
            self._title = "○"
        logger.debug("Badge updated: %d", count)

    def set_ai_tier(self, tier: str) -> None:
        """
        Update the AI tier indicator.

        Tiers: full, backup, local, heuristic
        """
        self._ai_tier = tier
        tier_icons = {
            "full": "🟢",
            "backup": "🟡",
            "local": "🟠",
            "heuristic": "🔴",
        }
        icon = tier_icons.get(tier, "⚪")
        logger.info("AI tier: %s %s", icon, tier)

    def toggle_panel(self) -> None:
        """Toggle the main panel visibility."""
        self._is_panel_visible = not self._is_panel_visible
        if self._panel:
            if self._is_panel_visible:
                self._panel.show()
            else:
                self._panel.hide()
        self._fire("panel_toggled", self._is_panel_visible)

    def show_panel(self) -> None:
        self._is_panel_visible = True
        if self._panel:
            self._panel.show()

    def hide_panel(self) -> None:
        self._is_panel_visible = False
        if self._panel:
            self._panel.hide()

    def get_menu_items(self) -> list[dict[str, str]]:
        """Build the dropdown menu."""
        items = [
            {"title": "Show Otto", "action": "toggle_panel", "shortcut": "⌥O"},
            {"title": "separator"},
            {"title": "Morning Briefing", "action": "show_briefing", "shortcut": "⌘B"},
            {"title": "Catch Me Up", "action": "catch_me_up", "shortcut": "⌘M"},
            {"title": "separator"},
            {"title": f"AI: {self._ai_tier.title()}", "action": None},
            {"title": "separator"},
            {"title": "Health Dashboard", "action": "show_health", "shortcut": "⌘H"},
            {"title": "Settings...", "action": "show_settings"},
            {"title": "separator"},
            {"title": "Quit Otto", "action": "quit", "shortcut": "⌘Q"},
        ]
        return items

    def on(self, event: str, callback: Callable) -> None:
        """Register an event callback."""
        self._callbacks[event] = callback

    def _fire(self, event: str, *args: Any) -> None:
        if event in self._callbacks:
            self._callbacks[event](*args)
