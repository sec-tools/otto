from __future__ import annotations
"""
Source adapter protocol and base types.

All adapters are read-only by design — there are NO write methods.
Adapters receive an InstrumentedHttpClient (WriteGuard-wrapped)
and their own scoped credential. They never get direct network
access or access to other adapters' credentials.
"""


import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from otto.storage.models import ConnectionState, ContentBlock, HealthStatus, SourceType

logger = logging.getLogger("otto.adapters.base")


@dataclass
class RawEvent:
    """
    Raw event from a source before normalization.

    Each adapter produces RawEvents which the ingestion pipeline
    normalizes into NormalizedEvents.
    """
    source: SourceType
    source_id: str              # Native ID in source system
    source_url: str             # Deep link to source
    timestamp: datetime         # When the event occurred (UTC)
    title: str
    content_blocks: list[ContentBlock] = field(default_factory=list)
    plain_text: str = ""
    sender_name: str | None = None
    sender_email: str | None = None
    sender_id: str | None = None
    recipients: list[dict[str, str]] = field(default_factory=list)
    thread_id: str | None = None    # Email thread ID, Slack thread_ts, Jira key
    has_attachments: bool = False
    attachment_summaries: list[str] = field(default_factory=list)
    is_auto_generated: bool = False
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConnectionStatus:
    """Result of a connection attempt."""
    state: ConnectionState
    error: str | None = None
    scopes_granted: set[str] = field(default_factory=set)


@runtime_checkable
class SourceAdapter(Protocol):
    """
    Read-only source adapter protocol.

    THERE IS NO send(). NO post(). NO write(). NO update(). NO delete().

    This is by design — the Protocol itself enforces the read-only
    guarantee at the type level. Any implementation that adds write
    methods violates the contract.
    """

    @property
    def name(self) -> str:
        """Human-readable adapter name (e.g., 'Gmail', 'Slack')."""
        ...

    @property
    def source_type(self) -> SourceType:
        """The source type this adapter handles."""
        ...

    # mode is accessed via getattr(adapter, 'mode', AdapterMode.API)
    # Not part of the protocol to preserve runtime_checkable compatibility.

    async def connect(self) -> ConnectionStatus:
        """
        Establish connection and verify credentials.
        Returns connection status with granted scopes.
        """
        ...

    async def poll(self, since: datetime) -> list[RawEvent]:
        """
        Poll for new events since the given timestamp.
        Returns raw events for the ingestion pipeline.
        """
        ...

    async def fetch_thread(self, thread_id: str) -> list[RawEvent]:
        """Fetch all messages in a thread/conversation."""
        ...

    async def health_check(self) -> HealthStatus:
        """Check if the connection is healthy."""
        ...

    async def disconnect(self) -> None:
        """Clean up resources."""
        ...


class AdapterManager:
    """
    Manages multiple source adapters with polling coordination.

    Each adapter runs in its own asyncio task with independent
    error boundaries and timeout wrappers.
    """

    def __init__(
        self,
        pipeline: Any = None,
        poll_interval: float = 60.0,
        on_events: Callable[[list[RawEvent]], Awaitable[None]] | None = None,
    ) -> None:
        self._adapters: dict[str, SourceAdapter] = {}
        self._poll_tasks: dict[str, asyncio.Task[None]] = {}
        self._last_poll: dict[str, datetime] = {}
        self._running = False
        self._pipeline = pipeline
        self._poll_interval = poll_interval
        self._on_events = on_events

    @property
    def name(self) -> str:
        return "adapter_manager"

    def register(self, adapter: SourceAdapter) -> None:
        """Register an adapter for management."""
        self._adapters[adapter.name] = adapter

    async def start(self) -> None:
        """Connect all adapters and start polling loops."""
        self._running = True
        for name, adapter in self._adapters.items():
            try:
                status = await asyncio.wait_for(adapter.connect(), timeout=60)
                logger.info("Adapter %s connected: %s", name, status.state)
                if status and status.state in (ConnectionState.HEALTHY, ConnectionState.DEGRADED):
                    self._poll_tasks[name] = asyncio.create_task(
                        self._poll_loop(name, adapter)
                    )
            except asyncio.TimeoutError:
                logger.error("Adapter %s connection timed out (60s)", name)
            except Exception as e:
                logger.error("Adapter %s connection failed: %s", name, e)

    async def _poll_loop(self, name: str, adapter: SourceAdapter) -> None:
        """Periodic poll loop for a single adapter."""
        while self._running:
            try:
                since = self._last_poll.get(
                    name,
                    datetime.now(timezone.utc) - timedelta(hours=24),
                )
                poll_start = datetime.now(timezone.utc)
                if hasattr(adapter, "poll"):
                    events = await asyncio.wait_for(adapter.poll(since), timeout=30)
                    if events:
                        if self._pipeline and hasattr(self._pipeline, "ingest"):
                            await self._pipeline.ingest(events)
                        elif self._on_events:
                            await self._on_events(events)
                    self._last_poll[name] = poll_start
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error polling adapter %s: %s", name, e)

            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break

    async def stop(self) -> None:
        """Stop all polling and disconnect adapters."""
        self._running = False
        for task in self._poll_tasks.values():
            task.cancel()
        if self._poll_tasks:
            await asyncio.gather(*self._poll_tasks.values(), return_exceptions=True)
            self._poll_tasks.clear()
        for name, adapter in self._adapters.items():
            try:
                await asyncio.wait_for(adapter.disconnect(), timeout=10.0)
            except Exception as e:
                logger.error("Error disconnecting %s: %s", name, e)

    async def health_check(self) -> HealthStatus:
        """Aggregate health across all adapters."""
        if not self._adapters:
            return HealthStatus.HEALTHY
        statuses = []
        for adapter in self._adapters.values():
            try:
                statuses.append(await adapter.health_check())
            except Exception:
                statuses.append(HealthStatus.UNHEALTHY)
        if all(s == HealthStatus.HEALTHY for s in statuses):
            return HealthStatus.HEALTHY
        if any(s == HealthStatus.HEALTHY for s in statuses):
            return HealthStatus.DEGRADED
        return HealthStatus.UNHEALTHY

    def get_checkpoint_data(self) -> dict[str, Any]:
        """Checkpoint last poll timestamps for restart recovery."""
        return {
            "last_poll_timestamps": {
                name: ts.isoformat() for name, ts in self._last_poll.items()
            }
        }

    def load_checkpoint_data(self, data: dict[str, Any]) -> None:
        """Restore checkpointed poll timestamps from disk."""
        raw_ts = data.get("last_poll_timestamps", {})
        for name, ts_str in raw_ts.items():
            try:
                self._last_poll[name] = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                pass
