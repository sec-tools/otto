from __future__ import annotations

import os

import pytest

from otto import paths
from otto import cli
from otto.cli import (
    _read_pid,
    _remove_pid,
    _write_pid,
    build_parser,
    cmd_config,
    cmd_status,
    cmd_stop,
    main,
)


@pytest.fixture(autouse=True)
def _no_running_engine(monkeypatch):
    """Never talk to a real engine or launchctl from unit tests."""
    monkeypatch.setattr(cli, "_api", lambda *a, **k: None)
    monkeypatch.setattr("otto.core.launchd._launchctl", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})())


class TestPidManagement:
    def test_pid_lifecycle(self):
        assert _read_pid() is None
        _write_pid()
        assert paths.pid_file().exists()
        assert _read_pid() == os.getpid()
        _remove_pid()
        assert not paths.pid_file().exists()

    def test_stale_pid_cleanup(self):
        paths.pid_file().parent.mkdir(parents=True, exist_ok=True)
        paths.pid_file().write_text("999999")
        assert _read_pid() is None
        assert not paths.pid_file().exists()

    def test_pid_file_is_inside_isolated_data_dir(self):
        assert str(paths.pid_file()).startswith(str(paths.data_dir()))
        assert "otto-data" in str(paths.pid_file())


class TestParser:
    def test_all_commands_parse(self):
        parser = build_parser()
        for cmd in ["setup", "start", "stop", "restart", "status", "open", "config", "logs",
                    "permissions", "install", "uninstall"]:
            assert parser.parse_args([cmd]).command == cmd
        assert parser.parse_args(["run"]).command == "run"                  # alias of start
        assert parser.parse_args(["key", "list"]).key_command == "list"
        assert parser.parse_args(["key", "add", "sk-x"]).key == "sk-x"
        assert parser.parse_args(["slack", "connect"]).slack_command == "connect"
        assert parser.parse_args(["start", "--foreground"]).foreground is True

    def test_the_surface_is_small_and_settings_live_in_the_config_file(self):
        """No `otto test`/`doctor`, no per-command tuning flags: those are config.toml settings now."""
        parser = build_parser()
        help_text = parser.format_help()
        for gone in ("test", "doctor", "briefing", "radar", "directive"):
            with pytest.raises(SystemExit):
                parser.parse_args([gone])
        for flag in ("--port", "--refresh", "--keychain", "--no-browser", "--banner", "--migrate"):
            assert flag not in help_text
        with pytest.raises(SystemExit):
            parser.parse_args(["start", "--port", "7078"])
        with pytest.raises(SystemExit):
            parser.parse_args(["key", "add", "sk-x", "--keychain"])
        assert "config.toml" in help_text
        # The menu bar's plumbing is there but not advertised.
        assert parser.parse_args(["key", "add", "--json"]).json is True
        assert parser.parse_args(["slack", "connect", "--json"]).json is True
        assert "--json" not in help_text


