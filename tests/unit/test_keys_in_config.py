"""
Keys in config.toml (``[keys]``): discovered next to api.key, removable from
there, reported with the right fix, and the in-app editor's save/load plumbing.

Every key here is a made-up string of the right shape; the data dir is the
per-test temp directory (conftest).
"""
from __future__ import annotations

import json
import os
import stat

from otto import config as C
from otto import paths
from otto.core import health
from otto.utils import keys as K

FAKE_SLACK = "xoxp-" + "1" * 11 + "-" + "2" * 12 + "-" + "3" * 12 + "-" + "a" * 32
FAKE_OR = "sk-or-v1-" + "c" * 60
FAKE_OPENAI = "sk-proj-" + "d" * 40


def _write_config(text: str):
    path = paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    C._STATUS_CACHE.update(key=None, value=None)
    return path


class TestDiscovery:
    def test_keys_section_is_read_after_env_before_api_key(self, monkeypatch):
        _write_config(f'[keys]\nslack = "{FAKE_SLACK}"\nmodel = ["{FAKE_OR}"]\n')
        K.add_key(FAKE_OPENAI)
        found = K.discover_keys()
        assert [k.key for k in found] == [FAKE_SLACK, FAKE_OR, FAKE_OPENAI]
        assert found[0].source.startswith("config:") and found[0].source.endswith("config.toml")
        assert found[2].source.startswith("file:")
        monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_OR)
        assert [k.source[:3] for k in K.discover_keys()] == ["env", "con", "fil"]     # env wins, no duplicate
        assert K.slack_token().key == FAKE_SLACK and K.slack_token().source.startswith("config:")

    def test_template_defaults_and_odd_shapes_are_tolerated(self):
        assert K.config_keys() == ("", [])                             # no file yet
        C.ensure_config_file()
        assert K.config_keys() == ("", [])                             # the template's empty defaults
        _write_config('[keys]\nmodel = "sk-or-v1-single"\nslack = 42\n')
        assert K.config_keys() == ("", ["sk-or-v1-single"])            # a lone string is accepted, a number is not a token
        _write_config("[keys\nbroken = \n")
        assert K.config_keys() == ("", [])                             # a broken file adds nothing (config_status reports it)
        _write_config("[engine]\nport = 7078\n")
        assert K.config_keys() == ("", [])

    def test_loose_mode_is_warned_once_only_when_keys_are_present(self, caplog):
        path = _write_config(f'[keys]\nmodel = ["{FAKE_OR}"]\n')
        os.chmod(path, 0o644)
        K._WARNED_LOOSE.clear()
        with caplog.at_level("WARNING", logger="otto.utils.keys"):
            K.config_keys()
            K.config_keys()
        assert sum("readable by other users" in r.message for r in caplog.records) == 1
        caplog.clear()
        _write_config("[keys]\nmodel = []\n")
        os.chmod(paths.config_file(), 0o644)
        K._WARNED_LOOSE.clear()
        with caplog.at_level("WARNING", logger="otto.utils.keys"):
            K.config_keys()
        assert not any("readable by other users" in r.message for r in caplog.records)

    def test_add_key_reports_already_stored_for_a_config_key(self):
        _write_config(f'[keys]\nmodel = ["{FAKE_OR}"]\n')
        assert any(k.key == FAKE_OR for k in K.discover_keys())


class TestRemoval:
    def test_remove_key_blanks_the_slack_line_and_drops_from_model(self):
        path = _write_config(
            "# header\n[keys]\n"
            f'slack = "{FAKE_SLACK}"   # read-only token\n'
            f'model = ["{FAKE_OR}", "{FAKE_OPENAI}"]  # model keys\n'
            "keychain = false\n[engine]\nport = 7077\n"
        )
        assert K.remove_key(FAKE_SLACK[:12]) == 1
        text = path.read_text()
        assert 'slack = ""   # read-only token' in text
        assert K.remove_key(FAKE_OPENAI) == 1
        text = path.read_text()
        assert f'model = ["{FAKE_OR}"]  # model keys' in text
        assert "# header" in text and "port = 7077" in text and "keychain = false" in text
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert [k.key for k in K.discover_keys()] == [FAKE_OR]
        assert K.remove_key("nothing-like-this") == 0

    def test_remove_key_spans_every_store(self):
        _write_config(f'[keys]\nmodel = ["{FAKE_OR}"]\n')
        K.add_key(FAKE_OR)                    # same key also in api.key
        assert K.remove_key(FAKE_OR) == 2
        assert K.discover_keys() == []

    def test_multi_line_array_is_left_for_a_person(self, caplog):
        path = _write_config(f'[keys]\nmodel = [\n  "{FAKE_OR}",\n]\n')
        before = path.read_text()
        with caplog.at_level("WARNING", logger="otto.utils.keys"):
            assert K.remove_key(FAKE_OR) == 0
        assert path.read_text() == before
        assert any("by hand" in r.message for r in caplog.records)


