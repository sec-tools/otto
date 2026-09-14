from __future__ import annotations
"""
Configuration: ``config.toml`` in the data dir over baked-in defaults.

Every key below is read by the running engine (``ConfigManager().get_or``);
nothing here is decorative. Layers, highest priority first: CLI, LEARNED and
UI (programmatic overrides, unused by the shipped commands), FILE
(``~/Library/Application Support/Otto/config.toml``, legacy ``~/.otto``),
DEFAULTS (this module). Environment variables such as ``OTTO_PORT`` are
applied by the code that reads the key, above the file.
"""

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]
import os
import re
from dataclasses import dataclass, field, fields
from enum import Enum, auto
from pathlib import Path
from typing import Any

from otto.utils.logging import get_logger

logger = get_logger("config")


class ConfigLayer(Enum):
    """Configuration priority layers, lowest to highest."""
    DEFAULTS = auto()
    FILE = auto()
    UI = auto()
    LEARNED = auto()
    CLI = auto()


@dataclass
class NotificationConfig:
    """Banner policy (see core/notify.py). Times are local, "HH:MM"."""
    urgency_threshold: float = 0.8          # only items at or above this urgency become banners
    max_per_hour: int = 5                   # the rest wait for the next window
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "07:00"
    critical_bypass_threshold: float = 0.95  # urgency that may interrupt quiet hours


@dataclass
class LLMConfig:
    """Daily spend guard; providers come from the keys you add (``otto key add``)."""
    daily_token_limit: int = 500_000
    daily_cost_limit_usd: float = 2.00      # estimate from token counts; past it the day runs on heuristics


@dataclass
class EngineConfig:
    """Background engine (web server + refresh loop) settings."""
    refresh_seconds: int = 60          # how often every interface gets fresh data
    port: int = 7077                   # loopback-only HTTP port
    screenshots: bool = True           # capture Slack window thumbnails (never focuses Slack)
    lookback_hours: int = 24           # how far back adapters look on each refresh
    recall: bool = True                # briefing includes remembered messages inside the window, not only what is on screen


@dataclass
class LinksConfig:
    """Link intelligence settings."""
    fetch: bool = False                # opt-in: download page titles / READMEs for shared links


@dataclass
class UserConfig:
    """Who the user is in the sources Otto reads — lets it tell asks aimed at *you* from noise,
    and explain each item in terms of *your* role and the things you said you own."""
    name: str = ""                     # display name as Slack shows it, e.g. "Alex Kim"
    aliases: list[str] = field(default_factory=list)   # handles / nicknames: ["alex", "akim"]
    role: str = ""                     # one line, e.g. "security engineer on the platform team"
    focus: list[str] = field(default_factory=list)     # projects / systems / topics you own: ["runner image", "billing"]
    directives: list[str] = field(default_factory=list)  # standing rules in your words, applied to every briefing


@dataclass
class KeysConfig:
    """Model keys and the Slack token (``[keys]``) — read by :mod:`otto.utils.keys`
    alongside api.key, the Keychain and the environment. The file is written
    mode 0600 for this reason."""
    slack: str = ""                    # read-only Slack user token ("xoxp-…"; a bot token "xoxb-…" also works)
    model: list[str] = field(default_factory=list)  # model keys: OpenRouter sk-or-…, OpenAI sk-…, Anthropic sk-ant-…, Gemini AIza…
    keychain: bool = False             # keys added from the menu bar / `otto key add` go to the macOS Keychain instead of api.key


@dataclass
class DebugConfig:
    """Diagnostics (``[debug]``)."""
    dump_extracts: bool = False        # write raw window/tab text to <data dir>/debug/ (also OTTO_DUMP_EXTRACTS=1)


@dataclass
class OttoConfig:
    """Top-level Otto configuration: one section per TOML table."""
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    links: LinksConfig = field(default_factory=LinksConfig)
    user: UserConfig = field(default_factory=UserConfig)
    keys: KeysConfig = field(default_factory=KeysConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)
    debug_mode: bool = False           # DEBUG logging (same as OTTO_DEBUG=1 or `otto -v`)
    log_level: str = "INFO"            # DEBUG / INFO / WARNING / ERROR for otto.log


