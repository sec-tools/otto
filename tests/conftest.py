"""
Shared test fixtures for the Otto test suite.

The most important fixture here is ``_isolate_user_data`` (autouse): every
test runs against a throw-away ``OTTO_DATA_DIR`` so the suite can never
read or clobber the real user's briefings, dismissed/snoozed state,
history, screenshots or API keys.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure the source directory is on the path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Programs that drive the real desktop / system. Tests must never run them:
# they are slow (osascript can block for 45s per call), prompt for
# permissions, or mutate the user's session (launchctl, pkill, open).
_BLOCKED_PROGRAMS = {
    "osascript", "open", "launchctl", "pkill", "pgrep", "screencapture",
    "security", "swift", "swiftc", "sqlite3", "find_window", "lsof", "ax_dump.py",
}

# Scripts that read the real desktop even though the program is just python.
_BLOCKED_SCRIPTS = ("ax_dump.py",)


def _program_name(argv) -> str:
    if isinstance(argv, (str, bytes)):
        parts = argv.split() if argv else []
    else:
        parts = list(argv) if argv else []
    first = parts[0] if parts else ""
    if any(str(p).endswith(_BLOCKED_SCRIPTS) for p in parts[1:3]):
        return os.path.basename(str(parts[1] if len(parts) > 1 else first))
    return os.path.basename(str(first))


class _FakeAsyncProcess:
    returncode = 1
    pid = 0

    async def communicate(self, *_a, **_k):
        return b"", b"blocked by test harness"

    async def wait(self):
        return self.returncode

    def kill(self):
        pass

    terminate = kill


@pytest.fixture(autouse=True)
def _no_system_automation(monkeypatch):
    """Fail fast (like a missing binary) instead of driving the real desktop."""
    real_run = subprocess.run
    real_popen = subprocess.Popen
    real_async_exec = asyncio.create_subprocess_exec

    def guarded_run(args, *a, **kw):
        if _program_name(args) in _BLOCKED_PROGRAMS:
            return subprocess.CompletedProcess(args, 1, "" if kw.get("text") else b"",
                                               "blocked by test harness" if kw.get("text") else b"blocked")
        return real_run(args, *a, **kw)

    def guarded_popen(args, *a, **kw):
        if _program_name(args) in _BLOCKED_PROGRAMS:
            raise FileNotFoundError(f"{_program_name(args)} is blocked in tests")
        return real_popen(args, *a, **kw)

    async def guarded_async_exec(program, *args, **kw):
        if _program_name([program, *args]) in _BLOCKED_PROGRAMS:
            return _FakeAsyncProcess()
        return await real_async_exec(program, *args, **kw)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded_async_exec)


@pytest.fixture(autouse=True)
def _isolate_user_data(tmp_path, monkeypatch):
    """Redirect every filesystem side effect into a per-test temp directory."""
    data_dir = tmp_path / "otto-data"
    data_dir.mkdir()
    monkeypatch.setenv("OTTO_DATA_DIR", str(data_dir))
    monkeypatch.delenv("OTTO_HISTORY_DIR", raising=False)
    monkeypatch.setenv("OTTO_LAUNCH_AGENTS_DIR", str(tmp_path / "LaunchAgents"))
    monkeypatch.setenv("OTTO_HOME", str(Path(__file__).parent.parent))
    # Talk to a port nothing listens on, so CLI tests never reach a live engine.
    monkeypatch.setenv("OTTO_PORT", "7999")
    # Never take real screenshots, spawn servers, or hit external URLs from tests.
    monkeypatch.setenv("OTTO_DISABLE_SCREENSHOTS", "1")
    monkeypatch.setenv("OTTO_LINK_FETCH", "0")
    # Keys: never pick up the developer's real keys (env or Keychain).
    for var in ("OTTO_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "DEVIN_API_KEY",
                "SLACK_TOKEN", "SLACK_USER_TOKEN", "SLACK_BOT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OTTO_DISABLE_KEYCHAIN", "1")
    # Slack workspace info comes from a per-test path (never the real Slack state).
    monkeypatch.setenv("OTTO_SLACK_STATE_PATH", str(tmp_path / "slack-root-state.json"))
    # Native helpers: tests must behave the same whether or not the developer
    # has run build_menubar.sh, so pretend the binaries are not built — and
    # never dump the developer's real Slack window via the Accessibility reader.
    import otto.paths as _paths
    monkeypatch.setattr(_paths, "find_window_binary", lambda: tmp_path / "bin" / "find_window")
    monkeypatch.setenv("OTTO_NATIVE_AX", "0")
    # Reset process-wide caches that would otherwise leak between tests.
    import otto.adapters.browser.slack_browser as _slack
    import otto.intelligence.link_intelligence as _li
    monkeypatch.setattr(_slack, "_WORKSPACE_INFO", None)
    monkeypatch.setattr(_li, "_MEMORY_CACHE", None)
    _slack.clear_slack_channel_cache()
    import otto.web.collect as _collect
    _collect.reset_source_backoff()
    _collect.reset_api_adapters()
    _collect.reset_screenshot_state()
    import otto.adapters.browser.calendar_native as _cal
    _cal.reset_cache()
    import otto.utils.identity as _identity
    _identity._reset_for_tests()
    yield data_dir


@pytest.fixture
def tmp_dir():
    """Provide a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
