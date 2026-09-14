from __future__ import annotations
"""
Browser-based adapter package for zero-API-key operation.

When no API credentials are configured, Otto falls back to reading
data directly from Chrome/Safari browser tabs and native macOS apps
using AppleScript. All operations are strictly read-only.
"""

from otto.adapters.browser.reader import BrowserContentReader, BrowserTab, ExtractedContent
from otto.adapters.browser.gmail_browser import BrowserGmailAdapter
from otto.adapters.browser.slack_browser import BrowserSlackAdapter
from otto.adapters.browser.calendar_browser import BrowserCalendarAdapter
from otto.adapters.browser.jira_browser import BrowserJiraAdapter
from otto.adapters.browser.factory import (
    AdapterFactory,
    create_adapter,
    create_all_adapters,
    AdapterUnavailableError,
)

__all__ = [
    "BrowserContentReader",
    "BrowserTab",
    "ExtractedContent",
    "BrowserGmailAdapter",
    "BrowserSlackAdapter",
    "BrowserCalendarAdapter",
    "BrowserJiraAdapter",
    "AdapterFactory",
    "create_adapter",
    "create_all_adapters",
    "AdapterUnavailableError",
]