class TestCommands:
    def test_status_without_engine(self, capsys):
        assert cmd_status() == 0
        out = capsys.readouterr().out
        assert "Otto Status Overview" in out
        assert "not running" in out
        assert "Model keys:  none" in out
        assert "Slack: no token" in out
        assert "Config:      defaults" in out and "otto config" in out

    def test_status_lists_what_needs_a_look_from_the_engine(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_api", lambda *a, **k: {
            "items": 1, "critical_items": 0, "last_updated": 0, "refreshing": False,
            "ai_powered": False, "sources": ["browser_slack:ws"], "last_error": "", "llm": {"configured": False},
            "config": {"exists": True, "path": "/x/config.toml", "error": ""},
            "problems": [
                {"key": "permissions", "title": "Slack needs a macOS permission",
                 "detail": "timed out (usually a pending macOS permission prompt)", "action": "permissions"},
                {"key": "key", "title": "A model key is being rejected", "detail": "openai: HTTP 401",
                 "action": "key_remove:sk-proj-ab"},
            ],
        })
        assert cmd_status() == 0
        out = capsys.readouterr().out
        assert "Needs a look:" in out
        assert "• Slack needs a macOS permission — timed out" in out and "→ otto permissions" in out
        assert "→ otto key remove sk-proj-ab" in out
        assert "Config:      /x/config.toml" in out

    def test_status_says_so_when_nothing_needs_a_look(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_api", lambda *a, **k: {
            "items": 0, "critical_items": 0, "last_updated": 0, "refreshing": False, "ai_powered": False,
            "sources": [], "last_error": "", "llm": {"configured": False}, "problems": [],
        })
        assert cmd_status() == 0
        assert "Nothing needs a look." in capsys.readouterr().out

    def test_status_without_engine_still_reports_a_broken_config_file(self, capsys):
        from otto.config import ensure_config_file
        path = ensure_config_file()
        path.write_text(path.read_text() + "\nrefresh_seconds = = 3\n")
        assert cmd_status() == 0
        out = capsys.readouterr().out
        assert "Needs a look:" in out and "config.toml has a problem" in out and "→ otto config" in out

    def test_status_explains_broken_llm_providers(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_api", lambda *a, **k: {
            "items": 1, "critical_items": 0, "last_updated": 0, "refreshing": False,
            "ai_powered": False, "sources": ["browser_slack:ws"], "last_error": "",
            "llm": {"configured": True, "providers": [
                {"name": "openai", "model": "gpt-4o-mini", "state": "API key rejected (HTTP 401)"},
                {"name": "devin", "model": "devin", "state": "quota / billing problem (HTTP 403)"},
            ]},
        })
        assert cmd_status() == 0
        out = capsys.readouterr().out
        assert "local heuristics — every LLM provider is unavailable" in out
        assert "openai: API key rejected (HTTP 401)" in out
        assert "devin: quota / billing problem (HTTP 403)" in out

    def test_status_with_engine(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_api", lambda *a, **k: {
            "items": 3, "critical_items": 1, "last_updated": 0, "refreshing": False,
            "ai_powered": True, "sources": ["browser_slack:ws"], "last_error": "",
            "llm": {"configured": True, "enriched": 3, "conversations": 3,
                    "providers": [{"name": "openrouter", "model": "google/gemini-2.5-flash", "state": "ok", "last_used": 1.0}]},
        })
        assert cmd_status() == 0
        out = capsys.readouterr().out
        assert "running" in out and "3 visible" in out and "slack" in out
        assert "AI · openrouter (google/gemini-2.5-flash)" in out

    def test_status_explains_a_slow_refresh(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_api", lambda *a, **k: {
            "items": 2, "critical_items": 0, "last_updated": 0, "refreshing": False, "ai_powered": False,
            "sources": ["browser_slack:ws"], "last_error": "", "llm": {"configured": False},
            "last_refresh_seconds": 31.4, "recalled": 5, "aged_out": 3,
            "phases": {"poll": 27.1, "memory": 0.2, "classify": 0.1, "build": 0.05},
            "source_status": [
                {"source": "slack", "ok": True, "items": 2, "seconds": 27.0,
                 "timings": {"tabs": 24.5, "app": 0.4, "tab_content": 2.1}},
                {"source": "calendar", "ok": True, "items": 0, "seconds": 0.5},
            ],
        })
        assert cmd_status() == 0
        out = capsys.readouterr().out
        assert "took 31.4s (slow: poll 27s)" in out
        assert "5 recalled from memory" in out and "3 on screen but older than the window" in out
        assert "slack took 27s — mostly tabs (24s); many browser tabs slow every read" in out
        assert "calendar took" not in out

    def test_status_keeps_quiet_about_a_quick_refresh(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_api", lambda *a, **k: {
            "items": 2, "critical_items": 0, "last_updated": 0, "refreshing": False, "ai_powered": False,
            "sources": ["browser_slack:ws"], "last_error": "", "llm": {"configured": False},
            "last_refresh_seconds": 2.3, "phases": {"poll": 1.9},
            "source_status": [{"source": "slack", "ok": True, "items": 2, "seconds": 1.9, "timings": {"tabs": 0.8}}],
        })
        assert cmd_status() == 0
        out = capsys.readouterr().out
        assert "took 2.3s" in out and "slow" not in out and "recalled" not in out

    def test_status_is_honest_when_no_provider_answered_yet(self, monkeypatch, capsys):
        """After a restart every item may come from the cache; a provider that has
        not been called must not be presented as the one that analysed them."""
        base = {"items": 3, "critical_items": 1, "last_updated": 0, "refreshing": False,
                "ai_powered": True, "sources": ["browser_slack:ws"], "last_error": ""}
        providers = [{"name": "openai", "model": "gpt-4o-mini", "state": "ok"},
                     {"name": "openrouter", "model": "google/gemini-2.5-flash", "state": "ok"}]
        monkeypatch.setattr(cli, "_api", lambda *a, **k: dict(
            base, llm={"configured": True, "enriched": 3, "conversations": 3, "providers": providers}))
        assert cmd_status() == 0
        out = capsys.readouterr().out
        assert "AI · cached analysis; next call goes to openai (gpt-4o-mini)" in out

        monkeypatch.setattr(cli, "_api", lambda *a, **k: dict(
            base, items=0, llm={"configured": True, "enriched": 0, "conversations": 0, "providers": providers}))
        assert cmd_status() == 0
        out = capsys.readouterr().out
        assert "AI configured · openai (gpt-4o-mini) — no analysis yet this session" in out

    def test_config_creates_the_file_and_opens_it_in_the_editor(self, monkeypatch, capsys):
        from otto.config import ConfigManager
        opened = []
        monkeypatch.setattr(cli.subprocess, "run", lambda args, **k: opened.append(args))
        assert cmd_config() == 0
        out = capsys.readouterr().out
        path = paths.data_dir() / "config.toml"
        assert path.exists() and str(path) in out
        assert "within a minute" in out
        assert opened == [["open", "-t", str(path)]]
        # Every setting is in the file, explained, and the file parses to the defaults.
        text = path.read_text()
        for key in ("refresh_seconds", "port", "screenshots", "keychain", "directives", "log_level"):
            assert key in text
        assert text.count("#") > 20
        assert ConfigManager(path).parsed and not ConfigManager(path).unknown_keys

    def test_config_reports_a_broken_file_instead_of_hiding_it(self, monkeypatch, capsys):
        from otto.config import ensure_config_file
        monkeypatch.setattr(cli.subprocess, "run", lambda args, **k: None)
        path = ensure_config_file()
        path.write_text(path.read_text() + "\nrefresh_seconds = = 3\n")
        assert cmd_config(open_editor=False) == 0
        out = capsys.readouterr().out
        assert "✗" in out and "line" in out and "last good values" in out

    def test_stop_not_running(self, capsys):
        assert cmd_stop() == 0
        assert "not running" in capsys.readouterr().out

    def test_stop_running_process(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_read_pid", lambda: 12345)
        signals = []

        def fake_kill(pid, sig):
            signals.append(sig)
            if sig == 0:
                raise ProcessLookupError()

        monkeypatch.setattr(os, "kill", fake_kill)
        assert cmd_stop() == 0
        assert "Stopping Otto" in capsys.readouterr().out
        assert signals[0] != 0  # SIGTERM first

    def test_key_add_list_remove(self, capsys):
        assert main(["key", "add", "sk-or-v1-" + "b" * 40]) == 0
        assert main(["key", "list"]) == 0
        out = capsys.readouterr().out
        assert "sk-or-v1-b" in out and ("b" * 40) not in out  # masked
        assert paths.key_file().exists()
        assert oct(paths.key_file().stat().st_mode & 0o777) == "0o600"
        assert main(["key", "remove", "sk-or-v1-b"]) == 0
        assert main(["key", "list"]) == 0
        assert "No API keys" in capsys.readouterr().out

    def test_key_add_twice_says_nothing_changed(self, capsys):
        key = "sk-or-v1-" + "d" * 40
        assert main(["key", "add", key]) == 0
        assert "Stored sk-or-v1-d" in capsys.readouterr().out
        assert main(["key", "add", key]) == 0
        out = capsys.readouterr().out
        assert "Already stored, nothing changed" in out and "Stored " not in out and ("d" * 40) not in out
        assert paths.key_file().read_text().count("sk-or-v1-") == 1

    def test_key_add_notes_an_older_key_for_the_same_provider(self, capsys):
        assert main(["key", "add", "sk-or-v1-" + "e" * 40]) == 0
        capsys.readouterr()
        assert main(["key", "add", "sk-or-v1-" + "f" * 40]) == 0
        out = capsys.readouterr().out
        assert "Stored sk-or-v1-f" in out
        assert "another openrouter key is still stored (sk-or-v1-e" in out and "otto key remove sk-or-v1-e" in out
        assert ("e" * 40) not in out and ("f" * 40) not in out

    def test_key_add_from_stdin(self, monkeypatch, capsys):
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO("sk-proj-" + "c" * 40 + "\n"))
        assert main(["key", "add"]) == 0
        assert "openai" in capsys.readouterr().out.lower()

    def test_no_command_prints_help(self, capsys):
        assert main([]) == 0
        out = capsys.readouterr().out
        assert "not running" in out and "usage:" in out

    def test_install_reports_failure_cleanly(self, monkeypatch, capsys):
        def boom(**k):
            raise RuntimeError("launchctl unavailable")
        monkeypatch.setattr("otto.core.launchd.install", boom)
        assert main(["install"]) == 1
        assert "Install failed" in capsys.readouterr().out


