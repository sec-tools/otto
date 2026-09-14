from __future__ import annotations

"""
Slack Web API adapter — read-only.

Why this exists next to the Slack.app / browser readers: the screen readers
see what the user is looking at; this adapter sees every channel, private
group and DM the token is a member of — the "all Slack messages" context the
briefing, the memory and the radar feed on.

Guarantees
----------
* Only ``GET`` requests, only to ``https://slack.com/api/…`` read methods
  (``auth.test``, ``users.*``, ``conversations.list/history/replies``).
  The client is built on the WriteGuard transport, so a ``POST`` would be
  refused at the network boundary even if a bug asked for one — and the class
  has no method that could ask.
* Budgeted: at most :data:`MAX_CALLS_PER_POLL` API calls per refresh, honoured
  ``Retry-After`` on 429, a hard per-poll deadline well under the collector's
  adapter timeout. Busy channels are read every minute; quiet ones round-robin.
* Never fatal: any error degrades to "fewer messages this refresh" and is
  reported through ``last_error`` so the status line can say why.

Scopes a user token needs (read-only):
``channels:history channels:read groups:history groups:read
im:history im:read mpim:history mpim:read users:read``.
"""

import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

from otto.adapters.base import ConnectionStatus, RawEvent
from otto.storage.models import (
    ConnectionState,
    ContentBlock,
    ContentType,
    HealthStatus,
    SourceType,
)

logger = logging.getLogger("otto.adapters.slack")

SLACK_API_BASE = "https://slack.com/api"

MAX_CALLS_PER_POLL = 40          # history + replies + users.info, per refresh
MAX_PAGES_PER_CHANNEL = 3        # 100 messages a page
MAX_THREAD_FETCHES_PER_POLL = 10
MAX_USER_LOOKUPS_PER_POLL = 8
POLL_DEADLINE_S = 25.0           # collector gives adapters 40 s; leave margin
CONNECT_TTL_S = 600.0            # re-run auth.test at most every 10 min
AUTH_RECHECK_S = 600.0           # a rejected token: stop calling, try auth.test again in 10 min
CHANNELS_TTL_S = 900.0           # conversations.list every 15 min
USERS_TTL_S = 3600.0             # users.list hourly (plus lazy users.info)
HOT_WINDOW_S = 24 * 3600.0       # a channel with a message in the last day is "hot"
OVERLAP_S = 120.0                # re-read this much before the last poll (clock skew, late edits)
RECENT_KEEP_S = 8 * 3600.0       # in-memory event cache (≥ the collector's look-back)

_SKIP_SUBTYPES = frozenset({
    "channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name",
    "channel_archive", "channel_unarchive", "group_join", "group_leave", "group_topic",
    "group_purpose", "group_name", "group_archive", "group_unarchive", "bot_add", "bot_remove",
    "pinned_item", "unpinned_item", "tombstone", "joiner_notification", "joiner_notification_for_inviter",
    "channel_convert_to_private", "channel_convert_to_public", "sh_room_created", "huddle_thread",
})
_AUTH_ERRORS = frozenset({"invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired"})
_SCOPE_TO_TYPE = {"channels:read": "public_channel", "groups:read": "private_channel", "im:read": "im", "mpim:read": "mpim"}

_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|([^>]*))?>")
_CHANNEL_RE = re.compile(r"<#([A-Z0-9]+)(?:\|([^>]*))?>")
_SPECIAL_RE = re.compile(r"<!([a-z]+)(?:\^[^|>]*)?(?:\|([^>]*))?>")
_LINK_RE = re.compile(r"<((?:https?|mailto):[^|>]+)(?:\|([^>]*))?>")


