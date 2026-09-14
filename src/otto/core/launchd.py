"""
launchd integration — Otto as per-user LaunchAgents.

``otto install`` writes ``~/Library/LaunchAgents/com.otto.engine.plist`` and
loads it; launchd then starts the engine at login and restarts it if it
ever exits (``KeepAlive``). Logs go to the rotating file in the data dir
(``otto logs`` tails it).

When the menu bar app is built, a second agent
(``com.otto.menubar.plist``) opens it at login too, so badge and banners
survive a reboot. launchd brings it back only after a crash (an exit by
signal or with a non-zero status); *Quit Otto Menu Bar* exits cleanly and
sticks until the next login.

Everything here is plain ``launchctl``; no sudo, nothing outside the user's
home directory.
"""
from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

from otto import paths

logger = logging.getLogger("otto.core.launchd")


def _uid() -> int:
    return os.getuid()


def _domain() -> str:
    return f"gui/{_uid()}"


def build_plist(
    *,
    python: Optional[Path] = None,
    project_root: Optional[Path] = None,
    port: Optional[int] = None,
    refresh_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    python = python or paths.python_executable()
    root = project_root or paths.project_root()
    data_dir = paths.ensure_data_dir()
    args = [str(python), "-m", "otto.core.engine", "--no-console-log"]
    if port:
        args += ["--port", str(port)]
    if refresh_seconds:
        args += ["--refresh", str(refresh_seconds)]

    env = {
        "PYTHONPATH": str(root / "src"),
        "PYTHONUNBUFFERED": "1",
        "OTTO_HOME": str(root),
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
    }
    for var in ("OTTO_DATA_DIR",):
        if os.environ.get(var):
            env[var] = os.environ[var]

    return {
        "Label": paths.LAUNCH_AGENT_LABEL,
        "ProgramArguments": args,
        "WorkingDirectory": str(root),
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
        "ThrottleInterval": 10,
        # Standard (not Background): the engine must answer the menu bar and
        # browser promptly; Nice/LowPriorityIO keep it polite otherwise.
        "ProcessType": "Standard",
        "LowPriorityIO": True,
        "Nice": 5,
        "StandardOutPath": str(data_dir / "launchd.out.log"),
        "StandardErrorPath": str(data_dir / "launchd.err.log"),
    }


def build_menubar_plist(*, port: Optional[int] = None) -> Dict[str, Any]:
    """LaunchAgent that opens the menu bar app at login and again after a crash.

    ``KeepAlive`` is conditional: a clean exit (Quit, or the second-copy
    guard) is final; a crash or non-zero exit is relaunched after
    ``ThrottleInterval`` seconds, so a bug in the panel costs a few seconds
    of missing icon rather than the rest of the session.
    """
    env = {"OTTO_HOME": str(paths.project_root()), "OTTO_PYTHON": str(paths.python_executable())}
    if port:
        env["OTTO_PORT"] = str(port)
    for var in ("OTTO_DATA_DIR",):
        if os.environ.get(var):
            env[var] = os.environ[var]
    return {
        "Label": paths.MENUBAR_AGENT_LABEL,
        "ProgramArguments": [str(paths.menubar_binary())],
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
        "ThrottleInterval": 10,
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
    }


def _launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=check)


def is_installed() -> bool:
    return paths.launch_agent_plist().exists()


def is_loaded() -> bool:
    res = _launchctl("print", f"{_domain()}/{paths.LAUNCH_AGENT_LABEL}")
    return res.returncode == 0


def menubar_installed() -> bool:
    return paths.menubar_agent_plist().exists()


def _menubar_loaded() -> bool:
    return _launchctl("print", f"{_domain()}/{paths.MENUBAR_AGENT_LABEL}").returncode == 0


def install_menubar(*, port: Optional[int] = None) -> Optional[Path]:
    """Register the built menu bar app to open at login and start it now.

    Returns the plist path, or None when the app is not built. An instance
    started by hand (``open bin/Otto.app``) is stopped first so launchd owns
    the one that runs — two copies would mean two icons.
    """
    if not paths.menubar_binary().exists():
        return None
    plist_path = paths.menubar_agent_plist()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    if _menubar_loaded():
        _bootout_and_wait(paths.MENUBAR_AGENT_LABEL)
    subprocess.run(["pkill", "-x", "OttoMenuBar"], capture_output=True)
    with open(plist_path, "wb") as f:
        plistlib.dump(build_menubar_plist(port=port), f)
    os.chmod(plist_path, 0o644)
    _bootstrap(plist_path, paths.MENUBAR_AGENT_LABEL)
    logger.info("Installed LaunchAgent %s", plist_path)
    return plist_path


