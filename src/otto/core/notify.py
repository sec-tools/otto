"""
Notification bridge: briefing items → native macOS banners, without nagging.

Policy (all tunable in ``[notifications]`` config):

* only ``critical``/``high`` items that carry an action (or are critical);
* never the same item twice (persisted in ``notified.json``);
* at most ``max_per_hour`` banners; the rest wait for the next window;
* quiet hours in **local** time (bypassed only above ``critical_bypass_threshold``);
* several new items at once collapse into one banner ("+N more");
* separately, one reminder banner when something *you* owe is due within
  the hour or has just slipped (``process_radar``).

Delivery is *pull*-based: the Swift menu bar app polls ``/api/notifications``
and posts through ``UNUserNotificationCenter`` (proper app identity, click
opens the source or the briefing). If nothing has pulled for 90 s — the menu
bar app is not running — the engine shows the banner itself: through the
built ``Otto.app --notify`` (with the source thumbnail) or, failing that,
``osascript``'s ``display notification``.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timezone
from typing import Any, Dict, List

from otto.ui.notifications import NotificationEngine, NotificationPolicy, NotificationRequest
from otto.web import state

logger = logging.getLogger("otto.core.notify")

PENDING_TTL_SECONDS = 3600
PENDING_MAX = 20
REMINDER_LEAD_MINUTES = 60      # remind about your own due items this far ahead
FALLBACK_AFTER_SECONDS = 90      # no menu-bar pull for this long → osascript fallback
CLIENT_ACTIVE_WINDOW = 300       # a pull within this window counts as "client present"


@dataclass
class PendingNotification:
    id: str
    title: str
    body: str
    url: str = ""
    image: str = ""              # absolute path of the source thumbnail, shown in the banner
    created: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def source_image(item: Dict[str, Any]) -> str:
    """Absolute path of the item's thumbnail if the file exists, else ``""``."""
    name = str(item.get("screenshot") or "").strip()
    if not name or "/" in name or name.startswith("."):
        return ""
    try:
        from otto import paths
        path = paths.screenshots_dir() / name
        return str(path) if path.is_file() else ""
    except Exception:
        return ""


def notification_lead(item: Dict[str, Any], limit: int = 2) -> str:
    """The item's strongest reasons, as one short lead: ``"Asked of you · 4 critical"``."""
    labels = []
    for r in item.get("why") or []:
        label = str((r or {}).get("label") or "").strip() if isinstance(r, dict) else ""
        if label:
            labels.append(label)
        if len(labels) >= limit:
            break
    return " · ".join(labels)


def _parse_hhmm(value: str, default: dtime) -> dtime:
    try:
        hh, mm = str(value).split(":")
        return dtime(int(hh), int(mm))
    except Exception:
        return default


def policy_from_config(cfg: Any | None = None) -> NotificationPolicy:
    """Build a local-time policy from ``[notifications]`` in config.toml (safe defaults otherwise)."""
    if cfg is None:
        try:
            from otto.config import ConfigManager
            cfg = ConfigManager()
        except Exception as e:  # pragma: no cover - a broken config must not kill banners
            logger.warning("Notification config unreadable, using defaults: %s", e)
            cfg = None
    get = (lambda k, d: cfg.get_or(k, d)) if cfg is not None else (lambda k, d: d)
    return NotificationPolicy.local(
        max_per_hour=int(get("notifications.max_per_hour", 5)),
        quiet_hours_start=_parse_hhmm(get("notifications.quiet_hours_start", "23:00"), dtime(23, 0)),
        quiet_hours_end=_parse_hhmm(get("notifications.quiet_hours_end", "07:00"), dtime(7, 0)),
        bypass_threshold=float(get("notifications.critical_bypass_threshold", 0.95)),
        min_urgency=float(get("notifications.urgency_threshold", 0.8)),
        require_action=True,
    )


