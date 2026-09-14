from __future__ import annotations

"""Otto UI package."""

from otto.ui.menubar import MenuBarApp
from otto.ui.notifications import (
    NotificationEngine,
    NotificationPolicy,
    NotificationRequest,
)
from otto.ui.panel import (
    FeedItem,
    PanelController,
    SearchResult,
    Tab,
)
from otto.ui.shortcuts import Shortcut, ShortcutManager

__all__ = [
    "MenuBarApp",
    "PanelController",
    "NotificationEngine",
    "NotificationPolicy",
    "NotificationRequest",
    "ShortcutManager",
    "Shortcut",
    "FeedItem",
    "SearchResult",
    "Tab",
]