class TestReplaceSetting:
    def test_only_the_named_line_changes(self, tmp_path):
        p = tmp_path / "c.toml"
        p.write_text('[user]\nname = "x"\n[keys]\n# note\nslack = "old"  # keep me\nmodel = []\n[engine]\nslack = "not this one"\n')
        assert C.replace_setting("keys", "slack", "", p) is True
        assert p.read_text() == '[user]\nname = "x"\n[keys]\n# note\nslack = ""  # keep me\nmodel = []\n[engine]\nslack = "not this one"\n'
        assert C.replace_setting("keys", "model", ["a", "b"], p) is True
        assert 'model = ["a", "b"]' in p.read_text()
        assert C.replace_setting("keys", "missing", 1, p) is False
        assert C.replace_setting("nope", "slack", "", p) is False
        assert C.replace_setting("keys", "slack", "", tmp_path / "absent.toml") is False

    def test_broken_or_multiline_files_are_untouched(self, tmp_path):
        p = tmp_path / "c.toml"
        p.write_text("[keys\nslack = \"x\"\n")
        assert C.replace_setting("keys", "slack", "", p) is False
        p.write_text('[keys]\nmodel = [\n "a",\n]\n')
        assert C.replace_setting("keys", "model", [], p) is False
        assert C._split_inline_value('"abc"  # c') == ('"abc"', '  # c')
        assert C._split_inline_value('["a", "b"] # c') == ('["a", "b"]', ' # c')
        assert C._split_inline_value('"esc\\"aped"') == ('"esc\\"aped"', "")
        assert C._split_inline_value("true # c") == ("true", " # c")
        assert C._split_inline_value('"unterminated') is None
        assert C._split_inline_value("[1, 2") is None
        assert C._split_inline_value("") is None


