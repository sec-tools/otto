"""
Typed internal event bus with publish/subscribe pattern.

Each subscriber runs in an independent error boundary — one subscriber's
failure NEVER affects other subscribers or the publisher. This is the
backbone of Otto's subsystem isolation.
"""
from __future__ import annotations
import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, TypeVar

from otto.storage.models import BusEvent

logger = logging.getLogger("otto.core.event_bus")

T = TypeVar("T", bound=BusEvent)

Subscriber = Callable[[Any], Awaitable[None]]


class EventBus:
    """
    Typed internal event bus for decoupled subsystem communication.

    Usage:
        bus = EventBus()
        await bus.subscribe(NewEventsIngested, my_handler)
        await bus.publish(NewEventsIngested(source=SourceType.EMAIL, event_ids=["1"], count=1))
    """

    def __init__(self) -> None:
        self._subscribers: dict[type[BusEvent], set[Subscriber]] = defaultdict(set)
        self._wildcard_subscribers: set[Subscriber] = set()
        self._lock: asyncio.Lock | None = None
        self._active_tasks: set[asyncio.Task] = set()

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def subscribe(self, event_type: type[BusEvent], subscriber: Subscriber) -> None:
        """Subscribe to a specific event type."""
        async with self._ensure_lock():
            self._subscribers[event_type].add(subscriber)

    async def subscribe_all(self, subscriber: Subscriber) -> None:
        """Subscribe to ALL events (wildcard)."""
        async with self._ensure_lock():
            self._wildcard_subscribers.add(subscriber)

    async def unsubscribe(self, event_type: type[BusEvent], subscriber: Subscriber) -> None:
        """Unsubscribe from a specific event type."""
        async with self._ensure_lock():
            self._subscribers[event_type].discard(subscriber)

    async def publish(self, event: BusEvent) -> None:
        """
        Publish an event to all matching subscribers.

        Each subscriber runs in an independent asyncio task with its own
        try/except boundary. A failing subscriber is logged but never
        blocks or crashes other subscribers.
        """
        async with self._ensure_lock():
            event_type = type(event)
            targets: list[Subscriber] = list(self._subscribers.get(event_type, set()))
            targets.extend(self._wildcard_subscribers)

        for subscriber in targets:
            task = asyncio.create_task(self._safe_invoke(subscriber, event))
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)

    async def _safe_invoke(self, subscriber: Subscriber, event: BusEvent) -> None:
        """Invoke a subscriber with full error isolation."""
        try:
            await subscriber(event)
        except Exception:
            logger.error(
                "Subscriber %s failed on event %s",
                getattr(subscriber, "__qualname__", subscriber),
                type(event).__name__,
                exc_info=True,
            )
