"""
Otto backend process entry point.

Sets up the asyncio event loop and initializes all core subsystems:
EventBus, Supervisor, ShutdownCoordinator, and placeholder subsystem stubs
that will be replaced with real implementations.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from otto.adapters.base import AdapterManager
from otto.adapters.browser.factory import AdapterFactory
from otto.adapters.browser.reader import BrowserContentReader
from otto.core.event_bus import EventBus
from otto.core.shutdown import ShutdownCoordinator
from otto.core.supervisor import Supervisor
from otto.intelligence.ingestion import IngestionPipeline
from otto.llm.gateway import LLMGateway

logger = logging.getLogger("otto.core.backend")


class OttoBackend:
    """
    Backend process for Otto.

    Manages the lifecycle of all subsystems:
    - Database writer
    - Source adapters (Gmail, Slack, Jira, Calendar)
    - Intelligence engine
    - LLM gateway
    - Briefing engine

    In the two-process architecture, this is the headless backend.
    The UI process communicates via Unix socket JSON-RPC.
    """

    def __init__(self) -> None:
        self.event_bus = EventBus()
        self.supervisor = Supervisor(self.event_bus)
        self.shutdown_coordinator = ShutdownCoordinator()
        self.llm_gateway = LLMGateway()
        self.reader = BrowserContentReader()
        self.adapter_factory = AdapterFactory(reader=self.reader)
        self.ingestion_pipeline = IngestionPipeline(event_bus=self.event_bus)
        self.adapter_manager = AdapterManager(pipeline=self.ingestion_pipeline)

    def _load_api_keys(self) -> None:
        """Configure LLM providers from every discoverable key (env → Keychain → data dir)."""
        if not self.llm_gateway.setup_from_discovered_keys():
            logger.info("No API keys found — using heuristic-only mode")

    def setup(self) -> None:
        """Register all subsystems with supervisor and shutdown coordinator."""
        # 1. Load API keys and register LLM Gateway
        self._load_api_keys()
        self.supervisor.register(self.llm_gateway)
        self.shutdown_coordinator.register_llm_drainable(self.llm_gateway)

        # 2. Discover and register adapters (API with browser fallback)
        try:
            adapters = self.adapter_factory.create_all_adapters()
            for adapter in adapters:
                self.adapter_manager.register(adapter)
                logger.info("Registered adapter: %s (%s)", adapter.name, getattr(adapter, "mode", "api"))
        except Exception as e:
            logger.warning("Could not auto-create all adapters: %s", e)

        # 3. Register AdapterManager lifecycle
        self.supervisor.register(self.adapter_manager)
        self.shutdown_coordinator.register_stopper(self.adapter_manager)
        self.shutdown_coordinator.register_checkpointable(self.adapter_manager)

    async def _run(self) -> None:
        """Main async entry point."""
        logger.info("Initializing Otto Backend v0.1.0...")

        self.setup()

        # Install signal handlers
        loop = asyncio.get_running_loop()
        self._shutdown_event = asyncio.Event()

        # Patch signal handlers to also set our shutdown event
        original_shutdown = self.shutdown_coordinator.shutdown

        async def _shutdown_and_signal(sig=None):
            await original_shutdown(sig)
            self._shutdown_event.set()

        self.shutdown_coordinator.shutdown = _shutdown_and_signal
        self.shutdown_coordinator.install_signal_handlers(loop)

        # Load checkpoint from previous run
        checkpoint = self.shutdown_coordinator.load_checkpoint()
        if checkpoint:
            logger.info("Resuming from checkpoint: %s", list(checkpoint.keys()))
            self.adapter_manager.load_checkpoint_data(checkpoint)

        # Start all subsystems
        await self.supervisor.start_all()
        logger.info("Otto Backend is running.")

        # Keep alive until shutdown signal
        try:
            await self._shutdown_event.wait()
            logger.info("Shutdown event received, exiting main loop.")
        except asyncio.CancelledError:
            logger.info("Backend main task cancelled.")
        finally:
            await self.supervisor.stop_all()

    def run(self) -> None:
        """Start the backend event loop (blocking)."""
        try:
            asyncio.run(self._run())
        except KeyboardInterrupt:
            logger.info("Interrupted by user.")
        except Exception as e:
            logger.critical("Backend crashed: %s", e, exc_info=True)
            sys.exit(1)


def main() -> None:
    """Direct invocation entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    backend = OttoBackend()
    backend.run()


if __name__ == "__main__":
    main()
