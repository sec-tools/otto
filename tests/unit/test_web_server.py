from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


from otto import paths
from otto.web import state
from otto.web.server import (
    GLOBAL_DATA,
    BriefingData,
    OttoRequestHandler,
    ThreadingHTTPServer,
    _clean_text,
    _format_time,
    _h,
    _is_duplicate_item,
    _is_snoozed,
    _load_dismissed,
    _merge_duplicate_items,
    _save_dismissed,
    _save_snoozed,
    build_status,
    render_briefing_html,
)


def _item(**kw):
    base = {"id": "id1", "text": "Hello world message that is long enough", "sender": "Alice",
            "source_url": "https://acme.slack.com/archives/C1/p1", "urgency": "high", "time_display": "5m ago"}
    base.update(kw)
    return base


def _data(*items, source="slack", channel="#general"):
    return {
        "total_items": len(items),
        "generated_at_human": "Today at 10:00 AM",
        "sections": [{"source": source, "title": source.title(), "channels": [{"name": channel, "items": list(items)}]}],
    }


class TestBriefingData:
    def test_init_empty(self):
        bd = BriefingData()
        assert bd.get() == {}
        assert bd.last_updated == 0.0
        assert bd.refreshing is False

    def test_update_and_get(self):
        bd = BriefingData()
        bd.update({"total_items": 5, "sections": []})
        assert bd.get()["total_items"] == 5
        assert bd.last_updated > 0.0

    def test_refresh_bookkeeping(self):
        bd = BriefingData()
        assert bd.begin_refresh() is True
        assert bd.begin_refresh() is False  # coalesced
        bd.end_refresh(error="boom", duration=1.5)
        assert bd.refreshing is False
        assert bd.last_error == "boom"
        assert bd.last_duration == 1.5
        bd.update({})
        assert bd.last_error == ""  # success clears the error

    def test_request_refresh_uses_hook(self):
        bd = BriefingData()
        called = []
        bd.refresh_hook = lambda: called.append(1)
        assert bd.request_refresh() == "refreshing"
        assert called == [1]
        bd.begin_refresh()
        assert bd.request_refresh() == "already_refreshing"

    def test_thread_safety(self):
        bd = BriefingData()

        def worker(val):
            for i in range(50):
                bd.update({"val": val, "i": i})
                _ = bd.get()

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert "val" in bd.get()


class TestTextAndFormatHelpers:
    def test_clean_text_empty(self):
        assert _clean_text("") == ""
        assert _clean_text(None) == ""

    def test_clean_text_strips_noise(self):
        raw = "\n".join(["Home", "#home", "DMs", "Activity", "", "12:34 PM", "123",
                         "Real important message line", "Real important message line", "Second line"])
        cleaned = _clean_text(raw)
        assert "Home" not in cleaned and "DMs" not in cleaned and "12:34" not in cleaned
        assert cleaned.count("Real important message line") == 1
        assert "Second line" in cleaned

    def test_clean_text_drops_thread_composer_chrome_kept_by_an_older_parse(self):
        """Memory recalls text as first read; the composer's checkbox must not become part of the message."""
        raw = "\n".join(["build failed twice this morning, could be an upstream issue", "Also send to security-alerts",
                         "Channel security-alerts", "Reply…"])
        assert _clean_text(raw) == "build failed twice this morning, could be an upstream issue"

    def test_format_time(self):
        now = datetime.now(timezone.utc)
        assert _format_time(now) == "just now"
        assert _format_time(now - timedelta(minutes=5)) == "5m ago"
        assert _format_time(now - timedelta(hours=3)) == "3h ago"
        assert _format_time(now - timedelta(days=2)) == "2d ago"
        assert _format_time((now - timedelta(minutes=10)).isoformat()) == "10m ago"
        assert _format_time("invalid-time") == ""

    def test_html_escape(self):
        assert _h("<script>alert('xss')</script>") == "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;"
        assert _h("A & B") == "A &amp; B"


