"""
The menu bar panel's payload (``/api/items``), the feedback loop
(``/api/feedback`` and the dismiss/snooze/clear hooks), the test-banner hook,
``otto slack connect`` and the deep-Slack facts that ride along with every
message.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from datetime import time as dtime

import pytest

from otto import paths
from otto.core import slack_connect
from otto.core.notify import NotificationPolicy, Notifier
from otto.intelligence import knowledge
from otto.intelligence.knowledge import KnowledgeStore
from otto.web import state
from otto.web.items import build_items, client_item
from otto.web.server import GLOBAL_DATA, OttoRequestHandler, ThreadingHTTPServer, build_status


def _item(**kw):
    base = {"id": "id1", "text": "Alice asked for a rollback decision before the demo", "sender": "alice",
            "source_url": "https://acme.slack.com/archives/C1/p1", "urgency": "high", "urgency_score": 0.85,
            "time_display": "5m ago", "timestamp": datetime.now(timezone.utc).isoformat(),
            "why": [{"kind": "asked", "label": "Asked of you", "detail": "a rollback decision", "tone": "you"}], "summary": "",
            "for_you": "Alice needs your call on the rollback.", "action_items": ["Decide on the rollback"]}
    base.update(kw)
    return base


def _data(*items, source="slack", channel="#eng", **extra):
    d = {
        "total_items": len(items),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_at_human": "Today at 10:00 AM",
        "sections": [{"source": source, "title": source.title(), "channels": [{"name": channel, "items": list(items)}]}],
        "radar": {"todo": [], "waiting": [], "open_calls": [], "upcoming": [], "patterns": [], "attention": [], "memory": {}},
        "digest": {"digest": "Alice's rollback question is the one thing that needs you.", "connections": [],
                   "predictions": [{"note": "She will ask again before the demo", "basis": "she asked twice this week"}],
                   "heads_up": ["Demo at 3"], "source": "model"},
        "source_status": [{"source": "slack", "ok": True, "items": 3}],
    }
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# /api/items payload
# ---------------------------------------------------------------------------

class TestItemsPayload:
    def test_groups_by_urgency_and_carries_what_the_panel_needs(self):
        data = _data(
            _item(id="a", urgency_score=0.9),
            _item(id="b", urgency_score=0.5, urgency="medium", why=[{"kind": "mention", "label": "Mentions you", "detail": ""}]),
            _item(id="c", urgency_score=0.1, urgency="info", why=[], opportunity_score=0.7, opportunity_type="tool",
                  opportunity_description="A library worth a look"),
            _item(id="d", urgency_score=0.05, urgency="info", why=[]),
        )
        payload = build_items(data)
        assert payload["count"] == 4 and payload["important_count"] == 1
        groups = {g["key"]: [i["id"] for i in g["items"]] for g in payload["groups"]}
        assert groups == {"important": ["a"], "for_you": ["b"], "worth_a_look": ["c"], "also_noticed": ["d"]}
        labels = [g["label"] for g in payload["groups"]]
        assert labels == ["Needs you", "For you", "Worth a look", "Also noticed"]
        top = payload["groups"][0]["items"][0]
        assert top["url"] == "https://acme.slack.com/archives/C1/p1"
        assert top["level"] in ("critical", "high") and top["channel"] == "#eng" and top["sender"] == "alice"
        assert top["why"][0]["kind"] == "asked" and top["why"][0]["tone"] == "you"
        assert top["action_items"] == ["Decide on the rollback"]
        assert payload["digest"]["predictions"][0]["note"].startswith("She will ask")
        assert payload["headline"] and payload["generated_at_human"] == "Today at 10:00 AM"
        assert "radar" in payload and "sections" in payload["radar"]

    def test_hidden_items_are_left_out(self):
        data = _data(_item(id="a"), _item(id="b"))
        payload = build_items(data, hidden={"a"})
        assert payload["count"] == 1 and [i["id"] for g in payload["groups"] for i in g["items"]] == ["b"]

    def test_thumbnail_path_only_for_a_real_file_in_the_screenshots_dir(self):
        sdir = paths.screenshots_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "abc123def456.png").write_bytes(b"PNG" * 100)
        good = client_item(_item(screenshot="abc123def456.png", _urgency_score=0.8))
        assert good["screenshot_path"] == str(sdir / "abc123def456.png")
        assert good["screenshot_url"] == "/static/screenshots/abc123def456.png"
        for bad in ("../api.key", "/etc/passwd", ".hidden.png", "missing.png"):
            it = client_item(_item(screenshot=bad, _urgency_score=0.8))
            assert it["screenshot_path"] == "" and it["screenshot_url"] == "", bad

    def test_urls_are_checked(self):
        it = client_item(_item(source_url="javascript:alert(1)", external_url="file:///etc/passwd", _urgency_score=0.8))
        assert it["url"] == "" and it["external_url"] == ""
        it = client_item(_item(source_url="slack://channel?team=T1&id=C1", _urgency_score=0.8))
        assert it["url"].startswith("slack://")

    def test_status_carries_the_digest_line(self):
        st = build_status(_data(_item()))
        assert st["digest"].startswith("Alice's rollback question")


# ---------------------------------------------------------------------------
# server: /api/items, /api/feedback, test banner, peek
# ---------------------------------------------------------------------------

class _Server:
    port_range = range(7400, 7480)

    @classmethod
    def setup_class(cls):
        cls.notifier = Notifier(NotificationPolicy(quiet_hours_start=dtime(23, 0), quiet_hours_end=dtime(7, 0)), fallback_osascript=False)
        handler = type("BoundHandler", (OttoRequestHandler,), {"notifier": cls.notifier})
        cls.server = None
        for port in cls.port_range:
            try:
                cls.server = ThreadingHTTPServer(("127.0.0.1", port), handler)
                cls.port = port
                break
            except OSError:
                continue
        assert cls.server is not None
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        GLOBAL_DATA.refresh_hook = lambda: None

    @classmethod
    def teardown_class(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        return urllib.request.urlopen(urllib.request.Request(self.url(path)), timeout=5)

    def post(self, path, data=None):
        body = urllib.parse.urlencode(data or {}).encode()
        req = urllib.request.Request(self.url(path), data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        return urllib.request.urlopen(req, timeout=5)

    @staticmethod
    def code(fn):
        try:
            with fn() as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code


class TestPanelEndpoints(_Server):
    @pytest.fixture(autouse=True)
    def _fresh(self, tmp_path, monkeypatch):
        store = KnowledgeStore(tmp_path / "k.db")
        monkeypatch.setattr(knowledge, "default_store", lambda: store)
        self.store = store
        GLOBAL_DATA.update(_data(_item(id="a"), _item(id="b", urgency_score=0.4, urgency="medium")))
        yield

    def test_items_endpoint_is_json_and_grouped(self):
        with self.get("/api/items") as resp:
            assert resp.headers["Content-Type"].startswith("application/json")
            payload = json.loads(resp.read())
        assert payload["count"] == 2
        assert [g["key"] for g in payload["groups"]] == ["important", "for_you"]
        assert self.code(lambda: self.post("/api/items")) in (404, 405)

    def test_feedback_is_recorded_for_items_on_the_briefing(self):
        with self.post("/api/feedback", {"kind": "open", "id": "a"}) as resp:
            assert json.loads(resp.read()) == {"status": "ok"}
        with self.post("/api/feedback", {"kind": "expand", "id": "a"}) as resp:
            assert json.loads(resp.read()) == {"status": "ok"}
        # an id that is not on the briefing teaches nothing, but the request succeeds
        with self.post("/api/feedback", {"kind": "open", "id": "nope"}) as resp:
            assert json.loads(resp.read()) == {"status": "ignored"}
        assert self.code(lambda: self.post("/api/feedback", {"kind": "like", "id": "a"})) == 400
        assert self.code(lambda: self.post("/api/feedback", {"kind": "open"})) == 400
        assert self.code(lambda: self.get("/api/feedback?kind=open&id=a")) == 405
        priors = self.store.feedback_priors()
        assert priors.events == 2 and priors.channel("#eng") > 0 and priors.sender("alice") > 0

    def test_dismiss_snooze_and_clear_teach_habits_too(self):
        with self.post("/api/dismiss", {"id": "a"}):
            pass
        with self.post("/api/snooze", {"id": "b", "hours": "2"}):
            pass
        GLOBAL_DATA.update(_data(_item(id="c"), _item(id="d")))
        with self.post("/api/clear"):
            pass
        with self.store._connect() as conn:  # noqa: SLF001
            kinds = sorted(r[0] for r in conn.execute("SELECT kind FROM feedback").fetchall())
        assert kinds == ["clear", "clear", "dismiss", "snooze"]
        state.hidden_ids()  # still readable

    def test_status_says_whether_a_banner_client_is_listening(self):
        with self.get("/api/status") as resp:
            st = json.loads(resp.read())
        assert "client_seen_seconds" in st

    def test_test_banner_and_peek(self):
        with self.post("/api/notifications/test") as resp:
            body = json.loads(resp.read())
        assert body["status"] == "queued" and body["id"].startswith("test:")
        before = self.notifier.seconds_since_pull()
        with self.get("/api/notifications?peek=1") as resp:
            pend = json.loads(resp.read())["notifications"]
        assert any(p["id"] == body["id"] for p in pend)
        assert pend[-1]["title"] == "Otto · Test" and "Banners are working" in pend[-1]["body"]
        assert self.notifier.seconds_since_pull() == before        # peeking is not being a client
        with self.get("/api/notifications") as resp:              # a real client pull marks presence…
            json.loads(resp.read())
        assert self.notifier.client_present()
        with self.post("/api/notifications/ack", {"ids": body["id"]}) as resp:   # …and acks
            assert json.loads(resp.read())["acked"] >= 1
        with self.get("/api/notifications?peek=1") as resp:
            assert not any(p["id"] == body["id"] for p in json.loads(resp.read())["notifications"])
        assert self.code(lambda: self.get("/api/notifications/test")) == 405


# ---------------------------------------------------------------------------
# otto slack connect
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload, status=200, headers=None):
        self._p, self.status_code, self.headers = payload, status, headers or {}

    def json(self):
        return self._p


class _FakeSlackHTTP:
    def __init__(self, *, auth=None, scopes="", listing=None):
        self.auth = auth or {"ok": True, "user": "sam", "team": "Acme", "url": "https://acme.slack.com/", "user_id": "UME"}
        self.scopes = scopes
        self.listing = listing or {}
        self.calls = []

    async def get(self, url, params=None, **_):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, dict(params or {})))
        if method == "auth.test":
            return _FakeResp(self.auth, headers={"x-oauth-scopes": self.scopes})
        if method == "conversations.list":
            kind = params["types"]
            if kind in self.listing:
                return _FakeResp(self.listing[kind])
            return _FakeResp({"ok": False, "error": "missing_scope", "needed": f"{kind}:read"})
        raise AssertionError(f"unexpected method {method}")

    async def post(self, *a, **k):
        raise AssertionError("otto slack connect must never POST")


class TestSlackConnect:
    def test_manifest_lists_only_read_scopes(self):
        text = slack_connect.manifest_text()
        for s in slack_connect.SCOPES:
            assert f"- {s}" in text
        assert "chat:write" not in text and "reactions:write" not in text and "files:write" not in text
        assert all(s.endswith((":read", ":history")) for s in slack_connect.SCOPES)
        # The README shows the manifest verbatim (minus the comment header) — keep them in step.
        readme = (paths.project_root() / "README.md").read_text()
        assert text[text.index("display_information"):].strip() in readme

    def test_refuses_browser_session_and_junk_tokens(self):
        assert slack_connect.classify("xoxc-123456789012345678901234")[0] is False
        assert "browser-session" in slack_connect.classify("xoxd-123456789012345678901234")[1]
        assert slack_connect.classify("sk-or-v1-notaslacktoken-abcdefghijklmnop")[0] is False
        assert slack_connect.classify("")[0] is False
        assert slack_connect.classify("xoxp-1234567890-1234567890-abcdefghijklmnopqrstuv") == (True, "")

    def test_validate_reports_identity_scopes_and_coverage(self):
        http = _FakeSlackHTTP(
            scopes="channels:history,channels:read,groups:history,groups:read,users:read",
            listing={"public_channel": {"ok": True, "channels": [{"id": "C1", "is_member": True}, {"id": "C2", "is_member": False}]},
                     "private_channel": {"ok": True, "channels": [{"id": "G1"}]}},
        )
        v = slack_connect.validate("xoxp-1234567890-1234567890-abcdefghijklmnopqrstuv", client=http)
        assert v.ok and v.user == "sam" and v.team == "Acme" and v.kind == "user"
        assert v.missing == ["im:history", "im:read", "mpim:history", "mpim:read"]
        assert v.readable == {"public_channel": 1, "private_channel": 1} and v.unlisted == ["im", "mpim"]
        assert v.coverage() == "1 channels, 1 private groups — cannot list DMs, group DMs"
        assert all(m == "auth.test" or m == "conversations.list" for m, _ in http.calls)

    def test_validate_explains_a_rejected_token(self):
        v = slack_connect.validate("xoxp-1234567890-1234567890-abcdefghijklmnopqrstuv",
                                   client=_FakeSlackHTTP(auth={"ok": False, "error": "invalid_auth"}))
        assert not v.ok and "invalid_auth" in v.error and "copy it again" in v.error

    def test_connect_stores_only_a_verified_token(self, monkeypatch):
        from otto.utils import keys as K
        monkeypatch.setenv("OTTO_DISABLE_KEYCHAIN", "1")
        out = []
        bad = _FakeSlackHTTP(auth={"ok": False, "error": "token_revoked"})
        rc, v = slack_connect.connect("xoxp-1234567890-1234567890-abcdefghijklmnopqrstuv", client=bad, out=out.append)
        assert rc == 1 and not K.discover_keys() and any("Nothing was stored" in line for line in out)
        good = _FakeSlackHTTP(scopes=",".join(slack_connect.SCOPES),
                              listing={k: {"ok": True, "channels": [{"id": "X", "is_member": True}]} for k in ("public_channel", "private_channel", "im", "mpim")})
        rc, v = slack_connect.connect("xoxp-1234567890-1234567890-abcdefghijklmnopqrstuv", client=good, out=out.append)
        assert rc == 0 and v.missing == []
        stored = K.slack_token()
        assert stored and stored.key.startswith("xoxp-")
        text = "\n".join(out)
        assert "xoxp-1234567890-1234567890-abcdefghijklmnopqrstuv" not in text and "xoxp-12345…stuv" in text
        assert "sam at Acme" in text and "1 channels, 1 private groups, 1 DMs, 1 group DMs" in text
        # a second connect with the same token changes nothing
        rc, _ = slack_connect.connect("xoxp-1234567890-1234567890-abcdefghijklmnopqrstuv", client=good, out=out.append)
        assert rc == 0 and len([k for k in K.discover_keys() if K.is_slack_token(k.key)]) == 1
        assert any("already stored" in line for line in out)

    def test_connect_prompts_with_echo_off_and_refuses_session_tokens(self, monkeypatch):
        out = []
        rc, v = slack_connect.connect(None, prompt=lambda msg: "xoxc-123456789012345678901234", out=out.append,
                                      opener=lambda url: True, interactive=True)
        assert rc == 1 and v is None
        text = "\n".join(out)
        assert "api.slack.com" in text and "will not be echoed" not in text     # the prompt text is the prompt's, not ours
        assert "browser-session token" in text
        assert "Scopes, all read-only:" in text

    # -- the create-app link and the browser hand-off ------------------------

    def test_create_app_link_is_slacks_documented_form_and_carries_exactly_the_manifest(self):
        import json
        import re
        from urllib.parse import parse_qs, unquote, urlsplit
        url = slack_connect.create_app_url()
        parts = urlsplit(url)
        assert parts.scheme == "https" and parts.netloc == "api.slack.com" and parts.path == "/apps"
        q = parse_qs(parts.query, keep_blank_values=True)
        assert q["new_app"] == ["1"] and set(q) == {"new_app", "manifest_json"}
        assert json.loads(q["manifest_json"][0]) == slack_connect.MANIFEST
        assert json.loads(unquote(url.split("manifest_json=", 1)[1])) == slack_connect.MANIFEST
        # nothing but the manifest: read-only user scopes, no bot user, no events, no redirect URLs
        m = slack_connect.MANIFEST
        assert m["oauth_config"] == {"scopes": {"user": list(slack_connect.SCOPES)}}
        assert "features" not in m and "redirect_urls" not in m["oauth_config"] and "bot" not in m["oauth_config"]["scopes"]
        assert m["settings"] == {"org_deploy_enabled": False, "socket_mode_enabled": False, "token_rotation_enabled": False}
        # safe to hand to `open` and to print: fully percent-encoded, no spaces or quotes, sane length
        assert re.fullmatch(r"[A-Za-z0-9%._~=&?:/-]+", url) and len(url) < 1500

    def test_yaml_and_json_are_the_same_manifest(self):
        text = slack_connect.manifest_text()
        body = "\n".join(line for line in text.splitlines() if not line.startswith("#")) + "\n"
        assert body == (
            "display_information:\n"
            "  name: Otto (read-only)\n"
            "  description: Otto read only\n"
            '  background_color: "#1f2933"\n'
            "oauth_config:\n"
            "  scopes:\n"
            "    user:\n"
            + "".join(f"      - {s}\n" for s in slack_connect.SCOPES) +
            "settings:\n"
            "  org_deploy_enabled: false\n"
            "  socket_mode_enabled: false\n"
            "  token_rotation_enabled: false\n"
        )
        # the renderer quotes what YAML would misread, and only that
        assert slack_connect._yaml_scalar("#1f2933") == '"#1f2933"'
        assert slack_connect._yaml_scalar("key: value") == '"key: value"'
        assert slack_connect._yaml_scalar("Otto (read-only)") == "Otto (read-only)"
        assert slack_connect._yaml_scalar(True) == "true" and slack_connect._yaml_scalar("") == '""'
        import json
        assert json.loads(slack_connect.manifest_json()) == slack_connect.MANIFEST

    def test_connect_opens_the_browser_once_on_the_create_page_and_keeps_the_link_out_of_the_way(self, monkeypatch):
        from otto.utils import keys as K
        monkeypatch.setenv("OTTO_DISABLE_KEYCHAIN", "1")
        opened = []
        good = _FakeSlackHTTP(scopes=",".join(slack_connect.SCOPES),
                              listing={k: {"ok": True, "channels": [{"id": "X", "is_member": True}]} for k in ("public_channel", "private_channel", "im", "mpim")})
        out = []
        rc, v = slack_connect.connect(None, prompt=lambda msg: "xoxp-1234567890-1234567890-abcdefghijklmnopqrstuv",
                                      client=good, out=out.append, opener=lambda url: opened.append(url) or True, interactive=True)
        assert rc == 0 and v.ok and K.slack_token()
        assert opened == [slack_connect.create_app_url()]
        text = "\n".join(out)
        assert "Your browser is opening" in text and "manifest_json=" not in text      # opened → no 700-char link in the way
        assert "--manifest" in text and "--no-browser" not in text                      # the by-hand route is named; no dead flags
        assert text.index("Connect Slack (read-only)") < text.index("Your browser is opening") < text.index("Paste it below")
        assert "Install to Workspace" in text and "xoxp-" in text and "xoxb-" in text

    def test_no_browser_or_no_terminal_or_no_luck_prints_the_link_instead(self):
        calls = []
        # open_browser=False (the --json flow, scripts): never even tries
        out = []
        slack_connect.connect(None, prompt=lambda m: "", out=out.append, open_browser=False, opener=lambda u: calls.append(u) or True, interactive=True)
        assert calls == [] and slack_connect.create_app_url() in "\n".join(out) and "Open this link" in "\n".join(out)
        # piped stdin (a script): no browser pops, the link is printed
        out = []
        slack_connect.connect(None, prompt=lambda m: "", out=out.append, opener=lambda u: calls.append(u) or True, interactive=False)
        assert calls == [] and slack_connect.create_app_url() in "\n".join(out)
        # `open` failed (no GUI session, odd default browser): fall back to the link, keep going to the prompt
        out = []
        asked = []
        slack_connect.connect(None, prompt=lambda m: asked.append(m) or "", out=out.append, opener=lambda u: False, interactive=True)
        assert slack_connect.create_app_url() in "\n".join(out) and asked

    def test_empty_enter_reopens_the_page_after_sign_in_and_ctrl_c_cancels(self, monkeypatch):
        """Slack drops the filled-in manifest when it has to route through sign-in; Enter re-opens it, filled in."""
        from otto.utils import keys as K
        monkeypatch.setenv("OTTO_DISABLE_KEYCHAIN", "1")
        opened = []
        answers = iter(["", "", "xoxp-1234567890-1234567890-abcdefghijklmnopqrstuv"])   # signed in after two tries
        good = _FakeSlackHTTP(scopes=",".join(slack_connect.SCOPES),
                              listing={k: {"ok": True, "channels": []} for k in ("public_channel", "private_channel", "im", "mpim")})
        out = []
        rc, v = slack_connect.connect(None, prompt=lambda m: next(answers), client=good, out=out.append,
                                      opener=lambda u: opened.append(u) or True, interactive=True)
        assert rc == 0 and v.ok and K.slack_token()
        assert opened == [slack_connect.create_app_url()] * 3                      # first open + two re-opens
        assert sum("Opened the page again" in line for line in out) == 2
        assert "press Enter here with nothing pasted" in "\n".join(out)
        # it does not loop forever: after REOPEN_LIMIT empty answers it stops, stores nothing
        K.remove_key("xoxp-")
        opened.clear()
        out.clear()
        rc, v = slack_connect.connect(None, prompt=lambda m: "", out=out.append, opener=lambda u: opened.append(u) or True, interactive=True)
        assert rc == 1 and v is None and len(opened) == 1 + slack_connect.REOPEN_LIMIT
        assert any("Nothing pasted" in line for line in out) and K.slack_token() is None
        # Ctrl-C / Ctrl-D (None from the reader) is a cancel, not a re-open
        opened.clear()
        out.clear()
        rc, v = slack_connect.connect(None, prompt=lambda m: None, out=out.append, opener=lambda u: opened.append(u) or True, interactive=True)
        assert rc == 1 and v is None and opened == [slack_connect.create_app_url()] and any("Cancelled" in line for line in out)
        # with the link printed instead of opened, an empty Enter just ends it (nothing to re-open)
        opened.clear()
        rc, _ = slack_connect.connect(None, prompt=lambda m: "", out=out.append, open_browser=False, opener=lambda u: opened.append(u) or True, interactive=True)
        assert rc == 1 and opened == []

    def test_read_token_tells_empty_from_cancelled(self, monkeypatch):
        import io
        assert slack_connect.read_token(lambda m: "  xoxp-abc  ") == "xoxp-abc"
        assert slack_connect.read_token(lambda m: "") == "" and slack_connect.read_token(lambda m: None) is None
        monkeypatch.setattr(slack_connect.sys, "stdin", io.StringIO("\n"))          # piped empty line
        assert slack_connect.read_token() == ""
        monkeypatch.setattr(slack_connect.sys, "stdin", io.StringIO(""))            # closed pipe
        assert slack_connect.read_token() is None
        monkeypatch.setattr(slack_connect.sys, "stdin", io.StringIO(" xoxp-1234567890-1234567890-abcdefghijklmnopqrstuv \n"))
        assert slack_connect.read_token() == "xoxp-1234567890-1234567890-abcdefghijklmnopqrstuv"

    def test_the_opener_only_ever_opens_the_create_page(self):
        # anything else is refused before a process is spawned (the test guard would raise on `open`)
        assert slack_connect.open_in_browser("https://example.com/") is False
        assert slack_connect.open_in_browser("https://api.slack.com/apps.evil.com/?new_app=1") is False
        assert slack_connect.open_in_browser("file:///etc/passwd") is False
        assert slack_connect.open_in_browser("") is False

    def test_connect_replaces_an_older_stored_token(self, monkeypatch):
        from otto.utils import keys as K
        monkeypatch.setenv("OTTO_DISABLE_KEYCHAIN", "1")
        K.add_key("xoxp-1111111111-1111111111-abcdefghijklmnopqrstuv")
        good = _FakeSlackHTTP(scopes=",".join(slack_connect.SCOPES),
                              listing={k: {"ok": True, "channels": []} for k in ("public_channel", "private_channel", "im", "mpim")})
        out = []
        rc, _ = slack_connect.connect("xoxp-2222222222-2222222222-abcdefghijklmnopqrstuv", client=good, out=out.append)
        assert rc == 0
        slack = [k.key for k in K.discover_keys() if K.is_slack_token(k.key)]
        assert slack == ["xoxp-2222222222-2222222222-abcdefghijklmnopqrstuv"]
        text = "\n".join(out)
        assert "Replaced the previous token xoxp-11111…stuv" in text and "1111111111-1111111111" not in text
        # a token from the environment is left alone (and said so by disconnect, not silently dropped here)
        monkeypatch.setenv("SLACK_TOKEN", "xoxp-3333333333-3333333333-abcdefghijklmnopqrstuv")
        out.clear()
        rc, _ = slack_connect.connect("xoxp-2222222222-2222222222-abcdefghijklmnopqrstuv", client=good, out=out.append)
        assert rc == 0 and not any("Replaced" in line for line in out)
        assert "xoxp-3333333333-3333333333-abcdefghijklmnopqrstuv" in [k.key for k in K.discover_keys()]

    def test_status_and_disconnect(self, monkeypatch):
        from otto.utils import keys as K
        monkeypatch.setenv("OTTO_DISABLE_KEYCHAIN", "1")
        out = []
        assert slack_connect.status(out=out.append) == 1 and any("no token" in line for line in out)
        K.add_key("xoxb-1234567890-1234567890-abcdefghijklmnopqrstuv")
        good = _FakeSlackHTTP(scopes=",".join(slack_connect.SCOPES),
                              listing={k: {"ok": True, "channels": []} for k in ("public_channel", "private_channel", "im", "mpim")})
        out.clear()
        rc = slack_connect.status(client=good, engine_status={"source_status": [{"source": "slack api", "ok": True, "channels": 7, "items": 90}]},
                                  out=out.append)
        text = "\n".join(out)
        assert rc == 0 and "bot token xoxb-12345…stuv" in text and "reading 7 channels, 90 messages" in text
        out.clear()
        assert slack_connect.disconnect(out=out.append) == 0 and K.slack_token() is None
        assert slack_connect.disconnect(out=out.append) == 1

    def test_cli_wiring(self, monkeypatch, capsys):
        from otto import cli
        monkeypatch.setattr(cli, "_api", lambda *a, **k: None)
        parser = cli.build_parser()
        assert parser.parse_args(["slack", "connect", "--manifest"]).manifest is True
        assert parser.parse_args(["slack", "disconnect"]).slack_command == "disconnect"
        assert cli.main(["slack", "connect", "--manifest"]) == 0
        assert capsys.readouterr().out == slack_connect.manifest_text()
        # where the token goes is a config.toml setting, not a flag
        seen = []
        monkeypatch.setattr(slack_connect, "connect", lambda **kw: seen.append(kw) or (1, None))
        assert cli.main(["slack", "connect"]) == 1
        monkeypatch.setattr(cli, "_keychain", lambda: True)
        assert cli.main(["slack", "connect"]) == 1
        assert seen == [{"use_keychain": False}, {"use_keychain": True}]
        for gone in (["slack", "status"], ["slack", "connect", "--no-browser"], ["slack", "connect", "--keychain"]):
            with pytest.raises(SystemExit):
                parser.parse_args(gone)


# ---------------------------------------------------------------------------
# deep Slack facts ride along with each message
# ---------------------------------------------------------------------------

class TestSlackFactsReachTheReasons:
    def test_adapter_meta_is_carried_into_normalized_events(self):
        from otto.intelligence.ingestion import _carry_meta
        meta = _carry_meta({
            "channel_id": "C1", "mentions_you": True, "is_dm": False, "reply_count": 3, "reactions": ["eyes", "white_check_mark"],
            "reply_users": ["alice", "bob"], "you_replied": True, "sender_title": "Staff Engineer" * 40,
            "channel_purpose": "Production incidents", "channel_topic": "", "extraction_mode": "slack_api", "is_edited": True,
            "workspace": "acme",
        })
        assert meta["mentions_you"] is True and meta["reply_count"] == 3 and meta["reactions"] == ["eyes", "white_check_mark"]
        assert meta["reply_users"] == ["alice", "bob"] and meta["you_replied"] is True and meta["is_edited"] is True
        assert len(meta["sender_title"]) == 300 and meta["channel_purpose"] == "Production incidents"
        assert "channel_topic" not in meta and "channel_id" not in meta and "workspace" not in meta

    @pytest.mark.asyncio
    async def test_slack_api_adapter_supplies_titles_purpose_replies(self):
        from tests.unit.test_slack_api import T0, Clock, FakeSlack, SlackAdapter
        fake = FakeSlack()
        fake.users["U1"]["profile"]["title"] = "Staff Engineer"
        fake.channels[0].update({"purpose": {"value": "Production incidents"}, "topic": {"value": "on-call: bob"}})
        parent = fake.add("C1", T0 - 600, "U1", "deploy is failing, can someone look?", reply_count=2, reply_users=["U2", "UME"],
                          reactions=[{"name": "eyes", "users": ["U2"]}], edited={"user": "U1"})
        adapter = SlackAdapter(fake, "default", clock=Clock())
        await adapter.connect()
        events = await adapter.poll(datetime.fromtimestamp(T0, tz=timezone.utc) - timedelta(hours=6))
        ev = next(e for e in events if e.source_id == f"C1:{parent['ts']}")
        m = ev.raw_metadata
        assert m["sender_title"] == "Staff Engineer" and m["channel_purpose"] == "Production incidents" and m["channel_topic"] == "on-call: bob"
        assert m["reply_users"] == ["Bob Lee", "Sam"] and m["you_replied"] is True and m["is_edited"] is True
        assert m["reactions"] == ["eyes"]

    def test_handled_answered_and_title_reasons(self):
        from types import SimpleNamespace
        from otto.intelligence.relevance import reasons_for
        now = datetime.now(timezone.utc)

        def ev(text, sender, *, minutes_ago, meta=None):
            return SimpleNamespace(plain_text_extract=text, sender=sender, timestamp=now - timedelta(minutes=minutes_ago),
                                   is_auto_generated=False, meta=meta or {})

        conv = SimpleNamespace(subject="#eng", matched_directive="", opportunity_score=0.0, opportunity_type="", opportunity_description="")
        # an ask somebody reacted 👀 to
        asked = ev("@Sam can you approve the deploy before 3pm?", "alice", minutes_ago=10, meta={"reactions": ["eyes"], "sender_title": "Staff Engineer"})
        kinds = {r.kind: r for r in reasons_for(conv, [asked], names=["Sam"])}
        assert "asked" in kinds and "handled" in kinds and kinds["handled"].detail.startswith("reacted with :eyes:")
        assert kinds["title"].label == "alice · Staff Engineer"
        # you asked, somebody answered
        mine = ev("does anyone know if the migration finished?", "Sam", minutes_ago=60)
        reply = ev("yes, done an hour ago", "bob", minutes_ago=5)
        kinds = {r.kind: r for r in reasons_for(conv, [mine, reply], names=["Sam"])}
        assert "answered" in kinds and "reply" not in kinds and "thread" not in kinds
        assert kinds["answered"].label == "Reply to your question" and "you asked" in kinds["answered"].detail
        # a plain reply in your thread stays a reply
        mine2 = ev("shipping the fix now", "Sam", minutes_ago=60)
        kinds = {r.kind for r in reasons_for(conv, [mine2, reply], names=["Sam"])}
        assert "reply" in kinds and "answered" not in kinds


# ---------------------------------------------------------------------------
# the web page speaks plainly and carries the digest + feedback hooks
# ---------------------------------------------------------------------------

class TestWebWording:
    def test_page_has_digest_and_feedback_hooks_and_no_ai_labels(self):
        from otto.web.render import render_page
        html = render_page(_data(_item()))
        assert 'id="digest"' in html and "Alice&#x27;s rollback question" in html
        assert "function feedback(" in html and "gotoItem" in html
        for phrase in ("AI-analyzed", "AI analysis", "LLM", "AI-powered"):
            assert phrase not in html, phrase
