"""
``otto slack connect | disconnect`` — a Slack token, set up like an API key.

The menu bar's *Connect Slack…* drives the same code through ``otto slack
connect --json`` (link out, token in on stdin, result out); ``otto status``
shows what the token can read.

Without a token Otto reads the one conversation Slack.app has on screen. With
a **read-only user token** it reads every channel, private group, DM and
thread you are in, every minute, whatever Slack is showing — the context the
briefing, the memory and the radar are built from.

The flow is deliberately boring, and as short as Slack allows:

1. open the browser on Slack's own *create an app* page with Otto's manifest
   already filled in (``api.slack.com/apps?new_app=1&manifest_json=…``, a
   documented link format) — the person signs in as themselves, clicks
   *Create*, *Install to Workspace*, *Allow*, and copies the *User OAuth
   Token*. Slack shows that token only on that page: there is no API that
   hands out a user token without a human clicking *Allow*, and Otto does not
   go around that by lifting session cookies out of Slack.app;
2. read the token with the terminal echo off (never as an argument on a
   shared machine, never into chat or a file you might commit);
3. verify it with ``auth.test`` and one page of ``conversations.list`` over
   the same GET-only WriteGuard client the engine uses;
4. store it in the 0600 key file (or the Keychain) via :mod:`otto.utils.keys`,
   replacing any older stored Slack token;
5. the caller restarts the engine and reports what it can now see.

Browser-session tokens (``xoxc-``/``xoxd-``) are refused: they need cookies,
expire without notice, and cannot be scoped down.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote

from otto.utils import keys as K

logger = logging.getLogger("otto.core.slack_connect")

# Read-only scopes. `channels/groups/im/mpim` cover public, private, DM and
# group DM; `:history` reads messages, `:read` lists conversations; `users:read`
# turns ids into names. Nothing here can post, react, edit, upload or join.
SCOPES: tuple[str, ...] = (
    "channels:history", "channels:read",
    "groups:history", "groups:read",
    "im:history", "im:read",
    "mpim:history", "mpim:read",
    "users:read",
)

_KINDS = (("public_channel", "channels"), ("private_channel", "private groups"), ("im", "DMs"), ("mpim", "group DMs"))


# The app, as Slack's manifest schema describes it: a name, the user scopes
# above, and every optional capability switched off (no bot user, no events,
# no slash commands, no redirect URLs — nothing that could act or listen).
# One structure, three forms: YAML from `otto slack connect --manifest` (and
# in the README), JSON in the create-app link, and `SCOPES` for the
# validation step.
MANIFEST: dict[str, Any] = {
    "display_information": {
        "name": "Otto (read-only)",
        "description": "Otto read only",
        "background_color": "#1f2933",
    },
    "oauth_config": {"scopes": {"user": list(SCOPES)}},
    "settings": {"org_deploy_enabled": False, "socket_mode_enabled": False, "token_rotation_enabled": False},
}

# Slack's documented way to open the "create an app" flow with a manifest
# pre-filled: https://docs.slack.dev/app-manifests/configuring-apps-with-app-manifests
CREATE_APP_URL = "https://api.slack.com/apps"
# How many times an empty Enter at the prompt re-opens the page (sign-in
# first, then the filled-in page again) before we take it as "not now".
REOPEN_LIMIT = 3

_MANIFEST_HEADER = (
    "# Otto — read-only Slack app manifest.\n"
    "# `otto slack connect` opens api.slack.com with this already filled in. By hand:\n"
    "# api.slack.com/apps → Create New App → From a manifest → YAML tab → paste this → Create,\n"
    "# then Install to Workspace → Allow, copy the *User OAuth Token* (xoxp-…) from\n"
    "# OAuth & Permissions, and run:  otto slack connect\n"
)


def _yaml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v)
    # Quote what YAML would otherwise misread: comment/anchor/flow indicators,
    # mapping colons, surrounding whitespace, empty strings.
    if not s or s != s.strip() or s[0] in "#&*!|>'\"%@`[{" or ": " in s or " #" in s or s.endswith(":"):
        return json.dumps(s)
    return s


def _yaml(value: Any, indent: int = 0) -> str:
    """Render the manifest's shapes (mappings, lists, strings, booleans) as YAML — no dependency needed."""
    pad = "  " * indent
    if isinstance(value, dict):
        return "".join(
            f"{pad}{k}:\n{_yaml(v, indent + 1)}" if isinstance(v, (dict, list)) else f"{pad}{k}: {_yaml_scalar(v)}\n"
            for k, v in value.items()
        )
    if isinstance(value, list):
        return "".join(f"{pad}- {_yaml_scalar(v)}\n" for v in value)
    raise TypeError(f"manifest cannot hold {type(value).__name__}")


