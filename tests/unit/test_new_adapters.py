from __future__ import annotations

"""
Tests for Slack and Calendar adapters — read-only protocol compliance,
message parsing, and connection management.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.adapters.calendar import CalendarAdapter
from otto.storage.models import (
    ConnectionState,
    HealthStatus,
    SourceType,
)


# Slack API adapter tests live in tests/unit/test_slack_api.py (fake HTTP client).


# =============================================================================
# Calendar Adapter Tests
# =============================================================================


class TestCalendarAdapter:
    """Test read-only Calendar adapter."""

    def setup_method(self):
        self.mock_http = AsyncMock()
        self.adapter = CalendarAdapter(self.mock_http, "user@example.net")

    def test_name(self):
        assert self.adapter.name == "calendar:user@example.net"

    def test_source_type(self):
        assert self.adapter.source_type == SourceType.CALENDAR

    def test_no_write_methods(self):
        """Calendar adapter must not have write methods."""
        methods = [m for m in dir(self.adapter) if not m.startswith("_")]
        write_words = {"send", "post", "write", "update", "delete", "modify", "insert", "create"}
        for method in methods:
            for word in write_words:
                assert word not in method.lower(), f"Write method found: {method}"

    @pytest.mark.asyncio
    async def test_connect_success(self):
        self.mock_http.get = AsyncMock(return_value=MagicMock(status_code=200))
        result = await self.adapter.connect()
        assert result.state == ConnectionState.HEALTHY

    @pytest.mark.asyncio
    async def test_connect_auth_failure(self):
        self.mock_http.get = AsyncMock(return_value=MagicMock(status_code=401))
        result = await self.adapter.connect()
        assert result.state == ConnectionState.FAILED

    @pytest.mark.asyncio
    async def test_poll_when_not_connected(self):
        result = await self.adapter.poll(datetime.now(timezone.utc))
        assert result == []

    @pytest.mark.asyncio
    async def test_poll_with_events(self):
        self.mock_http.get = AsyncMock(return_value=MagicMock(status_code=200))
        await self.adapter.connect()

        events_resp = MagicMock(
            status_code=200,
            json=MagicMock(return_value={
                "items": [
                    {
                        "id": "event_1",
                        "summary": "Team Standup",
                        "start": {"dateTime": "2026-01-01T09:00:00-05:00"},
                        "end": {"dateTime": "2026-01-01T09:15:00-05:00"},
                        "attendees": [
                            {"email": "alice@co.example", "displayName": "Alice"},
                            {"email": "bob@co.example", "displayName": "Bob"},
                        ],
                        "organizer": {"email": "alice@co.example", "displayName": "Alice"},
                        "htmlLink": "https://calendar.google.com/event/1",
                    },
                ],
            }),
        )
        self.mock_http.get = AsyncMock(return_value=events_resp)
        events = await self.adapter.poll(datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert len(events) == 1
        assert events[0].source == SourceType.CALENDAR
        assert events[0].title == "Team Standup"
        assert "Alice" in events[0].plain_text

    def test_event_parsing_all_day(self):
        """Should handle all-day events."""
        item = {
            "id": "allday_1",
            "summary": "Company Holiday",
            "start": {"date": "2026-07-04"},
            "end": {"date": "2026-07-05"},
        }
        event = self.adapter._event_to_raw(item)
        assert event is not None
        assert event.title == "Company Holiday"
        assert event.raw_metadata["all_day"] is True

    def test_event_parsing_with_location(self):
        """Should include location in content."""
        item = {
            "id": "loc_1",
            "summary": "Offsite",
            "start": {"dateTime": "2026-03-01T10:00:00Z"},
            "location": "Conference Room 3B",
        }
        event = self.adapter._event_to_raw(item)
        assert event is not None
        assert "Conference Room 3B" in event.plain_text

    def test_event_parsing_no_title(self):
        """Should handle events without title."""
        item = {
            "id": "notitle",
            "start": {"dateTime": "2026-03-01T10:00:00Z"},
        }
        event = self.adapter._event_to_raw(item)
        assert event is not None
        assert event.title == "(No title)"

    def test_event_attendee_metadata(self):
        """Should track attendee count in metadata."""
        item = {
            "id": "e1",
            "summary": "Big meeting",
            "start": {"dateTime": "2026-03-01T10:00:00Z"},
            "attendees": [
                {"email": f"person{i}@co.example"} for i in range(8)
            ],
        }
        event = self.adapter._event_to_raw(item)
        assert event.raw_metadata["attendee_count"] == 8

    @pytest.mark.asyncio
    async def test_health_check(self):
        self.adapter._connected = True
        self.mock_http.get = AsyncMock(return_value=MagicMock(status_code=200))
        status = await self.adapter.health_check()
        assert status == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_disconnect(self):
        self.adapter._connected = True
        await self.adapter.disconnect()
        assert self.adapter._connected is False

    @pytest.mark.asyncio
    async def test_calendar_poll_pagination(self):
        self.adapter._connected = True
        # Page 1 has nextPageToken, Page 2 ends
        p1 = MagicMock(status_code=200, json=MagicMock(return_value={
            "nextPageToken": "token2",
            "items": [{"id": "ev1", "summary": "Meeting 1", "start": {"dateTime": "2026-01-01T10:00:00Z"}}]
        }))
        p2 = MagicMock(status_code=200, json=MagicMock(return_value={
            "items": [{"id": "ev2", "summary": "Meeting 2", "start": {"dateTime": "2026-01-01T11:00:00Z"}}]
        }))
        self.mock_http.get = AsyncMock(side_effect=[p1, p2])

        events = await self.adapter.poll(datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert len(events) == 2
        assert events[0].title == "Meeting 1"
        assert events[1].title == "Meeting 2"
