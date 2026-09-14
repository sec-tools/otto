"""
Otto command line interface — deliberately small. After `otto setup`
everything is in the menu bar; the terminal is for the few things that need
one.

    otto setup            one-time setup (scripts/setup.sh)
    otto start / stop / restart
    otto status           what's running, what it reads, which model, what needs a look
    otto open             the briefing page in your browser
    otto config           open config.toml in your editor (created with every setting explained)
    otto logs             the engine log
    otto key add|list|remove       model keys (never in the repo)
    otto slack connect|disconnect  the read-only Slack token
    otto permissions      the macOS grants the engine needs
    otto install / uninstall       run at login (engine + menu bar app)

Settings that used to be flags (port, refresh interval, where keys go, standing
directives) live in config.toml. The menu bar app drives the same commands with
a few hidden machine-readable switches (``--json``) that are not part of the
user-facing surface.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from otto import paths
from otto.utils.logging import configure_logging

logger = logging.getLogger("otto.cli")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="otto",
        description="Otto — a read-only briefing assistant for macOS. After `otto setup`, everything is in the menu bar.",
        epilog="Settings (port, refresh interval, who you are, standing directives…) live in config.toml: `otto config`.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    sub = parser.add_subparsers(dest="command", metavar="command")

    sub.add_parser("setup", help="one-time setup: virtualenv, keys, Slack, menu bar, run at login, permissions")
    start = sub.add_parser("start", aliases=["run"], help="start the engine")
    start.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)   # development / launchd
    sub.add_parser("stop", help="stop the engine")
    sub.add_parser("restart", help="restart the engine")
    sub.add_parser("status", help="what's running, what it reads, which model, what needs a look")
    sub.add_parser("open", help="open the briefing page in your browser")
    config = sub.add_parser("config", help="open config.toml in your editor (created with every setting explained)")
    config.add_argument("--json", action="store_true", help=argparse.SUPPRESS)        # menu bar editor: the file as JSON
    config.add_argument("--save", action="store_true", help=argparse.SUPPRESS)        # menu bar editor: new text on stdin
    logs = sub.add_parser("logs", help="show the engine log")
    logs.add_argument("-n", type=int, default=40, help="lines to show (default 40)")
    logs.add_argument("-f", "--follow", action="store_true", help="keep following the log (Ctrl-C to stop)")

    key = sub.add_parser("key", help="model keys: add, list, remove")
    key_sub = key.add_subparsers(dest="key_command", metavar="add|list|remove")
    add = key_sub.add_parser("add", help="add a key (OpenRouter, OpenAI, Anthropic, Gemini, Devin); omitted → read from stdin")
    add.add_argument("key", nargs="?", help="the key")
    add.add_argument("--json", action="store_true", help=argparse.SUPPRESS)          # menu bar plumbing
    key_sub.add_parser("list", help="show configured keys (masked)")
    rm = key_sub.add_parser("remove", help="remove a key by prefix")
    rm.add_argument("prefix")

    slack = sub.add_parser("slack", help="the read-only Slack token: connect, disconnect")
    s_sub = slack.add_subparsers(dest="slack_command", metavar="connect|disconnect")
    s_conn = s_sub.add_parser("connect", help="opens Slack's create-app page with Otto's read-only manifest filled in, "
                                             "verifies the token you paste, stores it, restarts the engine")
    s_conn.add_argument("--json", action="store_true", help=argparse.SUPPRESS)       # menu bar plumbing
    s_conn.add_argument("--manifest", action="store_true", help=argparse.SUPPRESS)   # docs sync
    s_sub.add_parser("disconnect", help="remove the stored Slack token(s)")

    sub.add_parser("permissions", help="walk through the macOS permissions the engine needs")
    sub.add_parser("install", help="run at login and restart on crash (engine + menu bar app)")
    uninst = sub.add_parser("uninstall", help="remove the run-at-login jobs; your data is kept")
    uninst.add_argument("--keep-menubar", action="store_true", help=argparse.SUPPRESS)  # the menu bar's own toggle

    return parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _live_engine_port() -> Optional[int]:
    """Port of the engine that is actually running (``engine.json``), or None."""
    try:
        info = json.loads(paths.engine_info_file().read_text())
        pid, port = int(info["pid"]), int(info["port"])
        os.kill(pid, 0)
        return port
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _port() -> int:
    """``OTTO_PORT`` → the running engine's port → ``[engine] port`` in config.toml → default.

    Changing the port in config.toml takes effect on ``otto restart``; until
    then every command keeps talking to the engine that is up.
    """
    if os.environ.get("OTTO_PORT"):
        try:
            return int(os.environ["OTTO_PORT"])
        except ValueError:
            pass
    live = _live_engine_port()
    if live:
        return live
    try:
        from otto.config import ConfigManager
        return int(ConfigManager().get_or("engine.port", paths.DEFAULT_PORT))
    except Exception:
        return paths.DEFAULT_PORT


def _api(path: str, *, method: str = "GET", timeout: float = 3.0, data: Dict[str, str] | None = None) -> Optional[Dict[str, Any]]:
    """Talk to a running engine. Returns None if it isn't reachable."""
    url = f"http://127.0.0.1:{_port()}{path}"
    body = None
    headers = {"Host": f"localhost:{_port()}"}
    if method == "POST":
        body = urllib.parse.urlencode(data or {}).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _engine_pid() -> Optional[int]:
    from otto.core.launchd import running_pid
    return running_pid()


