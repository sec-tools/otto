"""Unit tests for the engine building blocks: security policy, classification
cache, notifier, launchd plist, and the engine refresh loop."""
from __future__ import annotations

import json
import plistlib
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone

import pytest

from otto import paths
from otto.core import launchd
from otto.core.engine import OttoEngine
from otto.core.notify import Notifier
from otto.intelligence.classification_cache import ClassificationCache, content_key
from otto.ui.notifications import NotificationPolicy
from otto.web import state
from otto.web.security import csp_header, host_allowed, safe_url, same_origin_ok


# ---------------------------------------------------------------------------
# Security policy
# ---------------------------------------------------------------------------

class TestSecurityPolicy:
    @pytest.mark.parametrize("host,ok", [
        ("localhost:7077", True), ("127.0.0.1:7077", True), ("[::1]:7077", True), ("localhost", True),
        ("evil.com", False), ("evil.com:7077", False), ("localhost:7078", False), ("", False), (None, False),
        ("127.0.0.1.evil.com:7077", False), ("[::1", False),
    ])
    def test_host_allowed(self, host, ok):
        assert host_allowed(host, 7077) is ok

    def test_same_origin_ok(self):
        assert same_origin_ok({"Origin": "http://localhost:7077"}, 7077)
        assert same_origin_ok({"Origin": "http://127.0.0.1:7077"}, 7077)
        assert same_origin_ok({"Referer": "http://localhost:7077/briefing"}, 7077)
        assert same_origin_ok({}, 7077)  # native client
        assert not same_origin_ok({"Origin": "https://localhost:7077"}, 7077)   # wrong scheme
        assert not same_origin_ok({"Origin": "http://localhost:7078"}, 7077)
        assert not same_origin_ok({"Origin": "http://evil.com"}, 7077)
        assert not same_origin_ok({"Origin": "null"}, 7077)
        assert not same_origin_ok({"Sec-Fetch-Site": "cross-site"}, 7077)
        assert not same_origin_ok({"Sec-Fetch-Site": "same-site"}, 7077)
        assert not same_origin_ok({"Referer": "http://evil.com/"}, 7077)

    def test_safe_url(self):
        assert safe_url("https://example.com/x?y=1") == "https://example.com/x?y=1"
        assert safe_url("slack://channel?team=T&id=C") == "slack://channel?team=T&id=C"
        assert safe_url("ical://x") == "ical://x"
        for bad in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "vbscript:x", "", None,
                    "https://a.b/c\nSet-Cookie: x", 42):
            assert safe_url(bad) == "", bad

    def test_csp(self):
        csp = csp_header("abc")
        assert "script-src 'nonce-abc'" in csp
        assert "'unsafe-inline'" not in csp
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp


# ---------------------------------------------------------------------------
# Classification cache
# ---------------------------------------------------------------------------

class TestClassificationCache:
    def test_roundtrip_and_persistence(self):
        c = ClassificationCache()
        key = content_key("slack", "t1", "hello world")
        assert c.get(key) is None
        c.put(key, {"urgency": 0.9, "summary": "S", "domain": "work", "ignored": 1, "llm_enriched": True})
        assert c.get(key) == {"urgency": 0.9, "summary": "S", "domain": "work", "llm_enriched": True}
        assert paths.classification_cache_file().exists()
        # New instance reads from disk
        c2 = ClassificationCache()
        assert c2.get(key)["summary"] == "S"
        assert c2.stats()["entries"] == 1

    def test_key_changes_with_content_and_directives(self):
        a = content_key("slack", "t", "msg one")
        assert a == content_key("slack", "t", "msg one")
        assert a != content_key("slack", "t", "msg two")
        assert a != content_key("slack", "t", "msg one", "directives-v2")
        assert a != content_key("email", "t", "msg one")

    def test_ttl_and_cap(self):
        c = ClassificationCache(ttl_seconds=0.01, max_entries=2)
        c.put("a", {"urgency": 1})
        time.sleep(0.02)
        assert c.get("a") is None
        c = ClassificationCache(max_entries=2)
        for i in range(5):
            c.put(f"k{i}", {"urgency": i})
        assert c.stats()["entries"] <= 2

    def test_corrupt_file_is_ignored(self):
        paths.classification_cache_file().parent.mkdir(parents=True, exist_ok=True)
        paths.classification_cache_file().write_text("{corrupt")
        c = ClassificationCache()
        assert c.get("x") is None
        c.put("x", {"urgency": 0.5})
        assert json.loads(paths.classification_cache_file().read_text())["x"]["urgency"] == 0.5

    def test_apply_and_snapshot(self):
        from otto.storage.models import Conversation, Domain, SourceType
        conv = Conversation(source=SourceType.SLACK, account_id="a", thread_id="t", subject="s", summary="",
                            domain=Domain.UNKNOWN, relevance_explanation="")
        conv.urgency = 0.8
        conv.topics = ["x"]
        conv.domain = Domain.WORK
        snap = ClassificationCache.snapshot(conv)
        assert snap["urgency"] == 0.8 and snap["domain"] == "work" and snap["topics"] == ["x"]
        conv2 = Conversation(source=SourceType.SLACK, account_id="a", thread_id="t", subject="s", summary="",
                             domain=Domain.UNKNOWN, relevance_explanation="")
        ClassificationCache().apply(conv2, snap)
        assert conv2.urgency == 0.8 and conv2.domain == Domain.WORK


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------

