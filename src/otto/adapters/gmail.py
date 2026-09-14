from __future__ import annotations
"""
Gmail adapter — read-only.

Fetches email via the Gmail API (googleapis). Handles thread
reconstruction, HTML→text extraction, and attachment metadata.
No write methods. All HTTP goes through WriteGuard.
"""


import base64
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from otto.adapters.base import ConnectionStatus, RawEvent
from otto.storage.models import ConnectionState, ContentBlock, ContentType, HealthStatus, SourceType

logger = logging.getLogger("otto.adapters.gmail")

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailAdapter:
    """
    Read-only Gmail adapter.

    Uses the Gmail API to fetch messages and threads.
    All HTTP requests go through the InstrumentedHttpClient (WriteGuard-wrapped).

    THERE IS NO send(). NO draft(). NO modify(). NO delete(). NO trash().
    """

    def __init__(self, http_client: Any, account_id: str) -> None:
        """
        Args:
            http_client: InstrumentedHttpClient (WriteGuard-wrapped).
            account_id: User's email address (for multi-account).
        """
        self._http = http_client
        self._account_id = account_id
        self._connected = False

    @property
    def name(self) -> str:
        return f"gmail:{self._account_id}"

    @property
    def source_type(self) -> SourceType:
        return SourceType.EMAIL

    async def connect(self) -> ConnectionStatus:
        """Verify Gmail API access with a lightweight profile request."""
        try:
            resp = await self._http.get(f"{GMAIL_API_BASE}/profile")
            if resp.status_code == 200:
                self._connected = True
                return ConnectionStatus(state=ConnectionState.HEALTHY)
            elif resp.status_code == 401:
                return ConnectionStatus(
                    state=ConnectionState.FAILED,
                    error="Authentication failed. Token may be expired.",
                )
            else:
                return ConnectionStatus(
                    state=ConnectionState.FAILED,
                    error=f"Gmail API returned {resp.status_code}",
                )
        except Exception as e:
            return ConnectionStatus(state=ConnectionState.FAILED, error=str(e))

    async def poll(self, since: datetime) -> list[RawEvent]:
        """
        Fetch new messages since the given timestamp.

        Uses Gmail's list + get pattern:
        1. List message IDs matching 'after:' query
        2. Batch-fetch full messages
        3. Convert to RawEvents
        """
        if not self._connected:
            return []

        epoch_seconds = int(since.timestamp())
        query = f"after:{epoch_seconds}"

        try:
            # List message IDs with pagination (up to 5 pages / 500 messages)
            messages: list[dict[str, Any]] = []
            page_token = None
            for _ in range(5):
                params: dict[str, Any] = {"q": query, "maxResults": 100}
                if page_token:
                    params["pageToken"] = page_token
                resp = await self._http.get(
                    f"{GMAIL_API_BASE}/messages",
                    params=params,
                )
                if resp.status_code != 200:
                    logger.warning("Gmail list failed: %d", resp.status_code)
                    break

                data = resp.json()
                page_msgs = data.get("messages", [])
                if page_msgs:
                    messages.extend(page_msgs)
                page_token = data.get("nextPageToken")
                if not page_token:
                    break

            if not messages:
                return []

            # Fetch full messages
            events: list[RawEvent] = []
            for msg_ref in messages:
                try:
                    event = await self._fetch_message(msg_ref["id"])
                    if event:
                        events.append(event)
                except Exception as e:
                    logger.error("Failed to fetch message %s: %s", msg_ref["id"], e)

            logger.info("Gmail poll: %d new messages since %s", len(events), since.isoformat())
            return events

        except Exception as e:
            logger.error("Gmail poll failed: %s", e)
            return []

    async def _fetch_message(self, message_id: str) -> RawEvent | None:
        """Fetch a single message by ID and convert to RawEvent."""
        resp = await self._http.get(
            f"{GMAIL_API_BASE}/messages/{message_id}",
            params={"format": "full"},
        )
        if resp.status_code != 200:
            return None

        msg = resp.json()
        payload = msg.get("payload") or {}
        raw_headers = payload.get("headers") or []
        headers = {
            h["name"].lower(): h["value"]
            for h in raw_headers
            if isinstance(h, dict) and "name" in h and "value" in h
        }

        # Parse timestamp
        date_str = headers.get("date", "")
        try:
            timestamp = parsedate_to_datetime(date_str).astimezone(timezone.utc)
        except (ValueError, TypeError):
            timestamp = datetime.now(timezone.utc)

        # Extract body content
        content_blocks = self._extract_content(msg.get("payload", {}))
        plain_text = self._extract_plain_text(msg.get("payload", {}))

        # Attachment detection
        attachments = self._extract_attachment_summaries(msg.get("payload", {}))

        # Build recipients
        recipients: list[dict[str, str]] = []
        for field_name in ("to", "cc"):
            if field_name in headers:
                for addr in headers[field_name].split(","):
                    recipients.append({"email": addr.strip(), "field": field_name})

        return RawEvent(
            source=SourceType.EMAIL,
            source_id=message_id,
            source_url=f"https://mail.google.com/mail/u/0/#inbox/{message_id}",
            timestamp=timestamp,
            title=headers.get("subject", "(no subject)"),
            content_blocks=content_blocks,
            plain_text=plain_text,
            sender_name=headers.get("from", ""),
            sender_email=headers.get("from", ""),
            recipients=recipients,
            thread_id=msg.get("threadId"),
            has_attachments=len(attachments) > 0,
            attachment_summaries=attachments,
            is_auto_generated="auto-submitted" in headers or "precedence" in headers,
            raw_metadata={
                "gmail_id": message_id,
                "thread_id": msg.get("threadId"),
                "label_ids": msg.get("labelIds", []),
            },
        )

    def _extract_content(self, payload: dict[str, Any]) -> list[ContentBlock]:
        """Extract content blocks from Gmail payload (handles multipart)."""
        blocks: list[ContentBlock] = []
        mime_type = payload.get("mimeType", "")

        if mime_type == "text/plain":
            text = self._decode_body(payload.get("body", {}))
            if text:
                blocks.append(ContentBlock(type=ContentType.TEXT, text=text))
        elif mime_type == "text/html":
            html = self._decode_body(payload.get("body", {}))
            if html:
                blocks.append(ContentBlock(type=ContentType.HTML, html=html))
        elif mime_type.startswith("multipart/"):
            for part in payload.get("parts", []):
                blocks.extend(self._extract_content(part))

        return blocks

    def _extract_plain_text(self, payload: dict[str, Any]) -> str:
        """Extract a plain text representation from the payload."""
        mime_type = payload.get("mimeType", "")
        if mime_type == "text/plain":
            return self._decode_body(payload.get("body", {}))
        elif mime_type.startswith("multipart/"):
            for part in payload.get("parts", []):
                text = self._extract_plain_text(part)
                if text:
                    return text
        return ""

    def _extract_attachment_summaries(self, payload: dict[str, Any]) -> list[str]:
        """Extract attachment summaries (filename + size) without downloading."""
        summaries: list[str] = []
        for part in payload.get("parts", []):
            filename = part.get("filename")
            if filename:
                size = part.get("body", {}).get("size", 0)
                size_kb = size / 1024
                summaries.append(f"{filename} ({size_kb:.0f}KB)")
            # Recurse for nested multipart
            summaries.extend(self._extract_attachment_summaries(part))
        return summaries

    @staticmethod
    def _decode_body(body: dict[str, Any]) -> str:
        """Decode base64url-encoded body data."""
        data = body.get("data", "")
        if not data:
            return ""
        try:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        except Exception:
            return ""

    async def fetch_thread(self, thread_id: str) -> list[RawEvent]:
        """Fetch all messages in a Gmail thread."""
        try:
            resp = await self._http.get(
                f"{GMAIL_API_BASE}/threads/{thread_id}",
                params={"format": "full"},
            )
            if resp.status_code != 200:
                return []

            thread = resp.json()
            events: list[RawEvent] = []
            for msg in thread.get("messages", []):
                event = await self._fetch_message(msg["id"])
                if event:
                    events.append(event)
            return events
        except Exception as e:
            logger.error("Failed to fetch thread %s: %s", thread_id, e)
            return []

    async def health_check(self) -> HealthStatus:
        """Lightweight health check via profile endpoint."""
        if not self._connected:
            return HealthStatus.UNHEALTHY
        try:
            resp = await self._http.get(f"{GMAIL_API_BASE}/profile")
            return HealthStatus.HEALTHY if resp.status_code == 200 else HealthStatus.DEGRADED
        except Exception:
            return HealthStatus.UNHEALTHY

    async def disconnect(self) -> None:
        """Clean up resources."""
        self._connected = False