# Backwards-compatible PID helpers (kept for scripts/tests)
OTTO_DATA_DIR = paths.data_dir()
OTTO_PID_FILE = paths.pid_file()
OTTO_LOG_FILE = paths.log_file()


def _read_pid() -> Optional[int]:
    pid_file = paths.pid_file()
    try:
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return pid
    except (ValueError, ProcessLookupError, PermissionError):
        try:
            pid_file.unlink()
        except OSError:
            pass
    return None


def _pid_on_port(port: int) -> Optional[int]:
    """PID of the process listening on ``port`` (an engine started outside our control)."""
    try:
        out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=3).stdout.split()
        return int(out[0]) if out else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _terminate(pid: int, grace: float = 5.0) -> bool:
    """SIGTERM then SIGKILL. Returns True once the process is gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    deadline = time.time() + grace
    while time.time() < deadline:
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def _write_pid() -> None:
    pid_file = paths.pid_file()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))


def _remove_pid() -> None:
    try:
        paths.pid_file().unlink()
    except OSError:
        pass


def _spawn_engine() -> int:
    """Start a detached engine process (used when launchd is not installed)."""
    args = [str(paths.python_executable()), "-m", "otto.core.engine", "--no-console-log"]
    env = {**os.environ, "PYTHONPATH": str(paths.project_root() / "src"), "OTTO_HOME": str(paths.project_root())}
    proc = subprocess.Popen(args, cwd=str(paths.project_root()), env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    return proc.pid


def _wait_for_engine(seconds: float = 8.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _api("/api/status") is not None:
            return True
        time.sleep(0.25)
    return False


def _keychain() -> bool:
    """``[keys] keychain`` in config.toml: where new keys go."""
    try:
        from otto.config import ConfigManager
        return bool(ConfigManager().get_or("keys.keychain", False))
    except Exception:
        return False


def engine_identity() -> tuple[str, str]:
    """``(name, path)`` macOS uses for the engine process in permission dialogs.

    Under launchd the interpreter itself is the "responsible process", so the
    Accessibility/Automation grants must be given to it — usually a
    ``Python.app`` bundle inside the framework, not to Terminal. TCC attributes
    the job to the *resolved* interpreter binary (verified against tccd's
    AUTHREQ_ATTRIBUTION log: responsible_path is the realpath).
    """
    real = os.path.realpath(str(paths.python_executable()))
    return os.path.basename(real) or "python3", real


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_run(*, foreground: bool, verbose: bool) -> int:
    from otto.core import launchd

    if foreground:
        from otto.core.engine import engine_from_config
        configure_logging(verbose=verbose, console=True)
        engine = engine_from_config()
        print(f"Otto engine → http://localhost:{engine.port}  (refresh every {engine.refresh_seconds}s; Ctrl-C to stop)")
        return engine.run_forever()

    if _api("/api/status") is not None:
        print(f"Otto is already running → http://localhost:{_port()}")
        return 0

    if launchd.is_installed():
        launchd.start()
        how = "via launchd"
    else:
        pid = _spawn_engine()
        how = f"in the background (PID {pid})"

    if _wait_for_engine():
        print(f"Otto started {how} → http://localhost:{_port()}")
        if not launchd.is_installed():
            print("Tip: `otto install` makes it start at login and restart automatically.")
        return 0
    print("Otto did not come up. Check `otto logs`.")
    return 1


def cmd_stop() -> int:
    from otto.core import launchd

    if launchd.is_loaded():
        launchd.stop()
        print("Otto stopped (run-at-login job unloaded; `otto start` or `otto install` to start again).")
        return 0
    pid = _read_pid() or _pid_on_port(_port())
    if pid is None:
        print("Otto is not running.")
        return 0
    print(f"Stopping Otto (PID {pid})...")
    _terminate(pid)
    _remove_pid()
    print("Otto stopped.")
    return 0


def cmd_restart(verbose: bool) -> int:
    from otto.core import launchd
    from otto.llm.gateway import clear_provider_health

    # An explicit restart means "try again": forget parked LLM providers
    # (a fixed account, a re-enabled key). Crash/reboot restarts keep them.
    clear_provider_health()
    if launchd.is_installed():
        launchd.restart()
        ok = _wait_for_engine(12)
        print("Otto restarted." if ok else "Otto is restarting (not reachable yet) — see `otto logs`.")
        return 0 if ok else 1
    cmd_stop()
    return cmd_run(foreground=False, verbose=verbose)


def _restart_if_running(*, quiet: bool = False) -> bool:
    """After a key or token changed: restart a running engine so it is picked up now."""
    if _api("/api/status") is None:
        return False
    if quiet:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_restart(False)
    else:
        print("Restarting Otto so it picks that up…")
        cmd_restart(False)
    return True


def cmd_install() -> int:
    from otto.core import launchd

    # Stop a manually started engine so launchd can own the port.
    if not launchd.is_loaded():
        stray = _read_pid() or _pid_on_port(_port())
        if stray:
            print(f"Stopping the engine started outside launchd (PID {stray})...")
            _terminate(stray)
            _remove_pid()
    try:
        plist = launchd.install()
    except Exception as e:
        print(f"Install failed: {e}")
        return 1
    ok = _wait_for_engine(12)
    print(f"Installed LaunchAgent: {plist}")
    print(f"Otto {'is running' if ok else 'is starting'} → http://localhost:{_port()}")
    print("It will start at login and restart automatically if it crashes.")
    _install_menubar()
    if ok and sys.stdin.isatty():
        cmd_permissions(wait_seconds=30)
    return 0


def cmd_permissions(*, wait_seconds: int = 30, open_settings: bool = True) -> int:
    """Walk the user through the macOS grants the *engine* needs.

    The engine runs under launchd, so macOS attributes its automation to the
    Python interpreter, not to the terminal. We therefore (1) let the engine
    do a refresh so the "python wants to control Slack" prompts appear now,
    while the user is here, and (2) open the Accessibility pane and reveal
    the interpreter in Finder so it can be dragged into the list.
    """
    if _api("/api/status") is None:
        print("Otto is not running. Start it first: otto start   (or otto install)")
        return 1
    name, binary = engine_identity()
    print("\nChecking what the engine can read (this triggers macOS permission prompts — click Allow)…")
    _api("/api/refresh", method="POST", timeout=3)
    deadline = time.time() + max(5, wait_seconds)
    statuses: list = []
    st: Dict[str, Any] = {}
    while time.time() < deadline:
        time.sleep(1.5)
        st = _api("/api/status", timeout=3) or {}
        if not st.get("refreshing") and st.get("source_status"):
            statuses = st["source_status"]
            break
    if not statuses:
        print("The engine has not finished a refresh yet — run `otto permissions` again in a minute.")
        return 1

    readable = [s["source"] for s in statuses if s.get("ok")]
    blocked = [s for s in statuses if not s.get("ok") and s.get("error") != "app not open"]
    closed = [s["source"] for s in statuses if not s.get("ok") and s.get("error") == "app not open"]
    shots = st.get("screenshots") or {}
    no_pictures = bool(shots.get("enabled", True)) and shots.get("granted") is False
    if readable:
        print("  ✓ readable: " + ", ".join(readable))
    if closed:
        print("  · not open: " + ", ".join(closed) + "  (open the app and Otto picks it up)")
    if no_pictures:
        print("  ✗ thumbnails: Screen Recording is not granted to the engine — items and banners show no picture of the source")
    if not blocked and not no_pictures:
        print("All permissions look good.")
        return 0

    for s in blocked:
        print(f"  ✗ {s['source']}: {s.get('error', '')}")
    step = 1
    print(f"\nmacOS needs the following for “{name}” ({binary}):")
    if blocked:
        print(f"  {step}. Click Allow on the “wants to control Slack / System Events / Google Chrome” prompts.")
        step += 1
        print(f"  {step}. Accessibility: System Settings → Privacy & Security → Accessibility → “+” → add that file.")
        step += 1
    if no_pictures:
        which = "the same file" if blocked else "that file"
        print(f"  {step}. Screen Recording: System Settings → Privacy & Security → Screen Recording → “+” → add {which}.")
        print("     (Only the Slack window is ever captured, and only while it shows the conversation being reported;")
        print("      Slack is never focused. `screenshots = false` in config.toml turns thumbnails off instead.)")
    if open_settings:
        print("     Opening System Settings and revealing the file in Finder — drag it into the list.")
        pane = "Privacy_Accessibility" if blocked else "Privacy_ScreenCapture"
        subprocess.run(["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"],
                       capture_output=True)
        if blocked and no_pictures:
            subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"],
                           capture_output=True)
        subprocess.run(["open", "-R", binary], capture_output=True)
    print("Then:  otto restart   — the menu bar shows anything that still needs a look.")
    return 2


def _install_menubar() -> None:
    """Open the menu bar app now and at every login (its own LaunchAgent)."""
    from otto.core import launchd

    if not paths.menubar_binary().exists():
        print("Menu bar app not built yet — run `scripts/build_menubar.sh` (optional; badge and banners).")
        return
    try:
        plist = launchd.install_menubar()
    except Exception as e:
        print(f"Menu bar app could not be registered at login ({e}); opening it once instead.")
        subprocess.run(["open", "-a", str(paths.menubar_app())], capture_output=True)
        return
    print(f"Menu bar app opens now and at login: {plist}")


def cmd_uninstall(keep_menubar: bool = False) -> int:
    from otto.core import launchd

    removed = launchd.uninstall()
    if keep_menubar:
        # The menu bar app's own "Run at Login" toggle: only the engine's
        # autostart goes; the app stays up to show "Engine offline".
        note = "LaunchAgent removed (the menu bar app keeps running)."
    else:
        removed = launchd.uninstall_menubar() or removed
        subprocess.run(["pkill", "-x", "OttoMenuBar"], capture_output=True)
        note = "LaunchAgents removed; the menu bar app has quit."
    print(note if removed else "Nothing to uninstall.")
    print(f"Your data is untouched in {paths.data_dir()}")
    return 0


def cmd_open() -> int:
    if _api("/api/status") is None:
        rc = cmd_run(foreground=False, verbose=False)
        if rc != 0:
            return rc
    subprocess.run(["open", f"http://localhost:{_port()}"], capture_output=True)
    return 0


def cmd_config(*, open_editor: bool = True, as_json: bool = False, save: bool = False) -> int:
    """Create ``config.toml`` (every setting, explained) if needed and open it in the text editor.

    The running engine re-reads it within a minute of a save. The menu bar's
    *Edit Config…* uses the hidden plumbing instead: ``--json`` returns the
    file's text, ``--save --json`` takes the edited text on stdin, writes it
    (0600, only if it parses) and asks the engine to reload right away.
    """
    from otto.config import config_status, ensure_config_file, load_config_text, save_config_text

    if save:
        text = sys.stdin.read()
        result = save_config_text(text)
        if result.get("saved"):
            result["applied"] = _api("/api/refresh", method="POST", timeout=3) is not None
        if as_json:
            print(json.dumps(result), flush=True)
        elif result.get("saved"):
            print(f"Saved {result['path']}" + (f"  ✗ {result['error']}" if result.get("error") else ""))
        else:
            print(f"Not saved: {result.get('error')}")
        return 0 if result.get("saved") else 1
    if as_json:
        print(json.dumps(load_config_text()), flush=True)
        return 0

    path = ensure_config_file()
    st = config_status(path)
    print(f"{path}")
    if st.get("error"):
        print(f"  ✗ {st['error']}  (Otto keeps the last good values until this is fixed)")
    for k in st.get("unknown_keys") or []:
        print(f"  ? unknown setting {k} is ignored")
    if open_editor:
        subprocess.run(["open", "-t", str(path)], capture_output=True)
    print("Saved changes apply within a minute; port and logging after `otto restart`.")
    return 0


def cmd_logs(n: int, follow: bool) -> int:
    log = paths.log_file()
    if not log.exists():
        print(f"No log yet at {log}")
        return 0
    cmd = ["tail", "-n", str(n)] + (["-f"] if follow else []) + [str(log)]
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 0


SLOW_REFRESH_SECONDS = 20.0
SLOW_SOURCE_SECONDS = 8.0


def _describe_refresh_speed(st: dict) -> str:
    """" · took 3.1s" — and where the time went when a refresh is slow."""
    secs = float(st.get("last_refresh_seconds") or 0)
    if secs <= 0:
        return ""
    line = f" · took {secs:.1f}s"
    if secs < SLOW_REFRESH_SECONDS:
        return line
    phases = st.get("phases") or {}
    slow = sorted(((v, k) for k, v in phases.items() if float(v or 0) >= 2.0), reverse=True)
    if slow:
        line += " (slow: " + ", ".join(f"{k} {float(v):.0f}s" for v, k in slow[:3]) + ")"
    return line


def describe_thumbnails(shots: dict) -> str:
    """One line on source thumbnails for ``otto status`` — only when there is something to say."""
    if not shots or not shots.get("enabled", True):
        return ""
    if shots.get("granted") is False:
        return "off — Screen Recording is not granted to the engine (run `otto permissions`)"
    paused = int(shots.get("paused_seconds") or 0)
    if paused:
        return f"paused {paused // 60 + 1} min — {shots.get('reason') or 'capture failed'}"
    return ""


def slow_source_lines(statuses: list) -> list[str]:
    """Explain a slow source in the user's terms, with the step that took the time."""
    out: list[str] = []
    for s in statuses:
        secs = float(s.get("seconds") or 0)
        if secs < SLOW_SOURCE_SECONDS:
            continue
        detail = hint = ""
        timings = {k: float(v or 0) for k, v in (s.get("timings") or {}).items()}
        if timings:
            worst, worst_s = max(timings.items(), key=lambda kv: kv[1])
            detail = f" — mostly {worst.replace('_', ' ')} ({worst_s:.0f}s)"
            if worst == "tabs":
                hint = "; many browser tabs slow every read — closing some helps"
            elif worst == "tab_content":
                hint = "; a heavy Slack tab — the Slack app reads faster"
        out.append(f"{s.get('source')} took {secs:.0f}s{detail}{hint}")
    return out


