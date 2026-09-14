"""
Who is "you"?

Otto needs to tell asks aimed at the user from everything else. The names
come from three places, merged here:

* ``[user] name`` / ``aliases`` in ``config.toml`` (explicit, always wins),
* what Slack.app showed next to the "(you)" marker in the sidebar,
* ``auth.test`` when a Slack API token is configured.

This module is the single, process-wide registry the readers write into and
the intelligence layer reads from. It holds display names only — never ids,
tokens or e-mail addresses.
"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()
_LEARNED: list[str] = []
_MAX = 8
_NOT_A_NAME = frozenset({
    "messages", "dms", "home", "activity", "files", "you", "(you)", "slack", "directories", "huddles", "me",
})


def remember_self_name(name: str) -> bool:
    """Record a display name a source presented as the user's own. Returns True if new."""
    n = (name or "").strip()
    if len(n) < 2 or len(n) > 60 or n.lower() in _NOT_A_NAME:
        return False
    with _LOCK:
        if any(n.lower() == k.lower() for k in _LEARNED):
            return False
        if len(_LEARNED) >= _MAX:
            return False
        _LEARNED.append(n)
        return True


def learned_self_names() -> tuple[str, ...]:
    with _LOCK:
        return tuple(_LEARNED)


def self_names() -> list[str]:
    """Configured names first, then learned ones; deduplicated case-insensitively."""
    names: list[str] = []
    try:
        from otto.config import ConfigManager
        cfg = ConfigManager()
        configured = str(cfg.get_or("user.name", "") or "").strip()
        if configured:
            names.append(configured)
        for alias in cfg.get_or("user.aliases", []) or []:
            if str(alias).strip():
                names.append(str(alias).strip())
    except Exception:       # config is optional
        pass
    names.extend(learned_self_names())
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def profile() -> dict[str, object]:
    """``{"role": str, "focus": [str, …]}`` from ``[user]`` in config — what the user does and owns.

    Empty when unset. Used to explain items in the user's own terms and to
    notice messages about the things they said they care about.
    """
    role, focus = "", []
    try:
        from otto.config import ConfigManager
        cfg = ConfigManager()
        role = str(cfg.get_or("user.role", "") or "").strip()[:160]
        for term in cfg.get_or("user.focus", []) or []:
            t = str(term or "").strip()
            if 3 <= len(t) <= 60 and t.lower() not in {x.lower() for x in focus}:
                focus.append(t)
    except Exception:       # config is optional
        pass
    return {"role": role, "focus": focus[:24]}


def profile_text(p: dict[str, object] | None = None) -> str:
    """The profile as prompt lines; '' when nothing is known."""
    p = p if p is not None else profile()
    lines = []
    if p.get("role"):
        lines.append(f"Role: {p['role']}")
    focus = [str(f) for f in (p.get("focus") or []) if f]
    if focus:
        lines.append("Focus (projects, systems, topics they own or watch): " + ", ".join(focus))
    return "\n".join(lines)


def _reset_for_tests() -> None:
    with _LOCK:
        _LEARNED.clear()
