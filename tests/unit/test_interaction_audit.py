"""
Every user-facing surface behaves the way its words promise.

The fixes covered here came out of clicking through the product: a header
that lagged behind a dismissed card, a Clear confirmation that expired before
a person could read it, "Tomorrow" meaning 16 hours, banners with a wrong
fallback path, config sections nobody read, and LLM prose about "the user".
"""
from __future__ import annotations

import json
import logging
import subprocess

import pytest

from otto import cli, paths
from otto.cli import build_parser, cmd_uninstall
from otto.core import launchd
from otto.core.notify import Notifier, policy_from_config
from otto.intelligence.classifier import to_second_person
from otto.llm.gateway import LLMGateway
from otto.web.render import JS, render_item


@pytest.fixture(autouse=True)
def _no_running_engine(monkeypatch):
    monkeypatch.setattr(cli, "_api", lambda *a, **k: None)


def _item(**over):
    base = {"id": "abc123", "text": "Deploy freeze starts Friday", "summary": "Deploy freeze starts Friday.",
            "urgency": "high", "urgency_score": 0.8, "source_url": "https://acme.slack.com/archives/C1/p1",
            "why": [], "action_items": []}
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Web page
# ---------------------------------------------------------------------------

class TestWebInteractions:
    def test_card_title_is_a_keyboard_reachable_toggle(self):
        html = render_item(_item())
        assert '<button type="button" class="title" data-action="toggle" aria-expanded="false">' in html
        assert 'aria-label="Dismiss"' in html
        assert 'data-hours="tomorrow"' in html          # not a fixed 16 hours

    def test_tomorrow_means_eight_in_the_morning_and_clear_asks_nothing(self):
        assert "d.setHours(8,0,0,0)" in JS
        # Clear is one click: no arming, no "click again", no confirm().
        assert "Click again" not in JS and "armed" not in JS and "confirm(" not in JS
        assert "post('/api/clear').then(function(){ refreshView(true); })" in JS
        # The Worth knowing drawer's Clear is the same one click, scoped to the notes and radar rows;
        # whether the drawer is open is the browser's memory, never the engine's.
        assert "post('/api/clear',{scope:'notes'}).then(function(){ refreshView(true); })" in JS
        assert "localStorage.setItem('otto.wk'" in JS and "localStorage.getItem('otto.wk')" in JS

    def test_every_removal_re_reads_the_page_so_the_header_follows(self):
        # leave() must refresh unconditionally, not only when the last card goes.
        assert "el.remove(); refreshView(true);" in JS
        assert "if(!r.ok) throw new Error" in JS           # a failed POST keeps the card
        assert ".catch(failed)" in JS
        assert "foldOpen" in JS                            # "Also noticed" stays open across swaps

    def test_checklist_ticks_survive_the_live_re_render(self):
        assert "localStorage.setItem(doneKey(e.target),'1')" in JS
        assert JS.count("restoreDone();") == 2             # on load and after every refreshView


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCliSurface:
    def test_directives_live_in_the_config_file_not_a_subcommand(self):
        parser = build_parser()
        for gone in (["directive", "list"], ["briefing"], ["radar"], ["test"], ["doctor"]):
            with pytest.raises(SystemExit):
                parser.parse_args(gone)
        from otto.config import ensure_config_file
        text = ensure_config_file().read_text()
        assert "directives" in text and str(paths.data_dir()) in str(paths.history_dir())   # never the real user's


# ---------------------------------------------------------------------------
# Run at login: engine and menu bar
# ---------------------------------------------------------------------------

