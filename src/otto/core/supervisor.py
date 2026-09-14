from __future__ import annotations
"""
Subsystem supervisor with health monitoring, auto-restart, and memory watchdog.

Manages the lifecycle of all Otto subsystems with exponential backoff
on restart and memory pressure handling.
"""

import asyncio
import logging
import os
from typing import Protocol

from otto.core.event_bus import EventBus
from otto.storage.models import FailureRecorded, HealthStatus

logger = logging.getLogger("otto.core.supervisor")


class Subsystem(Protocol):
    """Protocol that all Otto subsystems must implement."""

    @property
    def name(self) -> str: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def health_check(self) -> HealthStatus: ...


class Supervisor:
    """
    Subsystem watchdog.

    - Health check loop every 60s
    - Auto-restart with exponential backoff (5s, 15s, 60s, 300s)
    - Memory watchdog (warn at 500MB, restart at 800MB)
    - Publishes FailureRecorded events to the event bus
    """

    BACKOFF_SEQUENCE = [5, 15, 60, 300]
    HEALTH_CHECK_INTERVAL = 60
    MEMORY_WARN_MB = 500
    MEMORY_KILL_MB = 800

    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus
        self._subsystems: dict[str, Subsystem] = {}
        self._backoff_index: dict[str, int] = {}
        self._restarting: set[str] = set()
        self._running = False
        self._monitor_task: asyncio.Task[None] | None = None

    def register(self, subsystem: Subsystem) -> None:
        """Register a subsystem for lifecycle management."""
        self._subsystems[subsystem.name] = subsystem
        self._backoff_index[subsystem.name] = 0

    async def start_all(self) -> None:
        """Start all registered subsystems and the monitor loop."""
        self._running = True
        for name, sub in self._subsystems.items():
            logger.info("Starting subsystem: %s", name)
            try:
                await sub.start()
            except Exception as e:
                logger.error("Failed to start subsystem %s: %s", name, e)
                await self.event_bus.publish(
                    FailureRecorded(subsystem=name, failure_type="startup", details=str(e))
                )
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop_all(self) -> None:
        """Stop all subsystems and the monitor loop."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        for name, sub in self._subsystems.items():
            logger.info("Stopping subsystem: %s", name)
            try:
                await sub.stop()
            except Exception as e:
                logger.error("Error stopping subsystem %s: %s", name, e)

    async def _monitor_loop(self) -> None:
        """Periodic health check and memory watchdog."""
        while self._running:
            await asyncio.sleep(self.HEALTH_CHECK_INTERVAL)

            # Memory watchdog
            self._check_memory()

            # Subsystem health checks
            for name, sub in self._subsystems.items():
                try:
                    status = await asyncio.wait_for(sub.health_check(), timeout=30.0)
                    if status == HealthStatus.HEALTHY:
                        self._backoff_index[name] = 0
                    elif status == HealthStatus.DEGRADED:
                        # Degraded is expected (no API keys, some tabs closed).
                        # Log at debug level, do NOT restart.
                        logger.debug(
                            "Subsystem %s is degraded (expected without full config)", name
                        )
                    else:
                        # UNHEALTHY = actual failure, trigger restart
                        await self._handle_failure(name, sub, f"Health: {status.value}")
                except Exception as e:
                    await self._handle_failure(name, sub, f"Health check exception: {e}")

    def _check_memory(self) -> None:
        """Check RSS memory and act on pressure."""
        try:
            import psutil
            rss_mb = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
            if rss_mb > self.MEMORY_KILL_MB:
                logger.critical("Memory %dMB > %dMB. Forcing exit.", int(rss_mb), self.MEMORY_KILL_MB)
                os._exit(1)
            elif rss_mb > self.MEMORY_WARN_MB:
                logger.warning("Memory high: %dMB", int(rss_mb))
        except ImportError:
            pass  # psutil not available, skip memory check

    async def _handle_failure(self, name: str, sub: Subsystem, reason: str) -> None:
        """Handle a subsystem failure with exponential backoff restart."""
        logger.error("Subsystem %s failed: %s", name, reason)
        await self.event_bus.publish(
            FailureRecorded(subsystem=name, failure_type="runtime", details=reason)
        )

        if name in self._restarting:
            logger.debug("Subsystem %s is already scheduled for restart, skipping", name)
            return

        self._restarting.add(name)
        idx = self._backoff_index.get(name, 0)
        delay = self.BACKOFF_SEQUENCE[min(idx, len(self.BACKOFF_SEQUENCE) - 1)]
        self._backoff_index[name] = min(idx + 1, len(self.BACKOFF_SEQUENCE) - 1)

        logger.info("Restarting %s in %ds (backoff level %d)", name, delay, idx)
        asyncio.create_task(self._restart_subsystem(name, sub, delay))

    async def _restart_subsystem(self, name: str, sub: Subsystem, delay: int) -> None:
        """Restart a subsystem after a delay."""
        try:
            await asyncio.sleep(delay)
            if not self._running:
                return
            try:
                await sub.stop()
            except Exception:
                pass
            try:
                await sub.start()
                logger.info("Successfully restarted subsystem: %s", name)
            except Exception as e:
                logger.error("Failed to restart %s: %s", name, e)
                await self.event_bus.publish(
                    FailureRecorded(subsystem=name, failure_type="restart", details=str(e))
                )
        finally:
            self._restarting.discard(name)