def _config_to_flat_dict(config: OttoConfig) -> dict[str, Any]:
    """Flatten a nested config into dot-separated keys."""
    result: dict[str, Any] = {}
    for f in fields(config):
        value = getattr(config, f.name)
        if hasattr(value, "__dataclass_fields__"):
            for inner_f in fields(value):
                result[f"{f.name}.{inner_f.name}"] = getattr(value, inner_f.name)
        else:
            result[f.name] = value
    return result


class ConfigManager:
    """Manages layered configuration with resolution through priority layers."""

    def __init__(self, config_path: Path | None = None) -> None:
        self._layers: dict[ConfigLayer, dict[str, Any]] = {
            layer: {} for layer in ConfigLayer
        }
        if config_path is None:
            from otto import paths
            config_path = paths.config_file()
        self.config_path = Path(config_path)
        # What is wrong with the file, in one line, for `otto status` and the
        # menu bar ("" when it parsed and every value has the right type).
        self.file_error = ""
        # False when the file exists but could not be parsed at all (nothing
        # from it applies); a single wrong value leaves this True.
        self.parsed = True
        # Keys in the file Otto does not know (a typo such as `refresh_second`
        # silently does nothing otherwise).
        self.unknown_keys: list[str] = []

        # Layer 1: Defaults
        self._layers[ConfigLayer.DEFAULTS] = _config_to_flat_dict(OttoConfig())

        # Layer 2: File
        self._load_file_config()

    def _load_file_config(self) -> None:
        """Load configuration from TOML file if it exists."""
        if not self.config_path.exists():
            return
        if tomllib is None:
            self.file_error = "TOML support unavailable (install tomli)"
            logger.warning(self.file_error)
            return
        try:
            with open(self.config_path, "rb") as f:
                data = tomllib.load(f)
        except Exception as e:
            self.file_error = _describe_toml_error(e)
            self.parsed = False
            logger.warning("config.toml not applied: %s", self.file_error, extra={"event_type": "config_error"})
            return
        # Flatten nested TOML sections
        flat: dict[str, Any] = {}
        for section, values in data.items():
            if isinstance(values, dict):
                for key, val in values.items():
                    flat[f"{section}.{key}"] = val
            else:
                flat[section] = values
        defaults = self._layers[ConfigLayer.DEFAULTS]
        problems: list[str] = []
        for key, val in flat.items():
            if key not in defaults:
                self.unknown_keys.append(key)
                continue
            wrong = _type_problem(key, val, defaults[key])
            if wrong:
                problems.append(wrong)
                continue          # a value of the wrong type is ignored, the default stands
            self._layers[ConfigLayer.FILE][key] = val
        if problems:
            self.file_error = "; ".join(problems[:3])
            logger.warning("config.toml: %s", self.file_error, extra={"event_type": "config_error"})
        logger.debug("Loaded config", extra={"event_type": "config_loaded"})

    def get(self, key: str) -> Any:
        """Resolve a config value through layers (CLI > LEARNED > UI > FILE > DEFAULTS)."""
        for layer in reversed(ConfigLayer):
            if key in self._layers[layer]:
                return self._layers[layer][key]
        raise KeyError(f"Configuration key '{key}' not found in any layer.")

    def get_or(self, key: str, default: Any = None) -> Any:
        """Like :meth:`get` but returns ``default`` instead of raising."""
        try:
            return self.get(key)
        except KeyError:
            return default

    def set(self, layer: ConfigLayer, key: str, value: Any) -> None:
        """Set a value in a specific layer."""
        self._layers[layer][key] = value

    def reset_layer(self, layer: ConfigLayer) -> None:
        """Clear all values in a specific layer."""
        self._layers[layer] = {}
        logger.info(
            f"Reset config layer: {layer.name}",
            extra={"event_type": "config_reset"},
        )

    def get_effective(self) -> dict[str, Any]:
        """Return the fully resolved configuration as a flat dict with layer attribution."""
        result: dict[str, Any] = {}
        for key in self._layers[ConfigLayer.DEFAULTS]:
            for layer in reversed(ConfigLayer):
                if key in self._layers[layer]:
                    result[key] = {
                        "value": self._layers[layer][key],
                        "source": layer.name,
                    }
                    break
        return result


# ---------------------------------------------------------------------------
# The file itself: written once with every setting explained, edited by hand
# (menu bar → Edit Config…, or `otto config`), re-read by the running engine.
# ---------------------------------------------------------------------------