def manifest_text() -> str:
    """The Slack app manifest that grants exactly :data:`SCOPES`, as YAML — paste it at api.slack.com/apps."""
    return _MANIFEST_HEADER + _yaml(MANIFEST)


def manifest_json() -> str:
    """The same manifest, compact JSON, for the create-app link."""
    return json.dumps(MANIFEST, separators=(",", ":"), sort_keys=False)


def create_app_url() -> str:
    """Slack's *create an app* page with Otto's manifest filled in: sign in, pick a workspace, click Create.

    Nothing secret travels in the link — it is the public manifest, the same
    text ``otto slack connect --manifest`` prints — and the page does not
    create anything until the person clicks.
    """
    return f"{CREATE_APP_URL}?new_app=1&manifest_json={quote(manifest_json(), safe='')}"


def open_in_browser(url: str) -> bool:
    """Hand the create-app link to the default browser (macOS ``open``). False when that is not possible.

    Only :data:`CREATE_APP_URL` is ever opened — this is not a general URL
    opener — and the call is fire-and-forget: the person continues in the
    browser and comes back to the prompt.
    """
    if not url.startswith(CREATE_APP_URL + "?"):
        return False
    try:
        return subprocess.run(["/usr/bin/open", url], capture_output=True, timeout=15, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@dataclass
class Validation:
    ok: bool
    error: str = ""
    user: str = ""
    team: str = ""
    kind: str = ""                      # "user" | "bot"
    granted: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    readable: dict[str, int] = field(default_factory=dict)   # kind → count on the first page
    unlisted: list[str] = field(default_factory=list)        # kinds the token cannot list

    def coverage(self) -> str:
        parts = [f"{n} {label}" for k, label in _KINDS for n in [self.readable.get(k, 0)] if n]
        text = ", ".join(parts) if parts else "no conversations readable"
        if self.unlisted:
            text += " — cannot list " + ", ".join(dict(_KINDS).get(u, u) for u in self.unlisted)
        return text


def classify(token: str) -> tuple[bool, str]:
    """(usable, reason). Refuses browser-session tokens and unknown formats."""
    t = (token or "").strip()
    if not t:
        return False, "no token given"
    if t.startswith(("xoxc-", "xoxd-")):
        return False, "that is a browser-session token (xoxc/xoxd); Otto needs a User OAuth Token (xoxp-…) from a Slack app"
    if not K.is_slack_token(t):
        return False, "not a Slack token (expected xoxp-… or xoxb-…)"
    return True, ""


async def _validate_async(token: str, client: Any) -> Validation:
    v = Validation(ok=False, kind="user" if "xoxp-" in token else "bot")
    try:
        resp = await client.get("https://slack.com/api/auth.test")
    except Exception as e:
        v.error = f"could not reach slack.com: {type(e).__name__}"
        return v
    if resp.status_code != 200:
        v.error = f"auth.test: HTTP {resp.status_code}"
        return v
    try:
        data = resp.json()
    except Exception:
        v.error = "auth.test: bad JSON"
        return v
    if not data.get("ok"):
        err = str(data.get("error") or "unknown_error")
        v.error = {"invalid_auth": "token rejected (invalid_auth) — copy it again from the app's OAuth page",
                   "token_revoked": "token revoked — reinstall the app and copy the new token",
                   "account_inactive": "the account behind this token is deactivated",
                   "token_expired": "token expired — reinstall the app"}.get(err, f"auth.test: {err}")
        return v
    v.user = str(data.get("user") or "")
    v.team = str(data.get("team") or "")
    granted = str(resp.headers.get("x-oauth-scopes") or "")
    v.granted = [s.strip() for s in granted.split(",") if s.strip()]
    if v.granted:
        v.missing = [s for s in SCOPES if s not in v.granted]
    # One page per kind: what can this token actually list right now?
    for kind, _label in _KINDS:
        try:
            r = await client.get("https://slack.com/api/conversations.list",
                                 params={"types": kind, "limit": 200, "exclude_archived": "true"})
            payload = r.json() if r.status_code == 200 else {}
        except Exception:
            payload = {}
        if payload.get("ok"):
            chans = payload.get("channels") or []
            if kind == "public_channel":
                chans = [c for c in chans if c.get("is_member") is not False]
            v.readable[kind] = len(chans)
        elif payload.get("error") == "missing_scope":
            v.unlisted.append(kind)
    v.ok = True
    return v


def validate(token: str, *, client: Any = None) -> Validation:
    """Verify a token with GET-only calls; never raises."""
    async def _run() -> Validation:
        own = client is None
        c = client
        if own:
            from otto.adapters.slack import build_slack_http_client
            c = build_slack_http_client(token)
        try:
            return await _validate_async(token, c)
        finally:
            if own:
                try:
                    await c.aclose()
                except Exception:
                    pass
    try:
        return asyncio.run(_run())
    except Exception as e:  # pragma: no cover - defensive
        return Validation(ok=False, error=f"validation failed: {type(e).__name__}: {e}"[:160])


PROMPT = "Paste the User OAuth Token (xoxp-…), it will not be echoed: "


def read_token(prompt: Callable[[str], Optional[str]] | None = None) -> Optional[str]:
    """Read the token with echo off (getpass); a plain line when there is no TTY.

    Returns ``""`` for an empty line (Enter with nothing pasted) and ``None``
    when the person gave up (Ctrl-C, Ctrl-D, closed stdin) — the two mean
    different things to :func:`connect`.
    """
    import getpass

    if prompt is not None:
        got = prompt(PROMPT)
        return None if got is None else got.strip()
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        return None if line == "" else line.strip()
    try:
        return (getpass.getpass(PROMPT) or "").strip()
    except (EOFError, KeyboardInterrupt):
        return None


def header_lines() -> list[str]:
    return [
        "",
        "  Connect Slack (read-only)",
        "  " + "─" * 62,
        "  Otto will read every channel, private group, DM and thread you are in,",
        "  every minute, with GET requests only. It cannot post, react, edit or join.",
        "",
    ]


def steps_lines(*, opened: bool, url: str) -> list[str]:
    """What to do in the browser and back here — worded for whether the browser opened by itself."""
    lines: list[str] = []
    if opened:
        lines += [
            "  Your browser is opening Slack's \"create an app\" page (api.slack.com) with",
            "  Otto's manifest already filled in. There:",
        ]
    else:
        lines += [
            "  Open this link — Slack's \"create an app\" page with Otto's manifest filled in:",
            "",
            f"    {url}",
            "",
            "  There:",
        ]
    lines += [
        "    1. Sign in as yourself if asked, pick your workspace, click Create",
        "    2. Install to Workspace → Allow   (the button is on the page you land on",
        "       and under OAuth & Permissions; \"Request to Install\" means an admin",
        "       has to approve it first — send the request, come back when they have)",
        "       Allow window flashed and closed without finishing? Reload OAuth &",
        "       Permissions first — the token is usually already there. If not, install",
        "       from a normal tab instead: Manage Distribution → Sharable URL → open it",
        "       → Allow, then back to OAuth & Permissions.",
        "    3. OAuth & Permissions → copy the User OAuth Token — it starts with xoxp-",
        "       (not the Bot User OAuth Token, xoxb-: that only sees where a bot is invited)",
        "  Back here:",
        "    4. Paste it below (the terminal will not echo it)",
    ]
    if opened:
        lines += [
            "       Had to sign in first? Slack forgets the filled-in manifest on the way",
            "       through sign-in — press Enter here with nothing pasted and Otto opens",
            "       the page again, filled in.",
            "",
        ]
    else:
        lines += [
            "       (If Slack asks you to sign in first, open the link again afterwards —",
            "       the filled-in manifest does not survive the sign-in step.)",
            "",
        ]
    lines += [
        "  By hand:  otto slack connect --manifest   prints the manifest YAML",
        "            for Create New App → From a manifest.",
        "",
        "  Scopes, all read-only: " + " ".join(SCOPES),
        "",
    ]
    return lines


def intro_lines(*, opened: bool = False, url: str | None = None) -> list[str]:
    return header_lines() + steps_lines(opened=opened, url=url or create_app_url())


def connect(token: Optional[str] = None, *, use_keychain: bool = False, prompt: Callable[[str], str] | None = None,
            client: Any = None, out: Callable[[str], None] = print, open_browser: bool = True,
            opener: Callable[[str], bool] = open_in_browser, interactive: Optional[bool] = None,
            ) -> tuple[int, Optional[Validation]]:
    """Guided setup. Returns (exit code, validation) — the caller restarts the engine on 0.

    With no token in hand: print the header, open the browser on the
    create-app page (only in an interactive terminal, only unless asked not
    to, and only that one fixed URL), print the steps — with the link spelled
    out whenever the browser did not open — and read the token with echo off.
    """
    if not token:
        for line in header_lines():
            out(line)
        if interactive is None:
            interactive = sys.stdin.isatty()
        url = create_app_url()
        opened = bool(open_browser and interactive) and opener(url)
        for line in steps_lines(opened=opened, url=url):
            out(line)
        reopened = 0
        while True:
            got = read_token(prompt)
            if got is None:
                out("  Cancelled. Nothing was stored.")
                return 1, None
            if got:
                token = got
                break
            # Enter with nothing pasted: the usual reason is that Slack wanted a
            # sign-in first and then dropped the manifest. Open it again, filled in.
            if opened and reopened < REOPEN_LIMIT and opener(url):
                reopened += 1
                out("  Opened the page again with the manifest filled in. Paste the token when you have it.")
                continue
            out("  Nothing pasted. Nothing was stored.")
            return 1, None
    token = (token or "").strip()
    usable, why = classify(token)
    if not usable:
        out(f"  ✗ {why}")
        return 1, None
    out("  Checking the token with Slack (auth.test, conversations.list — read-only)…")
    v = validate(token, client=client)
    if not v.ok:
        out(f"  ✗ {v.error}")
        out("    Nothing was stored.")
        return 1, v
    stored = [k for k in K.discover_keys() if K.is_slack_token(k.key)]
    already = any(k.key == token for k in stored)
    where = "already stored" if already else K.add_key(token, use_keychain=use_keychain)
    # One Slack token at a time: an older stored one would only confuse
    # `otto status` and could win the lookup. Environment tokens are the
    # shell's business, not ours.
    replaced = [k for k in stored if k.key != token and not k.source.startswith("env:")]
    for old in replaced:
        K.remove_key(old.key)
    who = f"{v.user} at {v.team}" if v.user and v.team else (v.user or v.team or "verified")
    out(f"  ✓ {v.kind} token {K.mask(token)} — {who} ({where})")
    if replaced:
        out("    Replaced the previous token " + ", ".join(K.mask(k.key) for k in replaced))
    out(f"    Can read: {v.coverage()}")
    if v.missing:
        out("  ⚠ scopes not granted: " + ", ".join(v.missing) + " — those conversation kinds stay invisible until you add them and reinstall")
    if v.kind == "bot":
        out("  ⚠ bot tokens only see channels the bot was invited to; a user token (xoxp-) sees what you see")
    return 0, v


def status(*, client: Any = None, engine_status: Optional[dict] = None, out: Callable[[str], None] = print) -> int:
    tok = K.slack_token()
    out("")
    if tok is None:
        out("  Slack: no token — Otto reads the conversation Slack.app has on screen.")
        out("         otto slack connect   adds every channel, DM and thread you are in.")
        out("")
        return 1
    kind = "user" if "xoxp-" in tok.key else "bot"
    out(f"  Slack: {kind} token {K.mask(tok.key)}  ({tok.source})")
    v = validate(tok.key, client=client)
    if v.ok:
        who = f"{v.user} at {v.team}" if v.user and v.team else "verified"
        out(f"         {who}; can read {v.coverage()}")
        if v.missing:
            out("         scopes not granted: " + ", ".join(v.missing))
    else:
        out(f"         ✗ {v.error}")
    api = None
    for s in (engine_status or {}).get("source_status") or []:
        if s.get("source") == "slack api":
            api = s
    if api is None:
        out("         engine: not polling it yet (otto restart if this stays)" if engine_status else "         engine: not running")
    elif api.get("ok"):
        out(f"         engine: reading {api.get('channels', 0)} channels, {api.get('items', 0)} messages this window"
            + (f" — {api['note']}" if api.get("note") else ""))
    else:
        out(f"         engine: ✗ {api.get('error') or 'not readable'}")
    out("")
    return 0 if v.ok else 1


def disconnect(*, out: Callable[[str], None] = print) -> int:
    tokens = [k for k in K.discover_keys() if K.is_slack_token(k.key)]
    if not tokens:
        out("  No Slack token stored.")
        return 1
    removed = 0
    for t in tokens:
        if t.source.startswith("env:"):
            out(f"  {K.mask(t.key)} comes from ${t.source[4:]} — unset it in your shell; Otto cannot remove it")
            continue
        removed += K.remove_key(t.key)
    if removed:
        out(f"  Removed {removed} Slack token(s). Otto goes back to reading what Slack.app shows on screen.")
    return 0 if removed else 1