class TestPermissionsCommand:
    def test_not_running(self, capsys):
        assert cli.cmd_permissions(open_settings=False) == 1
        assert "not running" in capsys.readouterr().out

    def _fake_api(self, statuses, screenshots=None):
        def api(path, *, method="GET", timeout=3.0, data=None):
            if path == "/api/status":
                return {"running": True, "refreshing": False, "source_status": statuses,
                        "screenshots": screenshots if screenshots is not None else {"enabled": True, "granted": True}}
            return {"status": "refreshing"}
        return api

    def test_missing_screen_recording_gets_its_own_step(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_api", self._fake_api(
            [{"source": "slack", "ok": True, "error": "", "items": 3}],
            screenshots={"enabled": True, "granted": False, "paused_seconds": 1700, "reason": "Screen Recording is not granted"},
        ))
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        opened = []
        monkeypatch.setattr(cli.subprocess, "run", lambda args, **k: opened.append(args))
        assert cli.cmd_permissions(wait_seconds=5, open_settings=True) == 2
        out = capsys.readouterr().out
        assert "readable: slack" in out and "look good" not in out
        assert "thumbnails: Screen Recording" in out
        assert "1. Screen Recording:" in out and "never focused" in out
        assert "Accessibility:" not in out                       # reading works; only the picture is missing
        assert any("Privacy_ScreenCapture" in " ".join(a) for a in opened)
        assert not any("Privacy_Accessibility" in " ".join(a) for a in opened)

    def test_thumbnails_off_by_choice_are_not_a_problem(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_api", self._fake_api(
            [{"source": "slack", "ok": True, "error": "", "items": 3}],
            screenshots={"enabled": False, "granted": None},
        ))
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        assert cli.cmd_permissions(wait_seconds=5, open_settings=False) == 0
        assert "look good" in capsys.readouterr().out

    def test_all_good(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_api", self._fake_api([
            {"source": "slack", "ok": True, "error": "", "items": 3},
            {"source": "gmail", "ok": False, "error": "app not open", "items": 0},
        ]))
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        assert cli.cmd_permissions(wait_seconds=5, open_settings=False) == 0
        out = capsys.readouterr().out
        assert "readable: slack" in out and "not open: gmail" in out and "look good" in out

    def test_blocked_gives_exact_guidance(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_api", self._fake_api([
            {"source": "slack", "ok": False, "error": "timed out (usually a pending macOS permission prompt)", "items": 0},
        ]))
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        opened = []
        monkeypatch.setattr(cli.subprocess, "run", lambda args, **k: opened.append(args))
        assert cli.cmd_permissions(wait_seconds=5, open_settings=True) == 2
        out = capsys.readouterr().out
        assert "slack: timed out" in out
        assert "Accessibility" in out and "otto restart" in out and "otto test" not in out
        assert any("Privacy_Accessibility" in " ".join(a) for a in opened)
        assert any(a[:2] == ["open", "-R"] for a in opened)


