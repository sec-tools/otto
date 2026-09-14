"""
The Otto engine: one long-lived process that keeps every interface fresh.

    ┌─────────────────────────── OttoEngine ────────────────────────────┐
    │  HTTP server (127.0.0.1:7077)  ◀──  web UI · menu bar · CLI       │
    │  refresh loop (every 60 s)     ──▶  GLOBAL_DATA                    │
    │  notifier                      ──▶  /api/notifications · osascript │
    │  housekeeping                  ──▶  screenshots · caches · logs    │
    └────────────────────────────────────────────────────────────────────┘

Antifragile by construction:

* A refresh that throws never kills the loop; the failure is recorded in
  ``/api/status`` (``last_error``) and retried with exponential back-off.
* Adapters and LLM calls are time-boxed inside :mod:`otto.web.collect`.
* Consecutive failures widen the interval (60 s → 120 s → … ≤ 10 min) and a
  single success snaps it back.
* Lightweight ``/api/refresh`` requests are coalesced — never two refreshes
  at once.
* The system-sleep case: after wake, the loop notices the long gap and
  refreshes immediately instead of waiting a full interval.
* launchd (``otto install``) restarts the process if it ever dies.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Any, Optional

from otto import paths
from otto.utils.logging import configure_logging

logger = logging.getLogger("otto.core.engine")

MIN_INTERVAL = 15
MAX_BACKOFF = 600
# A refresh is bounded at 180s (collect.REFRESH_HARD_TIMEOUT); one that is
# still "running" long after that is wedged in a blocking call.
STUCK_REFRESH_SECONDS = 600


class OttoEngine:
    def __init__(
        self,
        *,
        port: int = paths.DEFAULT_PORT,
        refresh_seconds: int = 60,
        notifications: bool = True,
        collector: Any | None = None,
    ) -> None:
        from otto.web.server import GLOBAL_DATA

        self.port = port
        self.refresh_seconds = max(MIN_INTERVAL, int(refresh_seconds))
        self.data = GLOBAL_DATA
        self._collector = collector  # callable returning briefing dict (tests inject a fake)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._server = None
        self._server_thread: Optional[threading.Thread] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._consecutive_failures = 0
        self._force_next = False
        self._llm = None
        self._cache = None
        self.notifier = None
        if notifications:
            try:
                from otto.core.notify import Notifier
                self.notifier = Notifier()
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Notifications disabled: %s", e)
        self.data.refresh_hook = self.request_refresh

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        from otto.web.server import make_server

        self._write_pid()
        self._server = make_server(self.port, notifier=self.notifier)
        self._server_thread = threading.Thread(target=self._server.serve_forever, name="otto-http", daemon=True)
        self._server_thread.start()
        logger.info("Otto engine listening on http://localhost:%d (refresh every %ds)", self.port, self.refresh_seconds)

        self._loop_thread = threading.Thread(target=self._run_loop, name="otto-refresh-loop", daemon=True)
        self._loop_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=5)
        self._remove_pid()
        logger.info("Otto engine stopped")

    def run_forever(self) -> int:
        """Start, then block until SIGTERM/SIGINT. Returns exit code."""
        try:
            self.start()
        except OSError as e:
            logger.error("Cannot bind port %d: %s (is another Otto running?)", self.port, e)
            return 2

        def _handle(signum: int, _frame: Any) -> None:
            logger.info("Received %s", signal.Signals(signum).name)
            self._stop.set()
            self._wake.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _handle)
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
                if self._stuck():
                    # A wedged refresh thread cannot be killed from Python; the
                    # honest recovery is a clean exit — launchd's KeepAlive
                    # restarts the engine within seconds, with all state on disk.
                    logger.critical(
                        "Refresh has been running for %.0fs (limit %ds) — restarting the engine",
                        self.data.refreshing_for(), STUCK_REFRESH_SECONDS,
                    )
                    self._remove_pid()
                    os._exit(3)
        finally:
            self.stop()
        return 0

    def _stuck(self) -> bool:
        try:
            return self.data.refreshing_for() > STUCK_REFRESH_SECONDS
        except Exception:
            return False

    # -- refresh --------------------------------------------------------------

    def request_refresh(self) -> None:
        """Non-blocking: wake the loop so it refreshes as soon as possible.

        A requested refresh is a *forced* one: sources sitting in back-off
        (e.g. waiting for a permission grant) are retried immediately.
        """
        self._force_next = True
        self._wake.set()

    def refresh_once(self) -> bool:
        """Run one refresh synchronously. Returns True on success."""
        if not self.data.begin_refresh():
            return False
        started = time.monotonic()
        error = ""
        try:
            data = self._collect()
            self.data.update(data)
            self._consecutive_failures = 0
            self._after_refresh(data)
            return True
        except Exception as e:
            self._consecutive_failures += 1
            error = f"{type(e).__name__}: {e}"
            logger.exception("Refresh failed (%d in a row)", self._consecutive_failures)
            return False
        finally:
            self.data.end_refresh(error=error, duration=time.monotonic() - started)

    def _collect(self) -> dict:
        force, self._force_next = self._force_next, False
        if self._collector is not None:
            return self._collector()
        from otto.intelligence.classification_cache import ClassificationCache
        from otto.web.collect import build_llm_gateway, collect_briefing_data

        if self._llm is None:
            self._llm = build_llm_gateway()
        if self._cache is None:
            self._cache = ClassificationCache()
        return collect_briefing_data(llm=self._llm, classification_cache=self._cache, force=force)

    def _after_refresh(self, data: dict) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.process_briefing(data)
            self.notifier.process_radar(data)
            self.notifier.drain_quiet_queue()
            self.notifier.deliver_fallbacks()
        except Exception as e:
            logger.warning("Notification step failed: %s", e)

    def _current_interval(self) -> float:
        if self._consecutive_failures == 0:
            return float(self.refresh_seconds)
        return float(min(MAX_BACKOFF, self.refresh_seconds * (2 ** min(self._consecutive_failures, 4))))

    def _run_loop(self) -> None:
        # First refresh immediately so every interface has data within seconds of launch.
        self.reload_config()
        self.refresh_once()
        # Wall clock on purpose: on macOS the monotonic clock pauses during
        # system sleep, so it cannot see the gap we want to detect here.
        last = time.time()
        while not self._stop.is_set():
            interval = self._current_interval()
            self._wake.wait(timeout=interval)
            if self._stop.is_set():
                break
            self._wake.clear()
            now = time.time()
            if now - last > interval * 3:
                logger.info("Long gap detected (%.0fs) — likely system sleep; refreshing on wake", now - last)
                self._consecutive_failures = 0  # a stale back-off is meaningless after a sleep
            self.reload_config()
            self.refresh_once()
            last = time.time()

    # -- config -----------------------------------------------------------------

    _config_seen: tuple = ()
    _screens_off_by_config = False

    def reload_config(self) -> None:
        """Pick up an edited ``config.toml`` without a restart.

        Most settings are read fresh by the code that uses them; the two the
        engine holds — the refresh interval and whether thumbnails are taken —
        are re-applied here whenever the file changes. The port cannot move
        while the server is bound: that one is logged as needing a restart.
        """
        from otto import paths
        from otto.config import ConfigManager, ensure_config_file

        path = paths.config_file()
        if not self._config_seen and not path.exists():
            # First start on this machine: write the file (every setting, with
            # its explanation) so "Edit Config…" always opens something real.
            try:
                ensure_config_file(path)
            except OSError as e:
                logger.debug("config.toml not written: %s", e)
        try:
            st = path.stat()
            seen: tuple = (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            seen = (str(path), None)
        if seen == self._config_seen:
            return
        first = not self._config_seen
        self._config_seen = seen
        try:
            cfg = ConfigManager(path)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("config.toml could not be read: %s", e)
            return
        if not cfg.parsed:
            logger.warning("config.toml: %s — keeping the previous values", cfg.file_error)
            return
        if cfg.file_error:
            logger.warning("config.toml: %s — the default stands for that one", cfg.file_error)
        changes: list[str] = []
        if not os.environ.get("OTTO_REFRESH_SECONDS"):
            want = max(MIN_INTERVAL, int(cfg.get_or("engine.refresh_seconds", self.refresh_seconds)))
            if want != self.refresh_seconds:
                changes.append(f"refresh every {want}s")
                self.refresh_seconds = want
        shots_on = bool(cfg.get_or("engine.screenshots", True))
        if not shots_on and os.environ.get("OTTO_DISABLE_SCREENSHOTS") != "1":
            os.environ["OTTO_DISABLE_SCREENSHOTS"] = "1"
            self._screens_off_by_config = True
            changes.append("thumbnails off")
        elif shots_on and self._screens_off_by_config:
            os.environ.pop("OTTO_DISABLE_SCREENSHOTS", None)
            self._screens_off_by_config = False
            changes.append("thumbnails on")
        port = int(cfg.get_or("engine.port", self.port))
        if port != self.port and not os.environ.get("OTTO_PORT"):
            changes.append(f"port {port} after `otto restart`")
        # [keys] may have changed: model providers follow the key store; the
        # Slack API reader is picked from it on every refresh anyway.
        if not first and self._llm is not None and hasattr(self._llm, "sync_discovered_keys"):
            try:
                added, removed = self._llm.sync_discovered_keys()
                if added:
                    changes.append(f"{added} model key{'s' if added != 1 else ''} added")
                if removed:
                    changes.append(f"{removed} model key{'s' if removed != 1 else ''} removed")
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("key sync skipped: %s", e)
        if changes and not first:
            logger.info("config.toml changed: %s", ", ".join(changes))
        for key in cfg.unknown_keys[:3]:
            logger.warning("config.toml: unknown setting %r is ignored", key)

    # -- pid file -------------------------------------------------------------

    def _write_pid(self) -> None:
        try:
            pid_file = paths.pid_file()
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(os.getpid()))
            # Where we are listening, for the CLI and the menu bar app (the
            # port is a config.toml setting, so neither can assume 7077).
            paths.engine_info_file().write_text(json.dumps({"pid": os.getpid(), "port": self.port, "started": time.time()}))
        except OSError as e:
            logger.debug("Could not write pid file: %s", e)

    def _remove_pid(self) -> None:
        try:
            pid_file = paths.pid_file()
            if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
                pid_file.unlink()
                info = paths.engine_info_file()
                if info.exists():
                    info.unlink()
        except OSError:
            pass


def engine_from_config() -> OttoEngine:
    from otto.config import ConfigManager

    cfg = ConfigManager()
    port = int(os.environ.get("OTTO_PORT") or cfg.get_or("engine.port", paths.DEFAULT_PORT))
    refresh = int(os.environ.get("OTTO_REFRESH_SECONDS") or cfg.get_or("engine.refresh_seconds", 60))
    # `engine.screenshots` is applied (and re-applied on edits) by reload_config().
    return OttoEngine(port=port, refresh_seconds=refresh)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="otto-engine", description="Run the Otto engine in the foreground.")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--refresh", type=int, default=None, help="refresh interval in seconds")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--no-console-log", action="store_true", help="log only to the rotating file (launchd)")
    args = parser.parse_args(argv)

    configure_logging(verbose=args.verbose, console=not args.no_console_log)
    engine = engine_from_config()
    if args.port:
        engine.port = args.port
    if args.refresh:
        engine.refresh_seconds = max(MIN_INTERVAL, args.refresh)
    return engine.run_forever()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