def _describe_toml_error(e: Exception) -> str:
    """``line 12: Expected '=' after a key`` rather than the parser's class name."""
    msg = " ".join(str(e).split())
    m = re.search(r"\(at line (\d+), column (\d+)\)", msg)
    if m:
        return f"line {m.group(1)}: {msg[:m.start()].strip().rstrip(',')}"
    return msg[:160] or type(e).__name__


def _type_problem(key: str, value: Any, default: Any) -> str:
    """'' when *value* can stand in for *default*; otherwise one line saying what was expected."""
    if isinstance(default, bool):
        ok = isinstance(value, bool)
        want = "true or false"
    elif isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
        want = "a whole number"
    elif isinstance(default, float):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        want = "a number"
    elif isinstance(default, str):
        ok = isinstance(value, str)
        want = "text in quotes"
    elif isinstance(default, list):
        ok = isinstance(value, list) and all(isinstance(x, str) for x in value)
        want = 'a list like ["a", "b"]'
    else:
        return ""
    return "" if ok else f"{key} should be {want}"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    s = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{s}"'


# One entry per setting, in the order they appear in the file. Every key of
# every dataclass above is here (a test checks), so the file a user opens is
# the whole story, not a sample.
_TEMPLATE_SECTIONS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    ("user", "Who you are in what Otto reads, so it can tell asks aimed at *you* from noise\n"
             "and explain each item in your terms.", (
        ("name", 'your display name as Slack shows it, e.g. "Alex Kim"'),
        ("aliases", 'handles and nicknames people use for you: ["alex", "akim"]'),
        ("role", 'one line, e.g. "security engineer on the platform team"'),
        ("focus", 'projects, systems or topics you own: ["runner image", "billing"]'),
        ("directives", 'standing rules in your own words, checked against every item:\n'
                       '#   ["always flag anything about the payments migration", "never bother me with release notes"]'),
    )),
    ("engine", "The background engine.", (
        ("refresh_seconds", "how often everything is re-read (at least 15)"),
        ("port", "loopback-only port for the page and the menu bar (takes effect after `otto restart`)"),
        ("screenshots", "pictures of the Slack window on items and banners (needs Screen Recording; Slack is never focused)"),
        ("lookback_hours", "how far back each refresh looks, 1 to 168"),
        ("recall", "also show remembered messages inside that window, not only what is on screen right now"),
    )),
    ("notifications", "Banners from the menu bar app.", (
        ("urgency_threshold", "only items at or above this urgency (0 to 1) become a banner"),
        ("max_per_hour", "the rest wait for the next hour"),
        ("quiet_hours_start", 'local time, "HH:MM"'),
        ("quiet_hours_end", 'local time, "HH:MM"'),
        ("critical_bypass_threshold", "urgency that may interrupt quiet hours"),
    )),
    ("keys", "Model keys and the Slack token. Paste them here, or add them from the menu bar\n"
             "(Connect Slack… / Add a Model Key…), which keeps them in api.key next to this file;\n"
             "Otto reads both. This file is readable by you alone (mode 600). Without a Slack\n"
             "token Otto reads the Slack window on screen; with one it reads every conversation.", (
        ("slack", 'read-only Slack user token, "xoxp-…" (Connect Slack… walks you through getting one)'),
        ("model", 'model keys, any of: ["sk-or-v1-…"] OpenRouter · "sk-…" OpenAI · "sk-ant-…" Anthropic · "AIza…" Gemini'),
        ("keychain", "keys added from the menu bar or `otto key add` go to the macOS Keychain instead of api.key"),
    )),
    ("llm", "Spend guard for the model; past either limit the day runs on local signals.", (
        ("daily_token_limit", ""),
        ("daily_cost_limit_usd", "an estimate from token counts"),
    )),
    ("links", "", (
        ("fetch", "download titles and READMEs of links people share (public hosts only)"),
    )),
    ("debug", "", (
        ("dump_extracts", "write what was read from each app and tab to <data dir>/debug/ (last 20)"),
    )),
)
_TEMPLATE_TOP: tuple[tuple[str, str], ...] = (
    ("log_level", 'DEBUG, INFO, WARNING or ERROR for otto.log (after `otto restart`)'),
    ("debug_mode", "same as log_level = \"DEBUG\""),
)
_TEMPLATE_HEADER = """\
# Otto settings.
#
# Edit, save, done: the running engine picks changes up within a minute
# (port and logging apply after `otto restart`). Every setting is listed with
# the value in force; delete a line and the default comes back. Keys go under
# [keys] below — this file is readable by you alone. If a line will not parse
# Otto keeps the last good value and says so in the menu bar and in
# `otto status`.
"""


