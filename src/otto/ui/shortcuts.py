from __future__ import annotations

"""
Keyboard shortcuts — global and panel-scoped hotkey management.
"""

import logging
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("otto.ui.shortcuts")


@dataclass
class Shortcut:
    """A keyboard shortcut binding."""
    key_combo: str          # e.g., "⌥O", "⌘B", "⌘M"
    action: str             # Internal action identifier
    description: str        # Human-readable description
    is_global: bool = False # Global = works even when panel not focused
    scope: str = "panel"    # "global", "panel", "feed", "briefing"


# Default shortcut definitions from the original spec
DEFAULT_SHORTCUTS: list[Shortcut] = [
    # Global shortcuts
    Shortcut("⌥O", "toggle_panel", "Summon/dismiss Otto panel", is_global=True, scope="global"),

    # Panel-wide shortcuts
    Shortcut("⌘B", "show_briefing", "Show current briefing", scope="panel"),
    Shortcut("⌘M", "catch_me_up", "Catch me up (delta briefing)", scope="panel"),
    Shortcut("⌘F", "focus_search", "Focus search", scope="panel"),
    Shortcut("⌘1", "tab_briefing", "Switch to Briefing tab", scope="panel"),
    Shortcut("⌘2", "tab_feed", "Switch to Feed tab", scope="panel"),
    Shortcut("⌘3", "tab_search", "Switch to Search tab", scope="panel"),
    Shortcut("⌘H", "show_health", "Health dashboard", scope="panel"),
    Shortcut("Escape", "back_or_dismiss", "Back / dismiss panel", scope="panel"),

    # Feed navigation
    Shortcut("↑", "navigate_up", "Previous item", scope="feed"),
    Shortcut("↓", "navigate_down", "Next item", scope="feed"),
    Shortcut("Enter", "expand_item", "Expand item detail", scope="feed"),
    Shortcut("→", "mark_useful", "Mark useful (feedback)", scope="feed"),
    Shortcut("←", "mark_noise", "Mark noise (feedback)", scope="feed"),
    Shortcut("⌘Z", "undo_last", "Undo last feedback/dismiss", scope="panel"),
]


class ShortcutManager:
    """
    Manages keyboard shortcuts.

    Handles registration, scope resolution, and callback dispatch.
    """

    def __init__(self) -> None:
        self._shortcuts = {s.action: s for s in DEFAULT_SHORTCUTS}
        self._handlers: dict[str, Callable] = {}

    def register_handler(self, action: str, handler: Callable) -> None:
        """Register a handler for a shortcut action."""
        self._handlers[action] = handler

    def handle_key(self, key_combo: str, current_scope: str = "panel") -> bool:
        """
        Process a key event.

        Returns True if the key was handled, False if not bound.
        """
        for shortcut in self._shortcuts.values():
            if shortcut.key_combo == key_combo:
                # Check scope
                if shortcut.is_global or shortcut.scope in ("panel", current_scope):
                    handler = self._handlers.get(shortcut.action)
                    if handler:
                        handler()
                        logger.debug("Shortcut handled: %s → %s", key_combo, shortcut.action)
                        return True
        return False

    def get_shortcuts_for_scope(self, scope: str) -> list[Shortcut]:
        """Get all shortcuts available in a given scope."""
        return [
            s for s in self._shortcuts.values()
            if s.scope == scope or s.scope == "panel" or s.is_global
        ]

    def get_all_shortcuts(self) -> list[Shortcut]:
        return list(self._shortcuts.values())

    def customize(self, action: str, new_key_combo: str) -> bool:
        """Allow user to customize a shortcut."""
        if action in self._shortcuts:
            self._shortcuts[action].key_combo = new_key_combo
            return True
        return False