def _describe_llm(st: dict) -> str:
    """One line: which model answered, or why the briefing is heuristics-only."""
    llm = st.get("llm") or {}
    providers = llm.get("providers") or []
    if not llm.get("configured"):
        return "local heuristics (no LLM key — `otto key add …` for AI analysis)"
    working = [p for p in providers if p.get("state") == "ok"]
    # The provider that actually answered last comes first; Devin is a
    # last-resort fallback, so it is never the headline while others work.
    working.sort(key=lambda p: (not p.get("last_used"), p.get("name") == "devin"))
    broken = [f"{p.get('name')}: {p.get('state')}" for p in providers if p.get("state") != "ok"]
    used = [p for p in working if p.get("last_used")]
    enriched = int(llm.get("enriched") or 0)
    if used and st.get("ai_powered"):
        line = f"AI · {used[0].get('name')} ({used[0].get('model')})"
    elif working and enriched:
        # Every visible item came from the classification cache this session:
        # honest about what answered, and about what will answer next.
        line = f"AI · cached analysis; next call goes to {working[0].get('name')} ({working[0].get('model')})"
    elif working:
        line = f"AI configured · {working[0].get('name')} ({working[0].get('model')}) — no analysis yet this session"
    else:
        line = "local heuristics — every LLM provider is unavailable"
    if broken:
        line += "\n               " + "; ".join(broken)
    return line