def _entry(key: str, value: Any, note: str) -> list[str]:
    """One setting as it appears in the file: ``key = value  # note`` (extra note lines follow)."""
    note_lines = note.split("\n")
    assignment = f"{key} = {_toml_value(value)}"
    first = assignment + (f"{' ' * max(2, 34 - len(assignment))}# {note_lines[0]}" if note_lines[0] else "")
    return [first, *note_lines[1:]]


def _section_block(section: str, intro: str, entries: tuple[tuple[str, str], ...], current: dict[str, Any]) -> list[str]:
    out = [f"[{section}]"]
    for line in intro.splitlines():
        out.append(f"# {line}" if line else "#")
    for key, note in entries:
        out.extend(_entry(key, current[f"{section}.{key}"], note))
    return out


def render_config(values: dict[str, Any] | None = None) -> str:
    """The whole config file with *values* (flat ``section.key`` dict; defaults where missing)."""
    current = dict(_config_to_flat_dict(OttoConfig()))
    current.update(values or {})
    out = [_TEMPLATE_HEADER]
    for key, note in _TEMPLATE_TOP:
        out.extend(_entry(key, current[key], note))
    for section, intro, entries in _TEMPLATE_SECTIONS:
        out.append("")
        out.extend(_section_block(section, intro, entries, current))
    return "\n".join(out) + "\n"


# Section intros an earlier template wrote that a later one contradicts: when
# the completion below touches such a section, the old intro (matched line for
# line, comments only) is replaced by the current one so the file does not say
# "keys are not in this file" above the line where they go.
_RETIRED_INTROS: dict[str, tuple[str, ...]] = {
    "keys": (
        "# Model keys and the Slack token are not in this file: they live in api.key next to it",
        "# (menu bar → Add a Model Key… / Connect Slack…, or `otto key add`).",
    ),
}


def complete_config_text(text: str) -> tuple[str, list[str]]:
    """Add settings a file written by an older Otto does not mention yet.

    Returns ``(text, added)`` — *added* the flat names that were inserted, each
    with its default value and explanation, in place: a missing key goes at the
    end of its section, a missing section at the end of the file, a missing
    top-level setting before the first section. Nothing the user wrote moves or
    changes; the one exception is a section intro comment from an older template
    that the new lines would contradict (``_RETIRED_INTROS``), which is swapped
    for the current one. A file that does not parse is returned as is. The
    editor calls this on open so the file stays the whole story.
    """
    if tomllib is None:
        return text, []
    try:
        data = tomllib.loads(text)
    except Exception:
        return text, []
    current = _config_to_flat_dict(OttoConfig())
    lines = text.split("\n")
    trailing_newline = text.endswith("\n")
    if trailing_newline:
        lines = lines[:-1]
    added: list[str] = []

    def header_index(section: str) -> int:
        for i, line in enumerate(lines):
            if line.strip() == f"[{section}]":
                return i
        return -1

    def section_end(start: int) -> int:
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].lstrip().startswith("["):
                end = i
                break
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        return end

    missing_top = [(k, note) for k, note in _TEMPLATE_TOP if k not in data]
    if missing_top:
        first_header = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
        while first_header > 0 and not lines[first_header - 1].strip():
            first_header -= 1
        block: list[str] = []
        for key, note in missing_top:
            block.extend(_entry(key, current[key], note))
            added.append(key)
        lines[first_header:first_header] = block

    retired_swapped = False
    for section, intro, entries in _TEMPLATE_SECTIONS:
        table = data.get(section)
        if table is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(_section_block(section, intro, entries, current))
            added.extend(f"{section}.{k}" for k, _ in entries)
            continue
        if not isinstance(table, dict):
            continue
        start = header_index(section)
        if start < 0:  # declared some other way (dotted keys, inline table) — leave it be
            continue
        missing = [(k, note) for k, note in entries if k not in table]
        if missing:
            block = []
            for key, note in missing:
                block.extend(_entry(key, current[f"{section}.{key}"], note))
                added.append(f"{section}.{key}")
            end = section_end(start)
            lines[end:end] = block
        retired = _RETIRED_INTROS.get(section)
        if retired and tuple(lines[start + 1:start + 1 + len(retired)]) == retired:
            lines[start + 1:start + 1 + len(retired)] = [f"# {line}" if line else "#" for line in intro.splitlines()]
            retired_swapped = True

    if not added and not retired_swapped:
        return text, []
    return "\n".join(lines) + "\n", added


