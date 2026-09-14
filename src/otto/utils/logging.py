"""
Logging for Otto.

Design rules:

* Importing an Otto module never creates files. Handlers are attached only
  by :func:`configure_logging`, which the CLI / engine call once.
* Everything goes to one rotating file in the data dir (``otto.log``,
  5 MB × 3) plus, optionally, the console.
* Loggers are plain :mod:`logging` loggers that propagate to the ``otto``
  root, so third-party code and tests can capture them normally.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def get_logger(component: str) -> logging.Logger:
    """Return the ``otto.<component>`` logger (no side effects)."""
    full_name = component if component.startswith("otto.") else f"otto.{component}"
    return logging.getLogger(full_name)


def _configured_level() -> int:
    """``log_level`` / ``debug_mode`` from config.toml; INFO when unset or unreadable."""
    try:
        from otto.config import ConfigManager
        cfg = ConfigManager()
        if bool(cfg.get_or("debug_mode", False)):
            return logging.DEBUG
        level = getattr(logging, str(cfg.get_or("log_level", "INFO")).upper(), None)
        return level if isinstance(level, int) else logging.INFO
    except Exception:
        return logging.INFO


def configure_logging(
    *,
    verbose: bool = False,
    console: bool = True,
    log_file: Path | None = None,
) -> Path | None:
    """
    Attach handlers to the ``otto`` logger tree. Idempotent.

    Returns the log file path when file logging is active.
    """
    global _configured
    root = logging.getLogger("otto")
    level = _configured_level()
    if verbose or os.environ.get("OTTO_DEBUG"):
        level = logging.DEBUG
    root.setLevel(level)

    if _configured:
        return log_file

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    if log_file is None:
        from otto import paths
        log_file = paths.log_file()

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=str(log_file),
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as e:  # pragma: no cover - disk problems
        sys.stderr.write(f"otto: could not open log file {log_file}: {e}\n")
        log_file = None

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    # Quieten chatty third parties.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _configured = True
    return log_file