def _describe_radar(radar: Dict[str, Any]) -> str:
    parts = []
    if radar.get("todo"):
        overdue = radar.get("overdue") or 0
        parts.append(f"{radar['todo']} to do" + (f" ({overdue} overdue)" if overdue else ""))
    if radar.get("waiting"):
        parts.append(f"waiting on {radar['waiting']}")
    if radar.get("open_calls"):
        parts.append(f"{radar['open_calls']} unclaimed ask(s)")
    if radar.get("upcoming"):
        parts.append(f"{radar['upcoming']} coming up")
    if radar.get("patterns"):
        n = int(radar["patterns"])
        parts.append(f"{n} thing{'s' if n != 1 else ''} that keep{'' if n != 1 else 's'} coming back")
    if radar.get("attention"):
        parts.append(f"{len(radar['attention'])} needs attention")
    return " · ".join(parts) if parts else "nothing open"


_ACTION_HINT = {
    "permissions": "otto permissions",
    "slack": "otto slack connect",
    "config": "otto config",
    "menubar": "open bin/Otto.app",
    "restart": "otto restart",
}


def _action_hint(action: str) -> str:
    if action.startswith("key_remove:"):
        return f"otto key remove {action.split(':', 1)[1]}"
    return _ACTION_HINT.get(action, "")