def template_keys() -> set[str]:
    keys = {k for k, _ in _TEMPLATE_TOP}
    for section, _intro, entries in _TEMPLATE_SECTIONS:
        keys.update(f"{section}.{k}" for k, _ in entries)
    return keys


def ensure_config_file(path: Path | None = None) -> Path:
    """Create ``config.toml`` with every setting explained, if it does not exist yet.

    Standing directives added through the retired ``otto directive`` command are
    carried into ``[user] directives`` so the file is the one place they live
    from then on (the old store is renamed, not deleted).
    """
    from otto import paths

    path = Path(path) if path is not None else paths.config_file()
    if path.exists():
        return path
    values: dict[str, Any] = {}
    try:
        from otto.intelligence.history import retire_stored_directives
        stored = retire_stored_directives()
        if stored:
            values["user.directives"] = stored
    except Exception as e:  # pragma: no cover - migration is best effort
        logger.debug("directive migration skipped: %s", e)
    _write_private(path, render_config(values))
    logger.info("Wrote %s", path)
    return path


def _write_private(path: Path, text: str) -> None:
    """Write *text* to *path* atomically, readable by the owner only (it may hold keys)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(str(tmp), 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(str(path), 0o600)
    except OSError:  # pragma: no cover - unusual file systems
        pass


def load_config_text(path: Path | None = None) -> dict[str, Any]:
    """The file's text for an editor (creating it first if needed): ``{"ok", "path", "text", "error", "unknown_keys"}``."""
    from otto import paths

    path = Path(path) if path is not None else paths.config_file()
    try:
        ensure_config_file(path)
        text = path.read_text(encoding="utf-8")
        completed, added = complete_config_text(text)
        if completed != text:
            _write_private(path, completed)
            text = completed
            if added:
                logger.info("Added %d setting(s) written by a newer Otto to %s: %s", len(added), path.name, ", ".join(added))
    except OSError as e:
        return {"ok": False, "path": str(path), "text": "", "error": f"could not read {path.name}: {e.strerror or e}", "unknown_keys": []}
    st = config_status(path)
    return {"ok": True, "path": str(path), "text": text, "error": st.get("error", ""), "unknown_keys": list(st.get("unknown_keys") or [])}


def check_config_text(text: str) -> dict[str, Any]:
    """Validate *text* as Otto's config without writing it.

    ``{"ok": bool, "error": str, "unknown_keys": [...]}`` — ``ok`` is False only
    when the TOML does not parse (nothing from such a file would apply);
    ``error`` may also carry a type problem for a file that parses (Otto then
    keeps the default for that one setting).
    """
    if tomllib is None:
        return {"ok": False, "error": "TOML support unavailable (install tomli)", "unknown_keys": []}
    try:
        data = tomllib.loads(text)
    except Exception as e:
        return {"ok": False, "error": _describe_toml_error(e), "unknown_keys": []}
    defaults = _config_to_flat_dict(OttoConfig())
    unknown: list[str] = []
    problems: list[str] = []
    for section, values in data.items():
        items = values.items() if isinstance(values, dict) else [(None, values)]
        for key, val in items:
            flat = f"{section}.{key}" if key is not None else section
            if flat not in defaults:
                unknown.append(flat)
                continue
            wrong = _type_problem(flat, val, defaults[flat])
            if wrong:
                problems.append(wrong)
    return {"ok": True, "error": "; ".join(problems[:3]), "unknown_keys": unknown[:5]}


