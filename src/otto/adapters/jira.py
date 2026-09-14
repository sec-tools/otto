from __future__ import annotations

"""
Jira adapter — read-only.

Fetches issues, comments, and status changes from Jira.
No write methods. All HTTP goes through WriteGuard.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from otto.adapters.base import ConnectionStatus, RawEvent
from otto.storage.models import (
    ConnectionState,
    ContentBlock,
    ContentType,
    HealthStatus,
    SourceType,
)

logger = logging.getLogger("otto.adapters.jira")


class JiraAdapter:
    """
    Read-only Jira adapter.

    Uses the Jira REST API to fetch issues, comments, and transitions.
    All HTTP requests go through the InstrumentedHttpClient (WriteGuard-wrapped).

    THERE IS NO issue_create(). NO issue_update(). NO comment_add(). NO transition().
    """

    def __init__(self, http_client: Any, base_url: str, account_id: str) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._account_id = account_id
        self._connected = False

    @property
    def name(self) -> str:
        return f"jira:{self._account_id}"

    @property
    def source_type(self) -> SourceType:
        return SourceType.JIRA

    async def connect(self) -> ConnectionStatus:
        """Verify Jira API access."""
        try:
            resp = await self._http.get(f"{self._base_url}/rest/api/2/myself")
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
                error=f"Jira API returned {resp.status_code}",
            )
        except Exception as e:
            return ConnectionStatus(state=ConnectionState.FAILED, error=str(e))

    async def poll(self, since: datetime) -> list[RawEvent]:
        """Fetch recently updated issues assigned to or mentioning the user."""
        if not self._connected:
            return []

        try:
            since_str = since.strftime("%Y-%m-%d %H:%M")
            jql = f"(assignee = currentUser() OR watcher = currentUser()) AND updated >= '{since_str}' ORDER BY updated DESC"

            events: list[RawEvent] = []
            start_at = 0
            page_size = 50
            for _ in range(5):  # up to 5 pages / 250 issues
                resp = await self._http.get(
                    f"{self._base_url}/rest/api/2/search",
                    params={
                        "jql": jql,
                        "startAt": start_at,
                        "maxResults": page_size,
                        "fields": "summary,status,assignee,reporter,priority,updated,comment,description,issuetype,project",
                    },
                )

                if resp.status_code != 200:
                    logger.warning("Jira poll failed: %d", resp.status_code)
                    break

                data = resp.json()
                issues = data.get("issues", [])
                for issue in issues:
                    event = self._issue_to_event(issue)
                    if event:
                        events.append(event)

                total = data.get("total", len(issues))
                start_at += len(issues)
                if start_at >= total or not issues:
                    break

            logger.info("Jira poll: %d issues since %s", len(events), since.isoformat())
            return events

        except Exception as e:
            logger.error("Jira poll failed: %s", e)
            return []

    def _issue_to_event(self, issue: dict[str, Any]) -> RawEvent | None:
        """Convert a Jira issue to a RawEvent."""
        fields = issue.get("fields", {})
        key = issue.get("key", "")

        # Timestamp
        updated = fields.get("updated", "")
        try:
            timestamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            timestamp = datetime.now(timezone.utc)

        # Content
        summary = fields.get("summary", "")
        description = fields.get("description", "") or ""
        status = fields.get("status", {}).get("name", "Unknown")
        priority = fields.get("priority", {}).get("name", "None")
        issue_type = fields.get("issuetype", {}).get("name", "Task")
        project = fields.get("project", {}).get("key", "")

        # Reporter/assignee
        reporter = fields.get("reporter", {})
        assignee = fields.get("assignee", {})

        # Latest comment
        comments = fields.get("comment", {}).get("comments", [])
        latest_comment = ""
        if comments:
            latest_comment = comments[-1].get("body", "")[:500]

        content_parts = [
            f"[{key}] {summary}",
            f"Status: {status} | Priority: {priority} | Type: {issue_type}",
        ]
        if latest_comment:
            content_parts.append(f"Latest comment: {latest_comment}")
        if description:
            content_parts.append(f"Description: {description[:500]}")

        content_text = "\n".join(content_parts)

        return RawEvent(
            source=SourceType.JIRA,
            source_id=key,
            source_url=f"{self._base_url}/browse/{key}",
            timestamp=timestamp,
            title=f"[{key}] {summary}",
            content_blocks=[ContentBlock(type=ContentType.TEXT, text=content_text)],
            plain_text=content_text,
            sender_name=reporter.get("displayName", ""),
            sender_email=reporter.get("emailAddress", ""),
            thread_id=key,  # Jira key is the thread
            is_auto_generated=False,
            raw_metadata={
                "project": project,
                "status": status,
                "priority": priority,
                "issue_type": issue_type,
                "assignee": assignee.get("displayName", ""),
                "comment_count": len(comments),
            },
        )

    async def fetch_thread(self, issue_key: str) -> list[RawEvent]:
        """Fetch a single issue with full comment history."""
        try:
            resp = await self._http.get(
                f"{self._base_url}/rest/api/2/issue/{issue_key}",
                params={"fields": "summary,status,assignee,reporter,priority,updated,comment,description,issuetype,project"},
            )
            if resp.status_code != 200:
                return []
            event = self._issue_to_event(resp.json())
            return [event] if event else []
        except Exception as e:
            logger.error("Failed to fetch Jira issue %s: %s", issue_key, e)
            return []

    async def health_check(self) -> HealthStatus:
        if not self._connected:
            return HealthStatus.UNHEALTHY
        try:
            resp = await self._http.get(f"{self._base_url}/rest/api/2/myself")
            return HealthStatus.HEALTHY if resp.status_code == 200 else HealthStatus.DEGRADED
        except Exception:
            return HealthStatus.UNHEALTHY

    async def disconnect(self) -> None:
        self._connected = False