def needs_a_look_lines(problems: list) -> list[str]:
    """The engine's problem list (core/health.py) as terminal lines, each with the one thing to do."""
    out: list[str] = []
    for p in problems or []:
        line = f"• {p.get('title', '')}"
        if p.get("detail"):
            line += f" — {p['detail']}"
        hint = _action_hint(str(p.get("action") or ""))
        if hint:
            line += f"   → {hint}"
        out.append(line)
    return out


def cmd_status(backend: Any = None) -> int:
    from otto.config import config_status
    from otto.core import launchd, slack_connect
    from otto.utils.keys import discover_keys, is_slack_token, mask

    print("\n" + "=" * 58)
    print("  Otto Status Overview")
    print("=" * 58)

    st = _api("/api/status")
    ld = launchd.status()
    if st:
        age = time.time() - float(st.get("last_updated") or 0) if st.get("last_updated") else None
        age_s = f"{int(age)}s ago" if age is not None and age < 3600 else (f"{int(age // 3600)}h ago" if age else "never")
        print(f"\n  Engine:      running → http://localhost:{_port()}")
        print(f"  Last refresh: {age_s}" + ("  (refreshing now)" if st.get("refreshing") else "")
              + _describe_refresh_speed(st))
        if st.get("last_error"):
            print(f"  Last error:  {st['last_error'][:80]}")
        recalled = int(st.get("recalled") or 0)
        aged_out = int(st.get("aged_out") or 0)
        print(f"  Items:       {st.get('items', 0)} visible · {st.get('critical_items', 0)} important"
              + (f" · {recalled} recalled from memory" if recalled else "")
              + (f" · {aged_out} on screen but older than the window (not shown)" if aged_out else ""))
        statuses = st.get("source_status") or []
        if statuses:
            parts = []
            for s in statuses:
                label = s.get("source", "?")
                if s.get("ok"):
                    extra = f", {s['channels']} channels" if s.get("channels") else ""
                    parts.append(f"{label} ✓ ({s.get('items', 0)}{extra})")
                else:
                    parts.append(f"{label} ✗")
            print(f"  Sources:     {' · '.join(parts)}")
            for s in statuses:
                if not s.get("ok") and s.get("error"):
                    print(f"               {s.get('source')}: {s['error']}")
                elif s.get("source") == "slack" and s.get("coverage"):
                    from otto.adapters.browser.slack_browser import coverage_sentence
                    line = coverage_sentence(s["coverage"])
                    if line:
                        print(f"               slack: {line}")
            for line in slow_source_lines(statuses):
                print(f"               {line}")
        else:
            print(f"  Sources:     {', '.join(s.split(':')[0].replace('browser_', '') for s in st.get('sources', [])) or '—'}")
        print(f"  Analysis:    {_describe_llm(st)}")
        thumbs = describe_thumbnails(st.get("screenshots") or {})
        if thumbs:
            print(f"  Thumbnails:  {thumbs}")
        radar = st.get("radar") or {}
        if radar:
            print(f"  Radar:       {_describe_radar(radar)}")
            mem = radar.get("memory") or {}
            if mem.get("messages"):
                print(f"  Memory:      {int(mem['messages']):,} messages · {mem.get('channels', 0)} channels · "
                      f"{mem.get('people', 0)} people · {mem.get('days', 0)} day(s)")
    else:
        print("\n  Engine:      not running   (start: `otto start` · at login: `otto install`)")

    print(f"\n  Daemon:      {'launchd installed' if ld['installed'] else 'not installed'}"
          + (f" · PID {ld['pid']}" if ld["pid"] else ""))
    print(f"  Data dir:    {paths.data_dir()}")
    print(f"  Log:         {paths.log_file()}")
    cfg = (st or {}).get("config") or config_status()
    if cfg.get("exists"):
        print(f"  Config:      {cfg.get('path')}" + (f"\n               ✗ {cfg['error']}" if cfg.get("error") else ""))
    else:
        print("  Config:      defaults (`otto config` writes the file with every setting explained)")

    keys = [k for k in discover_keys() if not is_slack_token(k.key)]
    if keys:
        print("\n  Model keys:")
        for k in keys:
            print(f"    • {mask(k.key):24s} {k.source}")
    else:
        print("\n  Model keys:  none (local signals only) — `otto key add <key>` for written summaries")

    # The Slack token: what it can read, and what the engine reads with it.
    slack_connect.status(engine_status=st)

    if paths.menubar_binary().exists():
        running = subprocess.run(["pgrep", "-x", "OttoMenuBar"], capture_output=True).returncode == 0
        menubar = ("running" if running else "built, not running (open bin/Otto.app)") \
            + (" · opens at login" if ld.get("menubar_installed") else "")
    else:
        menubar = "not built (scripts/build_menubar.sh)"
    print(f"  Menu bar:    {menubar}")

    problems = list((st or {}).get("problems") or [])
    if not st and cfg.get("error"):
        problems.append({"title": "config.toml has a problem", "detail": cfg["error"], "action": "config"})
    if problems:
        print("\n  Needs a look:")
        for line in needs_a_look_lines(problems):
            print(f"    {line}")
    elif st:
        print("\n  Nothing needs a look.")
    print("=" * 58 + "\n")
    return 0