def save_config_text(text: str, path: Path | None = None) -> dict[str, Any]:
    """Write an edited config if it parses: ``{"ok", "path", "error", "unknown_keys", "saved"}``.

    A file that does not parse is *not* written (``ok`` False, ``saved``
    False) — the editor keeps the text so the line can be fixed. Type problems
    and unknown settings are saved and reported, exactly as the engine would
    report them. The file stays mode 0600.
    """
    from otto import paths

    path = Path(path) if path is not None else paths.config_file()
    if not text.endswith("\n"):
        text += "\n"
    verdict = check_config_text(text)
    if not verdict["ok"]:
        return {"ok": False, "path": str(path), "error": verdict["error"], "unknown_keys": [], "saved": False}
    try:
        _write_private(path, text)
    except OSError as e:
        return {"ok": False, "path": str(path), "error": f"could not write {path.name}: {e.strerror or e}", "unknown_keys": [], "saved": False}
    _STATUS_CACHE.update(key=None, value=None)
    logger.info("config.toml saved from the editor")
    return {"ok": True, "path": str(path), "error": verdict["error"], "unknown_keys": verdict["unknown_keys"], "saved": True}


def _split_inline_value(rest: str) -> tuple[str, str] | None:
    """``'"abc"  # note'`` → ``('"abc"', '  # note')``; ``None`` when the value is not on this one line."""
    s = rest
    if not s:
        return None
    if s[0] == '"' or s[0] == "'":
        quote = s[0]
        i = 1
        while i < len(s):
            if s[i] == "\\" and quote == '"':
                i += 2
                continue
            if s[i] == quote:
                return s[: i + 1], s[i + 1:]
            i += 1
        return None
    if s[0] == "[":
        depth, i, in_str = 0, 0, ""
        while i < len(s):
            ch = s[i]
            if in_str:
                if ch == "\\" and in_str == '"':
                    i += 2
                    continue
                if ch == in_str:
                    in_str = ""
            elif ch in "\"'":
                in_str = ch
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return s[: i + 1], s[i + 1:]
            i += 1
        return None
    m = re.match(r"[^\s#]+", s)
    return (m.group(0), s[m.end():]) if m else None


def replace_setting(section: str, key: str, value: Any, path: Path | None = None) -> bool:
    """Change one ``key = value`` line inside ``[section]`` in place, keeping everything else
    (comments included). Returns False — and writes nothing — when the file is missing, does
    not parse, lacks that line, or the value spans several lines.

    Used to take a key *out* of ``[keys]`` (``otto key remove``, Connect Slack… retiring an
    old token). Values are rendered with :func:`_toml_value`; the result must parse.
    """
    from otto import paths

    path = Path(path) if path is not None else paths.config_file()
    try:
        original = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if tomllib is None:
        return False
    try:
        tomllib.loads(original)
    except Exception:
        return False
    lines = original.split("\n")
    current = ""
    pattern = re.compile(rf"^(\s*){re.escape(key)}\s*=\s*")
    for index, line in enumerate(lines):
        header = re.match(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(#.*)?$", line)
        if header:
            current = header.group(1)
            continue
        if current != section:
            continue
        m = pattern.match(line)
        if not m:
            continue
        split = _split_inline_value(line[m.end():])
        if split is None:
            return False
        _old, rest = split
        lines[index] = f"{m.group(1)}{key} = {_toml_value(value)}{rest}"
        text = "\n".join(lines)
        try:
            tomllib.loads(text)
        except Exception:
            return False
        try:
            _write_private(path, text)
        except OSError:
            return False
        _STATUS_CACHE.update(key=None, value=None)
        return True
    return False


_STATUS_CACHE: dict[str, Any] = {"key": None, "value": None}


def config_status(path: Path | None = None) -> dict[str, Any]:
    """``{"path", "exists", "error", "unknown_keys", "modified"}`` for the status API and the CLI.

    Parsed again only when the file changed (the menu bar asks every ten seconds).
    """
    from otto import paths

    path = Path(path) if path is not None else paths.config_file()
    try:
        st = path.stat()
        key: Any = (str(path), st.st_mtime_ns, st.st_size)
        modified = st.st_mtime
    except OSError:
        key, modified = (str(path), None), 0.0
    if _STATUS_CACHE["key"] == key and _STATUS_CACHE["value"] is not None:
        return dict(_STATUS_CACHE["value"])
    error, unknown = "", []
    if modified:
        mgr = ConfigManager(path)
        error, unknown = mgr.file_error, list(mgr.unknown_keys)
    value = {"path": str(path), "exists": bool(modified), "error": error,
             "unknown_keys": unknown[:5], "modified": modified}
    _STATUS_CACHE.update(key=key, value=value)
    return dict(value)