class TestPortDiscovery:
    """The port is a config.toml setting; every command must find the engine that is actually up."""

    def test_default_when_nothing_is_known(self, monkeypatch):
        monkeypatch.delenv("OTTO_PORT", raising=False)
        assert cli._port() == paths.DEFAULT_PORT

    def test_env_wins(self, monkeypatch):
        monkeypatch.setenv("OTTO_PORT", "7099")
        assert cli._port() == 7099

    def test_running_engine_beats_the_config_file(self, monkeypatch):
        import json
        from otto.config import ensure_config_file
        monkeypatch.delenv("OTTO_PORT", raising=False)
        path = ensure_config_file()
        path.write_text(path.read_text().replace("port = 7077", "port = 7100"))
        assert cli._port() == 7100                                  # nothing running: what the file says
        paths.engine_info_file().write_text(json.dumps({"pid": os.getpid(), "port": 7077, "started": 1.0}))
        assert cli._port() == 7077                                  # a live engine: talk to it, not to the plan
        paths.engine_info_file().write_text(json.dumps({"pid": 999999, "port": 7077}))
        assert cli._port() == 7100                                  # stale file (dead pid) is ignored
        paths.engine_info_file().write_text("{not json")
        assert cli._port() == 7100

    def test_engine_writes_and_removes_the_info_file(self):
        import json
        from otto.core.engine import OttoEngine as Engine
        eng = Engine.__new__(Engine)
        eng.port = 7123
        Engine._write_pid(eng)
        info = json.loads(paths.engine_info_file().read_text())
        assert info["pid"] == os.getpid() and info["port"] == 7123
        Engine._remove_pid(eng)
        assert not paths.engine_info_file().exists() and not paths.pid_file().exists()