# ---------------------------------------------------------------------------
# Slack token and model keys
# ---------------------------------------------------------------------------

def cmd_slack(args: argparse.Namespace) -> int:
    """`otto slack connect|disconnect` — a Slack token, set up like an API key."""
    from otto.core import slack_connect

    if args.slack_command == "connect":
        if args.manifest:
            print(slack_connect.manifest_text(), end="")
            return 0
        if args.json:
            return _slack_connect_json()
        rc, _v = slack_connect.connect(use_keychain=_keychain())
        if rc != 0:
            return rc
        if _restart_if_running():
            print("  Give it a minute; the menu bar and `otto status` show what it reads.")
        else:
            print("  Start Otto to use it:  otto start   (or otto install)")
        print()
        return 0
    if args.slack_command == "disconnect":
        rc = slack_connect.disconnect()
        if rc == 0:
            _restart_if_running()
        return rc
    print("Usage: otto slack {connect|disconnect}")
    return 1


def _slack_connect_json() -> int:
    """The menu bar's Connect Slack…: one JSON line with the create-app link, the token on stdin, one JSON line back.

    The token never touches argv or the environment; stdout carries nothing
    but the two JSON lines, so the caller can parse them blind.
    """
    from otto.core import slack_connect

    print(json.dumps({"step": "url", "url": slack_connect.create_app_url()}), flush=True)
    token = (sys.stdin.readline() or "").strip()
    if not token:
        print(json.dumps({"step": "done", "ok": False, "error": "cancelled"}), flush=True)
        return 1
    lines: list[str] = []
    rc, v = slack_connect.connect(token, use_keychain=_keychain(), out=lines.append)
    if rc != 0 or v is None:
        error = (v.error if v is not None and v.error else next((ln.strip(" ✗") for ln in lines if "✗" in ln), "not stored"))
        print(json.dumps({"step": "done", "ok": False, "error": error}), flush=True)
        return 1
    restarted = _restart_if_running(quiet=True)
    who = f"{v.user} at {v.team}" if v.user and v.team else (v.user or v.team or "verified")
    warnings = [ln.strip(" ⚠") for ln in lines if "⚠" in ln]
    print(json.dumps({"step": "done", "ok": True, "identity": who, "coverage": v.coverage(),
                      "warnings": warnings, "restarted": restarted}), flush=True)
    return 0


