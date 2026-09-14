"""
Native (EventKit) calendar adapter — hermetic.

The adapter used to call ``subprocess.run(osascript …)`` synchronously from
inside the async poll, which froze the collector's event loop for seconds
every refresh and inflated every other source's timing. These tests pin the
async behaviour, the never-launch-Calendar.app rule and the read cache.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from otto.adapters.browser import calendar_native as cn
from otto.storage.models import ConnectionState, HealthStatus, SourceType

EVENTKIT_LINES = (
    "Work ||| Design review ||| 2026-09-11 14:30:00 +0000 ||| 2026-09-11 15:00:00 +0000 ||| Room 4 |||  ||| Alice, Bob\n"
    "Birthdays ||| Carol's birthday ||| 2026-09-11 00:00:00 +0000 ||| 2026-09-12 00:00:00 +0000 |||  |||  ||| \n"
    "Personal ||| Dentist ||| 2026-09-11 17:00:00 +0000 ||| 2026-09-11 17:30:00 +0000 |||  ||| bring card ||| "
)


class FakeProc:
    def __init__(self, rc: int, out: str = "", err: str = "", delay: float = 0.0):
        self.returncode_after = rc
        self.returncode = None
        self._out, self._err, self._delay = out, err, delay
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(self._delay)
        self.returncode = self.returncode_after
        return self._out.encode(), self._err.encode()

    async def wait(self):
        if self.killed:                      # a killed child is collected at once
            return self.returncode
        await asyncio.sleep(self._delay)
        self.returncode = self.returncode_after
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.fixture
def world(monkeypatch):
    """Route the adapter's subprocesses to a scripted fake."""
    calls: list[list[str]] = []
    script = {"eventkit": FakeProc(0, EVENTKIT_LINES), "calendar_app": FakeProc(0, ""), "pgrep": FakeProc(1)}

    async def fake_exec(program, *args, **kw):
        argv = [program, *args]
        calls.append(argv)
        if program == "pgrep":
            return script["pgrep"]
        body = args[1] if len(args) > 1 else ""
        return script["eventkit"] if "EventKit" in body else script["calendar_app"]

    monkeypatch.setattr(cn.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(cn.shutil, "which", lambda name: "/usr/bin/osascript")
    cn.reset_cache()
    return calls, script


def _run(coro):
    return asyncio.run(coro)


class TestNativeCalendar:
    def test_connect_sends_no_apple_event(self, world):
        calls, _ = world
        a = cn.NativeCalendarAdapter()
        st = _run(a.connect())
        assert st.state == ConnectionState.HEALTHY and calls == []
        assert _run(a.health_check()) == HealthStatus.HEALTHY

    def test_poll_parses_eventkit_and_skips_birthdays(self, world):
        calls, _ = world
        a = cn.NativeCalendarAdapter()
        _run(a.connect())
        events = _run(a.poll(datetime.now(timezone.utc)))
        assert [e.title for e in events] == ["Design review", "Dentist"]
        assert events[0].source == SourceType.CALENDAR and "Room 4" in events[0].plain_text and "Alice, Bob" in events[0].plain_text
        assert [c[0] for c in calls] == ["osascript"]           # EventKit only; no pgrep, no Calendar.app
        assert "eventkit" in a.timings

    def test_poll_never_blocks_the_event_loop(self, world):
        """Other coroutines keep running while osascript works."""
        _calls, script = world
        script["eventkit"] = FakeProc(0, EVENTKIT_LINES, delay=0.3)
        a = cn.NativeCalendarAdapter()
        _run(a.connect())
        ticks = []

        async def heartbeat():
            for _ in range(6):
                await asyncio.sleep(0.05)
                ticks.append(time.monotonic())

        async def both():
            return await asyncio.gather(a.poll(datetime.now(timezone.utc)), heartbeat())

        events, _ = _run(both())
        assert len(events) == 2 and len(ticks) == 6

    def test_read_is_cached_across_adapter_instances(self, world):
        calls, _ = world
        for _ in range(3):
            a = cn.NativeCalendarAdapter()
            _run(a.connect())
            assert len(_run(a.poll(datetime.now(timezone.utc)))) == 2
        assert len(calls) == 1                                    # one EventKit read serves several refreshes
        cn._CACHE["at"] -= cn._CACHE_TTL_S + 1
        a = cn.NativeCalendarAdapter()
        _run(a.connect())
        _run(a.poll(datetime.now(timezone.utc)))
        assert len(calls) == 2

    def test_fallback_never_launches_calendar_app(self, world):
        calls, script = world
        script["eventkit"] = FakeProc(1, "", "execution error: Not authorized to send Apple events (-1743)")
        a = cn.NativeCalendarAdapter()
        _run(a.connect())
        assert _run(a.poll(datetime.now(timezone.utc))) == []
        assert [c[0] for c in calls] == ["osascript", "pgrep"]  # checked whether Calendar.app runs — it does not → no script
        assert a.last_error.startswith("needs Calendar access")
        # Calendar.app running → the AppleScript fallback is allowed.
        script["pgrep"] = FakeProc(0)
        script["calendar_app"] = FakeProc(0, "Work ||| Standup ||| Friday, September 11, 2026 at 9:00:00 AM ||| Friday, September 11, 2026 at 9:15:00 AM |||  |||  ||| ")
        cn.reset_cache()
        b = cn.NativeCalendarAdapter()
        _run(b.connect())
        (ev,) = _run(b.poll(datetime.now(timezone.utc)))
        assert ev.title == "Standup" and "09:00 AM – 09:15 AM" in ev.plain_text
        assert b.last_error == ""

    def test_timeout_is_bounded_and_reaped(self, world, monkeypatch):
        _calls, script = world
        monkeypatch.setattr(cn, "_CALENDAR_TIMEOUT", 0.05)
        slow = FakeProc(0, EVENTKIT_LINES, delay=5.0)
        script["eventkit"] = slow
        a = cn.NativeCalendarAdapter()
        _run(a.connect())
        t0 = time.monotonic()
        assert _run(a.poll(datetime.now(timezone.utc))) == []
        assert time.monotonic() - t0 < 2.0
        assert slow.killed and a.last_error == "timed out"
        assert cn._CACHE["raw"] is None                          # a failure is not cached as "no events"

    def test_empty_day_is_cached_too(self, world):
        calls, script = world
        script["eventkit"] = FakeProc(0, "")
        for _ in range(2):
            a = cn.NativeCalendarAdapter()
            _run(a.connect())
            assert _run(a.poll(datetime.now(timezone.utc))) == []
        assert len(calls) == 1