class TestMenuBarAtLogin:
    def test_menubar_agent_only_when_the_app_is_built(self, monkeypatch, tmp_path):
        verbs = []
        monkeypatch.setattr(launchd, "_launchctl",
                            lambda *a: verbs.append(a[0]) or subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
        monkeypatch.setattr(paths, "menubar_binary", lambda: tmp_path / "missing" / "OttoMenuBar")
        assert launchd.install_menubar() is None
        assert not paths.menubar_agent_plist().exists()

        binary = tmp_path / "Otto.app" / "Contents" / "MacOS" / "OttoMenuBar"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        monkeypatch.setattr(paths, "menubar_binary", lambda: binary)
        plist = launchd.install_menubar(port=7077)
        assert plist == paths.menubar_agent_plist() and plist.exists()
        import plistlib
        data = plistlib.loads(plist.read_bytes())
        assert data["Label"] == "com.otto.menubar"
        assert data["ProgramArguments"] == [str(binary)]
        # A crash is relaunched (the icon comes back); Quit exits 0 and sticks until next login.
        assert data["RunAtLoad"] is True and data["KeepAlive"] == {"SuccessfulExit": False, "Crashed": True}
        assert data["ThrottleInterval"] == 10
        assert data["EnvironmentVariables"]["OTTO_PORT"] == "7077"
        assert launchd.status()["menubar_installed"] is True
        assert "bootstrap" in verbs
        assert launchd.uninstall_menubar() is True
        assert not plist.exists()

    def test_install_waits_for_bootout_before_bootstrapping(self, monkeypatch, tmp_path):
        """bootout returns early; bootstrapping before launchd let go left the engine uninstalled."""
        monkeypatch.setattr(paths, "launch_agent_plist", lambda: tmp_path / "com.otto.engine.plist")
        loaded = {"n": 3}                      # `print` keeps succeeding for a while after bootout
        verbs = []

        def fake(*a):
            verbs.append(a[0])
            if a[0] == "print":
                loaded["n"] -= 1
                return subprocess.CompletedProcess(a, 0 if loaded["n"] >= 0 else 1, stdout="", stderr="")
            return subprocess.CompletedProcess(a, 0, stdout="", stderr="")

        monkeypatch.setattr(launchd, "_launchctl", fake)
        monkeypatch.setattr(launchd.time, "sleep", lambda s: None)
        launchd.install(port=7077)
        assert verbs[0] == "print" and verbs[1] == "bootout"
        assert verbs.count("print") >= 4                          # polled until launchd said "gone"
        assert verbs.index("bootstrap") > verbs.index("bootout")
        assert verbs[-1] == "kickstart"

    def test_uninstall_from_the_menu_keeps_the_menu(self, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(launchd, "uninstall", lambda: True)
        monkeypatch.setattr(launchd, "uninstall_menubar", lambda: calls.append("menubar") or True)
        monkeypatch.setattr(subprocess, "run", lambda args, **k: calls.append(args[0]) or subprocess.CompletedProcess(args, 0))
        assert cmd_uninstall(keep_menubar=True) == 0
        assert calls == []                                  # no pkill, menu bar agent untouched
        assert "keeps running" in capsys.readouterr().out
        assert cmd_uninstall() == 0
        assert calls == ["menubar", "pkill"]


# ---------------------------------------------------------------------------
# Banners
# ---------------------------------------------------------------------------

class TestBannerFallback:
    def test_fallback_uses_the_built_app_else_display_notification(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "menubar_binary", lambda: tmp_path / "nope")
        cmd = Notifier.fallback_command("Otto · #eng", 'alice: "ship it"')
        assert cmd[0] == "osascript" and cmd[1] == "-e"
        assert cmd[2] == 'display notification "alice: \\"ship it\\"" with title "Otto · #eng"'

        binary = tmp_path / "OttoMenuBar"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        shot = tmp_path / "shot.png"
        shot.write_bytes(b"png")
        monkeypatch.setattr(paths, "menubar_binary", lambda: binary)
        assert Notifier.fallback_command("t", "b", str(shot)) == [str(binary), "--notify", "t", "b", str(shot)]
        assert Notifier.fallback_command("t", "b", str(tmp_path / "gone.png")) == [str(binary), "--notify", "t", "b"]

    def test_notification_policy_reads_config_toml(self):
        paths.ensure_data_dir()
        (paths.data_dir() / "config.toml").write_text(
            '[notifications]\nmax_per_hour = 2\nquiet_hours_start = "22:00"\nurgency_threshold = 0.9\n'
        )
        policy = policy_from_config()
        assert policy.max_per_hour == 2
        assert policy.quiet_hours_start.hour == 22
        assert policy.min_urgency == 0.9


# ---------------------------------------------------------------------------
# Config that is actually read
# ---------------------------------------------------------------------------

class TestConfigIsHonest:
    def test_no_decorative_sections(self):
        from otto.config import ConfigManager
        keys = set(ConfigManager().get_effective())
        assert not any(k.startswith(("polling.", "storage.")) for k in keys)
        assert "llm.primary_provider" not in keys
        assert {"engine.port", "engine.refresh_seconds", "engine.screenshots", "engine.lookback_hours", "engine.recall",
                "notifications.max_per_hour", "llm.daily_token_limit", "llm.daily_cost_limit_usd", "links.fetch",
                "user.name", "user.aliases", "user.role", "user.focus", "debug.dump_extracts",
                "debug_mode", "log_level"} <= keys

    def test_llm_budget_comes_from_config_and_cost_is_enforced(self):
        paths.ensure_data_dir()
        (paths.data_dir() / "config.toml").write_text("[llm]\ndaily_token_limit = 1000\ndaily_cost_limit_usd = 0.5\n")
        from otto.web.collect import build_llm_gateway
        gw = build_llm_gateway()
        assert gw._daily_token_limit == 1000 and gw._daily_cost_limit == 0.5
        assert gw._check_budget(10) is True
        gw._usage.total_cost_usd = 0.51
        assert gw._check_budget(10) is False                # cost limit used to be decorative
        fresh = LLMGateway(daily_token_limit=100, daily_cost_limit_usd=5)
        assert fresh._check_budget(200) is False

    def test_log_level_from_config(self):
        from otto.utils.logging import _configured_level
        paths.ensure_data_dir()
        (paths.data_dir() / "config.toml").write_text('log_level = "WARNING"\n')
        assert _configured_level() == logging.WARNING
        (paths.data_dir() / "config.toml").write_text("debug_mode = true\n")
        assert _configured_level() == logging.DEBUG


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------

class TestContextLeavesTheMachineRedacted:
    def test_history_and_memory_context_are_redacted_before_the_prompt(self, monkeypatch):
        """The conversation text was always redacted; remembered context was not."""
        from otto.intelligence.classifier import ConversationClassifier
        from otto.web import collect as collect_mod
        from tests.unit.test_collect import FakeAdapter, _event

        seen = {}

        async def spy(self, conv, events, recent_context="", **kwargs):
            seen["context"] = recent_context
            return conv

        monkeypatch.setattr(ConversationClassifier, "classify_llm", spy)
        monkeypatch.setattr("otto.intelligence.history.get_recent_context",
                            lambda **k: "[Sep 10] #eng · alice: ping carol@example.com about the 555-123-4567 line")

        class FakeLLM:
            _providers: list = []

            def ensure_chat_provider(self):
                return True

            def provider_status(self):
                return []

        events = [_event("Can you review the invoice batch before Friday? Finance is waiting on it")]
        collect_mod.collect_briefing_data(adapters=[FakeAdapter(events)], llm=FakeLLM())
        ctx = seen["context"]
        assert "alice" in ctx and "invoice" not in ctx            # the history line got through…
        assert "carol@example.com" not in ctx and "555-123-4567" not in ctx   # …without the PII


class TestSecondPerson:
    @pytest.mark.parametrize("text, expected", [
        ("Given the user's directive to prioritize payments bugs.", "Given your directive to prioritize payments bugs."),
        ("The user is mentioned and the user has asked twice.", "You are mentioned and you have asked twice."),
        ("The user needs to review this; this user watches the api project.",
         "You need to review this; you watch the api project."),
        ("the user prioritizes payments bugs", "you prioritize payments bugs"),
        ("Flag this for the user before Friday.", "Flag this for you before Friday."),
        ("Users of the API are affected.", "Users of the API are affected."),
        ("", ""),
    ])
    def test_rewrites(self, text, expected):
        assert to_second_person(text) == expected

    def test_llm_output_reaches_the_reader_in_second_person(self):
        import asyncio
        from datetime import datetime, timezone

        from otto.core.event_bus import EventBus
        from otto.intelligence.classifier import ConversationClassifier
        from otto.storage.models import Conversation, Domain, NormalizedEvent, SourceType

        class _LLM:
            async def complete(self, **kwargs):
                class R:
                    content = json.dumps({
                        "urgency": 0.8, "importance": 0.8, "opportunity_score": 0.0, "domain": "work",
                        "action_required": True, "action_summary": None,
                        "relevance_explanation": "The user's directive matches.",
                        "for_you": "The user is asked to review the report.",
                        "summary": "Scan found issues. The user should look.",
                        "topics": ["security"], "opportunity_type": None, "opportunity_description": None,
                        "ai_analysis": "This matters because the user owns the api project.",
                    })
                return R()

        conv = Conversation(source=SourceType.SLACK, account_id="acme", thread_id="t1", subject="#security-alerts",
                            summary="", domain=Domain.WORK, relevance_explanation="")
        event = NormalizedEvent(source=SourceType.SLACK, account_id="acme", source_id="m1", source_url="",
                                timestamp=datetime.now(timezone.utc), title="#security-alerts",
                                plain_text_extract="scanner: 2 high findings in api", content_hash="h1",
                                content_language="en", is_auto_generated=True, sender="scanner")
        clf = ConversationClassifier(event_bus=EventBus(), llm_gateway=_LLM())
        out = asyncio.run(clf.classify_llm(conv, [event]))
        for text in (out.relevance_explanation, out.for_you, out.summary, out.ai_analysis):
            assert "the user" not in text.lower(), text
        assert out.for_you == "You are asked to review the report."
        assert out.ai_analysis == "This matters because you own the api project."
