"""
Otto briefing HTTP server (loopback only).

Endpoints
---------
GET  /                      briefing page
GET  /briefing              same; ``?partial=1`` returns JSON fragments for in-place updates
GET  /api/briefing          full briefing data (JSON)
GET  /api/status            counts, last refresh, engine state (JSON)
GET  /api/notifications     pending banners for the menu bar app
GET  /static/screenshots/…  Slack window thumbnails
POST /api/refresh           trigger a refresh now
POST /api/dismiss           id=…
POST /api/snooze            id=… hours=…
POST /api/clear             dismiss every item shown (scope=notes: every Worth knowing note and radar row instead)
POST /api/notifications/ack ids=a,b,c

All state-changing endpoints are POST-only and require same-origin browser
provenance (or none, for native clients). See :mod:`otto.web.security`.

This module only knows how to *serve*; refreshing is delegated to whatever
sets :attr:`BriefingData.refresh_hook` (normally :class:`otto.core.engine.OttoEngine`).
Standalone (``python -m otto.web.server``) it falls back to an in-process
collector so it keeps working on its own.
"""
from __future__ import annotations

import html
import json
import logging
import math
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as _ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from otto import paths
from otto.web import state
from otto.web.collect import (
    capture_slack_screenshot,
    clean_text,
    collect_briefing_data,
    format_relative_time,
    is_duplicate_item,
    merge_duplicate_items,
)
from otto.web.render import ERROR_HTML, LOADING_HTML, h, render_body, render_briefing_html, render_page, worth_knowing_ids
from otto.web.security import SECURITY_HEADERS, csp_header, host_allowed, new_nonce, same_origin_ok

logger = logging.getLogger("otto.web.server")

OTTO_PORT = paths.DEFAULT_PORT

# Backwards-compatible aliases: the briefing pipeline used to live in this
# module, and tests/older callers still import these names from here.
_clean_text = clean_text
_format_time = format_relative_time
_is_duplicate_item = is_duplicate_item
_merge_duplicate_items = merge_duplicate_items
_h = h
_load_dismissed = state.load_dismissed
_save_dismissed = state.save_dismissed
_load_snoozed = state.load_snoozed
_save_snoozed = state.save_snoozed
_is_snoozed = state.is_snoozed

__all__ = [
    "BriefingData", "GLOBAL_DATA", "OTTO_PORT", "OttoRequestHandler", "ThreadingHTTPServer",
    "build_status", "make_server", "refresh_briefing_data", "start_server",
    "capture_slack_screenshot", "collect_briefing_data", "render_briefing_html",
]