def _describe_key(raw: str) -> tuple[Any, str]:
    from otto.llm.gateway import _detect_provider
    from otto.utils import keys as K

    if K.is_slack_token(raw):
        return None, "Slack read-only token" + (" — user" if "xoxp-" in raw else " — bot")
    provider = _detect_provider(raw)
    return provider, (provider.name if provider else "format not recognised — kept anyway")


def cmd_key(args: argparse.Namespace) -> int:
    from otto.llm.gateway import _detect_provider
    from otto.utils import keys as K

    if args.key_command == "add":
        if args.json:
            return _key_add_json()
        raw = (args.key or sys.stdin.readline() or "").strip()
        if not raw:
            print("No key given.")
            return 1
        provider, kind = _describe_key(raw)
        before = K.discover_keys()
        if any(k.key == raw for k in before):
            # Identical keys are stored once; say so instead of "Stored", which
            # reads as "something changed".
            print(f"Already stored, nothing changed: {K.mask(raw)}  ({kind})")
            print("`otto key list` shows every key; `otto key remove <prefix>` then `otto key add` replaces one.")
            return 0
        where = K.add_key(raw, use_keychain=_keychain())
        print(f"Stored {K.mask(raw)} → {where}  ({kind})")
        if provider is not None:
            same = [k for k in before if not K.is_slack_token(k.key)
                    and (_detect_provider(k.key) or provider).name == provider.name and k.key != raw]
            for k in same:
                print(f"Note: another {provider.name} key is still stored ({K.mask(k.key)}); "
                      f"remove it with `otto key remove {k.key[:10]}` if this one replaces it.")
        if K.is_slack_token(raw):
            print("Otto will read every channel, group and DM this token can see — GET only, nothing is ever written.")
        _restart_if_running()
        return 0
    if args.key_command == "list":
        found = K.discover_keys()
        if not found:
            print("No API keys configured.")
            return 0
        for k in found:
            label = "  (slack, read-only)" if K.is_slack_token(k.key) else ""
            print(f"{K.mask(k.key):24s} {k.source}{label}")
        return 0
    if args.key_command == "remove":
        n = K.remove_key(args.prefix)
        print(f"Removed {n} key(s)." if n else "No matching key.")
        if n:
            _restart_if_running()
        return 0 if n else 1
    print("Usage: otto key {add|list|remove}")
    return 1


