"""
Filesystem locations for Otto — the single source of truth.

Every module that reads or writes user data goes through this module so
that:

* the whole app can be relocated with one environment variable
  (``OTTO_DATA_DIR``), which is also how the test-suite isolates itself
  from the real user's data;
* nothing in the codebase hardcodes an absolute home directory or assumes
  the project lives in ``~/projects/Otto``.

All helpers are *functions*, not module constants, so that changes to the
environment (tests, launchd) are honoured at call time.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Otto"
LAUNCH_AGENT_LABEL = "com.otto.engine"
MENUBAR_AGENT_LABEL = "com.otto.menubar"
DEFAULT_PORT = 7077

_LEGACY_CONFIG_DIR = Path.home() / ".otto"


# ---------------------------------------------------------------------------
# Project / install locations
# ---------------------------------------------------------------------------

def project_root() -> Path:
    """Root of the Otto checkout (the directory containing ``src/`` and ``bin/``).

    Resolution order: ``$OTTO_HOME`` → location of this package.
    """
    env = os.environ.get("OTTO_HOME")
    if env:
        return Path(env).expanduser()
    # src/otto/paths.py → parents[0]=otto, [1]=src, [2]=project root
    return Path(__file__).resolve().parents[2]


def python_executable() -> Path:
    """Interpreter to use when spawning Otto processes (launchd, menu bar)."""
    return Path(os.environ.get("OTTO_PYTHON") or sys.executable)


def menubar_app() -> Path:
    return project_root() / "bin" / "Otto.app"


def menubar_binary() -> Path:
    return menubar_app() / "Contents" / "MacOS" / "OttoMenuBar"


def find_window_binary() -> Path:
    return project_root() / "bin" / "find_window"


# ---------------------------------------------------------------------------
# User data
# ---------------------------------------------------------------------------

def data_dir() -> Path:
    """``$OTTO_DATA_DIR`` or ``~/Library/Application Support/Otto``."""
    env = os.environ.get("OTTO_DATA_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Application Support" / APP_NAME


def ensure_data_dir() -> Path:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_file() -> Path:
    return data_dir() / "otto.log"


def pid_file() -> Path:
    return data_dir() / "otto.pid"


def engine_info_file() -> Path:
    """``engine.json`` — ``{"pid", "port"}`` of the running engine, so the CLI and the
    menu bar app find it wherever ``[engine] port`` in config.toml put it."""
    return data_dir() / "engine.json"


def briefings_dir() -> Path:
    return data_dir() / "briefings"


def history_dir() -> Path:
    """Conversation history. Honours the legacy ``OTTO_HISTORY_DIR`` override."""
    env = os.environ.get("OTTO_HISTORY_DIR")
    if env:
        return Path(env).expanduser()
    return data_dir() / "history"


def screenshots_dir() -> Path:
    return data_dir() / "screenshots"


def dismissed_file() -> Path:
    return data_dir() / "dismissed.json"


def snoozed_file() -> Path:
    return data_dir() / "snoozed.json"


def notified_file() -> Path:
    return data_dir() / "notified.json"


def checkpoint_file() -> Path:
    return data_dir() / "state_checkpoint.json"


def link_cache_file() -> Path:
    return data_dir() / "link_cache.json"


def classification_cache_file() -> Path:
    return data_dir() / "classification_cache.json"


def synthesis_cache_file() -> Path:
    """The last few briefing-level digests, keyed by what was on the briefing."""
    return data_dir() / "synthesis_cache.json"


def knowledge_db() -> Path:
    """Otto's memory: every message it has read, kept locally (SQLite, mode 0600)."""
    return data_dir() / "knowledge.db"


def config_file() -> Path:
    """``config.toml`` — new location in the data dir, legacy ``~/.otto``."""
    new = data_dir() / "config.toml"
    if new.exists():
        return new
    legacy = _LEGACY_CONFIG_DIR / "config.toml"
    if legacy.exists():
        return legacy
    return new


def key_file() -> Path:
    """Primary API key file (mode 0600) inside the data dir."""
    return data_dir() / "api.key"


def legacy_key_files() -> list[Path]:
    """Older key locations that are still read (with a warning) for migration."""
    return [
        _LEGACY_CONFIG_DIR / "api.key",
        project_root() / "api.key",
    ]


# ---------------------------------------------------------------------------
# launchd
# ---------------------------------------------------------------------------

def launch_agents_dir() -> Path:
    env = os.environ.get("OTTO_LAUNCH_AGENTS_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "LaunchAgents"


def launch_agent_plist() -> Path:
    return launch_agents_dir() / f"{LAUNCH_AGENT_LABEL}.plist"


def menubar_agent_plist() -> Path:
    return launch_agents_dir() / f"{MENUBAR_AGENT_LABEL}.plist"
