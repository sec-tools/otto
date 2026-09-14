"""
What needs a person's attention about Otto itself — and the one thing to do.

The engine already knows everything the old ``otto test`` used to probe: which
source is blocked on a macOS permission, whether the Slack token was rejected
or lacks a scope, which model key came back 401, whether Screen Recording is
granted, whether ``config.toml`` parsed, whether a menu bar app is listening.
This module turns that into a short list the menu bar, the page and
``otto status`` all show the same way, each entry carrying an *action* the
menu bar can offer as a button:

    permissions       run the macOS permission walk-through
    slack             connect (or reconnect) Slack
    config            open config.toml
    key_remove:<pfx>  remove the key with that prefix
    menubar           open the menu bar app
    restart           restart the engine

Nothing here talks to the network or the desktop; it is a pure function of
the last refresh's payload plus two cheap local reads (config, key store).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from otto.web.render import _SRC_NAMES, _short_reason

# A menu bar app polls /api/notifications every 15 s; twice that with margin.
MENUBAR_SILENT_S = 45.0


def _problem(key: str, title: str, detail: str = "", action: str = "") -> Dict[str, str]:
    return {"key": key, "title": title, "detail": detail, "action": action}


def _key_for_provider(name: str) -> tuple:
    """``(prefix, source)`` of the stored key for a provider name — ``("", "")`` if unknown.

    The prefix feeds ``key_remove:<prefix>``; the source tells whether that
    can work (Keychain / api.key) or the key lives in config.toml, where the
    fix is to edit the file. Runs on every status poll, so nothing here logs.
    """
    try:
        from otto.llm.gateway import provider_name_for_key
        from otto.utils.keys import discover_keys, is_slack_token
        for k in discover_keys():
            if is_slack_token(k.key) or k.source.startswith("env"):
                continue
            if provider_name_for_key(k.key) == name:
                return k.key[:10], k.source
    except Exception:
        pass
    return "", ""


def _key_prefix_for_provider(name: str) -> str:
    """The stored key's prefix for a provider name, for ``key_remove:<prefix>`` ('' if unknown)."""
    return _key_for_provider(name)[0]


def source_problems(source_status: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for s in source_status or []:
        src = str(s.get("source") or "")
        name = _SRC_NAMES.get(src, src.title() or "A source")
        err = str(s.get("error") or "")
        note = str(s.get("note") or "")
        if s.get("ok"):
            if src == "slack api" and note and "scope" in note.lower():
                out.append(_problem("slack_scope", "Slack token is missing a scope", note, "slack"))
            continue
        if not err or err == "app not open":
            continue
        low = err.lower()
        if src == "slack api":
            if "rejected" in low or "revoked" in low or "invalid_auth" in low:
                out.append(_problem("slack_token", "Slack token was rejected",
                                    "Reconnect Slack to get a fresh one.", "slack"))
            elif "scope" in low:
                out.append(_problem("slack_scope", "Slack token is missing a scope", err, "slack"))
            continue
        reason = _short_reason(err)
        if reason in ("needs Accessibility", "needs permission"):
            out.append(_problem(f"perm_{src}", f"{name} needs a macOS permission", err, "permissions"))
    return out


def llm_problems(llm: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for p in (llm or {}).get("providers") or []:
        state = str(p.get("state") or "")
        name = str(p.get("name") or "model")
        low = state.lower()
        if not state or low == "ok":
            continue
        if "rejected" in low or "401" in low or "403" in low:
            prefix, source = _key_for_provider(name)
            if source.startswith("config"):
                detail, action = "It is in config.toml under [keys] — replace or remove it there.", "config"
            else:
                detail, action = "Every refresh retries it; remove it or replace it.", (f"key_remove:{prefix}" if prefix else "")
            out.append(_problem(f"key_{name}", f"{name} key was rejected", detail, action))
        elif "quota" in low or "billing" in low or "429" in low or "insufficient" in low:
            out.append(_problem(f"quota_{name}", f"{name} is out of quota", state, ""))
    return out


def screenshot_problems(shots: Dict[str, Any]) -> List[Dict[str, str]]:
    if not shots or not shots.get("enabled", True) or shots.get("granted") is not False:
        return []
    return [_problem("screen_recording", "Thumbnails are off: Screen Recording not granted",
                     "Items and banners carry no picture of their source until it is.", "permissions")]


def config_problems(cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not cfg:
        return out
    if cfg.get("error"):
        out.append(_problem("config_error", "config.toml has a problem", str(cfg["error"]), "config"))
    unknown = [str(k) for k in cfg.get("unknown_keys") or []]
    if unknown:
        out.append(_problem("config_unknown", "config.toml: unknown setting " + ", ".join(unknown[:3]),
                            "Ignored — check the spelling.", "config"))
    return out


def menubar_problems(client_seen_seconds: Optional[float], installed: bool) -> List[Dict[str, str]]:
    """Installed to run at login but not listening: banners would go nowhere."""
    if not installed:
        return []
    if client_seen_seconds is None or client_seen_seconds > MENUBAR_SILENT_S:
        return [_problem("menubar", "Menu bar app isn't running",
                         "Banners and the Briefings panel need it.", "menubar")]
    return []


def problems(data: Dict[str, Any], *, config: Optional[Dict[str, Any]] = None,
             client_seen_seconds: Optional[float] = None, menubar_installed: Optional[bool] = None) -> List[Dict[str, str]]:
    """Everything worth a look, most consequential first. Never raises."""
    out: List[Dict[str, str]] = []
    try:
        out += source_problems(data.get("source_status") or [])
        out += llm_problems(data.get("llm") or {})
        out += screenshot_problems(data.get("screenshots") or {})
        out += config_problems(config or {})
        if menubar_installed is None:
            # The plist on disk, not launchctl: this runs on every status poll.
            try:
                from otto import paths
                menubar_installed = paths.menubar_agent_plist().exists()
            except Exception:
                menubar_installed = False
        out += menubar_problems(client_seen_seconds, menubar_installed)
    except Exception:  # pragma: no cover - a status line must never fail a request
        pass
    seen: set = set()
    unique = []
    for p in out:
        if p["key"] not in seen:
            seen.add(p["key"])
            unique.append(p)
    return unique