class TestRenderBriefingHtml:
    def test_empty_state(self):
        html = render_briefing_html({"sections": [], "generated_at_human": "Today", "total_items": 0})
        assert "All clear" in html
        assert "just keeping you up to date" in html.lower()
        assert "needs your attention" not in html.lower()      # casual, not a verdict

    def test_empty_state_is_honest_when_sources_failed(self):
        data = {
            "sections": [], "generated_at_human": "Today", "total_items": 0,
            "source_status": [
                {"source": "slack", "ok": False, "error": "needs Accessibility permission (System Settings)", "items": 0},
                {"source": "gmail", "ok": False, "error": "app not open", "items": 0},
            ],
        }
        html = render_briefing_html(data)
        assert "All clear" not in html
        assert "Nothing read yet — Otto could not read Slack." in html      # Gmail is merely closed, not named
        assert "could not read Slack, Gmail" not in html
        assert "Slack · needs Accessibility" in html
        assert "Gmail · not open" in html
        # The page says what is wrong in its own words; nobody is sent to a terminal.
        assert "Slack needs a macOS permission" in html
        assert "otto test" not in html and "terminal" not in html

    def test_empty_state_when_nothing_is_open(self):
        data = {"sections": [], "total_items": 0,
                "source_status": [{"source": "slack", "ok": False, "error": "app not open", "items": 0},
                                  {"source": "gmail", "ok": False, "error": "app not open", "items": 0}]}
        html = render_briefing_html(data)
        assert "Nothing to read yet — open Slack, Gmail and Otto will start." in html   # the headline says it, once
        assert "Slack · not open" in html and "Gmail · not open" in html
        assert "needs a macOS permission" not in html  # nothing is wrong, nothing to fix
        assert 'class="prob"' not in html

    def test_empty_state_with_one_source_ok_and_one_failed(self):
        data = {
            "sections": [], "total_items": 0,
            "source_status": [
                {"source": "slack", "ok": True, "error": "", "items": 0},
                {"source": "jira", "ok": False, "error": "timed out (usually a pending macOS permission prompt)", "items": 0},
            ],
        }
        html = render_briefing_html(data)
        assert "All clear" in html
        assert "Jira unavailable" in html                     # the coverage line under the headline
        assert "Slack · Connected" in html
        assert "Jira · needs permission" in html
        assert "Jira needs a macOS permission" in html       # the needs-a-look row, with where the button is
        assert "Fix… in the menu bar" in html

    def test_meta_line_mentions_failed_sources_when_items_exist(self):
        data = _data(_item(id="a", text="Critical zero-day patch required in the auth service", urgency="critical"))
        data["source_status"] = [
            {"source": "slack", "ok": True, "error": "", "items": 1},
            {"source": "gmail", "ok": False, "error": "app not open", "items": 0},
            {"source": "jira", "ok": False, "error": "timed out (usually a pending macOS permission prompt)", "items": 0},
        ]
        html = render_briefing_html(data)
        assert "Jira unavailable" in html
        assert "Gmail unavailable" not in html  # a closed app is not a problem worth a warning

    def test_rendered_items_and_summary(self):
        html = render_briefing_html(_data(
            _item(id="a", text="Critical zero-day patch required in the auth service", urgency="critical"),
            _item(id="b", text="High priority issue with the deploy pipeline today", urgency="high",
                  source_url="https://acme.slack.com/archives/C1/p2"),
            _item(id="c", text="General notice about the office plants schedule", urgency="medium", source_url=""),
        ))
        assert "2 items needing your attention" in html
        assert "1 other update" in html
        assert "Critical zero-day patch" in html
        assert 'href="https://acme.slack.com/archives/C1/p1"' in html
        assert 'rel="noopener noreferrer"' in html

    def test_card_titles_say_what_happened(self):
        """Seen live: two cards both titled "Review the full report notebook…" — a
        generic LLM action hid a CRITICAL finding. Specific beats generic."""
        from otto.web.render import item_title

        long_text = "Scanner scan complete — payments-api main " + "details " * 30
        # generic action + specific summary → the summary leads
        assert item_title({
            "text": long_text,
            "action_items": ["Review the full report notebook for the scan findings"],
            "summary": "A scan of 'payments-api (main)' identified 4 critical and 2 high severity findings, including Remote Code Execution. Full report attached.",
        }) == "A scan of 'payments-api (main)' identified 4 critical and 2 high severity findings, including Remote Code Execution."
        # specific action → the action leads, with the trailing URL dropped
        assert item_title({
            "text": long_text,
            "action_items": ["Address the new finding: Empty dashboard token fail-open (CVSS 8.7) in https://github.com/acme/tool"],
            "summary": "A new high severity vulnerability was identified.",
        }) == "Address the new finding: Empty dashboard token fail-open (CVSS 8.7)"
        # generic action and a generic summary → still the action (an imperative beats prose)
        assert item_title({
            "text": long_text,
            "action_items": ["Review the thread when you have a moment"],
            "summary": "A discussion about the plan took place.",
        }) == "Review the thread when you have a moment"
        # very long summary without an early sentence end is truncated, not replaced by a generic action
        summary = "A scan of 'gost (main)' identified 11 high severity findings including " + ", ".join(f"issue {i}" for i in range(30))
        title = item_title({"text": long_text, "action_items": ["Review the full report notebook"], "summary": summary})
        assert title.startswith("A scan of 'gost (main)' identified 11 high") and title.endswith("…") and len(title) <= 140
        # short raw text always wins
        assert item_title({"text": "can you review PR 42 by Friday?", "action_items": ["Review PR 42"]}) == "Can you review PR 42 by Friday?"

    def test_no_inline_javascript(self):
        """Strict CSP: no on* handlers, no javascript: URLs, exactly one nonce'd script."""
        html = render_briefing_html(_data(
            _item(id="x", screenshot="abc12345.png", action_items=["Do the thing"],
                  link_intelligence={"title": "t", "url": "https://ex.com", "summary": "s", "why_useful": ["u"], "key_features": ["f"]}),
        ))
        import re
        assert not re.search(r'\son\w+\s*=', html), "inline event handlers are forbidden"
        assert "javascript:" not in html
        assert html.count("<script") == 1
        assert re.search(r'<script nonce="[A-Za-z0-9_-]{16,}">', html)
        assert re.search(r'<style nonce="[A-Za-z0-9_-]{16,}">', html)
        assert ' style="' not in html
        assert 'http-equiv="refresh"' not in html  # no meta refresh; JS polling instead

    def test_screenshot_uses_data_attributes(self):
        html = render_briefing_html(_data(_item(id="s1", screenshot="abc12345.png",
                                                source_url="https://acme.slack.com/archives/C123/p456")))
        assert 'data-action="lightbox"' in html
        assert 'data-shot="/static/screenshots/abc12345.png"' in html
        assert 'data-link="https://acme.slack.com/archives/C123/p456"' in html
        assert 'id="lightbox-modal"' in html

    def test_unsafe_urls_are_dropped(self):
        html = render_briefing_html(_data(
            _item(id="u1", source_url="javascript:alert(1)", external_url="data:text/html,hi",
                  screenshot="../../etc/passwd.png"),
        ))
        assert "javascript:" not in html
        assert "data:text" not in html
        assert "etc/passwd" not in html

    def test_xss_in_text_is_escaped(self):
        payload = "<img src=x onerror=alert(1)> and \"quotes\" 'single'"
        html = render_briefing_html(_data(_item(id="q", text=payload + " padding text to pass length filter",
                                                sender=payload, action_items=[payload], ai_analysis=payload)))
        assert "<img src=x" not in html
        assert "&lt;img src=x onerror=alert(1)&gt;" in html
        import re
        assert not re.search(r"<[^>]*\sonerror=", html), "payload must never become a live attribute"

    def test_action_checklist_and_link_block(self):
        html = render_briefing_html(_data(_item(
            id="item123", text="Review project: https://github.com/test/repo for our next sprint",
            action_items=["Evaluate test/repo for project use", "Verify license compatibility"],
            link_intelligence={"title": "test/repo", "url": "https://github.com/test/repo", "summary": "A test repository.",
                               "why_useful": ["Great library"], "key_features": ["Fast", "Lightweight"]},
        )))
        assert "Action Checklist" in html
        assert 'class="todo-item"' in html
        assert "Evaluate test/repo for project use" in html
        assert "Verify license compatibility" in html
        assert "A test repository." in html
        assert 'data-action="refresh"' in html and 'data-action="clear"' in html

    def test_dismissed_and_snoozed_items_hidden(self):
        state.dismiss(["gone"])
        state.snooze("later", 2)
        html = render_briefing_html(_data(
            _item(id="gone", text="This item should be dismissed and not shown anywhere"),
            _item(id="later", text="This item is snoozed and should be hidden as well"),
            _item(id="keep", text="This item stays visible in the briefing page for sure"),
        ))
        assert "should be dismissed" not in html
        assert "is snoozed" not in html
        assert "stays visible" in html

    def test_relevance_fallback_in_context(self):
        html = render_briefing_html(_data(_item(
            id="r", text="Hello", urgency="medium", relevance="Fallback relevance text for this medium priority notification",
            ai_analysis="",
        )))
        assert "Fallback relevance text" in html

    def test_ai_analysis_rendered(self):
        html = render_briefing_html(_data(_item(
            id="cve", text="CVE-2024-1234 reported in the image parsing library", urgency="critical",
            relevance="Security vulnerability", ai_analysis="This is a critical security issue that needs immediate patching.",
            action_items=["Patch servers"],
        )))
        assert "Needs you" in html
        assert "CVE-2024-1234" in html
        assert "needs immediate patching" in html