def _briefing(*items):
    return {"sections": [{"source": "slack", "channels": [{"name": "#eng", "items": list(items)}]}]}


def _important(id_, text="Prod is down, need a decision on rollback", **kw):
    d = {"id": id_, "text": text, "urgency": "high", "urgency_score": 0.85, "sender": "Alice",
         "action_items": ["Decide"], "source_url": "https://acme.slack.com/archives/C/p1"}
    d.update(kw)
    return d


class TestNotifier:
    def _notifier(self, **policy):
        pol = NotificationPolicy(quiet_hours_start=dtime(23, 0), quiet_hours_end=dtime(7, 0), max_per_hour=5, **policy)
        return Notifier(pol, fallback_osascript=False)

    def test_one_banner_for_new_important_items(self):
        n = self._notifier()
        noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        assert n.process_briefing(_briefing(_important("a"), _important("b"), {"id": "c", "urgency": "medium"}), noon) == 1
        pending = n.pending()
        assert len(pending) == 1
        assert "(+1 more)" in pending[0]["body"]
        assert pending[0]["title"].startswith("Otto · #eng")
        assert pending[0]["url"] == "https://acme.slack.com/archives/C/p1"
        # Same items again → nothing new
        assert n.process_briefing(_briefing(_important("a"), _important("b")), noon) == 0
        assert state.load_notified().keys() >= {"a", "b"}

    def test_dismissed_items_never_notify(self):
        n = self._notifier()
        state.dismiss(["a"])
        assert n.process_briefing(_briefing(_important("a")), datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)) == 0

    def test_a_message_read_twice_is_one_banner_under_every_id(self):
        """Screen copy + API copy = one item with two ids: banner once, remembered under both,
        and a dismissal of the other copy silences it."""
        n = self._notifier()
        noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        assert n.process_briefing(_briefing(_important("screen1", ids=["screen1", "api1"])), noon) == 1
        assert state.load_notified().keys() >= {"screen1", "api1"}
        # Next refresh only the API copy is read: already notified.
        assert n.process_briefing(_briefing(_important("api1")), noon) == 0
        state.dismiss(["api2"])
        assert n.process_briefing(_briefing(_important("screen2", ids=["screen2", "api2"])), noon) == 0

    def test_banner_carries_a_picture_of_the_source_when_there_is_one(self, monkeypatch):
        """The menu bar app attaches ``image`` to the banner; only a real file under screenshots/ qualifies."""
        from otto import paths
        sdir = paths.screenshots_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "abc123def456.png").write_bytes(b"PNG" * 400)
        n = self._notifier()
        noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        assert n.process_briefing(_briefing(_important("a", screenshot="abc123def456.png")), noon) == 1
        assert n.pending()[0]["image"] == str(sdir / "abc123def456.png")
        # missing file → no image; a path-ish name is never trusted
        assert n.process_briefing(_briefing(_important("b", screenshot="gone.png")), noon) == 1
        assert n.pending()[1]["image"] == ""
        assert n.process_briefing(_briefing(_important("c", screenshot="../api.key")), noon) == 1
        assert n.pending()[2]["image"] == ""
        # the fallback path hands the picture to the app as a fourth argument
        seen: list = []
        monkeypatch.setattr(n, "_osascript", lambda title, body, image="": seen.append(image))
        n._fallback = True
        n._last_pull = 0.0
        for p in n._pending:
            p.created -= 1000
        n.deliver_fallbacks()
        assert seen == [str(sdir / "abc123def456.png"), "", ""]

    def test_radar_reminder_for_your_due_items(self):
        n = self._notifier()
        noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

        def _todo(id_, minutes, **kw):
            due = (noon + timedelta(minutes=minutes)).isoformat()
            row = {"id": id_, "kind": "ask", "who": "alice", "what": "send the numbers", "channel": "@alice",
                   "due": due, "due_label": "today 12:30", "url": "https://acme.slack.com/x"}
            row.update(kw)
            return row

        data = {"sections": [], "radar": {"todo": [
            _todo("radar:soon", 30), _todo("radar:later", 300), _todo("radar:undated", 10, due=""),
            _todo("radar:slipped", -20, kind="promise", who="you", what="post the summary"),
        ]}}
        assert n.process_radar(data, noon) == 1
        (banner,) = n.pending()
        assert banner["title"] == "Otto · Slipped"                  # the most pressing one leads
        assert "post the summary" in banner["body"] and "(+1 more due soon)" in banner["body"]
        assert banner["id"] == "radar:slipped:due"
        # Both due-soon items are now remembered; the far-off one is not.
        notified = state.load_notified()
        assert {"radar:slipped:due", "radar:soon:due"} <= set(notified) and "radar:later:due" not in notified
        assert n.process_radar(data, noon) == 0

    def test_radar_reminder_respects_dismissal_and_missing_radar(self):
        n = self._notifier()
        noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        state.dismiss(["radar:x"])
        row = {"id": "radar:x", "kind": "ask", "who": "alice", "what": "w", "channel": "#c",
               "due": (noon + timedelta(minutes=5)).isoformat(), "due_label": "soon"}
        assert n.process_radar({"sections": [], "radar": {"todo": [row]}}, noon) == 0
        assert n.process_radar({"sections": []}, noon) == 0

    def test_quiet_hours_hold_then_drain(self):
        n = self._notifier()
        midnight = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        assert n.process_briefing(_briefing(_important("q")), midnight) == 0
        assert n.pending() == []
        assert n.drain_quiet_queue(midnight) == 0  # still quiet
        assert n.drain_quiet_queue(datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)) == 1
        assert len(n.pending()) == 1

    def test_critical_bypasses_quiet_hours(self):
        n = self._notifier()
        midnight = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        item = _important("crit", urgency="critical", urgency_score=0.97)
        assert n.process_briefing(_briefing(item), midnight) == 1

    def test_ack_and_client_presence(self):
        n = self._notifier()
        n.process_briefing(_briefing(_important("a")), datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
        assert not n.client_present()
        ids = [p["id"] for p in n.pending()]
        assert n.client_present()
        assert n.ack(ids) == 1
        assert n.pending() == []

    def test_fallback_only_when_no_client(self, monkeypatch):
        import otto.core.notify as mod
        sent = []
        monkeypatch.setattr(Notifier, "_osascript", staticmethod(lambda t, b, image="": sent.append((t, b))))
        n = Notifier(NotificationPolicy(quiet_hours_start=dtime(23, 0), quiet_hours_end=dtime(7, 0)), fallback_osascript=True)
        n._fallback = True
        n.process_briefing(_briefing(_important("a")), datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
        assert n.deliver_fallbacks() == 0  # too fresh
        monkeypatch.setattr(mod, "FALLBACK_AFTER_SECONDS", 0)
        assert n.deliver_fallbacks() == 1
        assert sent and sent[0][0].startswith("Otto")
        # A client that pulled recently suppresses the fallback
        n.process_briefing(_briefing(_important("b")), datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc))
        n.pending()
        assert n.deliver_fallbacks() == 0

    def test_local_policy_uses_local_timezone(self):
        pol = NotificationPolicy.local()
        assert pol.timezone is not None


# ---------------------------------------------------------------------------
# launchd
# ---------------------------------------------------------------------------

class TestLaunchd:
    def test_plist_contents(self, tmp_path):
        data = launchd.build_plist(python=tmp_path / "py", project_root=tmp_path / "proj", port=7099, refresh_seconds=45)
        assert data["Label"] == "com.otto.engine"
        assert data["ProgramArguments"][:3] == [str(tmp_path / "py"), "-m", "otto.core.engine"]
        assert "--port" in data["ProgramArguments"] and "7099" in data["ProgramArguments"]
        assert "--refresh" in data["ProgramArguments"] and "45" in data["ProgramArguments"]
        assert data["RunAtLoad"] is True
        assert data["KeepAlive"] == {"SuccessfulExit": False, "Crashed": True}
        assert data["EnvironmentVariables"]["PYTHONPATH"] == str(tmp_path / "proj" / "src")
        assert data["EnvironmentVariables"]["OTTO_DATA_DIR"] == str(paths.data_dir())
        assert data["StandardErrorPath"].startswith(str(paths.data_dir()))
        plistlib.dumps(data)  # serialisable

    def test_install_and_uninstall_write_plist(self, monkeypatch):
        calls = []

        def fake_launchctl(*args, check=False):
            calls.append(args)
            return type("R", (), {"returncode": 0 if args[0] != "print" else 1, "stdout": "", "stderr": ""})()

        monkeypatch.setattr(launchd, "_launchctl", fake_launchctl)
        plist = launchd.install(port=7077)
        assert plist == paths.launch_agent_plist()
        assert plist.exists()
        with open(plist, "rb") as f:
            assert plistlib.load(f)["Label"] == "com.otto.engine"
        assert any(c[0] == "bootstrap" for c in calls)
        assert launchd.is_installed()
        assert launchd.uninstall() is True
        assert not plist.exists()
        assert launchd.status()["installed"] is False

    def test_install_raises_on_launchctl_failure(self, monkeypatch):
        monkeypatch.setattr(launchd, "_launchctl", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": "nope"})())
        with pytest.raises(RuntimeError):
            launchd.install()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class TestEngine:
    def _free_port(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_refresh_once_success_and_failure(self):
        calls = {"n": 0}

        def collector():
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("adapter exploded")
            return {"sections": [], "total_items": 0, "generated_at": "x"}

        eng = OttoEngine(port=self._free_port(), refresh_seconds=60, notifications=False, collector=collector)
        assert eng.refresh_once() is True
        assert eng.data.last_error == ""
        assert eng.refresh_once() is False
        assert "adapter exploded" in eng.data.last_error
        assert eng._consecutive_failures == 1
        assert eng._current_interval() == 120  # back-off
        assert eng.refresh_once() is True
        assert eng._current_interval() == 60   # snaps back
        assert eng.data.last_error == ""

    def test_backoff_is_capped(self):
        eng = OttoEngine(port=self._free_port(), refresh_seconds=60, notifications=False, collector=lambda: {})
        eng._consecutive_failures = 50
        assert eng._current_interval() == 600

    def test_stuck_refresh_is_detected(self, monkeypatch):
        from otto.core import engine as eng_mod

        eng = OttoEngine(port=self._free_port(), refresh_seconds=60, notifications=False, collector=lambda: {})
        assert eng._stuck() is False                      # idle
        assert eng.data.begin_refresh() is True
        assert eng.data.refreshing_for() < 5 and eng._stuck() is False
        monkeypatch.setattr(eng_mod, "STUCK_REFRESH_SECONDS", 0)
        time.sleep(0.01)
        assert eng._stuck() is True                       # over the limit → run_forever exits for launchd to restart
        eng.data.end_refresh(duration=0.01)
        assert eng._stuck() is False and eng.data.refreshing_for() == 0.0

    def test_whole_refresh_is_bounded(self, monkeypatch):
        import asyncio

        from otto.web import collect

        async def hang(**kw):
            await asyncio.sleep(30)
            return {}

        monkeypatch.setattr(collect, "_collect", hang)
        monkeypatch.setattr(collect, "REFRESH_HARD_TIMEOUT", 0.05)
        eng = OttoEngine(port=self._free_port(), refresh_seconds=60, notifications=False)
        eng._llm = object()
        eng._cache = object()
        t0 = time.monotonic()
        assert eng.refresh_once() is False
        assert time.monotonic() - t0 < 5 and "TimeoutError" in eng.data.last_error

    def test_min_interval(self):
        eng = OttoEngine(port=self._free_port(), refresh_seconds=1, notifications=False, collector=lambda: {})
        assert eng.refresh_seconds == 15

    def test_start_serves_and_refreshes_then_stops(self):
        import urllib.request
        port = self._free_port()
        refreshed = threading.Event()

        def collector():
            refreshed.set()
            return {"sections": [{"source": "slack", "channels": [{"name": "#x", "items": [
                {"id": "1", "text": "An item that is definitely long enough to show", "urgency": "high",
                 "urgency_score": 0.85, "action_items": ["Review: #x"]}]}]}],
                "total_items": 1, "generated_at": "x", "generated_at_human": "now"}

        eng = OttoEngine(port=port, refresh_seconds=60, notifications=True, collector=collector)
        eng.notifier._fallback = False
        # The test runs at any hour; a 23:00–07:00 run must not hold the banner.
        policy = eng.notifier._engine.policy
        policy.quiet_hours_start = policy.quiet_hours_end
        eng.start()
        try:
            assert refreshed.wait(5)
            deadline = time.time() + 5
            while time.time() < deadline and eng.data.last_updated == 0:
                time.sleep(0.05)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3) as r:
                st = json.loads(r.read())
            assert st["items"] == 1 and st["critical_items"] == 1
            assert paths.pid_file().exists()
            # request_refresh wakes the loop
            refreshed.clear()
            eng.request_refresh()
            assert refreshed.wait(5)
            # notification produced for the important item
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/notifications", timeout=3) as r:
                assert len(json.loads(r.read())["notifications"]) == 1
        finally:
            eng.stop()
        assert not paths.pid_file().exists()
