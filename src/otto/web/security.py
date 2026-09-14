"""
Security policy for the localhost briefing server.

Threat model: Otto's web server binds to 127.0.0.1 only, but the user's
browser is a confused deputy. Any web page they visit can make requests to
``http://localhost:7077`` — so the server must assume every request may be
cross-site until proven otherwise.

Defences implemented here:

* **Host header allow-list** — blocks DNS-rebinding (``evil.com`` → 127.0.0.1).
* **No CORS wildcard** — cross-origin pages cannot *read* responses.
* **Same-origin check for mutations** — ``POST`` only, and the ``Origin`` /
  ``Sec-Fetch-Site`` headers, when present, must be same-origin. Requests
  from non-browser clients (the menu bar app, ``curl``) carry neither header
  and are allowed.
* **Content-Security-Policy with a per-response nonce** — the page contains
  no inline event handlers, so a stray unescaped string can never execute.
* **URL scheme allow-list** — links rendered into the page must be
  ``http(s)``, ``slack:`` or ``ical:``; anything else (``javascript:``,
  ``data:``, ``file:``) is dropped.
"""
from __future__ import annotations

import secrets
from urllib.parse import urlsplit

ALLOWED_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})
ALLOWED_URL_SCHEMES = frozenset({"http", "https", "slack", "ical"})


def host_allowed(host_header: str | None, port: int) -> bool:
    """Only accept requests addressed to us by loopback name."""
    if not host_header:
        return False
    host = host_header.strip().lower()
    # Strip port. IPv6 literal looks like "[::1]:7077".
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return False
        name, rest = host[: end + 1], host[end + 1:]
        if rest and rest != f":{port}":
            return False
        return name in ALLOWED_HOSTNAMES
    if ":" in host:
        name, _, p = host.rpartition(":")
        if p != str(port):
            return False
    else:
        name = host
    return name in ALLOWED_HOSTNAMES


def _origin_is_local(origin: str, port: int) -> bool:
    try:
        parts = urlsplit(origin.strip())
    except ValueError:
        return False
    if parts.scheme != "http":
        return False
    hostname = (parts.hostname or "").lower()
    if hostname == "::1":
        hostname = "[::1]"
    if hostname not in ALLOWED_HOSTNAMES:
        return False
    return (parts.port or 80) == port


def same_origin_ok(headers, port: int) -> bool:
    """
    Decide whether a state-changing request may proceed.

    ``headers`` is any mapping with a case-insensitive ``.get`` (the
    ``http.server`` message object qualifies).
    """
    fetch_site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if fetch_site in ("cross-site", "same-site"):
        # "same-site" would mean e.g. another port on localhost — still not us.
        return False

    origin = headers.get("Origin")
    if origin:
        if origin.strip().lower() == "null":
            return False
        return _origin_is_local(origin, port)

    referer = headers.get("Referer")
    if referer:
        return _origin_is_local(referer, port)

    # No browser provenance headers at all → native client (menu bar, curl).
    return True


def new_nonce() -> str:
    return secrets.token_urlsafe(16)


def csp_header(nonce: str) -> str:
    return (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    )


def safe_url(url: object) -> str:
    """Return ``url`` if it uses an allowed scheme, else ``""``."""
    if not isinstance(url, str):
        return ""
    candidate = url.strip()
    if not candidate or any(ch in candidate for ch in ("\n", "\r", "\x00")):
        return ""
    try:
        scheme = urlsplit(candidate).scheme.lower()
    except ValueError:
        return ""
    return candidate if scheme in ALLOWED_URL_SCHEMES else ""


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cache-Control": "no-store",
}