class BriefingData:
    """Thread-safe holder for the latest briefing plus refresh bookkeeping."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {}
        self._last_updated = 0.0
        self._refreshing = False
        self._last_error = ""
        self._last_duration = 0.0
        self.refresh_hook: Optional[Callable[[], None]] = None

    def update(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self._data = dict(data)
            self._last_updated = time.time()
            self._last_error = ""

    def get(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    @property
    def last_updated(self) -> float:
        with self._lock:
            return self._last_updated

    # -- refresh bookkeeping --------------------------------------------------

    def begin_refresh(self) -> bool:
        with self._lock:
            if self._refreshing:
                return False
            self._refreshing = True
            self._refresh_started = time.time()
            return True

    def end_refresh(self, *, error: str = "", duration: float = 0.0) -> None:
        with self._lock:
            self._refreshing = False
            self._last_duration = duration
            if error:
                self._last_error = error

    @property
    def refreshing(self) -> bool:
        with self._lock:
            return self._refreshing

    def refreshing_for(self) -> float:
        """Seconds the current refresh has been running (0 when idle)."""
        with self._lock:
            return time.time() - getattr(self, "_refresh_started", time.time()) if self._refreshing else 0.0

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    @property
    def last_duration(self) -> float:
        with self._lock:
            return self._last_duration

    def request_refresh(self) -> str:
        """Ask whoever owns refreshing to do one now. Returns a status string."""
        if self.refreshing:
            return "already_refreshing"
        hook = self.refresh_hook or _default_refresh_hook
        try:
            hook()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Refresh request failed: %s", e)
            return "error"
        return "refreshing"


GLOBAL_DATA = BriefingData()


def refresh_briefing_data() -> None:
    """Synchronous refresh into GLOBAL_DATA (used standalone and by tests)."""
    if not GLOBAL_DATA.begin_refresh():
        logger.info("Refresh already in progress, skipping")
        return
    started = time.monotonic()
    error = ""
    try:
        data = collect_briefing_data()
        GLOBAL_DATA.update(data)
        logger.info("Briefing data refreshed: %d items", data.get("total_items", 0))
    except Exception as e:
        error = str(e)
        logger.error("Failed to refresh briefing: %s", e)
    finally:
        GLOBAL_DATA.end_refresh(error=error, duration=time.monotonic() - started)


def _default_refresh_hook() -> None:
    threading.Thread(target=refresh_briefing_data, name="otto-refresh", daemon=True).start()


def find_item(data: Dict[str, Any], item_id: str) -> tuple[Dict[str, Any], str, str] | None:
    """``(item, source, channel)`` for an id in the current briefing, or None."""
    for section in data.get("sections", []) or []:
        src = str(section.get("source") or "")
        for ch in section.get("channels", []) or []:
            for item in ch.get("items", []) or []:
                if item_id in state.item_ids(item):
                    return item, src, str(ch.get("name") or "")
    return None


def _all_ids(item_id: str, data: Dict[str, Any] | None = None) -> list[str]:
    """The id plus every id of the copies folded into that item (see state.item_ids)."""
    found = find_item(data if data is not None else GLOBAL_DATA.get(), item_id)
    return sorted(state.item_ids(found[0]) | {item_id}) if found else [item_id]


def _close_commitments(ids: Iterable[str]) -> None:
    """A dismissed open loop (``radar:<sha>``) is closed in Otto's memory too, so it never resurfaces."""
    radar_ids = [i for i in ids if str(i).startswith("radar:")]
    if not radar_ids:
        return
    try:
        from otto.intelligence.knowledge import default_store
        store = default_store()
        for item_id in radar_ids:
            store.set_commitment_status(item_id[len("radar:"):], "dismissed")
    except Exception as e:  # pragma: no cover - memory is optional
        logger.debug("could not close radar items: %s", e)


def record_feedback(kind: str, item_id: str, *, data: Dict[str, Any] | None = None) -> bool:
    """
    Write one open / expand / snooze / dismiss / clear to Otto's memory.

    Only items on the current briefing count (an id nobody can map to a
    channel and a sender teaches nothing). Never raises: feedback is a
    nicety, the request that carried it must still succeed.
    """
    try:
        found = find_item(data if data is not None else GLOBAL_DATA.get(), item_id)
        if found is None:
            return False
        item, src, channel = found
        from otto.intelligence.knowledge import default_store
        return default_store().record_feedback(
            kind, item_id=item_id, channel=channel, sender=str(item.get("sender") or ""), source=src,
        )
    except Exception as e:  # pragma: no cover - memory is optional
        logger.debug("feedback not recorded (%s %s): %s", kind, item_id, e)
        return False


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def build_status(data: Dict[str, Any], store: BriefingData = GLOBAL_DATA) -> Dict[str, Any]:
    hidden = state.hidden_ids()
    items = [
        i for s in data.get("sections", []) for c in s.get("channels", []) for i in c.get("items", [])
        if not state.is_hidden(i, hidden)
    ]
    important = [i for i in items if i.get("urgency") in ("critical", "high")]
    important.sort(key=lambda i: float(i.get("urgency_score", 0) or 0), reverse=True)
    top = important[0] if important else None
    headline = (top.get("summary") or top.get("text") or "") if top else ""
    return {
        "running": True,
        "items": len(items),
        "critical_items": len(important),
        "top_headline": headline[:80],
        "last_updated": store.last_updated,
        "refreshing": store.refreshing,
        "last_error": store.last_error,
        "last_refresh_seconds": round(store.last_duration, 2),
        "phases": data.get("phases", {}),
        "recalled": int(data.get("recalled", 0) or 0),
        "aged_out": int(data.get("aged_out", 0) or 0),
        "screenshots": data.get("screenshots") or {},
        "sections": len(data.get("sections", [])),
        "ai_powered": bool(data.get("ai_powered")),
        "llm": data.get("llm", {}),
        "sources": data.get("sources_polled", []),
        "sources_failed": data.get("sources_failed", []),
        "source_status": data.get("source_status", []),
        "generated_at": data.get("generated_at", ""),
        "radar": _radar_summary(data.get("radar") or {}, hidden),
        "digest": str((data.get("digest") or {}).get("digest") or "")[:240],
        "config": _config_status(),
        "version": _version(),
    }