def uninstall_menubar() -> bool:
    """Unload and delete the menu bar agent (this quits the app). True if something was removed."""
    removed = False
    if _menubar_loaded():
        _launchctl("bootout", f"{_domain()}/{paths.MENUBAR_AGENT_LABEL}")
        removed = True
    plist_path = paths.menubar_agent_plist()
    if plist_path.exists():
        plist_path.unlink()
        removed = True
    return removed


def running_pid() -> Optional[int]:
    """PID from launchd if it is running the job, else from the engine's pid file."""
    res = _launchctl("print", f"{_domain()}/{paths.LAUNCH_AGENT_LABEL}")
    if res.returncode == 0:
        for line in res.stdout.splitlines():
            line = line.strip()
            if line.startswith("pid = "):
                try:
                    return int(line.split("=", 1)[1].strip())
                except ValueError:
                    break
    pid_file = paths.pid_file()
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            return None
    return None


def _bootout_and_wait(label: str, timeout: float = 15.0) -> None:
    """Unload ``label`` and wait until launchd has really let go of it.

    ``bootout`` returns while the old process is still shutting down; a
    ``bootstrap`` issued in that window is silently discarded once the
    teardown completes — the job would look installed and never run.
    """
    _launchctl("bootout", f"{_domain()}/{label}")
    deadline = time.time() + timeout
    while time.time() < deadline and _launchctl("print", f"{_domain()}/{label}").returncode == 0:
        time.sleep(0.25)


def _bootstrap(plist_path: Path, label: str) -> None:
    """Load ``plist_path`` (legacy verb on older macOS); raise when launchd refuses."""
    res = _launchctl("bootstrap", _domain(), str(plist_path))
    if res.returncode != 0 and _launchctl("print", f"{_domain()}/{label}").returncode != 0:
        legacy = _launchctl("load", "-w", str(plist_path))
        if legacy.returncode != 0 and _launchctl("print", f"{_domain()}/{label}").returncode != 0:
            raise RuntimeError(f"launchctl failed: {res.stderr.strip() or legacy.stderr.strip()}")


def install(*, port: Optional[int] = None, refresh_seconds: Optional[int] = None) -> Path:
    """Write the LaunchAgent plist and (re)load it. Returns the plist path."""
    plist_path = paths.launch_agent_plist()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    data = build_plist(port=port, refresh_seconds=refresh_seconds)

    if is_loaded():
        _bootout_and_wait(paths.LAUNCH_AGENT_LABEL)

    with open(plist_path, "wb") as f:
        plistlib.dump(data, f)
    os.chmod(plist_path, 0o644)

    _bootstrap(plist_path, paths.LAUNCH_AGENT_LABEL)
    _launchctl("kickstart", "-k", f"{_domain()}/{paths.LAUNCH_AGENT_LABEL}")
    logger.info("Installed LaunchAgent %s", plist_path)
    return plist_path


def uninstall() -> bool:
    """Unload and delete the LaunchAgent. Returns True if something was removed."""
    removed = False
    if is_loaded():
        _launchctl("bootout", f"{_domain()}/{paths.LAUNCH_AGENT_LABEL}")
        removed = True
    plist_path = paths.launch_agent_plist()
    if plist_path.exists():
        plist_path.unlink()
        removed = True
    return removed


def start() -> bool:
    if not is_installed():
        return False
    if not is_loaded():
        res = _launchctl("bootstrap", _domain(), str(paths.launch_agent_plist()))
        if res.returncode != 0:
            _launchctl("load", "-w", str(paths.launch_agent_plist()))
    _launchctl("kickstart", f"{_domain()}/{paths.LAUNCH_AGENT_LABEL}")
    return True


def stop() -> bool:
    """Stop the running job without uninstalling (launchd will not restart it until `start`)."""
    if not is_loaded():
        return False
    _launchctl("bootout", f"{_domain()}/{paths.LAUNCH_AGENT_LABEL}")
    return True


def restart() -> bool:
    if not is_installed():
        return False
    if is_loaded():
        _launchctl("kickstart", "-k", f"{_domain()}/{paths.LAUNCH_AGENT_LABEL}")
        return True
    return start()


def status() -> Dict[str, Any]:
    pid = running_pid()
    return {
        "installed": is_installed(),
        "loaded": is_loaded(),
        "running": pid is not None,
        "pid": pid,
        "plist": str(paths.launch_agent_plist()),
        "log": str(paths.log_file()),
        "menubar_installed": menubar_installed(),
    }
