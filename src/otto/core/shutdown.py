from __future__ import annotations
"""
Graceful shutdown coordinator.

Orchestrates ordered teardown:
  1. Stop polling
  2. Drain in-flight LLM calls (5s timeout)
  3. Drain database writer queue (3s timeout)
  4. WAL checkpoint
  5. Save state checkpoint
  6. Stop event loop
"""

import asyncio
import json
import logging
import signal
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("otto.core.shutdown")


class Stoppable(Protocol):
    """Anything that can be asked to stop accepting new work."""
    async def stop(self) -> None: ...


class Drainable(Protocol):
    """Anything that has in-flight work to drain."""
    async def drain(self) -> None: ...


class Checkpointable(Protocol):
    """Anything that can checkpoint its state for restart recovery."""
    def get_checkpoint_data(self) -> dict[str, Any]: ...


class WALCheckpointable(Protocol):
    """Database that can execute a WAL checkpoint."""
    async def wal_checkpoint(self) -> None: ...


class ShutdownCoordinator:
    """
    Coordinates graceful shutdown across all subsystems.

    Handles SIGTERM and SIGINT. Saves state checkpoint for restart recovery.
    """

    def __init__(
        self,
        checkpoint_dir: Path | str | None = None,
    ) -> None:
        if checkpoint_dir:
            self._checkpoint_path = Path(checkpoint_dir) / "state_checkpoint.json"
        else:
            from otto import paths
            self._checkpoint_path = paths.checkpoint_file()
        self._stoppers: list[Stoppable] = []
        self._llm_drainables: list[Drainable] = []
        self._db_drainables: list[Drainable] = []
        self._wal_targets: list[WALCheckpointable] = []
        self._checkpointables: list[Checkpointable] = []
        self._shutting_down = False

    def register_stopper(self, stopper: Stoppable) -> None:
        """Register something to stop on shutdown (e.g., pollers)."""
        self._stoppers.append(stopper)

    def register_llm_drainable(self, drainable: Drainable) -> None:
        """Register LLM gateway for draining (5s timeout)."""
        self._llm_drainables.append(drainable)

    def register_db_drainable(self, drainable: Drainable) -> None:
        """Register DB writer for draining (3s timeout)."""
        self._db_drainables.append(drainable)

    def register_wal_target(self, target: WALCheckpointable) -> None:
        """Register database for WAL checkpoint."""
        self._wal_targets.append(target)

    def register_checkpointable(self, target: Checkpointable) -> None:
        """Register something that can checkpoint state for restart."""
        self._checkpointables.append(target)

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        """Install SIGTERM and SIGINT handlers on the event loop."""
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self.shutdown(s)))
        logger.info("Signal handlers installed (SIGTERM, SIGINT)")

    async def shutdown(self, sig: signal.Signals | None = None) -> None:
        """
        Execute the graceful shutdown sequence.

        Idempotent — calling twice is safe.
        """
        if self._shutting_down:
            return
        self._shutting_down = True

        sig_name = sig.name if sig else "manual"
        logger.info("Initiating graceful shutdown (signal: %s)...", sig_name)

        # 1. Stop accepting new work
        for stopper in self._stoppers:
            try:
                await stopper.stop()
            except Exception as e:
                logger.error("Error stopping %s: %s", stopper, e)

        # 2. Drain LLM calls (5s timeout)
        for drainable in self._llm_drainables:
            try:
                await asyncio.wait_for(drainable.drain(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("LLM drain timed out after 5s")
            except Exception as e:
                logger.error("Error draining LLM: %s", e)

        # 3. Drain DB writer (3s timeout)
        for drainable in self._db_drainables:
            try:
                await asyncio.wait_for(drainable.drain(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("DB writer drain timed out after 3s")
            except Exception as e:
                logger.error("Error draining DB writer: %s", e)

        # 4. WAL checkpoint
        for target in self._wal_targets:
            try:
                await target.wal_checkpoint()
            except Exception as e:
                logger.error("WAL checkpoint error: %s", e)

        # 5. Save state checkpoint
        self._save_checkpoint()

        logger.info("Shutdown sequence complete.")

    def _save_checkpoint(self) -> None:
        """Save all checkpointable state to disk for restart recovery."""
        state: dict[str, Any] = {}
        for cp in self._checkpointables:
            try:
                data = cp.get_checkpoint_data()
                state.update(data)
            except Exception as e:
                logger.error("Checkpoint data error: %s", e)

        try:
            self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            self._checkpoint_path.write_text(json.dumps(state, indent=2, default=str))
            logger.info("State checkpoint saved to %s", self._checkpoint_path)
        except Exception as e:
            logger.error("Failed to save checkpoint: %s", e)

    def load_checkpoint(self) -> dict[str, Any]:
        """Load state checkpoint from disk. Returns empty dict if none exists."""
        if not self._checkpoint_path.exists():
            return {}
        try:
            data = json.loads(self._checkpoint_path.read_text())
            logger.info("Loaded checkpoint from %s", self._checkpoint_path)
            return data
        except Exception as e:
            logger.error("Failed to load checkpoint: %s", e)
            return {}
