"""
Tests for the pure-Python Accessibility reader (``otto.adapters.browser.ax_dump``).

The walk itself needs a real macOS window and an Accessibility grant, so it
is exercised live (the engine), not here. These tests pin
the parts that must not regress silently: the CLI contract the reader
process is spawned with, process matching, and the never-prompt guarantee.
"""
from __future__ import annotations

import pytest

from otto.adapters.browser import ax_dump


class TestAppNameFromPath:
    def test_app_bundle_main_binary(self):
        assert ax_dump._app_name_from_path("/Applications/Slack.app/Contents/MacOS/Slack") == "Slack"

    def test_nested_helper_bundles_never_match_the_parent(self):
        helper = ("/Applications/Slack.app/Contents/Frameworks/Slack Helper (Renderer).app"
                  "/Contents/MacOS/Slack Helper (Renderer)")
        assert ax_dump._app_name_from_path(helper) == "Slack Helper (Renderer)"
        chrome_helper = ("/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework"
                         "/Versions/1/Helpers/Google Chrome Helper.app/Contents/MacOS/Google Chrome Helper")
        assert ax_dump._app_name_from_path(chrome_helper) == "Google Chrome Helper"

    def test_plain_binary_uses_basename(self):
        assert ax_dump._app_name_from_path("/usr/bin/python3") == "python3"
        assert ax_dump._app_name_from_path("") == ""


class TestCli:
    def test_defaults_and_overrides(self):
        opts = ax_dump._parse_args([])
        assert opts == {"app": "Slack", "max_nodes": ax_dump.DEFAULT_MAX_NODES,
                        "max_chars": ax_dump.DEFAULT_MAX_CHARS, "max_ms": ax_dump.DEFAULT_MAX_MS,
                        "json": False, "announce": True}
        opts = ax_dump._parse_args(["Google Chrome", "--max-ms", "1500", "--max-nodes", "10", "--max-chars", "0",
                                    "--json", "--no-announce"])
        assert opts["app"] == "Google Chrome"
        assert opts["max_ms"] == 1500 and opts["max_nodes"] == 10
        assert opts["max_chars"] == 1  # clamped to a sane minimum
        assert opts["json"] is True and opts["announce"] is False

    def test_bad_numbers_are_ignored(self):
        opts = ax_dump._parse_args(["--max-ms", "soon", "Slack"])
        assert opts["max_ms"] == ax_dump.DEFAULT_MAX_MS and opts["app"] == "Slack"

    def test_exit_codes_and_streams(self, monkeypatch, capsys):
        calls = []

        def fake_dump(app, **kw):
            calls.append((app, kw))
            return ax_dump.DumpResult(ax_dump.EXIT_OK, "general (Channel) - acme - Slack\nhello", "")

        monkeypatch.setattr(ax_dump, "dump_window_text", fake_dump)
        assert ax_dump.main(["Slack", "--max-ms", "800"]) == 0
        out, err = capsys.readouterr()
        assert out == "general (Channel) - acme - Slack\nhello\n" and err == ""
        assert calls == [("Slack", {"max_nodes": ax_dump.DEFAULT_MAX_NODES, "max_chars": ax_dump.DEFAULT_MAX_CHARS,
                                    "max_ms": 800, "announce_client": True})]

        monkeypatch.setattr(ax_dump, "dump_window_text",
                            lambda app, **kw: ax_dump.DumpResult(ax_dump.EXIT_NOT_TRUSTED, "", ax_dump.NOT_TRUSTED_MESSAGE))
        assert ax_dump.main(["Slack"]) == 2
        out, err = capsys.readouterr()
        assert out == "" and err.strip() == "not allowed assistive access"

        monkeypatch.setattr(ax_dump, "dump_window_text",
                            lambda app, **kw: ax_dump.DumpResult(ax_dump.EXIT_UNAVAILABLE, "", "Slack is not running"))
        assert ax_dump.main(["Slack"]) == 1

    def test_json_mode_prints_every_window_and_the_sidebar(self, monkeypatch, capsys):
        import json

        result = ax_dump.AppDump(
            ax_dump.EXIT_OK,
            [ax_dump.WindowDump("ops (Channel) - acme - Slack", "ops (Channel) - acme - Slack\nalice 9:00 AM\nhi"),
             ax_dump.WindowDump("Thread - acme - Slack", "Thread - acme - Slack\nbob 9:05 AM\nreply")],
            [{"name": "leads", "section": "Channels", "dm": False, "unread": True, "badge": 2,
              "selected": False, "muted": False, "self": False}],
            "", announced=True, truncated=False, nodes=321, ms=140,
        )
        seen = {}
        monkeypatch.setattr(ax_dump, "dump_app", lambda app, **kw: seen.setdefault("kw", kw) and result or result)
        assert ax_dump.main(["Slack", "--json", "--no-announce"]) == 0
        out, err = capsys.readouterr()
        payload = json.loads(out)
        assert err == ""
        assert payload["app"] == "Slack" and payload["announced"] is True and payload["nodes"] == 321
        assert [w["title"] for w in payload["windows"]] == ["ops (Channel) - acme - Slack", "Thread - acme - Slack"]
        assert payload["conversations"][0]["name"] == "leads" and payload["conversations"][0]["badge"] == 2
        assert seen["kw"]["announce_client"] is False

        monkeypatch.setattr(ax_dump, "dump_app", lambda app, **kw: ax_dump.AppDump(ax_dump.EXIT_NOT_TRUSTED, [], [], ax_dump.NOT_TRUSTED_MESSAGE))
        assert ax_dump.main(["Slack", "--json"]) == 2
        out, err = capsys.readouterr()
        assert out == "" and "assistive" in err

    def test_reader_contract_matches_the_engine(self):
        """reader.py maps exit 2 → permission denied and exit 1 → app closed."""
        assert ax_dump.EXIT_OK == 0 and ax_dump.EXIT_UNAVAILABLE == 1 and ax_dump.EXIT_NOT_TRUSTED == 2
        assert ax_dump.NOT_TRUSTED_MESSAGE == "not allowed assistive access"

    def test_text_mode_is_the_main_window(self):
        dump = ax_dump.AppDump(ax_dump.EXIT_OK, [ax_dump.WindowDump("a", "a\nfirst"), ax_dump.WindowDump("b", "b\nsecond")], [], "")
        assert dump.text == "a\nfirst"
        assert ax_dump.AppDump(ax_dump.EXIT_UNAVAILABLE, [], [], "x").text == ""