def _key_add_json() -> int:
    """The menu bar's Add a Model Key…: the key on stdin, one JSON line back."""
    from otto.utils import keys as K

    raw = (sys.stdin.readline() or "").strip()
    if not raw:
        print(json.dumps({"ok": False, "error": "cancelled"}), flush=True)
        return 1
    if K.is_slack_token(raw):
        print(json.dumps({"ok": False, "error": "that is a Slack token — use Connect Slack… for it"}), flush=True)
        return 1
    provider, kind = _describe_key(raw)
    if provider is None:
        print(json.dumps({"ok": False, "error": "not a key Otto recognises (OpenRouter sk-or-…, OpenAI sk-…, "
                                                "Anthropic sk-ant-…, Gemini AIza…, Devin apk_user_…)"}), flush=True)
        return 1
    if any(k.key == raw for k in K.discover_keys()):
        print(json.dumps({"ok": True, "masked": K.mask(raw), "kind": kind, "where": "already stored", "restarted": False}), flush=True)
        return 0
    where = K.add_key(raw, use_keychain=_keychain())
    restarted = _restart_if_running(quiet=True)
    print(json.dumps({"ok": True, "masked": K.mask(raw), "kind": kind, "where": where, "restarted": restarted}), flush=True)
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(args: list[str] | None = None) -> int:
    parser = build_parser()
    parsed = parser.parse_args(args)
    cmd = parsed.command
    if cmd == "run":
        cmd = "start"

    if cmd != "start":
        configure_logging(verbose=parsed.verbose, console=parsed.verbose)

    if cmd == "start":
        return cmd_run(foreground=parsed.foreground, verbose=parsed.verbose)
    if cmd == "stop":
        return cmd_stop()
    if cmd == "restart":
        return cmd_restart(parsed.verbose)
    if cmd == "status":
        return cmd_status()
    if cmd == "open":
        return cmd_open()
    if cmd == "config":
        return cmd_config(as_json=parsed.json, save=parsed.save)
    if cmd == "install":
        return cmd_install()
    if cmd == "uninstall":
        return cmd_uninstall(keep_menubar=parsed.keep_menubar)
    if cmd == "logs":
        return cmd_logs(parsed.n, parsed.follow)
    if cmd == "slack":
        return cmd_slack(parsed)
    if cmd == "permissions":
        return cmd_permissions()
    if cmd == "key":
        return cmd_key(parsed)
    if cmd == "setup":
        script = paths.project_root() / "scripts" / "setup.sh"
        if script.exists():
            return subprocess.call(["/bin/bash", str(script)])
        print("Setup script not found.")
        return 1

    # No command: one-line state + help
    st = _api("/api/status")
    if st:
        print(f"Otto is running → http://localhost:{_port()}  ({st.get('items', 0)} items, {st.get('critical_items', 0)} important)")
        problems = st.get("problems") or []
        if problems:
            print(f"{len(problems)} thing{'s' if len(problems) != 1 else ''} need{'s' if len(problems) == 1 else ''} a look — `otto status`")
    else:
        print("Otto is not running. `otto start` to start, `otto install` to run at login.")
    print()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
