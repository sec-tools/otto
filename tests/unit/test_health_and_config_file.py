"""
core/health.py — the "needs a look" list every surface shows — and the
config.toml that is written once with every setting explained and re-read
by the running engine.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from otto import config as C
from otto.core import health


# ---------------------------------------------------------------------------
# health.problems
# ---------------------------------------------------------------------------

class TestProblems:
    def test_nothing_wrong_is_an_empty_list(self):
        data = {"source_status": [{"source": "slack", "ok": True}, {"source": "gmail", "ok": False, "error": "app not open"}],
                "llm": {"providers": [{"name": "openrouter", "state": "ok"}]},
                "screenshots": {"enabled": True, "granted": True}}
        assert health.problems(data, config={"exists": True, "error": "", "unknown_keys": []}, menubar_installed=False) == []

    def test_each_kind_has_a_title_and_an_action(self):
        data = {
            "source_status": [
                {"source": "slack", "ok": False, "error": "needs Accessibility permission (System Settings)"},
                {"source": "slack api", "ok": False, "error": "Slack token rejected (token_revoked)"},
            ],
            "llm": {"providers": [{"name": "openai", "state": "API key rejected (HTTP 401)"},
                                  {"name": "openrouter", "state": "ok"}]},
            "screenshots": {"enabled": True, "granted": False},
        }
        cfg = {"exists": True, "error": "line 3: Expected '='", "unknown_keys": ["engine.prot"]}
        got = health.problems(data, config=cfg, client_seen_seconds=None, menubar_installed=True)
        by_key = {p["key"]: p for p in got}
        assert by_key["perm_slack"]["action"] == "permissions" and "Slack needs a macOS permission" in by_key["perm_slack"]["title"]
        assert by_key["slack_token"]["action"] == "slack"
        assert by_key["key_openai"]["title"] == "openai key was rejected"
        assert by_key["screen_recording"]["action"] == "permissions"
        assert by_key["config_error"]["action"] == "config" and "line 3" in by_key["config_error"]["detail"]
        assert "engine.prot" in by_key["config_unknown"]["title"]
        assert by_key["menubar"]["action"] == "menubar"
        for p in got:
            assert p["title"] and set(p) == {"key", "title", "detail", "action"}

    def test_rejected_key_points_at_the_stored_key_by_prefix(self, monkeypatch):
        from otto.utils import keys as K
        monkeypatch.setattr(K, "discover_keys", lambda: [
            K.DiscoveredKey("sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG", "file:x"),
            K.DiscoveredKey("xoxp-1111111111-2222222222-3333333333-abcdefabcdefabcdefabcdef", "file:x"),
        ])
        got = health.llm_problems({"providers": [{"name": "openai", "state": "API key rejected (HTTP 401)"}]})
        assert got and got[0]["action"] == "key_remove:sk-proj-ab"

    def test_menubar_only_matters_when_installed_and_silent(self):
        assert health.menubar_problems(None, installed=False) == []
        assert health.menubar_problems(3.0, installed=True) == []
        assert health.menubar_problems(120.0, installed=True)[0]["action"] == "menubar"

    def test_slack_scope_note_on_a_healthy_reader_is_still_a_problem(self):
        got = health.source_problems([{"source": "slack api", "ok": True, "note": "token lacks scope im:history"}])
        assert got and got[0]["key"] == "slack_scope" and got[0]["action"] == "slack"

    def test_transient_read_errors_are_not_problems(self):
        # A timeout shows in the status pills; it is not something a person fixes.
        assert health.source_problems([{"source": "calendar", "ok": False, "error": "timed out"}]) == []

    def test_status_endpoint_carries_them(self):
        from otto.web.server import build_status
        st = build_status({"sections": [], "source_status": [], "llm": {}, "screenshots": {}})
        assert "config" in st and set(st["config"]) >= {"path", "exists", "error"}


# ---------------------------------------------------------------------------
# config.toml
# ---------------------------------------------------------------------------

class TestConfigFile:
    def test_template_lists_every_setting_and_parses_back_to_the_defaults(self, tmp_path):
        defaults = C._config_to_flat_dict(C.OttoConfig())
        assert C.template_keys() == set(defaults)
        p = C.ensure_config_file(tmp_path / "config.toml")
        text = p.read_text()
        assert text.startswith("# Otto settings.")
        assert "Keys go under\n# [keys]" in text and "readable by you alone" in text   # where secrets may live
        assert 'slack = ""' in text and "model = []" in text
        assert oct(stat.S_IMODE(p.stat().st_mode)) == "0o600"          # it may hold keys
        m = C.ConfigManager(p)
        assert m.file_error == "" and m.unknown_keys == []
        for key, value in defaults.items():
            assert m.get(key) == value, key
        # Every value is commented, and the comments line up.
        assert "refresh_seconds = 60              # how often" in text

    def test_existing_file_is_never_overwritten(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text("[engine]\nrefresh_seconds = 45\n")
        assert C.ensure_config_file(p) == p
        assert p.read_text() == "[engine]\nrefresh_seconds = 45\n"
        assert C.ConfigManager(p).get("engine.refresh_seconds") == 45

    def test_current_values_are_rendered(self):
        text = C.render_config({"user.name": 'Alex "Al" Kim', "user.directives": ["flag payments", "skip release notes"],
                                "engine.refresh_seconds": 30, "engine.screenshots": False})
        assert 'name = "Alex \\"Al\\" Kim"' in text
        assert 'directives = ["flag payments", "skip release notes"]' in text
        assert "refresh_seconds = 30" in text and "screenshots = false" in text

    def test_wrong_types_and_unknown_keys_are_reported_not_fatal(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('[engine]\nrefresh_seconds = "60"\nprot = 7078\nscreenshots = "yes"\n[user]\nfocus = "billing"\n')
        m = C.ConfigManager(p)
        assert m.get("engine.refresh_seconds") == 60                  # default stands
        assert m.get("engine.screenshots") is True
        assert m.unknown_keys == ["engine.prot"]
        assert "engine.refresh_seconds should be a whole number" in m.file_error
        assert "engine.screenshots should be true or false" in m.file_error
        assert 'user.focus should be a list like ["a", "b"]' in m.file_error

    def test_syntax_error_names_the_line_and_keeps_defaults(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text("[engine]\nrefresh_seconds = 45\n[user\nname = 1\n")
        m = C.ConfigManager(p)
        assert m.file_error.startswith("line 3:")
        assert m.get("engine.refresh_seconds") == 60                  # nothing from a file that did not parse
        st = C.config_status(p)
        assert st["exists"] and st["error"].startswith("line 3:") and st["path"] == str(p)

    def test_status_is_cached_by_mtime(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text("[engine]\nrefresh_seconds = 45\n")
        assert C.config_status(p)["error"] == ""
        p.write_text("[engine]\nrefresh_seconds = 45\nbogus = 1\n")
        os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
        assert C.config_status(p)["unknown_keys"] == ["engine.bogus"]
        missing = C.config_status(tmp_path / "none.toml")
        assert missing == {"path": str(tmp_path / "none.toml"), "exists": False, "error": "", "unknown_keys": [], "modified": 0.0}

    def test_directives_move_from_the_old_store_into_the_file(self, tmp_path, monkeypatch):
        from otto import paths
        from otto.intelligence import history
        monkeypatch.setattr(paths, "history_dir", lambda: tmp_path / "history")
        (tmp_path / "history").mkdir()
        assert history.save_directive("always flag anything about the payments migration")
        assert history.save_directive("Always flag anything about the payments migration") is False   # dup
        assert history.save_directive("never bother me with release notes")
        # An older build stored Otto's own explanations back as directives; those are not carried over.
        assert history.save_directive('Identified as directly relevant to standing directive: "flag the payments migration".')
        assert history.save_directive("High severity · Directive: flag the payments migr · Personal action item")
        p = C.ensure_config_file(tmp_path / "config.toml")
        m = C.ConfigManager(p)
        assert m.get("user.directives") == ["always flag anything about the payments migration",
                                            "never bother me with release notes"]
        assert not (tmp_path / "history" / "directives.jsonl").exists()
        assert (tmp_path / "history" / "directives.jsonl.migrated").exists()

    def test_load_directives_reads_the_config_first(self, tmp_path, monkeypatch):
        from otto import paths
        from otto.intelligence import history
        monkeypatch.setattr(paths, "history_dir", lambda: tmp_path / "history")
        monkeypatch.setattr(paths, "config_file", lambda: tmp_path / "config.toml")
        (tmp_path / "history").mkdir()
        (tmp_path / "config.toml").write_text('[user]\ndirectives = ["flag the payments migration", "  x  ", "flag the payments migration"]\n')
        history.save_directive("watch the runner image")
        got = history.load_directives()
        assert [d["directive"] for d in got] == ["flag the payments migration", "watch the runner image"]
        assert got[0]["source"] == "config"
        assert "flag the payments migration" in history.get_standing_directives_text()


class TestEngineReloadsConfig:
    def test_refresh_interval_and_thumbnails_follow_the_file(self, tmp_path, monkeypatch):
        from otto import paths
        from otto.core.engine import OttoEngine
        p = tmp_path / "config.toml"
        monkeypatch.setattr(paths, "config_file", lambda: p)
        monkeypatch.delenv("OTTO_REFRESH_SECONDS", raising=False)
        monkeypatch.delenv("OTTO_DISABLE_SCREENSHOTS", raising=False)
        eng = OttoEngine(port=7999, refresh_seconds=60, notifications=False, collector=lambda: {})
        eng.reload_config()                                    # first start: the file is written with the defaults
        assert p.exists() and "refresh_seconds = 60" in p.read_text() and p.read_text().count("#") > 20
        assert eng.refresh_seconds == 60 and "OTTO_DISABLE_SCREENSHOTS" not in os.environ
        p.write_text("[engine]\nrefresh_seconds = 30\nscreenshots = false\n")
        eng.reload_config()
        assert eng.refresh_seconds == 30 and os.environ.get("OTTO_DISABLE_SCREENSHOTS") == "1"
        p.write_text("[engine]\nrefresh_seconds = 5\nscreenshots = true\n")
        os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
        eng.reload_config()
        assert eng.refresh_seconds == 15                        # floor
        assert "OTTO_DISABLE_SCREENSHOTS" not in os.environ      # turned back on
        p.write_text("[engine]\nrefresh_seconds = 30\nscreenshots = false\n[user\n")   # broken edit
        os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 10))
        eng.reload_config()
        assert eng.refresh_seconds == 15                        # previous values kept

    def test_env_override_wins_over_the_file(self, tmp_path, monkeypatch):
        from otto import paths
        from otto.core.engine import OttoEngine
        p = tmp_path / "config.toml"
        p.write_text("[engine]\nrefresh_seconds = 30\n")
        monkeypatch.setattr(paths, "config_file", lambda: p)
        monkeypatch.setenv("OTTO_REFRESH_SECONDS", "90")
        eng = OttoEngine(port=7999, refresh_seconds=90, notifications=False, collector=lambda: {})
        eng.reload_config()
        assert eng.refresh_seconds == 90


@pytest.fixture(autouse=True)
def _isolate_config_status_cache():
    C._STATUS_CACHE.update(key=None, value=None)
    yield
    C._STATUS_CACHE.update(key=None, value=None)


def test_paths_config_file_is_under_the_data_dir():
    from otto import paths
    assert Path(paths.config_file()).name == "config.toml"