class TestNeverPrompts:
    def test_off_macos_is_unavailable_not_an_error(self, monkeypatch):
        monkeypatch.setattr(ax_dump.sys, "platform", "linux")
        assert ax_dump.available() is False
        assert ax_dump.is_trusted() is False
        result = ax_dump.dump_window_text("Slack")
        assert result.code == ax_dump.EXIT_UNAVAILABLE and "unavailable" in result.error

    def test_untrusted_process_returns_exit_2_without_touching_the_app(self, monkeypatch):
        class FakeAX:
            def AXIsProcessTrusted(self):
                return 0

        class FakeFrameworks:
            ax = FakeAX()

            def pids_for_app(self, name):  # pragma: no cover — must not be reached
                raise AssertionError("process lookup must not happen when untrusted")

        monkeypatch.setattr(ax_dump.sys, "platform", "darwin")
        monkeypatch.setattr(ax_dump, "_frameworks", lambda: FakeFrameworks())
        result = ax_dump.dump_window_text("Slack")
        assert result == ax_dump.DumpResult(ax_dump.EXIT_NOT_TRUSTED, "", ax_dump.NOT_TRUSTED_MESSAGE)

    def test_module_is_read_only(self):
        """No action or event API is ever bound: this reader cannot type, click or post.

        The one write it makes is Electron's "an assistive client is present"
        flag, on the application element, inside announce() — and nowhere else.
        """
        import inspect
        import re
        source = inspect.getsource(ax_dump)
        for forbidden in ("AXUIElementPerformAction(", "CGEventPost", "CGEventCreate", "AXEnhancedUserInterface\""):
            assert forbidden not in source
        calls = re.findall(r"\bax\.AXUIElementSetAttributeValue\(([^)]*)\)", source)
        assert len(calls) == 1, calls
        assert "names[ANNOUNCE_ATTRIBUTE]" in calls[0] and "fw.true_ref" in calls[0]
        assert ax_dump.ANNOUNCE_ATTRIBUTE == "AXManualAccessibility"
        announce_src = inspect.getsource(ax_dump.announce)
        assert "AXUIElementSetAttributeValue" in announce_src

    def test_announce_only_flips_a_false_flag(self):
        """Native apps (no attribute) and already-announced apps are left alone."""
        class FakeAX:
            def __init__(self):
                self.sets = []

            def AXUIElementCopyAttributeValue(self, element, name, out):
                return 1  # attribute missing

            def AXUIElementSetAttributeValue(self, element, name, value):
                self.sets.append((element, name, value))
                return 0

        class FakeFW:
            def __init__(self):
                self.ax = FakeAX()
                self.true_ref = 7
                self.value = None

            def attribute(self, element, name):
                return self.value

            def to_bool(self, ref):
                return ref

            def release(self, ref):
                pass

        fw = FakeFW()
        names = {ax_dump.ANNOUNCE_ATTRIBUTE: 42}
        assert ax_dump.announce(fw, 1, names) is False           # no such attribute → untouched
        fw.value = True
        assert ax_dump.announce(fw, 1, names) is False           # already on → untouched
        fw.value = False
        assert ax_dump.announce(fw, 1, names) is True            # off → flipped, once
        assert fw.ax.sets == [(1, 42, 7)]

    def test_no_announce_flag_skips_the_write(self, monkeypatch):
        class FakeAX:
            def AXIsProcessTrusted(self):
                return 1

        class FakeCF:
            @staticmethod
            def CFArrayCreate(*a):
                return 1

        class FakeFW:
            ax = FakeAX()
            cf = FakeCF()
            type_array_callbacks = 0
            _n = 0

            def cfstr(self, key):
                FakeFW._n += 1
                return FakeFW._n

            def release(self, ref):
                pass

            def pids_for_app(self, name):
                return [1]

        monkeypatch.setattr(ax_dump.sys, "platform", "darwin")
        monkeypatch.setattr(ax_dump, "_frameworks", lambda: FakeFW())
        monkeypatch.setattr(ax_dump, "_find_app", lambda fw, app, names: 99)
        monkeypatch.setattr(ax_dump, "_windows", lambda fw, app, names: [5])
        monkeypatch.setattr(ax_dump, "_walk_window", lambda *a: ax_dump.WindowDump("t", ""))
        counts = {"n": 1}
        monkeypatch.setattr(ax_dump, "_node_count", lambda *a: counts["n"])
        called = []
        monkeypatch.setattr(ax_dump, "announce", lambda *a: called.append(a) or True)
        # a bare title bar, announcing turned off: nothing is written
        result = ax_dump.dump_app("Slack", announce_client=False)
        assert result.code == ax_dump.EXIT_UNAVAILABLE and "no readable text" in result.error
        assert called == []
        # a bare title bar, announcing on: the flag is set once (warm-up skipped here)
        result = ax_dump.dump_app("Slack", warmup_ms=0)
        assert result.announced is True and len(called) == 1 and called[0][1] == 99
        # a tree that is already published: no write at all
        counts["n"] = 500
        result = ax_dump.dump_app("Slack", warmup_ms=0)
        assert result.announced is False and len(called) == 1


@pytest.mark.skipif(ax_dump.sys.platform != "darwin", reason="macOS frameworks only")
class TestOnMacOS:
    def test_frameworks_load_and_bind(self):
        assert ax_dump.available() is True
        # Never prompts; the value depends on the terminal's grant, only the type is fixed.
        assert isinstance(ax_dump.is_trusted(), bool)

    def test_missing_app_is_reported_as_not_running(self):
        """Even without an Accessibility grant, an absent app is a clean exit 1."""
        fw = ax_dump._frameworks()
        assert fw.pids_for_app("Definitely Not An App 12345") == []
