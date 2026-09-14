from __future__ import annotations

import stat
from pathlib import Path


from otto import paths
from otto.utils import keys


class TestPaths:
    def test_data_dir_honours_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OTTO_DATA_DIR", str(tmp_path / "custom"))
        assert paths.data_dir() == tmp_path / "custom"
        assert paths.ensure_data_dir().is_dir()
        for fn in (paths.log_file, paths.pid_file, paths.dismissed_file, paths.snoozed_file, paths.notified_file,
                   paths.checkpoint_file, paths.link_cache_file, paths.classification_cache_file, paths.key_file):
            assert str(fn()).startswith(str(tmp_path / "custom")), fn.__name__

    def test_default_data_dir_is_application_support(self, monkeypatch):
        monkeypatch.delenv("OTTO_DATA_DIR", raising=False)
        assert paths.data_dir() == Path.home() / "Library" / "Application Support" / "Otto"

    def test_history_dir_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OTTO_HISTORY_DIR", str(tmp_path / "h"))
        assert paths.history_dir() == tmp_path / "h"

    def test_project_root_and_python(self, tmp_path, monkeypatch):
        assert (paths.project_root() / "src" / "otto").is_dir()
        monkeypatch.setenv("OTTO_HOME", str(tmp_path))
        assert paths.project_root() == tmp_path
        monkeypatch.setenv("OTTO_PYTHON", "/opt/py")
        assert paths.python_executable() == Path("/opt/py")

    def test_launch_agents_dir_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OTTO_LAUNCH_AGENTS_DIR", str(tmp_path / "LA"))
        assert paths.launch_agent_plist() == tmp_path / "LA" / f"{paths.LAUNCH_AGENT_LABEL}.plist"

    def test_no_hardcoded_user_paths_in_source(self):
        src = Path(__file__).resolve().parents[2] / "src"
        offenders = []
        for p in src.rglob("*"):
            if p.suffix in (".py", ".swift") and "/Users/" in p.read_text(errors="ignore"):
                offenders.append(str(p))
        assert not offenders, f"hardcoded /Users/ paths in {offenders}"


class TestKeys:
    def test_mask(self):
        assert keys.mask("sk-or-v1-abcdefghijklmnop") == "sk-or-v1-a…mnop"
        assert keys.mask("short") == "***"

    def test_add_list_remove_file(self):
        where = keys.add_key("sk-or-v1-" + "x" * 40)
        assert where.startswith("file:")
        kf = paths.key_file()
        assert kf.exists()
        assert stat.S_IMODE(kf.stat().st_mode) == 0o600
        found = keys.discover_keys()
        assert len(found) == 1 and found[0].source.startswith("file:")
        # duplicates are ignored
        keys.add_key("sk-or-v1-" + "x" * 40)
        assert len(keys._read_key_file(kf)) == 1
        assert keys.remove_key("sk-or-v1-x") == 1
        assert keys.discover_keys() == []

    def test_env_has_priority_and_dedupes(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-" + "e" * 40)
        keys.add_key("sk-proj-" + "e" * 40)
        found = keys.discover_keys()
        assert [f.source for f in found] == ["env:OPENAI_API_KEY"]

    def test_legacy_migration(self, tmp_path, monkeypatch):
        legacy = tmp_path / "legacy" / "api.key"
        legacy.parent.mkdir()
        legacy.write_text("sk-ant-" + "l" * 40 + "\n# comment\n\n")
        monkeypatch.setattr(paths, "legacy_key_files", lambda: [legacy])
        moved = keys.migrate_legacy_keys(delete_legacy=True)
        assert moved == [legacy]
        assert not legacy.exists()
        assert any(k.key.startswith("sk-ant-") for k in keys.discover_keys())

    def test_key_file_never_inside_repo(self):
        assert not str(paths.key_file()).startswith(str(paths.project_root()))

    def test_gitignore_excludes_keys(self):
        gi = (Path(__file__).resolve().parents[2] / ".gitignore").read_text()
        assert "*.key" in gi

    def test_keychain_unavailable_falls_back_to_file(self, monkeypatch):
        monkeypatch.setattr(keys, "_keychain_available", lambda: False)
        where = keys.add_key("sk-or-v1-" + "k" * 40, use_keychain=True)
        assert where.startswith("file:")

    def test_parse_key_text(self):
        text = "# comment\n\n sk-or-v1-abc \nsk-or-v1-abc\nOPENAI_API_KEY=\"sk-proj-zzz\"\n"
        assert keys._parse_key_text(text) == ["sk-or-v1-abc", "sk-proj-zzz"]