class TestMenuBarPlumbing:
    """`--json` modes: the secret travels on stdin, stdout is machine-readable, nothing else is printed."""

    def test_key_add_json_stores_and_masks(self, monkeypatch, capsys):
        import io
        import json
        restarted = []
        monkeypatch.setattr(cli, "_restart_if_running", lambda quiet=False: restarted.append(quiet) or False)
        secret = "sk-or-v1-" + "g" * 40
        monkeypatch.setattr("sys.stdin", io.StringIO(secret + "\n"))
        assert main(["key", "add", "--json"]) == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1
        reply = json.loads(out[0])
        assert reply["ok"] is True and reply["kind"] == "openrouter" and secret not in out[0]
        assert reply["masked"].startswith("sk-or-v1-g") and paths.key_file().exists()
        assert restarted == [True]

    def test_key_add_json_refuses_slack_tokens_and_junk(self, monkeypatch, capsys):
        import io
        import json
        monkeypatch.setattr("sys.stdin", io.StringIO("xoxp-" + "1" * 11 + "-" + "2" * 11 + "-" + "a" * 24 + "\n"))
        assert main(["key", "add", "--json"]) == 1
        assert "Connect Slack" in json.loads(capsys.readouterr().out)["error"]
        monkeypatch.setattr("sys.stdin", io.StringIO("hello\n"))
        assert main(["key", "add", "--json"]) == 1
        assert json.loads(capsys.readouterr().out)["ok"] is False
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        assert main(["key", "add", "--json"]) == 1
        assert json.loads(capsys.readouterr().out)["error"] == "cancelled"
        assert not paths.key_file().exists()

    def test_slack_connect_json_protocol(self, monkeypatch, capsys):
        import io
        import json
        from otto.core import slack_connect

        class V:
            ok, error, user, team, kind, missing = True, "", "alice", "acme", "user", []

            def coverage(self):
                return "channels, DMs"

        seen = {}

        def fake_connect(token=None, **kw):
            seen["token"] = token
            kw["out"]("  ⚠ bot tokens only see channels the bot was invited to")
            return 0, V()

        monkeypatch.setattr(slack_connect, "connect", fake_connect)
        monkeypatch.setattr(cli, "_restart_if_running", lambda quiet=False: True)
        monkeypatch.setattr("sys.stdin", io.StringIO("xoxp-secret-token\n"))
        assert main(["slack", "connect", "--json"]) == 0
        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        assert [ln["step"] for ln in lines] == ["url", "done"]
        assert lines[0]["url"].startswith("https://api.slack.com/apps?")
        assert lines[1] == {"step": "done", "ok": True, "identity": "alice at acme", "coverage": "channels, DMs",
                            "warnings": ["bot tokens only see channels the bot was invited to"], "restarted": True}
        assert seen["token"] == "xoxp-secret-token"

    def test_slack_connect_json_reports_failures(self, monkeypatch, capsys):
        import io
        import json
        from otto.core import slack_connect

        class Bad:
            ok, error, user, team = False, "invalid_auth — Slack rejected the token", "", ""

        def fake_connect(token=None, **kw):
            kw["out"]("  ✗ invalid_auth — Slack rejected the token")
            return 1, Bad()

        monkeypatch.setattr(slack_connect, "connect", fake_connect)
        monkeypatch.setattr("sys.stdin", io.StringIO("xoxp-bad\n"))
        assert main(["slack", "connect", "--json"]) == 1
        done = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()][-1]
        assert done == {"step": "done", "ok": False, "error": "invalid_auth — Slack rejected the token"}
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        assert main(["slack", "connect", "--json"]) == 1
        done = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()][-1]
        assert done["error"] == "cancelled"

    def test_manifest_flag_prints_the_read_only_manifest(self, capsys):
        assert main(["slack", "connect", "--manifest"]) == 0
        out = capsys.readouterr().out
        assert "channels:history" in out and "chat:write" not in out

    def test_key_change_restarts_a_running_engine(self, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(cli, "_api", lambda *a, **k: {"running": True})
        monkeypatch.setattr(cli, "cmd_restart", lambda verbose: calls.append("restart") or 0)
        assert main(["key", "add", "sk-or-v1-" + "h" * 40]) == 0
        assert calls == ["restart"] and "Restarting Otto" in capsys.readouterr().out
        assert main(["key", "remove", "sk-or-v1-h"]) == 0
        assert calls == ["restart", "restart"]


class TestFreshCheckout:
    """`python3 -m otto` on a Python that lacks Otto's dependencies (a checkout
    before ./otto setup) says what to do; anything else still raises."""

    def test_missing_dependency_is_a_sentence_not_a_traceback(self, monkeypatch, capsys):
        import otto.__main__ as entry

        def boom():
            raise ModuleNotFoundError("No module named 'aiosqlite'", name="aiosqlite")

        monkeypatch.setattr(cli, "main", boom)
        assert entry.run() == 1
        err = capsys.readouterr().err
        assert "'aiosqlite' is not installed" in err and "./otto setup" in err and "pip install -e ." in err

    def test_other_import_errors_still_raise(self, monkeypatch):
        import otto.__main__ as entry

        def boom():
            raise ModuleNotFoundError("No module named 'nosuchthing'", name="nosuchthing")

        monkeypatch.setattr(cli, "main", boom)
        with pytest.raises(ModuleNotFoundError):
            entry.run()

    def test_runs_the_cli(self, monkeypatch):
        import otto.__main__ as entry
        monkeypatch.setattr(cli, "main", lambda: 7)
        assert entry.run() == 7
