from __future__ import annotations

"""
Google Calendar adapter — read-only.

Fetches calendar events for meeting prep and schedule awareness.
No write methods. All HTTP goes through WriteGuard.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from otto.adapters.base import ConnectionStatus, RawEvent
from otto.storage.models import (
    ConnectionState,
    ContentBlock,
    ContentType,
    HealthStatus,
    SourceType,
)

logger = logging.getLogger("otto.adapters.calendar")

GCAL_API_BASE = "https://www.googleapis.com/calendar/v3"


class CalendarAdapter:
    """
    Read-only Google Calendar adapter.

    Uses the Calendar API to fetch events and attendee lists.
    All HTTP requests go through the InstrumentedHttpClient (WriteGuard-wrapped).

    THERE IS NO events_insert(). NO events_update(). NO events_delete().
    """

    def __init__(self, http_client: Any, account_id: str) -> None:
        self._http = http_client
        self._account_id = account_id
        self._connected = False

    @property
    def name(self) -> str:
        return f"calendar:{self._account_id}"

    @property
    def source_type(self) -> SourceType:
        return SourceType.CALENDAR

    async def connect(self) -> ConnectionStatus:
        """Verify Calendar API access."""
        try:
            resp = await self._http.get(f"{GCAL_API_BASE}/calendars/primary")
            if resp.status_code == 200:
                self._connected = True
                return ConnectionStatus(state=ConnectionState.HEALTHY)
            elif resp.status_code == 401:
                return ConnectionStatus(
                    state=ConnectionState.FAILED,
                    error="Authentication failed.",
                )
            return ConnectionStatus(
                state=ConnectionState.FAILED,
                error=f"Calendar API returned {resp.status_code}",
            )
        except Exception as e:
            return ConnectionStatus(state=ConnectionState.FAILED, error=str(e))

    async def poll(self, since: datetime) -> list[RawEvent]:
        """Fetch calendar events in a time window."""
        if not self._connected:
            return []

        try:
            # Fetch events from `since` to 7 days out
            time_min = since.isoformat()
            time_max = (since + timedelta(days=7)).isoformat()

            events: list[RawEvent] = []
            page_token = None
            for _ in range(5):  # up to 5 pages / 500 events
                params: dict[str, Any] = {
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": 100,
                }
                if page_token:
                    params["pageToken"] = page_token

                resp = await self._http.get(
                    f"{GCAL_API_BASE}/calendars/primary/events",
                    params=params,
                )
                if resp.status_code != 200:
                    logger.warning("Calendar poll failed: %d", resp.status_code)
                    break

                data = resp.json()
                for item in data.get("items", []):
                    event = self._event_to_raw(item)
                    if event:
                        events.append(event)

                page_token = data.get("nextPageToken")
                if not page_token:
                    break

            logger.info("Calendar poll: %d events", len(events))
            return events

        except Exception as e:
            logger.error("Calendar poll failed: %s", e)
            return []

    def _event_to_raw(self, item: dict[str, Any]) -> RawEvent | None:
        """Convert a Calendar event to a RawEvent."""
        # Parse start time
        start = item.get("start", {})
        start_str = start.get("dateTime") or start.get("date", "")
        try:
            if "T" in start_str:
                timestamp = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            else:
                timestamp = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            timestamp = datetime.now(timezone.utc)

        title = item.get("summary", "(No title)")
        description = item.get("description", "")

        # Attendees
        attendees = item.get("attendees", [])
        recipients = [
            {"email": a.get("email", ""), "field": "attendee"}
            for a in attendees
        ]

        # Content
        content_parts = [f"Event: {title}"]
        if item.get("location"):
            content_parts.append(f"Location: {item['location']}")
        if description:
            content_parts.append(f"Description: {description[:500]}")
        if attendees:
            names = [a.get("displayName", a.get("email", "")) for a in attendees[:10]]
            content_parts.append(f"Attendees: {', '.join(names)}")

        content_text = "\n".join(content_parts)

        return RawEvent(
            source=SourceType.CALENDAR,
            source_id=item.get("id", ""),
            source_url=item.get("htmlLink", ""),
            timestamp=timestamp,
            title=title,
            content_blocks=[ContentBlock(type=ContentType.TEXT, text=content_text)],
            plain_text=content_text,
            sender_name=item.get("organizer", {}).get("displayName", ""),
            sender_email=item.get("organizer", {}).get("email", ""),
            recipients=recipients,
            thread_id=item.get("recurringEventId", item.get("id", "")),
            is_auto_generated=False,
            raw_metadata={
                "calendar_id": "primary",
                "status": item.get("status", "confirmed"),
                "conference_data": bool(item.get("conferenceData")),
                "all_day": "date" in start and "dateTime" not in start,
                "attendee_count": len(attendees),
            },
        )

    async def get_upcoming(self, hours: int = 2) -> list[RawEvent]:
        """Get events in the next N hours (for meeting prep triggers)."""
        now = datetime.now(timezone.utc)
        events = await self.poll(now)
        cutoff = now + timedelta(hours=hours)
        return [e for e in events if e.timestamp <= cutoff]

    async def fetch_thread(self, thread_id: str) -> list[RawEvent]:
        """Fetch a single calendar event by ID."""
        try:
            resp = await self._http.get(
                f"{GCAL_API_BASE}/calendars/primary/events/{thread_id}",
            )
            if resp.status_code != 200:
                return []
            event = self._event_to_raw(resp.json())
            return [event] if event else []
        except Exception as e:
            logger.error("Failed to fetch calendar event %s: %s", thread_id, e)
            return []

    async def health_check(self) -> HealthStatus:
        if not self._connected:
            return HealthStatus.UNHEALTHY
        try:
            resp = await self._http.get(f"{GCAL_API_BASE}/calendars/primary")
            return HealthStatus.HEALTHY if resp.status_code == 200 else HealthStatus.DEGRADED
        except Exception:
            return HealthStatus.UNHEALTHY

    async def disconnect(self) -> None:
        self._connected = False