def _config_status() -> Dict[str, Any]:
    """Where config.toml is and whether it parsed — the menu bar's Edit Config… needs the path."""
    try:
        from otto.config import config_status
        return config_status()
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("config status unavailable: %s", e)
        return {}


def _radar_summary(radar: Dict[str, Any], hidden: set) -> Dict[str, Any]:
    """Counts the CLI and menu bar can show without the full payload."""
    def _visible(key: str) -> int:
        return sum(1 for r in radar.get(key) or [] if r.get("id") not in hidden)
    return {
        "todo": _visible("todo"),
        "overdue": sum(1 for r in radar.get("todo") or [] if r.get("overdue") and r.get("id") not in hidden),
        "waiting": _visible("waiting"),
        "open_calls": _visible("open_calls"),
        "upcoming": _visible("upcoming"),
        "patterns": _visible("patterns"),
        "attention": list(radar.get("attention") or [])[:3],
        "memory": radar.get("memory") or {},
    }


def _version() -> str:
    try:
        from otto import __version__
        return __version__
    except Exception:
        return "dev"


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class ThreadingHTTPServer(_ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:  # noqa: D401
        """Clients hanging up mid-response are routine; keep them out of stderr."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            logger.debug("Client %s disconnected: %s", client_address, exc)
            return
        logger.warning("Request from %s failed: %r", client_address, exc)


class OttoRequestHandler(BaseHTTPRequestHandler):
    """Request handler; the engine may set ``notifier`` for /api/notifications."""

    server_version = "Otto"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    notifier: Any = None  # set by the engine

    # -- plumbing -------------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("%s - %s", self.address_string(), format % args)

    @property
    def _port(self) -> int:
        try:
            return int(self.server.server_address[1])
        except Exception:
            return OTTO_PORT

    def _respond(self, code: int, content_type: str, body: bytes,
                 extra: Dict[str, str] | None = None, head_only: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200, head_only: bool = False) -> None:
        self._respond(code, "application/json; charset=utf-8", json.dumps(payload, default=str).encode("utf-8"),
                      head_only=head_only)

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message}, code=code)

    def _read_form(self) -> Dict[str, str]:
        """Parse a small urlencoded/JSON POST body (bounded)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 64 * 1024:
            self.close_connection = True  # don't try to resync the stream
            return {}
        raw = self.rfile.read(length)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype == "application/json":
            try:
                obj = json.loads(raw.decode("utf-8"))
                return {str(k): str(v) for k, v in obj.items()} if isinstance(obj, dict) else {}
            except (ValueError, UnicodeDecodeError):
                return {}
        return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", errors="replace")).items() if v}

    # -- verbs ----------------------------------------------------------------

    def do_HEAD(self) -> None:  # noqa: N802
        self._guarded(lambda: self._dispatch_get(head_only=True))

    def do_GET(self) -> None:  # noqa: N802
        self._guarded(lambda: self._dispatch_get(head_only=False))

    def do_POST(self) -> None:  # noqa: N802
        self._guarded(self._dispatch_post)

    def _guarded(self, handler: Any) -> None:
        """A bug while rendering one request must not drop the connection with a
        traceback: answer with a short, honest page and keep the engine going."""
        try:
            handler()
        except (BrokenPipeError, ConnectionResetError):
            pass                                    # the browser went away mid-response
        except Exception as e:
            logger.exception("request %s %s failed: %s", self.command, self.path, e)
            try:
                if (urlsplit(self.path).path or "/").startswith("/api/"):
                    self._error(500, "internal error — see otto logs")
                else:
                    body = ERROR_HTML.format(detail=html.escape(f"{type(e).__name__}: {e}")[:300]).encode("utf-8")
                    self._respond(500, "text/html; charset=utf-8", body)
            except Exception:                       # pragma: no cover - headers already sent
                pass

    def _dispatch_post(self) -> None:
        if not host_allowed(self.headers.get("Host"), self._port):
            return self._error(400, "bad host")
        if not same_origin_ok(self.headers, self._port):
            return self._error(403, "cross-origin request rejected")

        path = urlsplit(self.path).path
        form = self._read_form()
        if path == "/api/refresh":
            return self._json({"status": GLOBAL_DATA.request_refresh()})
        if path == "/api/dismiss":
            return self._dismiss(form)
        if path == "/api/snooze":
            return self._snooze(form)
        if path == "/api/clear":
            return self._clear(form)
        if path == "/api/notifications/ack":
            ids = [x for x in (form.get("ids") or "").split(",") if x]
            n = self.notifier.ack(ids) if self.notifier else 0
            return self._json({"status": "ok", "acked": n})
        if path == "/api/feedback":
            return self._feedback(form)
        if path == "/api/notifications/test":
            # A banner that only says banners work (a delivery-path check).
            if not self.notifier:
                return self._json({"status": "unavailable"}, code=503)
            return self._json({"status": "queued", "id": self.notifier.enqueue_test()})
        self._error(404, "not found")

    def _dispatch_get(self, head_only: bool) -> None:
        if not host_allowed(self.headers.get("Host"), self._port):
            return self._error(400, "bad host")

        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query)

        if path.startswith("/static/"):
            return self._serve_static(path, head_only)
        if path in ("/", "/briefing"):
            if query.get("partial", [""])[0] == "1":
                return self._json(render_body(GLOBAL_DATA.get(), state.hidden_ids()), head_only=head_only)
            return self._serve_briefing(head_only)
        if path == "/api/briefing":
            return self._json(GLOBAL_DATA.get(), head_only=head_only)
        if path == "/api/items":
            # The briefing the way the menu bar panel and the CLI draw it:
            # grouped, titled, dismissed/snoozed items already removed.
            from otto.web.items import build_items
            return self._json(build_items(GLOBAL_DATA.get(), state.hidden_ids()), head_only=head_only)
        if path == "/api/status":
            data = GLOBAL_DATA.get()
            payload = build_status(data)
            seen = None
            if self.notifier is not None:
                # Is a banner client (the menu bar app) listening? None = never pulled.
                seen = self.notifier.seconds_since_pull()
                payload["client_seen_seconds"] = seen
            # What needs a person's attention about Otto itself, each with the
            # one thing to do (the menu bar shows these with a Fix… button).
            from otto.core.health import problems
            payload["problems"] = problems(data, config=payload.get("config") or {},
                                           client_seen_seconds=seen, menubar_installed=None if self.notifier else False)
            return self._json(payload, head_only=head_only)
        if path == "/api/notifications":
            # ?peek=1 looks without counting as a banner client (checks use it).
            peek = query.get("peek", [""])[0] == "1"
            pending = self.notifier.pending(peek=peek) if self.notifier else []
            return self._json({"notifications": pending}, head_only=head_only)
        if path in ("/api/refresh", "/api/dismiss", "/api/snooze", "/api/clear", "/api/notifications/ack", "/api/feedback",
                    "/api/notifications/test"):
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        self._error(404, "not found")

    # -- GET handlers ---------------------------------------------------------

    def _serve_static(self, path: str, head_only: bool) -> None:
        prefix = "/static/screenshots/"
        if path.startswith(prefix):
            filename = unquote(path[len(prefix):])
            if "/" in filename or "\\" in filename or ".." in filename or not filename.endswith(".png"):
                return self._error(404, "not found")
            filepath = paths.screenshots_dir() / filename
            if filepath.is_file():
                try:
                    body = filepath.read_bytes()
                except OSError:
                    return self._error(404, "not found")
                return self._respond(200, "image/png", body, head_only=head_only,
                                     extra={"Cache-Control": "private, max-age=60"})
        self._error(404, "not found")

    def _serve_briefing(self, head_only: bool) -> None:
        data = GLOBAL_DATA.get()
        nonce = new_nonce()
        headers = {"Content-Security-Policy": csp_header(nonce)}
        if not data:
            GLOBAL_DATA.request_refresh()
            body = LOADING_HTML.format(nonce=nonce).encode("utf-8")
            return self._respond(200, "text/html; charset=utf-8", body, extra=headers, head_only=head_only)
        html_out = render_page(data, state.hidden_ids(), nonce=nonce, last_updated=GLOBAL_DATA.last_updated)
        self._respond(200, "text/html; charset=utf-8", html_out.encode("utf-8"), extra=headers, head_only=head_only)

    # -- POST handlers --------------------------------------------------------

    def _dismiss(self, form: Dict[str, str]) -> None:
        item_id = (form.get("id") or "").strip()
        if not item_id or len(item_id) > 200:
            return self._error(400, "missing id")
        state.dismiss(_all_ids(item_id))
        if item_id.startswith("radar:"):
            _close_commitments([item_id])
        elif not item_id.startswith(("pattern:", "unread:", "note:")):
            record_feedback("dismiss", item_id)     # a note or a radar row is nobody's habit
        self._json({"status": "ok"})

    def _snooze(self, form: Dict[str, str]) -> None:
        item_id = (form.get("id") or "").strip()
        if not item_id or len(item_id) > 200:
            return self._error(400, "missing id")
        try:
            hours = float(form.get("hours") or "1")
            if math.isnan(hours) or math.isinf(hours) or hours <= 0:
                hours = 1.0
        except (TypeError, ValueError):
            hours = 1.0
        wake = None
        for one in _all_ids(item_id):
            wake = state.snooze(one, hours)
        if not item_id.startswith(("radar:", "pattern:")):
            record_feedback("snooze", item_id)
        self._json({"status": "ok", "until": wake.isoformat() if wake else ""})

    def _clear(self, form: Dict[str, str]) -> None:
        """Every item leaves the briefing (Worth knowing stays) — or, with ``scope=notes``,
        every note and radar row under Worth knowing does (the items stay). No
        confirmation either way, by design; nothing is deleted from memory."""
        data = GLOBAL_DATA.get()
        scope = (form.get("scope") or "items").strip().lower()
        if scope not in ("items", "notes"):
            return self._error(400, "bad scope")        # never guess what to clear
        if scope == "notes":
            ids = worth_knowing_ids(data, state.hidden_ids())
            state.dismiss(ids)
            _close_commitments(ids)
            return self._json({"status": "ok", "cleared": len(ids), "scope": "notes"})
        items = [i for s in data.get("sections", []) for c in s.get("channels", []) for i in c.get("items", []) if i.get("id")]
        state.dismiss({one for i in items for one in state.item_ids(i)})
        for item in items:
            record_feedback("clear", str(item["id"]), data=data)
        self._json({"status": "ok", "cleared": len(items)})

    def _feedback(self, form: Dict[str, str]) -> None:
        """``kind=open|expand`` for an item id: what you looked at, so Otto learns what matters."""
        kind = (form.get("kind") or "").strip().lower()
        item_id = (form.get("id") or "").strip()
        if kind not in ("open", "expand") or not item_id or len(item_id) > 200:
            return self._error(400, "bad feedback")
        ok = record_feedback(kind, item_id)
        self._json({"status": "ok" if ok else "ignored"})


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def make_server(port: int = OTTO_PORT, notifier: Any = None) -> ThreadingHTTPServer:
    """Bind a loopback server (raises OSError if the port is taken)."""
    handler = OttoRequestHandler
    if notifier is not None:
        handler = type("OttoRequestHandlerBound", (OttoRequestHandler,), {"notifier": notifier})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def start_server(port: int = OTTO_PORT, notifier: Any = None) -> Optional[ThreadingHTTPServer]:
    """Start serving in a daemon thread. Returns None if the port is in use."""
    try:
        server = make_server(port, notifier)
    except OSError:
        logger.info("Port %d already in use — another Otto is probably running", port)
        return None
    threading.Thread(target=server.serve_forever, name="otto-http", daemon=True).start()
    logger.info("Briefing server listening on http://localhost:%d", port)
    return server


if __name__ == "__main__":  # pragma: no cover
    from otto.core.engine import main as _engine_main
    raise SystemExit(_engine_main())