class TestEditorPlumbing:
    def test_load_creates_the_file_and_returns_its_text(self):
        result = C.load_config_text()
        assert result["ok"] and result["text"].startswith("# Otto settings.") and result["error"] == ""
        assert result["path"] == str(paths.config_file())
        assert stat.S_IMODE(paths.config_file().stat().st_mode) == 0o600

    def test_save_writes_only_what_parses(self):
        C.ensure_config_file()
        bad = C.save_config_text("[engine]\nrefresh_seconds = \n")
        assert bad["ok"] is False and bad["saved"] is False and bad["error"].startswith("line 2")
        assert C.ConfigManager().get("engine.refresh_seconds") == 60         # untouched
        good = C.save_config_text('[engine]\nrefresh_seconds = 45\nprot = 1\n[keys]\nmodel = "oops"')
        assert good["ok"] and good["saved"]
        assert good["unknown_keys"] == ["engine.prot"]
        assert "keys.model should be a list" in good["error"]
        text = paths.config_file().read_text()
        assert text.endswith("\n") and "refresh_seconds = 45" in text
        assert stat.S_IMODE(paths.config_file().stat().st_mode) == 0o600
        assert C.config_status()["unknown_keys"] == ["engine.prot"]          # status cache was reset

    OLD_FILE = (
        "# my notes\n"
        "log_level = \"DEBUG\"\n"
        "\n"
        "[user]\n"
        "name = \"Sam\"   # keep\n"
        "aliases = [\"sam\"]\n"
        "\n"
        "[keys]\n"
        "# from before keys lived here\n"
        "keychain = true\n"
        "\n\n"
        "[llm]\n"
        "daily_token_limit = 5\n"
    )

    def test_load_completes_a_file_written_by_an_older_otto(self):
        tomllib = C.tomllib
        path = _write_config(self.OLD_FILE)
        os.chmod(path, 0o644)
        result = C.load_config_text()
        text = result["text"]
        assert result["ok"] and result["error"] == "" and result["unknown_keys"] == []
        assert path.read_text() == text and stat.S_IMODE(path.stat().st_mode) == 0o600
        before, after = tomllib.loads(self.OLD_FILE), tomllib.loads(text)
        for section, values in before.items():                         # nothing the user wrote changed
            if isinstance(values, dict):
                assert all(after[section][k] == v for k, v in values.items())
            else:
                assert after[section] == values
        keys_block = text[text.index("[keys]"):text.index("[llm]")]
        assert "# from before keys lived here\nkeychain = true\n" in keys_block   # comment and value kept, in place
        assert 'slack = ""' in keys_block and "model = []" in keys_block         # the new settings, explained, in the section
        assert "Connect Slack" in keys_block
        assert 'name = "Sam"   # keep' in text
        assert "debug_mode = false" in text.split("[user]")[0]                  # top-level setting before the first section
        assert "[engine]" in text and "[notifications]" in text and "[links]" in text and "[debug]" in text
        assert set(after) == {"log_level", "debug_mode", "user", "engine", "notifications", "keys", "llm", "links", "debug"}
        assert after["keys"]["keychain"] is True and after["llm"]["daily_token_limit"] == 5
        assert C.complete_config_text(text) == (text, [])                      # idempotent
        assert C.load_config_text()["text"] == text

    def test_completion_retires_an_intro_the_new_lines_would_contradict(self):
        old = (
            "[keys]\n"
            "# Model keys and the Slack token are not in this file: they live in api.key next to it\n"
            "# (menu bar → Add a Model Key… / Connect Slack…, or `otto key add`).\n"
            "keychain = true                   # store new keys in the macOS Keychain instead of api.key\n"
        )
        text, added = C.complete_config_text(old)
        assert "keys.slack" in added and "keys.model" in added
        assert "not in this file" not in text
        assert "# Model keys and the Slack token. Paste them here" in text
        assert "keychain = true                   # store new keys" in text            # the user's line as it was
        # A reworded intro is the user's own text: left alone.
        mine = "[keys]\n# my own note about keys\nkeychain = false\n"
        text, _ = C.complete_config_text(mine)
        assert "# my own note about keys\n" in text and 'slack = ""' in text
        # Already complete but still carrying the old intro: only the intro changes.
        stale = old + 'slack = ""\nmodel = []\n'
        text, added = C.complete_config_text(stale)
        assert not any(k.startswith("keys.") for k in added)
        assert "not in this file" not in text and text.count('slack = ""') == 1
        full = C.render_config().replace("# Model keys and the Slack token. Paste them here, or add them from the menu bar\n"
                                         "# (Connect Slack… / Add a Model Key…), which keeps them in api.key next to this file;\n"
                                         "# Otto reads both. This file is readable by you alone (mode 600). Without a Slack\n"
                                         "# token Otto reads the Slack window on screen; with one it reads every conversation.\n",
                                         "\n".join(C._RETIRED_INTROS["keys"]) + "\n")
        assert "not in this file" in full                                             # the fixture is what it claims
        text, added = C.complete_config_text(full)
        assert added == [] and text == C.render_config()                              # only the intro changed

    def test_completion_leaves_odd_files_alone(self):
        assert C.complete_config_text("[keys\n") == ("[keys\n", [])                        # does not parse: untouched
        full = C.render_config()
        assert C.complete_config_text(full) == (full, [])                                  # nothing missing
        dotted = "keys.keychain = true\n"                                                  # declared without a header: skipped
        text, added = C.complete_config_text(dotted)
        assert "keys.slack" not in added and "[keys]" not in text and "[engine]" in text

    def test_check_without_writing(self):
        assert C.check_config_text("[engine]\nport = 7078\n") == {"ok": True, "error": "", "unknown_keys": []}
        verdict = C.check_config_text("top = 1\n[user]\nname = 3\n[what]\nx = 1\n")
        assert verdict["ok"] and "user.name should be text in quotes" in verdict["error"]
        assert verdict["unknown_keys"] == ["top", "what.x"]
        assert C.check_config_text("[[[")["ok"] is False

    def test_cli_json_round_trip(self, monkeypatch, capsys):
        import io
        from otto import cli
        assert cli.main(["config", "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] and "[keys]" in out["text"]
        edited = out["text"].replace("refresh_seconds = 60", "refresh_seconds = 90")
        monkeypatch.setattr("sys.stdin", io.StringIO(edited))
        assert cli.main(["config", "--save", "--json"]) == 0
        saved = json.loads(capsys.readouterr().out)
        assert saved["ok"] and saved["saved"] and saved["error"] == "" and saved["applied"] is False   # no engine on 7999
        assert C.ConfigManager().get("engine.refresh_seconds") == 90
        monkeypatch.setattr("sys.stdin", io.StringIO("[engine\n"))
        assert cli.main(["config", "--save", "--json"]) == 1
        failed = json.loads(capsys.readouterr().out)
        assert failed["ok"] is False and failed["error"].startswith("line")
        assert C.ConfigManager().get("engine.refresh_seconds") == 90                # still the last good file


class TestHealthKnowsWhereAKeyLives:
    def test_rejected_config_key_points_at_the_editor(self):
        _write_config(f'[keys]\nmodel = ["{FAKE_OPENAI}"]\n')
        out = health.llm_problems({"providers": [{"name": "openai", "state": "API key rejected (HTTP 401)"}]})
        assert out[0]["action"] == "config" and "config.toml" in out[0]["detail"]

    def test_rejected_file_key_can_be_removed(self):
        K.add_key(FAKE_OPENAI)
        out = health.llm_problems({"providers": [{"name": "openai", "state": "API key rejected (HTTP 401)"}]})
        assert out[0]["action"] == f"key_remove:{FAKE_OPENAI[:10]}"

    def test_health_never_logs_while_looking_up_keys(self, caplog):
        """The menu bar polls status every 10 s; provider detection must stay silent."""
        K.add_key("apk_user_" + "z" * 30)
        with caplog.at_level("INFO"):
            health.llm_problems({"providers": [{"name": "devin", "state": "API key rejected (HTTP 401)"}]})
        assert not any("Devin API key loaded" in r.message for r in caplog.records)

    def test_provider_name_for_key(self):
        from otto.llm.gateway import provider_name_for_key
        assert provider_name_for_key(FAKE_OR) == "openrouter"
        assert provider_name_for_key(FAKE_OPENAI) == "openai"
        assert provider_name_for_key("sk-ant-" + "q" * 30) == "anthropic"
        assert provider_name_for_key("AIza" + "q" * 30) == "gemini"
        assert provider_name_for_key("apk_user_" + "q" * 30) == "devin"
        assert provider_name_for_key(FAKE_SLACK) == "" and provider_name_for_key("") == "" and provider_name_for_key("nope") == ""


class TestGatewayFollowsTheKeyStore:
    def test_sync_adds_and_drops_providers(self):
        from otto.llm.gateway import LLMGateway
        gw = LLMGateway()
        _write_config(f'[keys]\nmodel = ["{FAKE_OR}"]\n')
        assert gw.setup_from_discovered_keys() is True
        assert [p.name for p in gw._providers] == ["openrouter"]
        _write_config(f'[keys]\nmodel = ["{FAKE_OPENAI}"]\n')
        assert gw.sync_discovered_keys() == (1, 1)
        assert [p.name for p in gw._providers] == ["openai"]
        _write_config("[keys]\nmodel = []\n")
        assert gw.sync_discovered_keys() == (0, 1)
        assert gw._providers == []
        assert gw.sync_discovered_keys() == (0, 0)

    def test_engine_reload_syncs_keys_when_the_file_changes(self, monkeypatch):
        from otto.core.engine import OttoEngine

        class FakeLLM:
            calls = 0

            def sync_discovered_keys(self):
                FakeLLM.calls += 1
                return (1, 0)

        engine = OttoEngine(port=7999, refresh_seconds=60)
        engine._llm = FakeLLM()
        engine.reload_config()                        # first sight of the file: written, no sync yet
        assert FakeLLM.calls == 0
        path = paths.config_file()
        path.write_text(path.read_text().replace("model = []", f'model = ["{FAKE_OR}"]'))
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
        engine.reload_config()
        assert FakeLLM.calls == 1
        engine.reload_config()                        # unchanged file: nothing to do
        assert FakeLLM.calls == 1
