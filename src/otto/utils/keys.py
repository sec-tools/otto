"""
API key discovery and storage.

Otto works with **no keys at all** (local heuristics, the Slack window on
screen). Keys unlock the optional model enrichment and the full Slack read.
Resolution order — first hit wins per provider:

1. Environment variables (``OTTO_API_KEY`` — any supported format — plus the
   conventional ``OPENROUTER_API_KEY``, ``OPENAI_API_KEY``,
   ``ANTHROPIC_API_KEY``, ``GEMINI_API_KEY``, ``DEVIN_API_KEY``).
2. ``config.toml`` → ``[keys]`` (``slack = "xoxp-…"``, ``model = ["sk-…"]``):
   the file the menu bar's *Edit Config…* opens, written mode 0600.
3. macOS Keychain (service ``Otto``), via the built-in ``security`` CLI so
   there is no third-party dependency.
4. The key file in the data dir (``~/Library/Application Support/Otto/api.key``,
   mode 0600, one key per line, ``#`` comments allowed) — where *Connect
   Slack…*, *Add a Model Key…* and ``otto key add`` put keys.
5. Legacy locations (``~/.otto/api.key`` and ``<repo>/api.key``). These are
   read with a warning so existing setups keep working; ``otto key add``
   stores keys in the data dir (then delete the old file).

A key file inside the git checkout is a liability (one careless ``git add``
away from a leak), which is why the repo location is legacy-only and
``*.key`` is git-ignored.
"""
from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from otto import paths

logger = logging.getLogger("otto.utils.keys")

try:
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError:
        _toml = None  # type: ignore[assignment]

KEYCHAIN_SERVICE = "Otto"
KEYCHAIN_ACCOUNT = "api-keys"

_ENV_VARS = (
    "OTTO_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "DEVIN_API_KEY",
    "SLACK_TOKEN",
    "SLACK_USER_TOKEN",
    "SLACK_BOT_TOKEN",
)

# Slack tokens Otto can use read-only: user (xoxp) and bot (xoxb) tokens,
# including the rotating "xoxe.xoxp-" form. Browser-session tokens (xoxc/xoxd)
# need cookies and are deliberately not supported.
_SLACK_TOKEN_PREFIXES = ("xoxp-", "xoxb-", "xoxe.xoxp-", "xoxe.xoxb-")


@dataclass(frozen=True)
class DiscoveredKey:
    key: str
    source: str  # "env:OPENAI_API_KEY", "keychain", "file:<path>", "legacy:<path>"


def is_slack_token(key: str) -> bool:
    k = (key or "").strip()
    return k.startswith(_SLACK_TOKEN_PREFIXES) and len(k) > 20


def slack_token() -> DiscoveredKey | None:
    """The first configured Slack token (user tokens preferred), or None."""
    tokens = [k for k in discover_keys() if is_slack_token(k.key)]
    tokens.sort(key=lambda k: 0 if "xoxp-" in k.key else 1)
    return tokens[0] if tokens else None


def mask(key: str) -> str:
    """``sk-or-v1-4b…c9d1`` style masking for display."""
    key = key.strip()
    if len(key) <= 12:
        return "***"
    return f"{key[:10]}…{key[-4:]}"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _parse_key_text(text: str) -> list[str]:
    keys: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Tolerate dotenv-style lines: OPENAI_API_KEY=sk-…
        if "=" in line and re.match(r"^[A-Z][A-Z0-9_]*\s*=", line):
            line = line.split("=", 1)[1].strip().strip('"').strip("'")
        if line and line not in keys:
            keys.append(line)
    return keys


def _read_key_file(path: Path) -> list[str]:
    try:
        if not path.is_file():
            return []
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            logger.warning(
                "Key file %s is readable by other users (mode %o); chmod 600 it.",
                path, mode,
            )
        return _parse_key_text(path.read_text(encoding="utf-8"))
    except OSError as e:
        logger.warning("Could not read key file %s: %s", path, e)
        return []


_WARNED_LOOSE: set[str] = set()


def config_keys(path: Path | None = None) -> tuple[str, list[str]]:
    """``(slack_token, model_keys)`` from ``[keys]`` in config.toml — ``("", [])`` when absent or unreadable.

    Parsed directly (a few kilobytes) because this runs on every refresh and
    status poll; problems with the file itself are reported by the config
    module, not here. A loose file mode is warned about once, only when the
    file actually holds a key.
    """
    path = Path(path) if path is not None else paths.config_file()
    if _toml is None or not path.is_file():
        return "", []
    try:
        with open(path, "rb") as f:
            data = _toml.load(f)
    except Exception:
        return "", []
    section = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        return "", []
    slack = section.get("slack")
    slack = slack.strip() if isinstance(slack, str) else ""
    raw_model = section.get("model")
    if isinstance(raw_model, str):
        raw_model = [raw_model]
    model = [m.strip() for m in raw_model if isinstance(m, str) and m.strip()] if isinstance(raw_model, list) else []
    if (slack or model) and str(path) not in _WARNED_LOOSE:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                _WARNED_LOOSE.add(str(path))
                logger.warning("%s holds keys but is readable by other users (mode %o); chmod 600 it.", path, mode)
        except OSError:
            pass
    return slack, model


def _keychain_available() -> bool:
    return os.environ.get("OTTO_DISABLE_KEYCHAIN") != "1" and Path("/usr/bin/security").exists()