class _ServerMixin:
    port_range = range(7180, 7250)

    @classmethod
    def setup_class(cls):
        cls.server = None
        for port in cls.port_range:
            try:
                cls.server = ThreadingHTTPServer(("127.0.0.1", port), OttoRequestHandler)
                cls.port = port
                break
            except OSError:
                continue
        assert cls.server is not None
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        GLOBAL_DATA.refresh_hook = lambda: None
        GLOBAL_DATA.update(_data(_item(id="hello", text="Hello world message that is long enough")))

    @classmethod
    def teardown_class(cls):
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path, headers=None):
        req = urllib.request.Request(self.url(path), headers=headers or {})
        return urllib.request.urlopen(req, timeout=5)

    def post(self, path, data=None, headers=None):
        body = urllib.parse.urlencode(data or {}).encode()
        h = {"Content-Type": "application/x-www-form-urlencoded"}
        h.update(headers or {})
        req = urllib.request.Request(self.url(path), data=body, method="POST", headers=h)
        return urllib.request.urlopen(req, timeout=5)

    @staticmethod
    def status_of(fn):
        try:
            with fn() as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code


class TestWebServerEndpoints(_ServerMixin):
    def test_get_root(self):
        with self.get("/") as resp:
            assert resp.status == 200
            content = resp.read().decode("utf-8")
            assert "Otto Briefing" in content and "Hello world" in content
            assert resp.headers["Content-Security-Policy"].startswith("default-src 'none'")
            assert resp.headers["X-Content-Type-Options"] == "nosniff"
            assert resp.headers["Cache-Control"] == "no-store"
            assert "Access-Control-Allow-Origin" not in resp.headers

    def test_partial_render_json(self):
        with self.get("/briefing?partial=1") as resp:
            data = json.loads(resp.read())
            assert set(data) >= {"summary", "meta", "main", "count"}
            assert "Hello world" in data["main"]

    def test_get_api_briefing(self):
        with self.get("/api/briefing") as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["total_items"] == 1
            assert "Access-Control-Allow-Origin" not in resp.headers

    def test_get_api_status(self):
        with self.get("/api/status") as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["running"] is True
            assert data["items"] == 1
            assert data["critical_items"] == 1
            assert data["sections"] == 1
            assert "refreshing" in data and "last_error" in data

    def test_notifications_endpoint_without_notifier(self):
        with self.get("/api/notifications") as resp:
            assert json.loads(resp.read()) == {"notifications": []}

    def test_404(self):
        assert self.status_of(lambda: self.get("/not-found-endpoint")) == 404

    def test_render_bug_gives_an_honest_page_not_a_dropped_connection(self, monkeypatch):
        from otto.web import server as srv

        def boom(*a, **k):
            raise ValueError("bad payload field")

        monkeypatch.setattr(srv, "render_page", boom)
        monkeypatch.setattr(srv, "build_status", boom)
        try:
            self.get("/")
            assert False, "expected a 500"
        except urllib.error.HTTPError as e:
            assert e.code == 500
            body = e.read().decode("utf-8")
            assert "Otto could not draw this page" in body and "otto logs" in body
            assert "ValueError: bad payload field" in body and "<script" not in body
        try:
            self.get("/api/status")
            assert False, "expected a 500"
        except urllib.error.HTTPError as e:
            assert e.code == 500 and json.loads(e.read())["error"].startswith("internal error")
        # The server is still serving afterwards.
        with self.get("/api/briefing") as resp:
            assert resp.status == 200

    def test_mutations_require_post(self):
        for path in ("/api/clear", "/api/dismiss?id=x", "/api/snooze?id=x&hours=1", "/api/refresh"):
            assert self.status_of(lambda p=path: self.get(p)) == 405, path

    def test_dismiss_and_snooze_via_post(self):
        with self.post("/api/dismiss", {"id": "hello"}) as resp:
            assert json.loads(resp.read())["status"] == "ok"
        assert "hello" in _load_dismissed()
        with self.post("/api/snooze", {"id": "zzz", "hours": "2"}) as resp:
            body = json.loads(resp.read())
            assert body["status"] == "ok" and body["until"]
        assert _is_snoozed("zzz")
        with self.get("/api/status") as resp:
            assert json.loads(resp.read())["items"] == 0  # dismissed item no longer counted

    def test_dismiss_requires_id(self):
        assert self.status_of(lambda: self.post("/api/dismiss", {})) == 400

    def test_snooze_invalid_hours_is_graceful(self):
        with self.post("/api/snooze", {"id": "nan-item", "hours": "invalid"}) as resp:
            assert resp.status == 200
        with self.post("/api/snooze", {"id": "neg-item", "hours": "-10"}) as resp:
            assert resp.status == 200
        assert _is_snoozed("nan-item") and _is_snoozed("neg-item")

    def test_clear_via_post(self):
        GLOBAL_DATA.update(_data(_item(id="c1"), _item(id="c2")))
        with self.post("/api/clear") as resp:
            assert json.loads(resp.read())["cleared"] == 2
        assert {"c1", "c2"} <= _load_dismissed()

    def test_clear_scope_notes_takes_worth_knowing_and_leaves_the_items(self):
        """Worth knowing (heads-ups, predictions, radar rows) clears on its own:
        `scope=notes` dismisses exactly those ids, closes the radar's open loops
        in memory, and the items stay."""
        from otto.web.render import note_id

        data = _data(_item(id="keep-me"))
        data["radar"] = {"todo": [{"id": "radar:t1", "kind": "ask", "who": "alice", "what": "review the PR", "channel": "#eng"}],
                         "waiting": [], "open_calls": [], "upcoming": [],
                         "unread": [{"id": "unread:u1", "kind": "unread", "what": "#leads", "channel": "mentions you"}],
                         "patterns": [{"id": "pattern:p1", "label": "Nightly build", "channel": "#ci", "n": 3, "span": "3 d"}],
                         "attention": ["#ci is much quieter than usual"], "memory": {}}
        data["digest"] = {"source": "model", "items": 1, "digest": "One review is waiting.", "connections": [],
                          "predictions": [{"note": "Alice will nudge you tomorrow"}], "heads_up": ["The cert expires Friday"]}
        GLOBAL_DATA.update(data)
        closed = []
        from otto.intelligence import knowledge

        class _Store:
            def set_commitment_status(self, sha, status):
                closed.append((sha, status))

        real = knowledge.default_store
        knowledge.default_store = lambda: _Store()
        try:
            with self.post("/api/clear", {"scope": "notes"}) as resp:
                body = json.loads(resp.read())
        finally:
            knowledge.default_store = real
        assert body == {"status": "ok", "cleared": 5, "scope": "notes"}
        assert self.status_of(lambda: self.post("/api/clear", {"scope": "everything"})) == 400   # never guess
        gone = _load_dismissed()
        assert {"radar:t1", "unread:u1", "pattern:p1", note_id("The cert expires Friday"), note_id("Alice will nudge you tomorrow")} <= gone
        assert "keep-me" not in gone and closed == [("t1", "dismissed")]
        with self.get("/api/items") as resp:
            payload = json.loads(resp.read())
        assert payload["count"] == 1 and payload["worth_knowing"]["count"] == 0 and payload["radar"]["sections"] == []
        assert payload["digest"]["heads_up"] == [] and payload["digest"]["predictions"] == []
        with self.get("/briefing?partial=1") as resp:
            parts = json.loads(resp.read())
        assert parts["wk_count"] == "0" and "Nothing more to know right now." in parts["wk"] and "keep-me" in parts["main"]

    def test_dismiss_snooze_and_clear_cover_every_copy_of_a_merged_item(self):
        """One message read off the screen *and* through the API is one item with
        two ids; acting on it by either id must stick under both."""
        GLOBAL_DATA.update(_data(_item(id="screen1", ids=["screen1", "api1"]),
                                 _item(id="screen2", ids=["screen2", "api2"])))
        with self.post("/api/dismiss", {"id": "api1"}):        # the copy the panel did not even show
            pass
        assert {"screen1", "api1"} <= _load_dismissed()
        with self.post("/api/snooze", {"id": "screen2", "hours": "1"}):
            pass
        assert _is_snoozed("screen2") and _is_snoozed("api2")
        with self.get("/api/status") as resp:
            assert json.loads(resp.read())["items"] == 0
        GLOBAL_DATA.update(_data(_item(id="s3", ids=["s3", "a3"])))
        with self.post("/api/clear") as resp:
            assert json.loads(resp.read())["cleared"] == 1
        assert {"s3", "a3"} <= _load_dismissed()

    def test_refresh_via_post(self):
        with self.post("/api/refresh") as resp:
            assert json.loads(resp.read())["status"] in ("refreshing", "already_refreshing")

    def test_json_body_accepted(self):
        req = urllib.request.Request(self.url("/api/dismiss"), data=json.dumps({"id": "json-id"}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
        assert "json-id" in _load_dismissed()


class TestSecurityPolicy(_ServerMixin):
    port_range = range(7251, 7350)

    def test_dns_rebinding_host_rejected(self):
        assert self.status_of(lambda: self.get("/api/status", {"Host": "evil.example.com"})) == 400
        assert self.status_of(lambda: self.get("/api/status", {"Host": f"127.0.0.1:{self.port}"})) == 200
        assert self.status_of(lambda: self.get("/api/status", {"Host": f"localhost:{self.port}"})) == 200

    def test_cross_origin_post_rejected(self):
        for headers in (
            {"Origin": "https://evil.example.com"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Origin": f"http://localhost:{self.port + 1}"},   # another local port is not us
        ):
            assert self.status_of(lambda h=headers: self.post("/api/clear", headers=h)) == 403, headers

    def test_same_origin_post_allowed(self):
        for headers in (
            {"Origin": f"http://localhost:{self.port}", "Sec-Fetch-Site": "same-origin"},
            {"Origin": f"http://127.0.0.1:{self.port}"},
            {"Referer": f"http://localhost:{self.port}/"},
            {},  # native client (menu bar / curl)
        ):
            assert self.status_of(lambda h=headers: self.post("/api/refresh", headers=h)) == 200, headers

    def test_head_request(self):
        req = urllib.request.Request(self.url("/api/status"), method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("application/json")
            assert resp.read() == b""

    def test_static_directory_traversal_blocked(self):
        for path in ("/static/screenshots/../../etc/passwd", "/static/screenshots/..%2F..%2Fetc%2Fpasswd",
                     "/static/screenshots/notes.txt", "/static/other/x.png"):
            assert self.status_of(lambda p=path: self.get(p)) == 404, path

    def test_static_screenshot_served_from_data_dir(self):
        sdir = paths.screenshots_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "shot1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        with self.get("/static/screenshots/shot1.png") as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "image/png"
            assert resp.read().startswith(b"\x89PNG")

    def test_csp_nonce_is_unique_per_response(self):
        import re
        nonces = set()
        for _ in range(3):
            with self.get("/") as resp:
                m = re.search(r"nonce-([A-Za-z0-9_-]+)", resp.headers["Content-Security-Policy"])
                assert m
                body = resp.read().decode()
                assert f'nonce="{m.group(1)}"' in body
                nonces.add(m.group(1))
        assert len(nonces) == 3


class TestDuplicateConsolidation:
    def test_detects_repeated_bot_reports_with_changing_numbers(self):
        it1 = {"text": "Nightly scan complete — api Duration : 81.3s Raw leads: 33 Confirmed: 0 Qualified: 0"}
        it2 = {"text": "Nightly scan complete — api Duration : 69.3s Raw leads: 31 Confirmed: 1 Qualified: 0"}
        assert _is_duplicate_item(it1, it2) is True

    def test_different_subjects(self):
        it1 = {"text": "Nightly scan complete — api Duration : 81.3s"}
        it2 = {"text": "Team lunch at 12:30 PM tomorrow at the cafe"}
        assert _is_duplicate_item(it1, it2) is False
        assert _is_duplicate_item({"text": ""}, it2) is False

    def test_merge(self):
        existing = {"occurrence_count": 1, "urgency": "medium", "action_items": ["Action 1"], "topics": ["security"],
                    "timestamp": "2026-09-07T10:00:00Z", "ai_analysis": "Short analysis"}
        new_item = {"urgency": "high", "action_items": ["Action 1", "Action 2"], "topics": ["command-injection"],
                    "timestamp": "2026-09-07T10:30:00Z", "ai_analysis": "Much longer and more detailed analysis"}
        _merge_duplicate_items(existing, new_item)
        assert existing["occurrence_count"] == 2
        assert existing["urgency"] == "high"
        assert existing["action_items"] == ["Action 1", "Action 2"]
        assert "command-injection" in existing["topics"]
        assert existing["timestamp"] == "2026-09-07T10:30:00Z"
        assert existing["ai_analysis"].startswith("Much longer")


class TestStatePersistence:
    def test_state_lives_in_isolated_data_dir(self):
        _save_dismissed({"a"})
        assert paths.dismissed_file().exists()
        assert str(paths.dismissed_file()).startswith(str(paths.data_dir()))
        assert "otto-data" in str(paths.data_dir())  # conftest isolation

    def test_snooze_naive_aware_and_corrupt(self):
        _save_snoozed({"item-naive": "2099-01-01T00:00:00"})
        assert _is_snoozed("item-naive") is True
        _save_snoozed({"item-expired": "2000-01-01T00:00:00"})
        assert _is_snoozed("item-expired") is False
        _save_snoozed({"item-bad": "not-a-timestamp"})
        assert _is_snoozed("item-bad") is False
        assert state.load_snoozed() == {}  # pruned on load

    def test_legacy_list_format_for_dismissed(self):
        paths.dismissed_file().write_text(json.dumps(["x", "y"]))
        assert _load_dismissed() == {"x", "y"}

    def test_corrupt_files_do_not_crash(self):
        paths.dismissed_file().write_text("{not json")
        paths.snoozed_file().write_text("[1,2,3]")
        assert _load_dismissed() == set()
        assert state.load_snoozed() == {}

    def test_snooze_clamps(self):
        wake = state.snooze("s", 0)      # → 15 min minimum
        assert wake - datetime.now(timezone.utc) < timedelta(minutes=16)
        wake = state.snooze("s2", 10**9)  # → 1 year maximum
        assert wake - datetime.now(timezone.utc) <= timedelta(days=366)

    def test_build_status_counts(self):
        data = _data(_item(id="1", urgency="critical", summary="Fix prod now"), _item(id="2", urgency="medium"))
        st = build_status(data)
        assert st["items"] == 2 and st["critical_items"] == 1
        assert st["top_headline"] == "Fix prod now"