class RequestLedger:
    """Counts what went over the wire; the WriteGuard's audit sink for this adapter.

    Deliberately not the SQLite audit log: a poll makes dozens of requests a
    minute and a hash-chained table would grow without bound. What matters —
    "how many calls, any blocked?" — is kept in memory and shown in status.
    """

    def __init__(self) -> None:
        self.requests = 0
        self.blocked = 0
        self.by_method: dict[str, int] = {}
        self._minute: list[float] = []

    def append(self, event_type: Any = None, source: str = "", method: str | None = None,
               url: str | None = None, blocked: bool = False) -> None:
        if blocked:
            self.blocked += 1
            logger.error("Slack API: write attempt refused (%s %s)", method, (url or "").split("?")[0])
            return
        self.requests += 1
        api_method = (url or "").split("?")[0].rsplit("/", 1)[-1]
        self.by_method[api_method] = self.by_method.get(api_method, 0) + 1
        now = time.monotonic()
        self._minute.append(now)
        cutoff = now - 60.0
        while self._minute and self._minute[0] < cutoff:
            self._minute.pop(0)

    def per_minute(self) -> int:
        return len(self._minute)


def build_slack_http_client(token: str, *, ledger: RequestLedger | None = None) -> Any:
    """An httpx client that can only read: WriteGuard transport, bearer auth, short timeouts."""
    import httpx

    from otto.safety.write_guard import create_guarded_client

    return create_guarded_client(
        "slack",
        ledger or RequestLedger(),        # type: ignore[arg-type]
        allowlist=[],                     # nothing but GET, ever
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Otto (read-only)"},
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=False,
    )