def read_keychain() -> list[str]:
    """Return keys stored in the macOS Keychain (may be several lines)."""
    if not _keychain_available():
        return []
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.debug("Keychain read failed: %s", e)
        return []
    if result.returncode != 0:
        return []
    # Multi-line secrets are stored newline-joined.
    return _parse_key_text(result.stdout.replace("\\n", "\n"))


def discover_keys() -> list[DiscoveredKey]:
    """All keys Otto can see, in priority order, deduplicated."""
    found: list[DiscoveredKey] = []
    seen: set[str] = set()

    def _add(key: str, source: str) -> None:
        key = key.strip()
        if key and key not in seen:
            seen.add(key)
            found.append(DiscoveredKey(key=key, source=source))

    for var in _ENV_VARS:
        val = os.environ.get(var, "")
        if val:
            for k in _parse_key_text(val.replace(",", "\n")):
                _add(k, f"env:{var}")

    slack, model = config_keys()
    config_source = f"config:{paths.config_file()}"
    if slack:
        _add(slack, config_source)
    for k in model:
        _add(k, config_source)

    for k in read_keychain():
        _add(k, "keychain")

    for k in _read_key_file(paths.key_file()):
        _add(k, f"file:{paths.key_file()}")

    for legacy in paths.legacy_key_files():
        keys = _read_key_file(legacy)
        if keys:
            logger.warning(
                "Reading API key from legacy location %s — store it with `otto key add` "
                "(it goes to %s) and delete the old file", legacy, paths.key_file(),
            )
            for k in keys:
                _add(k, f"legacy:{legacy}")

    return found


def discover_key_strings() -> list[str]:
    return [d.key for d in discover_keys()]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _write_key_file(keys: list[str]) -> Path:
    path = paths.key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    body = "# Otto API keys — one per line. Managed by `otto key`.\n" + "".join(f"{k}\n" for k in keys)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.chmod(str(tmp), 0o600)
    tmp.replace(path)
    os.chmod(str(path), 0o600)
    return path


def write_keychain(keys: list[str]) -> bool:
    """Store keys (newline-joined) in the macOS Keychain. Returns success."""
    if not _keychain_available():
        return False
    if not keys:
        return delete_keychain()
    secret = "\n".join(keys)
    try:
        result = subprocess.run(
            ["/usr/bin/security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT, "-w", secret, "-T", "/usr/bin/security"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.debug("Keychain write failed: %s", e)
        return False


def delete_keychain() -> bool:
    if not _keychain_available():
        return False
    try:
        result = subprocess.run(
            ["/usr/bin/security", "delete-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def add_key(key: str, *, use_keychain: bool = False) -> str:
    """
    Persist a key. Default: the 0600 key file in the data dir (works
    unattended under launchd with no GUI prompts). ``use_keychain=True``
    stores it in the macOS Keychain instead. Returns where it went.
    """
    key = key.strip()
    if not key:
        raise ValueError("empty key")

    if use_keychain and _keychain_available():
        existing = read_keychain()
        if key not in existing:
            existing.append(key)
        if write_keychain(existing):
            return "keychain"
        logger.info("Keychain unavailable; falling back to key file")

    existing_file = _read_key_file(paths.key_file())
    if key not in existing_file:
        existing_file.append(key)
    return f"file:{_write_key_file(existing_file)}"


def remove_key(key_or_prefix: str) -> int:
    """Remove keys matching exactly or by prefix from the Keychain, the key file and
    ``[keys]`` in config.toml. Returns how many were removed."""
    removed = 0
    needle = key_or_prefix.strip()
    if not needle:
        return 0

    def hit(k: str) -> bool:
        return k == needle or k.startswith(needle)

    kc = read_keychain()
    kept = [k for k in kc if not hit(k)]
    if len(kept) != len(kc):
        removed += len(kc) - len(kept)
        write_keychain(kept)

    fk = _read_key_file(paths.key_file())
    kept_f = [k for k in fk if not hit(k)]
    if len(kept_f) != len(fk):
        removed += len(fk) - len(kept_f)
        _write_key_file(kept_f)

    removed += _remove_from_config(hit)
    return removed


def _remove_from_config(hit) -> int:
    """Blank ``slack`` / drop ``model`` entries matching *hit* in config.toml's ``[keys]``."""
    slack, model = config_keys()
    count = 0
    if not slack and not model:
        return 0
    from otto.config import replace_setting

    path = paths.config_file()
    if slack and hit(slack):
        if replace_setting("keys", "slack", "", path):
            count += 1
        else:
            logger.warning("Could not edit %s — remove the Slack token from [keys] by hand", path)
    kept_model = [m for m in model if not hit(m)]
    if len(kept_model) != len(model):
        if replace_setting("keys", "model", kept_model, path):
            count += len(model) - len(kept_model)
        else:
            logger.warning("Could not edit %s — remove the key from [keys] model by hand", path)
    return count


def migrate_legacy_keys(*, delete_legacy: bool = True, use_keychain: bool = False) -> list[Path]:
    """Move keys out of legacy locations (notably the git checkout). Returns migrated paths."""
    migrated: list[Path] = []
    for legacy in paths.legacy_key_files():
        keys = _read_key_file(legacy)
        if not keys:
            continue
        for k in keys:
            add_key(k, use_keychain=use_keychain)
        migrated.append(legacy)
        if delete_legacy:
            try:
                legacy.unlink()
            except OSError as e:
                logger.warning("Could not delete legacy key file %s: %s", legacy, e)
    # Fix permissions on the primary file if it exists.
    kf = paths.key_file()
    if kf.exists():
        try:
            os.chmod(str(kf), 0o600)
        except OSError:
            pass
    return migrated
