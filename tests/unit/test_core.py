"""
Tests for core architecture — EventBus, Supervisor, ShutdownCoordinator.

Covers pub/sub isolation, health monitoring, backoff restarts,
graceful shutdown ordering, and state checkpointing.
"""

import asyncio
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.core.event_bus import EventBus
from otto.core.shutdown import ShutdownCoordinator
from otto.core.supervisor import Supervisor
from otto.storage.models import FailureRecorded, HealthStatus, NewEventsIngested, SourceType


# =============================================================================
# EventBus Tests
# =============================================================================


class TestEventBus:
    """Test typed event bus with error isolation."""

    @pytest.mark.asyncio
    async def test_publish_to_subscriber(self):
        """Published event should reach the subscribed handler."""
        bus = EventBus()
        received = []

        async def handler(event: NewEventsIngested):
            received.append(event)

        await bus.subscribe(NewEventsIngested, handler)
        event = NewEventsIngested(source=SourceType.EMAIL, event_ids=["1"], count=1)
        await bus.publish(event)
        await asyncio.sleep(0.05)  # Let tasks complete

        assert len(received) == 1
        assert received[0].source == SourceType.EMAIL

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self):
        """Multiple subscribers should all receive the event."""
        bus = EventBus()
        received_a = []
        received_b = []

        async def handler_a(event):
            received_a.append(event)

        async def handler_b(event):
            received_b.append(event)

        await bus.subscribe(NewEventsIngested, handler_a)
        await bus.subscribe(NewEventsIngested, handler_b)

        event = NewEventsIngested(source=SourceType.SLACK, event_ids=["1"], count=1)
        await bus.publish(event)
        await asyncio.sleep(0.05)

        assert len(received_a) == 1
        assert len(received_b) == 1

    @pytest.mark.asyncio
    async def test_type_filtering(self):
        """Subscriber should only receive events of subscribed type."""
        bus = EventBus()
        received = []

        async def handler(event: NewEventsIngested):
            received.append(event)

        await bus.subscribe(NewEventsIngested, handler)

        # Publish different event type
        await bus.publish(FailureRecorded(subsystem="test", failure_type="test", details="test"))
        await asyncio.sleep(0.05)

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_error_isolation(self):
        """A failing subscriber must NOT affect other subscribers."""
        bus = EventBus()
        received = []

        async def failing_handler(event):
            raise ValueError("I'm broken!")

        async def healthy_handler(event):
            received.append(event)

        await bus.subscribe(NewEventsIngested, failing_handler)
        await bus.subscribe(NewEventsIngested, healthy_handler)

        event = NewEventsIngested(source=SourceType.EMAIL, event_ids=["1"], count=1)
        await bus.publish(event)
        await asyncio.sleep(0.05)

        # Healthy handler should still receive the event
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_wildcard_subscriber(self):
        """Wildcard subscriber should receive ALL event types."""
        bus = EventBus()
        received = []

        async def wildcard_handler(event):
            received.append(event)

        await bus.subscribe_all(wildcard_handler)

        await bus.publish(NewEventsIngested(source=SourceType.EMAIL, event_ids=["1"], count=1))
        await bus.publish(FailureRecorded(subsystem="test", failure_type="err", details="d"))
        await asyncio.sleep(0.05)

        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        """Unsubscribed handler should no longer receive events."""
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        await bus.subscribe(NewEventsIngested, handler)
        await bus.publish(NewEventsIngested(source=SourceType.EMAIL, event_ids=["1"], count=1))
        await asyncio.sleep(0.05)
        assert len(received) == 1

        await bus.unsubscribe(NewEventsIngested, handler)
        await bus.publish(NewEventsIngested(source=SourceType.EMAIL, event_ids=["2"], count=1))
        await asyncio.sleep(0.05)
        assert len(received) == 1  # No new events

    @pytest.mark.asyncio
    async def test_no_subscribers_no_error(self):
        """Publishing with no subscribers should not raise."""
        bus = EventBus()
        await bus.publish(NewEventsIngested(source=SourceType.EMAIL, event_ids=["1"], count=1))

    @pytest.mark.asyncio
    async def test_publish_preserves_event_data(self):
        """Event data should arrive intact."""
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        await bus.subscribe(NewEventsIngested, handler)
        event = NewEventsIngested(
            source=SourceType.JIRA,
            event_ids=["JIRA-1", "JIRA-2", "JIRA-3"],
            count=3,
        )
        await bus.publish(event)
        await asyncio.sleep(0.05)

        assert received[0].source == SourceType.JIRA
        assert received[0].event_ids == ["JIRA-1", "JIRA-2", "JIRA-3"]
        assert received[0].count == 3


# =============================================================================
# Supervisor Tests
# =============================================================================