class Notifier:
    """Turns each refresh's data into (at most) one banner, delivered via the pending queue."""

    def __init__(self, policy: NotificationPolicy | None = None, *, fallback_osascript: bool = True) -> None:
        self._engine = NotificationEngine(policy or policy_from_config())
        self._engine.set_deliver_callback(self._enqueue)
        self._pending: List[PendingNotification] = []
        self._lock = threading.Lock()
        self._last_pull = 0.0
        self._fallback = fallback_osascript and bool(shutil.which("osascript"))
        self._batch: List[PendingNotification] = []

    # -- inbound: engine tells us about a refresh -----------------------------

    def process_briefing(self, data: Dict[str, Any], now: datetime | None = None) -> int:
        """Evaluate a refresh. Returns the number of banners produced (0 or 1)."""
        now = now or datetime.now(timezone.utc)
        already = state.load_notified()
        hidden = state.hidden_ids()
        candidates: List[Dict[str, Any]] = []
        for s in data.get("sections", []):
            for c in s.get("channels", []):
                for item in c.get("items", []):
                    ids = state.item_ids(item)
                    if not ids or not ids.isdisjoint(already) or not ids.isdisjoint(hidden):
                        continue
                    if item.get("urgency") not in ("critical", "high"):
                        continue
                    candidates.append(item)
        if not candidates:
            return 0

        candidates.sort(key=lambda i: float(i.get("urgency_score", 0) or 0), reverse=True)
        top = candidates[0]
        # Lead with the strongest reason this is for *you* ("Asked of you",
        # "Your directive", "4 critical"), then the sentence in your terms.
        body = (top.get("for_you") or top.get("summary") or top.get("text") or "").replace("\n", " ").strip()
        body = body[:110].rstrip() + ("…" if len(body) > 110 else "")
        why = notification_lead(top)
        if why:
            body = f"{why} — {body}" if body else why
        if len(candidates) > 1:
            body = f"{body}  (+{len(candidates) - 1} more)"
        sender = (top.get("sender") or "").strip()
        channel = ""
        for s in data.get("sections", []):
            for c in s.get("channels", []):
                if top in c.get("items", []):
                    channel = c.get("name", "")
        title_bits = [b for b in (channel, f"from {sender}" if sender and sender.lower() != "you" else "") if b]
        title = "Otto · " + (" ".join(title_bits) if title_bits else "Needs your attention")

        req = NotificationRequest(
            conversation_id=top["id"],
            title=title,
            body=body,
            urgency=float(top.get("urgency_score", 0.8) or 0.8),
            action_url=top.get("source_url") or "",
            action_required=bool(top.get("action_items")) or top.get("urgency") == "critical",
            image_path=source_image(top),
        )
        result = self._engine.request(req, now)
        # Whatever happened, do not re-evaluate these items every minute —
        # under every id they answer to (screen copy and API copy alike).
        state.mark_notified(sorted({one for c in candidates for one in state.item_ids(c)}))
        logger.info("Notification %s: %s", result, body[:60])
        return 1 if result == "delivered" else 0

    def process_radar(self, data: Dict[str, Any], now: datetime | None = None) -> int:
        """
        Reminders from the radar: one banner when something *you* owe is due
        within the hour (or has just slipped past), once per item.
        """
        now = now or datetime.now(timezone.utc)
        radar = data.get("radar") or {}
        already = state.load_notified()
        hidden = state.hidden_ids()
        due_soon: List[Dict[str, Any]] = []
        for row in radar.get("todo") or []:
            rid = row.get("id") or ""
            if not rid or rid in hidden or f"{rid}:due" in already or not row.get("due"):
                continue
            try:
                due = datetime.fromisoformat(str(row["due"]).replace("Z", "+00:00"))
            except ValueError:
                continue
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            minutes = (due - now).total_seconds() / 60
            if -180 <= minutes <= REMINDER_LEAD_MINUTES:
                due_soon.append({**row, "_minutes": minutes})
        if not due_soon:
            return 0
        due_soon.sort(key=lambda r: r["_minutes"])
        top = due_soon[0]
        who = (top.get("who") or "").strip()
        asked = f" — {who} asked" if who and who != "you" and top.get("kind") == "ask" else ""
        when = top.get("due_label") or ("overdue" if top["_minutes"] < 0 else "due soon")
        body = f"{top.get('what', '')}{asked} ({when}, {top.get('channel', '')})".strip()
        if len(due_soon) > 1:
            body += f"  (+{len(due_soon) - 1} more due soon)"
        req = NotificationRequest(
            conversation_id=f"{top['id']}:due",
            title="Otto · Reminder" if top["_minutes"] >= 0 else "Otto · Slipped",
            body=body[:160],
            urgency=0.9 if top["_minutes"] < 0 else 0.85,
            action_url=top.get("url") or "",
            action_required=True,
        )
        result = self._engine.request(req, now)
        state.mark_notified([f"{r['id']}:due" for r in due_soon])
        logger.info("Reminder %s: %s", result, body[:60])
        return 1 if result == "delivered" else 0

    def drain_quiet_queue(self, now: datetime | None = None) -> int:
        """Deliver anything held back by quiet hours / rate limiting, if allowed now."""
        now = now or datetime.now(timezone.utc)
        if self._engine._is_quiet_hours(now):  # noqa: SLF001 - intentional reuse
            return 0
        return len(self._engine.drain_queue(now))

    # -- outbound: menu bar pulls ---------------------------------------------

    def _enqueue(self, req: NotificationRequest) -> None:
        with self._lock:
            self._prune()
            self._pending.append(PendingNotification(
                id=req.conversation_id, title=req.title, body=req.body, url=req.action_url or "",
                image=req.image_path or "",
            ))
            self._pending = self._pending[-PENDING_MAX:]

    def _prune(self) -> None:
        cutoff = time.time() - PENDING_TTL_SECONDS
        self._pending = [p for p in self._pending if p.created >= cutoff]

    def pending(self, *, peek: bool = False) -> List[Dict[str, Any]]:
        """Called by ``GET /api/notifications`` (marks a client as present unless *peek*)."""
        with self._lock:
            if not peek:
                self._last_pull = time.time()
            self._prune()
            return [p.to_dict() for p in self._pending]

    def ack(self, ids: List[str]) -> int:
        with self._lock:
            before = len(self._pending)
            wanted = set(ids)
            self._pending = [p for p in self._pending if p.id not in wanted]
            return before - len(self._pending)

    def client_present(self) -> bool:
        return (time.time() - self._last_pull) < CLIENT_ACTIVE_WINDOW

    def seconds_since_pull(self) -> float | None:
        """How long since a client last pulled ``/api/notifications`` (None: never)."""
        return None if not self._last_pull else max(0.0, time.time() - self._last_pull)

    def enqueue_test(self) -> str:
        """
        A banner that says only that banners work (``POST /api/notifications/test``).

        Skips the policy on purpose (quiet hours, rate limit, urgency) so the
        delivery path itself can be checked at any hour; nothing about the
        briefing is involved. Returns the pending id.
        """
        nid = f"test:{int(time.time())}"
        self._enqueue(NotificationRequest(
            conversation_id=nid, title="Otto · Test", body="Banners are working.",
            urgency=1.0, action_url="", action_required=False,
        ))
        return nid

    # -- fallback -------------------------------------------------------------

    def deliver_fallbacks(self) -> int:
        """If no client is pulling, show stale pending banners via osascript."""
        if not self._fallback or self.client_present():
            return 0
        with self._lock:
            self._prune()
            due = [p for p in self._pending if time.time() - p.created >= FALLBACK_AFTER_SECONDS]
            if not due:
                return 0
            self._pending = [p for p in self._pending if p not in due]
        for p in due:
            self._osascript(p.title, p.body, p.image)
        return len(due)

    @staticmethod
    def fallback_command(title: str, body: str, image: str = "") -> List[str]:
        """The banner command when nothing is pulling ``/api/notifications``.

        The built menu bar binary in ``--notify`` mode can show the source
        thumbnail; without it, a plain ``display notification`` still gets the
        words across.
        """
        import os

        from otto import paths

        app_bin = paths.menubar_binary()
        if app_bin.is_file() and os.access(app_bin, os.X_OK):
            return [str(app_bin), "--notify", title, body] + ([image] if image and os.path.isfile(image) else [])
        quote = lambda s: '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'  # noqa: E731
        return ["osascript", "-e", f"display notification {quote(body)} with title {quote(title)}"]

    @classmethod
    def _osascript(cls, title: str, body: str, image: str = "") -> None:
        try:
            subprocess.run(cls.fallback_command(title, body, image), capture_output=True, timeout=5)
        except Exception as e:
            logger.debug("fallback notification failed: %s", e)