class SlackAdapter:
    """
    Read-only Slack adapter over the Web API.

    THERE IS NO chat_postMessage(). NO reactions_add(). NO files_upload().
    """

    def __init__(self, http_client: Any, workspace_id: str, *, ledger: RequestLedger | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self._http = http_client
        self._workspace_id = workspace_id
        self._ledger = ledger
        self._clock = clock or time.time
        self._connected = False
        self._connected_at = 0.0
        self.last_error = ""
        self._team_domain = ""
        self._self_id = ""
        self._self_names: list[str] = []
        self._channels: dict[str, str] = {}           # channel_id → display name ("#eng", "@alice")
        self._channel_meta: dict[str, dict] = {}
        self._channels_at = 0.0
        self._users: dict[str, dict] = {}             # user_id → {"name", "is_bot"}
        self._users_at = 0.0
        self._last_polled: dict[str, float] = {}      # channel_id → ts of last successful read
        self._activity: dict[str, float] = {}         # channel_id → ts of newest message seen
        self._recent: dict[str, RawEvent] = {}        # source_id → event (bounded by RECENT_KEEP_S)
        self._backoff_until = 0.0
        self._missing_scopes: set[str] = set()
        self._listable_types = ["public_channel", "private_channel", "im", "mpim"]
        self._scope_needed = ""
        self.scope_note = ""
        self._failed = False
        self._calls = 0
        self._deadline = 0.0
        self._poll_count = 0
        self.stats: dict[str, Any] = {"channels": 0, "polled": 0, "new": 0, "calls": 0, "threads": 0}

    @property
    def name(self) -> str:
        return f"api_slack:{self._workspace_id}"

    @property
    def source_type(self) -> SourceType:
        return SourceType.SLACK

    @property
    def self_names(self) -> tuple[str, ...]:
        return tuple(self._self_names)

    @property
    def channel_count(self) -> int:
        return len(self._channels)

    # -- HTTP -----------------------------------------------------------------

    async def _call(self, method: str, **params: Any) -> dict[str, Any] | None:
        """One GET to a Slack read method. Returns the JSON payload, or None (never raises).

        Auth errors disconnect the adapter and park it for AUTH_RECHECK_S (one
        warning, no more calls this poll); ``ratelimited``/429 set a back-off
        the next poll respects; ``missing_scope`` disables that method.
        """
        if self._deadline and self._clock() > self._deadline:
            self._failed = True
            return None
        if not self._connected and method != "auth.test":
            self._failed = True                    # token already rejected: nothing else may go out
            return None
        clean = {k: v for k, v in params.items() if v not in (None, "", 0)}
        try:
            self._calls += 1
            resp = await self._http.get(f"{SLACK_API_BASE}/{method}", params=clean)
        except Exception as e:
            self._failed = True
            self.last_error = f"{method}: {type(e).__name__}: {e}"[:200]
            logger.debug("Slack API %s failed: %s", method, e)
            return None
        status = getattr(resp, "status_code", 0)
        if status == 429:
            retry = _retry_after(resp)
            self._backoff_until = self._clock() + retry
            self._failed = True
            self.last_error = f"rate limited; pausing {retry:.0f}s"
            logger.info("Slack API rate limited; pausing %.0fs", retry)
            return None
        if status != 200:
            self._failed = True
            self.last_error = f"{method}: HTTP {status}"
            return None
        try:
            data = resp.json()
        except Exception as e:
            self._failed = True
            self.last_error = f"{method}: bad JSON ({e})"
            return None
        if not isinstance(data, dict):
            self._failed = True
            return None
        if data.get("ok"):
            return data
        self._failed = True
        err = str(data.get("error") or "unknown_error")
        if err in _AUTH_ERRORS:
            self._connected = False
            self._backoff_until = self._clock() + AUTH_RECHECK_S
            self.last_error = f"Slack token rejected ({err})"
            logger.warning("Slack API: token rejected (%s) — replace it with `otto slack connect`; "
                           "checking again in %d min", err, int(AUTH_RECHECK_S // 60))
        elif err == "ratelimited":
            retry = _retry_after(resp)
            self._backoff_until = self._clock() + retry
            self.last_error = f"rate limited; pausing {retry:.0f}s"
        elif err == "missing_scope":
            needed = str(data.get("needed") or "")
            self._scope_needed = needed.split(",")[0].strip()
            # History scopes are per conversation kind (im:history, groups:history…):
            # remember the kind that failed so quiet DMs do not burn budget every poll.
            kind = self._kind(str(clean.get("channel") or "")) if method in ("conversations.history", "conversations.replies") else ""
            self._missing_scopes.add(f"history:{kind}" if kind else method)
            self.last_error = f"{method}: token lacks scope {needed}".strip()
            logger.warning("Slack API: %s needs scope %s — add it to the token", method, needed or "?")
        else:
            self.last_error = f"{method}: {err}"
            logger.debug("Slack API %s: %s", method, err)
        return None

    async def _pages(self, method: str, key: str, *, max_pages: int, **params: Any) -> list[dict]:
        """Follow ``response_metadata.next_cursor`` up to *max_pages* pages."""
        out: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            data = await self._call(method, cursor=cursor, **params)
            if not data:
                break
            out.extend(x for x in data.get(key, []) or [] if isinstance(x, dict))
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
        return out

    # -- lifecycle ------------------------------------------------------------

    async def connect(self) -> ConnectionStatus:
        """Verify the token (``auth.test``); refresh users and channels when stale. Cheap when already connected."""
        now = self._clock()
        if now < self._backoff_until:
            if not self._connected:               # parked after a rejected token: say so, send nothing
                return ConnectionStatus(state=ConnectionState.FAILED, error=self.last_error or "Slack token rejected")
            return ConnectionStatus(state=ConnectionState.DEGRADED, error=self.last_error or "rate limited")
        if self._connected and now - self._connected_at < CONNECT_TTL_S:
            return ConnectionStatus(state=ConnectionState.HEALTHY)
        self._deadline = 0.0
        self.last_error = ""
        data = await self._call("auth.test")
        if not data:
            self._connected = False
            return ConnectionStatus(state=ConnectionState.FAILED, error=self.last_error or "auth.test failed")
        self._connected = True
        self._connected_at = now
        self._self_id = str(data.get("user_id") or "")
        url = str(data.get("url") or "")
        m = re.match(r"https?://([^/.]+)\.slack\.com", url)
        self._team_domain = m.group(1) if m else ""
        if not self._workspace_id or self._workspace_id == "default":
            self._workspace_id = self._team_domain or str(data.get("team") or "slack")
        self._remember_self(str(data.get("user") or ""))
        await self._refresh_users(force=False)
        await self._refresh_channels(force=False)
        return ConnectionStatus(state=ConnectionState.HEALTHY)

    def _remember_self(self, name: str) -> None:
        n = (name or "").strip()
        if not n or n in self._self_names:
            return
        self._self_names.append(n)
        try:
            from otto.utils.identity import remember_self_name
            remember_self_name(n)
        except Exception:
            pass

    async def _refresh_users(self, *, force: bool) -> None:
        if "users.list" in self._missing_scopes:
            return
        if not force and self._users and self._clock() - self._users_at < USERS_TTL_S:
            return
        members = await self._pages("users.list", "members", max_pages=6, limit=500)
        if not members and self._users:
            return
        for u in members:
            self._users[str(u.get("id") or "")] = _user_record(u)
        self._users_at = self._clock()
        me = self._users.get(self._self_id)
        if me:
            self._remember_self(me["name"])
            for alt in (me.get("real_name"), me.get("display_name")):
                if alt:
                    self._remember_self(alt)

    async def _refresh_channels(self, *, force: bool) -> None:
        if not force and self._channels and self._clock() - self._channels_at < CHANNELS_TTL_S:
            return
        chans: list[dict] = []
        # A token without im:read / groups:read fails the whole listing; fall
        # back to the kinds it can see rather than seeing nothing.
        for _attempt in range(4):
            if not self._listable_types:
                break
            self._missing_scopes.discard("conversations.list")
            self._scope_needed = ""
            chans = await self._pages(
                "conversations.list", "channels", max_pages=5,
                types=",".join(self._listable_types), exclude_archived="true", limit=200,
            )
            if chans or "conversations.list" not in self._missing_scopes:
                break
            dropped = _SCOPE_TO_TYPE.get(self._scope_needed, "")
            if dropped not in self._listable_types:
                dropped = self._listable_types[-1]
            self._listable_types.remove(dropped)
            self.scope_note = "token cannot list " + ", ".join(
                t for t in ("public_channel", "private_channel", "im", "mpim") if t not in self._listable_types
            ).replace("_channel", " channels").replace("im", "DMs").replace("mpDMs", "group DMs")
        if chans and self.scope_note and self.last_error.startswith("conversations.list"):
            self.last_error = ""            # narrowed listing worked; the note carries the caveat
        if not chans and self._channels:
            return
        found: dict[str, str] = {}
        meta: dict[str, dict] = {}
        for ch in chans:
            cid = str(ch.get("id") or "")
            if not cid or ch.get("is_archived"):
                continue
            if not (ch.get("is_im") or ch.get("is_mpim")) and ch.get("is_member") is False:
                continue          # public channels the token is not in are not "yours"
            found[cid] = self._channel_display(ch)
            meta[cid] = {"is_im": bool(ch.get("is_im")), "is_mpim": bool(ch.get("is_mpim")),
                         "is_private": bool(ch.get("is_private")), "user": ch.get("user", ""),
                         # What the channel is for, in the workspace's own words —
                         # "#eng-oncall: production incidents" reads differently from "#random".
                         "purpose": _channel_text(ch, "purpose"), "topic": _channel_text(ch, "topic")}
        if found:
            self._channels = found
            self._channel_meta = meta
            self._channels_at = self._clock()
            self.stats["channels"] = len(found)

    def _kind(self, cid: str) -> str:
        meta = self._channel_meta.get(cid, {})
        if meta.get("is_im"):
            return "im"
        if meta.get("is_mpim"):
            return "mpim"
        if meta.get("is_private"):
            return "private"
        return "public"

    def _channel_display(self, ch: dict) -> str:
        if ch.get("is_im"):
            return "@" + self._user_name(str(ch.get("user") or ""))
        name = str(ch.get("name") or ch.get("id") or "")
        if ch.get("is_mpim"):
            inner = re.sub(r"^mpdm-", "", name)
            inner = re.sub(r"-\d+$", "", inner)
            return "@" + ", ".join(p for p in inner.split("--") if p)
        return "#" + name

    def _user_name(self, uid: str) -> str:
        rec = self._users.get(uid)
        if rec and rec.get("name"):
            return str(rec["name"])
        return uid or "someone"

    # -- polling ------------------------------------------------------------------

    def _plan(self, now: float, budget: int) -> list[str]:
        """Which channels to read this poll: hot ones every time, quiet ones round-robin."""
        chans = [c for c in self._channels if f"history:{self._kind(c)}" not in self._missing_scopes]
        hot = [c for c in chans if now - self._activity.get(c, 0.0) < HOT_WINDOW_S]
        cold = [c for c in chans if c not in set(hot)]
        hot.sort(key=lambda c: self._last_polled.get(c, 0.0))
        cold.sort(key=lambda c: self._last_polled.get(c, 0.0))
        never = [c for c in cold if c not in self._last_polled]
        # Never-read channels first (cold start), then keep a quarter of the
        # budget for quiet channels so hot ones cannot starve discovery.
        reserve = max(1, budget // 4) if cold else 0
        n_hot = min(len(hot), max(0, budget - reserve))
        plan = never[: max(0, budget - n_hot)]
        plan += hot[:n_hot]
        rest = [c for c in cold if c not in plan]
        plan += rest[: max(0, budget - len(plan))]
        return plan[:budget]

    async def poll(self, since: datetime) -> list[RawEvent]:
        """Every message since *since* across the token's channels, budgeted per refresh."""
        if not self._connected:
            return []
        now = self._clock()
        self._poll_count += 1
        self._calls = 0
        self._deadline = now + POLL_DEADLINE_S
        since_ts = since.timestamp()
        new = 0
        polled = 0
        threads = 0
        try:
            if now >= self._backoff_until:
                await self._refresh_channels(force=False)
                await self._refresh_users(force=False)
                budget = MAX_CALLS_PER_POLL - self._calls - MAX_USER_LOOKUPS_PER_POLL // 2
                plan = self._plan(now, max(1, budget - MAX_THREAD_FETCHES_PER_POLL // 2))
                thread_todo: list[tuple[float, str, str]] = []   # (latest_reply, channel_id, thread_ts)
                for cid in plan:
                    if (self._connected and self._clock() >= self._backoff_until
                            and self._clock() < self._deadline and self._calls < MAX_CALLS_PER_POLL):
                        oldest = max(since_ts, self._last_polled.get(cid, 0.0) - OVERLAP_S)
                        self._failed = False
                        msgs = await self._pages(
                            "conversations.history", "messages", max_pages=MAX_PAGES_PER_CHANNEL,
                            channel=cid, oldest=f"{oldest:.6f}", limit=100, inclusive="false",
                        )
                        if self._failed and not msgs:
                            continue          # read failed: leave it due for the next poll
                        polled += 1
                        self._last_polled[cid] = self._clock()
                        for msg in msgs:
                            if self._absorb(msg, cid):
                                new += 1
                            try:
                                if float(msg.get("reply_count") or 0) > 0 and float(msg.get("latest_reply") or 0) >= oldest:
                                    thread_todo.append((float(msg.get("latest_reply") or 0), cid, str(msg.get("ts") or "")))
                            except (TypeError, ValueError):
                                pass
                    else:
                        break
                thread_todo.sort(reverse=True)
                for _latest, cid, tts in thread_todo[:MAX_THREAD_FETCHES_PER_POLL]:
                    if (self._connected and self._clock() >= self._backoff_until
                            and self._clock() < self._deadline and self._calls < MAX_CALLS_PER_POLL):
                        oldest = max(since_ts, self._last_polled.get(cid, 0.0) - OVERLAP_S - 3600.0)
                        replies = await self._pages(
                            "conversations.replies", "messages", max_pages=1,
                            channel=cid, ts=tts, oldest=f"{oldest:.6f}", limit=100,
                        )
                        threads += 1
                        for msg in replies:
                            if str(msg.get("ts")) == tts:
                                continue          # the parent came with the history
                            if self._absorb(msg, cid, thread_ts=tts):
                                new += 1
                await self._resolve_unknown_users()
        except Exception as e:                      # belt and braces: never fail the refresh
            self.last_error = f"poll: {e}"[:200]
            logger.warning("Slack API poll failed: %s", e)
        finally:
            self._deadline = 0.0
        self._prune_recent(min(now - RECENT_KEEP_S, since_ts))
        self.stats.update(polled=polled, new=new, calls=self._calls, threads=threads, channels=len(self._channels))
        if polled or new:
            logger.info("Slack API: %d channels read, %d new messages, %d threads, %d calls",
                        polled, new, threads, self._calls)
        events = [e for e in self._recent.values() if e.timestamp.timestamp() >= since_ts]
        events.sort(key=lambda e: e.timestamp)
        return events

    def _absorb(self, msg: dict, channel_id: str, *, thread_ts: str = "") -> bool:
        event = self._message_to_event(msg, channel_id, self._channels.get(channel_id, channel_id), thread_ts=thread_ts)
        if event is None:
            return False
        ts = event.timestamp.timestamp()
        if ts > self._activity.get(channel_id, 0.0):
            self._activity[channel_id] = ts
        if event.source_id in self._recent:
            self._recent[event.source_id] = event      # edits: keep the latest text
            return False
        self._recent[event.source_id] = event
        return True

    def _prune_recent(self, cutoff: float) -> None:
        for sid in [k for k, e in self._recent.items() if e.timestamp.timestamp() < cutoff]:
            del self._recent[sid]

    async def _resolve_unknown_users(self) -> None:
        """Names for senders users.list did not know yet (new hires, shared-channel guests)."""
        if "users.info" in self._missing_scopes:
            return
        unknown: list[str] = []
        for ev in self._recent.values():
            uid = ev.sender_id or ""
            if uid and uid not in self._users and uid not in unknown and not ev.is_auto_generated:
                unknown.append(uid)
        for uid in unknown[:MAX_USER_LOOKUPS_PER_POLL]:
            if self._calls >= MAX_CALLS_PER_POLL:
                break
            data = await self._call("users.info", user=uid)
            user = (data or {}).get("user") if data else None
            self._users[uid] = _user_record(user) if user else {"name": uid, "is_bot": False}
        if unknown:
            for sid, ev in list(self._recent.items()):
                if ev.sender_id in self._users and ev.sender_name == ev.sender_id:
                    ev.sender_name = self._user_name(ev.sender_id or "")

    # -- message → event ---------------------------------------------------------

    def _message_to_event(
        self, msg: dict[str, Any], channel_id: str, channel_name: str, *, thread_ts: str = ""
    ) -> RawEvent | None:
        """Convert a Slack message to a RawEvent (None for system noise)."""
        if msg.get("subtype") in _SKIP_SUBTYPES or msg.get("hidden"):
            return None
        if not channel_name.startswith(("#", "@")):
            channel_name = f"#{channel_name}"

        ts = str(msg.get("ts") or "0")
        try:
            timestamp = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            timestamp = datetime.now(timezone.utc)

        raw_text = str(msg.get("text") or "")
        mentions_you = bool(self._self_id) and f"<@{self._self_id}" in raw_text
        text = self.render_text(raw_text)
        extra = _attachment_text(msg)
        if extra:
            text = f"{text}\n{extra}".strip() if text else extra
        for f in msg.get("files") or []:
            if isinstance(f, dict) and f.get("name"):
                text = f"{text}\n[file: {f['name']}]".strip()
        if not text.strip():
            return None

        user_id = str(msg.get("user") or "")
        is_bot = bool(msg.get("bot_id")) or msg.get("subtype") == "bot_message" or bool(
            user_id and self._users.get(user_id, {}).get("is_bot"))
        profile = msg.get("user_profile") or {}
        bot_profile = msg.get("bot_profile") or {}
        user_name = (
            self._user_name(user_id) if user_id and user_id in self._users else ""
        ) or profile.get("display_name") or profile.get("real_name") or msg.get("username") \
            or bot_profile.get("name") or user_id or ("bot" if is_bot else "someone")
        if user_id and user_id not in self._users and user_name != user_id:
            self._users[user_id] = {"name": user_name, "is_bot": is_bot}

        parent = thread_ts or (str(msg.get("thread_ts")) if msg.get("thread_ts") else "")
        is_thread_parent = float(msg.get("reply_count") or 0) > 0
        thread_id = f"{channel_id}:{parent}" if parent else (f"{channel_id}:{ts}" if is_thread_parent else None)
        domain = f"{self._team_domain}.slack.com" if self._team_domain else "slack.com"
        url = f"https://{domain}/archives/{channel_id}/p{ts.replace('.', '')}"
        if parent and parent != ts:
            url += f"?thread_ts={parent}&cid={channel_id}"

        meta: dict[str, Any] = {
            "channel_id": channel_id,
            "channel_name": channel_name.lstrip("#@"),
            "workspace": self._workspace_id,
            "extraction_mode": "slack_api",
            "has_mention": "<@" in raw_text,
            "mentions_you": mentions_you,
            "reply_count": int(msg.get("reply_count") or 0),
            "reactions": [r.get("name") for r in msg.get("reactions") or [] if isinstance(r, dict)],
        }
        cmeta = self._channel_meta.get(channel_id, {})
        if cmeta.get("is_im") or cmeta.get("is_mpim"):
            meta["is_dm"] = True
        if cmeta.get("purpose"):
            meta["channel_purpose"] = cmeta["purpose"]
        if cmeta.get("topic"):
            meta["channel_topic"] = cmeta["topic"]
        title = self._users.get(user_id, {}).get("title") if user_id else ""
        if title and not is_bot:
            meta["sender_title"] = title
        reply_users = [str(u) for u in (msg.get("reply_users") or []) if u]
        if reply_users:
            meta["reply_users"] = [self._user_name(u) for u in reply_users[:10]]
            meta["you_replied"] = bool(self._self_id) and self._self_id in reply_users
        if msg.get("edited"):
            meta["is_edited"] = True

        return RawEvent(
            source=SourceType.SLACK,
            source_id=f"{channel_id}:{ts}",
            source_url=url,
            timestamp=timestamp,
            title=channel_name,
            content_blocks=[ContentBlock(type=ContentType.TEXT, text=text)],
            plain_text=text,
            sender_name=str(user_name),
            sender_id=user_id or None,
            thread_id=thread_id,
            has_attachments=bool(msg.get("files")),
            is_auto_generated=is_bot,
            raw_metadata=meta,
        )

    def render_text(self, raw: str) -> str:
        """Slack mrkdwn tokens → plain text: ``<@U1>`` → ``@alice``, ``<#C1|eng>`` → ``#eng``, links → ``label (url)``."""
        def _mention(m: re.Match) -> str:
            uid, alias = m.group(1), m.group(2)
            if uid == self._self_id and self._self_names:
                return "@" + self._self_names[0]
            if alias:
                return "@" + alias
            return "@" + self._user_name(uid)

        def _channel(m: re.Match) -> str:
            cid, alias = m.group(1), m.group(2)
            if alias:
                return "#" + alias
            disp = self._channels.get(cid)
            return disp if disp else "#" + cid

        def _special(m: re.Match) -> str:
            kind, label = m.group(1), m.group(2)
            if kind in ("here", "channel", "everyone"):
                return "@" + kind
            if kind == "subteam":
                return label or "@team"
            return label or ""

        def _link(m: re.Match) -> str:
            url, label = m.group(1), m.group(2)
            if url.startswith("mailto:"):
                return label or url[len("mailto:"):]
            if label and label.strip() and label.strip() != url:
                return f"{label.strip()} ({url})"
            return url

        text = _MENTION_RE.sub(_mention, raw)
        text = _CHANNEL_RE.sub(_channel, text)
        text = _SPECIAL_RE.sub(_special, text)
        text = _LINK_RE.sub(_link, text)
        text = html.unescape(text)
        return re.sub(r"[ \t]+\n", "\n", text).strip()

    # -- protocol extras ---------------------------------------------------------

    async def fetch_thread(self, thread_id: str) -> list[RawEvent]:
        """All messages of one thread (``"<channel_id>:<thread_ts>"``)."""
        if ":" not in thread_id:
            return []
        channel_id, thread_ts = thread_id.split(":", 1)
        msgs = await self._pages("conversations.replies", "messages", max_pages=5, channel=channel_id, ts=thread_ts, limit=100)
        out: list[RawEvent] = []
        for msg in msgs:
            ev = self._message_to_event(msg, channel_id, self._channels.get(channel_id, channel_id), thread_ts=thread_ts)
            if ev:
                out.append(ev)
        return out

    async def health_check(self) -> HealthStatus:
        if not self._connected:
            return HealthStatus.UNHEALTHY
        if self._clock() < self._backoff_until:
            return HealthStatus.DEGRADED
        data = await self._call("auth.test")
        if data:
            return HealthStatus.HEALTHY
        return HealthStatus.DEGRADED if self._connected else HealthStatus.UNHEALTHY

    async def disconnect(self) -> None:
        self._connected = False


# -- helpers ---------------------------------------------------------------------

def _retry_after(resp: Any) -> float:
    try:
        headers = getattr(resp, "headers", {}) or {}
        return min(900.0, max(5.0, float(headers.get("Retry-After", 30))))
    except (TypeError, ValueError):
        return 30.0


def _user_record(u: dict) -> dict:
    profile = u.get("profile") or {}
    name = (profile.get("display_name") or profile.get("real_name") or u.get("real_name")
            or u.get("name") or str(u.get("id") or "someone"))
    return {
        "name": str(name).strip(),
        "real_name": str(profile.get("real_name") or u.get("real_name") or "").strip(),
        "display_name": str(profile.get("display_name") or "").strip(),
        "title": str(profile.get("title") or "").strip()[:80],     # "Staff Engineer", "VP Sales" — who is speaking
        "is_bot": bool(u.get("is_bot")) or u.get("id") == "USLACKBOT",
    }


def _channel_text(ch: dict, key: str) -> str:
    """``purpose`` / ``topic`` come as ``{"value": "...", ...}``; the text or ''."""
    value = ch.get(key)
    if isinstance(value, dict):
        value = value.get("value")
    return " ".join(str(value or "").split())[:300]


def _attachment_text(msg: dict) -> str:
    """Bot posts (GitHub, Jira, PagerDuty…) carry their content in attachments/blocks, not ``text``."""
    parts: list[str] = []
    for att in msg.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        title = str(att.get("title") or att.get("author_name") or "").strip()
        body = str(att.get("text") or att.get("fallback") or att.get("pretext") or "").strip()
        piece = " — ".join(p for p in (title, body) if p)
        if piece:
            parts.append(piece)
    if not parts:
        for block in msg.get("blocks") or []:
            if not isinstance(block, dict) or block.get("type") not in ("section", "header", "context"):
                continue
            t = block.get("text")
            if isinstance(t, dict) and t.get("text"):
                parts.append(str(t["text"]))
            for el in block.get("elements") or []:
                if isinstance(el, dict) and isinstance(el.get("text"), str):
                    parts.append(el["text"])
    text = "\n".join(p for p in parts if p)
    text = re.sub(r"<((?:https?):[^|>]+)\|([^>]*)>", lambda m: f"{m.group(2)} ({m.group(1)})", text)
    text = re.sub(r"<((?:https?):[^|>]+)>", lambda m: m.group(1), text)
    return html.unescape(text)[:2000]