class TestSupervisor:
    """Test subsystem lifecycle management."""

    def _make_subsystem(self, name: str, healthy: bool = True):
        sub = MagicMock()
        sub.name = name
        sub.start = AsyncMock()
        sub.stop = AsyncMock()
        sub.health_check = AsyncMock(
            return_value=HealthStatus.HEALTHY if healthy else HealthStatus.UNHEALTHY
        )
        return sub

    @pytest.mark.asyncio
    async def test_start_all_subsystems(self):
        """All registered subsystems should be started."""
        bus = EventBus()
        supervisor = Supervisor(bus)
        sub_a = self._make_subsystem("a")
        sub_b = self._make_subsystem("b")

        supervisor.register(sub_a)
        supervisor.register(sub_b)
        await supervisor.start_all()
        await asyncio.sleep(0.05)

        sub_a.start.assert_called_once()
        sub_b.start.assert_called_once()
        await supervisor.stop_all()

    @pytest.mark.asyncio
    async def test_stop_all_subsystems(self):
        """All subsystems should be stopped on stop_all."""
        bus = EventBus()
        supervisor = Supervisor(bus)
        sub = self._make_subsystem("test")
        supervisor.register(sub)
        await supervisor.start_all()
        await supervisor.stop_all()

        sub.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_failed_start_publishes_event(self):
        """Failed subsystem start should publish a FailureRecorded event."""
        bus = EventBus()
        failures = []

        async def on_failure(event):
            failures.append(event)

        await bus.subscribe(FailureRecorded, on_failure)

        supervisor = Supervisor(bus)
        sub = self._make_subsystem("broken")
        sub.start = AsyncMock(side_effect=RuntimeError("Boom"))
        supervisor.register(sub)
        await supervisor.start_all()
        await asyncio.sleep(0.05)

        assert len(failures) == 1
        assert failures[0].subsystem == "broken"
        await supervisor.stop_all()

    @pytest.mark.asyncio
    async def test_register_multiple(self):
        """Should handle multiple subsystems."""
        bus = EventBus()
        supervisor = Supervisor(bus)
        for i in range(5):
            supervisor.register(self._make_subsystem(f"sub_{i}"))
        assert len(supervisor._subsystems) == 5


# =============================================================================
# ShutdownCoordinator Tests
# =============================================================================


class TestShutdownCoordinator:
    """Test graceful shutdown sequence."""

    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.coordinator = ShutdownCoordinator(
            checkpoint_dir=tempfile.mkdtemp()
        )

    @pytest.mark.asyncio
    async def test_shutdown_calls_stoppers(self):
        """Shutdown should call stop() on all registered stoppers."""
        stopper = AsyncMock()
        stopper.stop = AsyncMock()
        self.coordinator.register_stopper(stopper)

        await self.coordinator.shutdown()
        stopper.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_drains_llm(self):
        """Shutdown should drain LLM gateway."""
        drainable = AsyncMock()
        drainable.drain = AsyncMock()
        self.coordinator.register_llm_drainable(drainable)

        await self.coordinator.shutdown()
        drainable.drain.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_drains_db(self):
        """Shutdown should drain database writer."""
        drainable = AsyncMock()
        drainable.drain = AsyncMock()
        self.coordinator.register_db_drainable(drainable)

        await self.coordinator.shutdown()
        drainable.drain.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_checkpoints_wal(self):
        """Shutdown should perform WAL checkpoint."""
        target = AsyncMock()
        target.wal_checkpoint = AsyncMock()
        self.coordinator.register_wal_target(target)

        await self.coordinator.shutdown()
        target.wal_checkpoint.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_is_idempotent(self):
        """Calling shutdown twice should only execute once."""
        stopper = AsyncMock()
        stopper.stop = AsyncMock()
        self.coordinator.register_stopper(stopper)

        await self.coordinator.shutdown()
        await self.coordinator.shutdown()
        stopper.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_handles_stopper_error(self):
        """Shutdown should continue even if a stopper fails."""
        failing_stopper = AsyncMock()
        failing_stopper.stop = AsyncMock(side_effect=RuntimeError("Boom"))
        healthy_stopper = AsyncMock()
        healthy_stopper.stop = AsyncMock()

        self.coordinator.register_stopper(failing_stopper)
        self.coordinator.register_stopper(healthy_stopper)

        await self.coordinator.shutdown()
        healthy_stopper.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_checkpoint_save_and_load(self):
        """Checkpoint should save and restore state."""
        checkpointable = MagicMock()
        checkpointable.get_checkpoint_data.return_value = {
            "last_poll": "2026-01-01T00:00:00Z"
        }
        self.coordinator.register_checkpointable(checkpointable)

        await self.coordinator.shutdown()

        loaded = self.coordinator.load_checkpoint()
        assert loaded["last_poll"] == "2026-01-01T00:00:00Z"

    def test_load_checkpoint_missing_file(self):
        """Loading non-existent checkpoint should return empty dict."""
        coord = ShutdownCoordinator(checkpoint_dir=tempfile.mkdtemp())
        assert coord.load_checkpoint() == {}

    @pytest.mark.asyncio
    async def test_llm_drain_timeout(self):
        """LLM drain that exceeds timeout should not block shutdown."""
        slow_drainable = AsyncMock()

        async def slow_drain():
            await asyncio.sleep(100)

        slow_drainable.drain = slow_drain
        self.coordinator.register_llm_drainable(slow_drainable)

        # Should complete in ~5s timeout, not 100s
        await asyncio.wait_for(self.coordinator.shutdown(), timeout=10)
