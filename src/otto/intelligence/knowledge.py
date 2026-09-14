"""
Otto's memory.

Every message Otto reads — from whichever path read it (Slack.app
accessibility tree, a browser tab, the Slack API) — is remembered once in a
local SQLite database so the intelligence layer can reason across days and
weeks instead of across one screenful. Nothing here talks to a source; it is
a write-once-per-message notebook that lives in the data dir with mode 0600.

Design notes
------------
* **Cross-path dedup.** The same message seen through two readers has
  different ``source_id``\\s but the same channel, sender, day and text, so
  the primary key is a hash of exactly those. Re-seeing a message bumps
  ``seen`` and ``last_seen``; it never creates a second row.
* **Bounded.** Retention is ``retention_days`` (default 90) and at most
  ``max_messages`` rows; pruning runs on write, at most every few minutes.
* **Cheap to query.** One connection per call, WAL mode, indexes on
  ``(channel, ts)``, ``(sender, ts)`` and ``ts``. Typical stores (tens of
  thousands of rows) answer every query here in single-digit milliseconds.
* **Never fatal.** The store is a helper for the briefing, not its
  foundation; every public method swallows ``sqlite3`` errors into logs and
  returns an empty/zero result so a corrupt or locked database degrades
  Otto to "no memory" rather than "no briefing".
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from otto import paths

logger = logging.getLogger("otto.intelligence.knowledge")

DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_MESSAGES = 100_000
MAX_TEXT_CHARS = 4000
_PRUNE_INTERVAL_S = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    key         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    channel     TEXT NOT NULL,
    sender      TEXT NOT NULL,
    is_bot      INTEGER NOT NULL DEFAULT 0,
    ts          REAL NOT NULL,
    text        TEXT NOT NULL,
    url         TEXT NOT NULL DEFAULT '',
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    seen        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_channel_ts ON messages(channel, ts);
CREATE INDEX IF NOT EXISTS idx_messages_sender_ts ON messages(sender, ts);

CREATE TABLE IF NOT EXISTS commitments (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    who         TEXT NOT NULL,
    what        TEXT NOT NULL,
    channel     TEXT NOT NULL,
    due         REAL,
    due_text    TEXT NOT NULL DEFAULT '',
    created     REAL NOT NULL,
    updated     REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    confidence  REAL NOT NULL DEFAULT 0.5,
    for_you     INTEGER NOT NULL DEFAULT 0,
    source_key  TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_commitments_status ON commitments(status, due);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    item_id     TEXT NOT NULL DEFAULT '',
    channel     TEXT NOT NULL DEFAULT '',
    sender      TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_feedback_ts ON feedback(ts);
"""

# What each thing you do with an item says about how much it mattered.
# Opening is the strongest "yes"; dismissing without opening the clearest "no";
# snoozing means "yes, later"; Clear sweeps everything and says little.
FEEDBACK_WEIGHTS: dict[str, float] = {
    "open": 1.0,
    "expand": 0.5,
    "snooze": 0.3,
    "dismiss": -1.0,
    "clear": -0.25,
}
FEEDBACK_DAYS = 45                 # habits older than this no longer count
FEEDBACK_SHRINK = 4.0              # a handful of clicks moves a prior a little, dozens move it a lot

# Columns added after the first release; applied with ALTER TABLE on open so an
# existing knowledge.db keeps working (and its history) across upgrades.
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("messages", "conv TEXT NOT NULL DEFAULT ''"),        # conversation/thread id the reader assigned
    ("messages", "source_id TEXT NOT NULL DEFAULT ''"),   # reader's own message id
)


@dataclass(frozen=True)
class Message:
    key: str
    source: str
    channel: str
    sender: str
    is_bot: bool
    ts: float
    text: str
    url: str = ""
    first_seen: float = 0.0
    last_seen: float = 0.0
    seen: int = 1
    conv: str = ""
    source_id: str = ""

    @property
    def when(self) -> datetime:
        return datetime.fromtimestamp(self.ts, tz=timezone.utc)

    def to_event(self) -> Any:
        """Rebuild a ``NormalizedEvent`` so a remembered message can take part in
        the briefing exactly like one read this minute (same grouping and the
        same classification-cache key, so recalling costs no LLM calls)."""
        from otto.storage.models import ContentBlock, ContentType, NormalizedEvent, SourceType

        try:
            source = SourceType(self.source)
        except ValueError:
            source = SourceType.SLACK
        return NormalizedEvent(
            source=source,
            account_id="",
            source_id=self.source_id or self.key,
            source_url=self.url,
            timestamp=self.when,
            title=self.channel,
            plain_text_extract=self.text,
            content_hash=self.key,
            content_language="en",
            is_auto_generated=self.is_bot,
            conversation_id=self.conv or self.source_id or self.key,
            content_blocks=[ContentBlock(type=ContentType.TEXT, text=self.text)],
            sender=self.sender or None,
        )


@dataclass(frozen=True)
class ChannelActivity:
    channel: str
    total: int
    humans: int
    bots: int
    senders: int
    first_ts: float
    last_ts: float


@dataclass(frozen=True)
class PersonActivity:
    sender: str
    total: int
    channels: int
    last_ts: float


@dataclass(frozen=True)
class FeedbackPriors:
    """Habit scores in [-1, 1] per channel and per sender (see ``feedback_priors``)."""
    channels: dict[str, float]
    senders: dict[str, float]
    events: int = 0

    def channel(self, name: str) -> float:
        return self.channels.get(_norm_key(name), 0.0)

    def sender(self, name: str) -> float:
        return self.senders.get(_norm_key(name), 0.0)

    def habits(self, *, limit: int = 3, threshold: float = 0.25) -> str:
        """One plain sentence about what you tend to open and what you tend to skip, or ''."""
        likes = sorted(((v, k) for k, v in {**self.channels, **self.senders}.items() if v >= threshold), reverse=True)
        skips = sorted(((v, k) for k, v in {**self.channels, **self.senders}.items() if v <= -threshold))
        parts = []
        if likes:
            parts.append("you usually open things from " + ", ".join(k for _, k in likes[:limit]))
        if skips:
            parts.append("you usually skip " + ", ".join(k for _, k in skips[:limit]))
        return ("; ".join(parts) + ".") if parts else ""


def _norm_key(name: Any) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()[:120]


_URL_RE = re.compile(r"\(?\bhttps?://\S+?\)?(?=\s|$)")
_PUNCT_RE = re.compile(r"[^\w\s]")


_MESSAGE_LINK = re.compile(r"/archives/[A-Z0-9]+/p\d{10,}", re.IGNORECASE)   # a Slack permalink
_CHANNEL_OF_LINK = re.compile(r"(.*?/archives/[A-Z0-9]+)/p\d{10,}", re.IGNORECASE)


def _norm_text(text: str) -> str:
    """Reader-independent form: the screen shows a link's label, the API its
    URL as well — drop URLs and punctuation so both readers agree on identity."""
    t = _URL_RE.sub(" ", (text or "").lower())
    t = _PUNCT_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def message_key(source: str, channel: str, sender: str, ts: float, text: str) -> str:
    """Stable identity of a message independent of which reader saw it."""
    day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    raw = f"{source}|{(channel or '').strip().lower()}|{(sender or '').strip().lower()}|{day}|{_norm_text(text)[:240]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def keys_for(events: Iterable[Any], *, now: float | None = None) -> list[str]:
    """The store keys the given normalised events map to (without writing anything)."""
    now = now if now is not None else time.time()
    out: list[str] = []
    for ev in events:
        text = (getattr(ev, "plain_text_extract", None) or getattr(ev, "plain_text", "") or "").strip()
        if len(text) < 2:
            continue
        out.append(message_key(
            _source_name(getattr(ev, "source", "")), (getattr(ev, "title", "") or "").strip(),
            (getattr(ev, "sender", None) or getattr(ev, "sender_name", None) or "").strip(),
            _epoch(getattr(ev, "timestamp", None), now), text,
        ))
    return out


def _epoch(value: Any, default: float) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _source_name(value: Any) -> str:
    v = getattr(value, "value", value)
    return str(v or "").lower()


class KnowledgeStore:
    """SQLite-backed memory of everything Otto has read. See module docstring."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_messages: int = DEFAULT_MAX_MESSAGES,
    ) -> None:
        self.path = Path(path) if path else paths.knowledge_db()
        self.retention_days = max(1, int(retention_days))
        self.max_messages = max(100, int(max_messages))
        self._last_prune = 0.0
        self._ready = False

    # -- plumbing ------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = self._open()
        except sqlite3.DatabaseError as e:
            # A damaged file must not take memory down for good: set it aside
            # (nothing is deleted) and start a fresh one.
            if not self._looks_corrupt(e):
                raise
            self._quarantine(e)
            conn = self._open()
        try:
            yield conn
        except sqlite3.DatabaseError as e:
            # Damage discovered mid-session: this call fails (the caller logs
            # and degrades), the next one starts on a fresh file.
            if self._looks_corrupt(e):
                conn.close()
                self._quarantine(e)
            raise
        finally:
            conn.close()
            # The WAL/SHM side files come and go with connections; keep all of
            # them private to the user like every other file in the data dir.
            self._restrict(self.path)

    def _open(self) -> sqlite3.Connection:
        existed = self.path.exists()
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            if not self._ready or not existed:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                self._migrate(conn)
                self._ready = True
            return conn
        except Exception:
            conn.close()
            raise

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        for table, column in _MIGRATIONS:
            name = column.split()[0]
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")

    @staticmethod
    def _looks_corrupt(err: BaseException) -> bool:
        msg = str(err).lower()
        return any(s in msg for s in ("malformed", "not a database", "file is encrypted", "disk image", "corrupt"))

    def _quarantine(self, err: BaseException) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        for candidate in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            if candidate.exists():
                try:
                    os.replace(str(candidate), f"{candidate}.corrupt-{stamp}")
                except OSError as e:
                    logger.warning("could not set aside %s: %s", candidate.name, e)
        self._ready = False
        logger.warning("knowledge store was unreadable (%s); kept as %s.corrupt-%s and started a new one",
                       err, self.path.name, stamp)

    @staticmethod
    def _restrict(path: Path) -> None:
        for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            try:
                if candidate.exists():
                    os.chmod(str(candidate), 0o600)
            except OSError:
                pass

    # -- writing -------------------------------------------------------------

    def remember(self, events: Iterable[Any], *, now: float | None = None) -> list[str]:
        """
        Store normalised events. Returns the keys that were *new* (first time
        Otto has seen that message), which is what downstream extraction runs on.
        """
        now = now if now is not None else time.time()
        rows: list[tuple] = []
        for ev in events:
            text = (getattr(ev, "plain_text_extract", None) or getattr(ev, "plain_text", "") or "").strip()
            if len(text) < 2:
                continue
            channel = (getattr(ev, "title", "") or "").strip()
            sender = (getattr(ev, "sender", None) or getattr(ev, "sender_name", None) or "").strip()
            source = _source_name(getattr(ev, "source", ""))
            ts = _epoch(getattr(ev, "timestamp", None), now)
            key = message_key(source, channel, sender, ts, text)
            rows.append((
                key, source, channel, sender, 1 if getattr(ev, "is_auto_generated", False) else 0,
                ts, text[:MAX_TEXT_CHARS], str(getattr(ev, "source_url", "") or "")[:500], now, now,
                str(getattr(ev, "conversation_id", "") or "")[:200], str(getattr(ev, "source_id", "") or "")[:200],
            ))
        if not rows:
            return []

        new_keys: list[str] = []
        try:
            with self._connect() as conn:
                keys = [r[0] for r in rows]
                existing: set[str] = set()
                for i in range(0, len(keys), 500):
                    chunk = keys[i:i + 500]
                    marks = ",".join("?" * len(chunk))
                    existing.update(
                        r[0] for r in conn.execute(f"SELECT key FROM messages WHERE key IN ({marks})", chunk)
                    )
                conn.execute("BEGIN")
                for row in rows:
                    if row[0] in existing:
                        conn.execute(
                            "UPDATE messages SET last_seen = ?, seen = seen + 1 WHERE key = ?",
                            (now, row[0]),
                        )
                        # The same message read by the other reader: keep the
                        # link that lands on the message over one that opens the
                        # channel (the screen reader stores first, the API knows more).
                        if _MESSAGE_LINK.search(row[7] or ""):
                            conn.execute(
                                "UPDATE messages SET url = ? WHERE key = ? AND url NOT LIKE '%/archives/%/p%'",
                                (row[7], row[0]),
                            )
                    else:
                        conn.execute(
                            "INSERT OR IGNORE INTO messages "
                            "(key, source, channel, sender, is_bot, ts, text, url, first_seen, last_seen, conv, source_id, seen) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                            row,
                        )
                        existing.add(row[0])
                        new_keys.append(row[0])
                conn.execute("COMMIT")
                if now - self._last_prune > _PRUNE_INTERVAL_S:
                    self._prune(conn, now)
                    self._last_prune = now
        except sqlite3.Error as e:
            logger.warning("knowledge store write failed: %s", e)
            return []
        return new_keys

    def _prune(self, conn: sqlite3.Connection, now: float) -> int:
        cutoff = now - self.retention_days * 86400
        removed = conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,)).rowcount or 0
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if total > self.max_messages:
            excess = total - self.max_messages
            conn.execute(
                "DELETE FROM messages WHERE key IN (SELECT key FROM messages ORDER BY ts ASC LIMIT ?)",
                (excess,),
            )
            removed += excess
        conn.execute(
            "DELETE FROM commitments WHERE status != 'open' AND updated < ?",
            (now - 30 * 86400,),
        )
        conn.execute("DELETE FROM feedback WHERE ts < ?", (now - 2 * FEEDBACK_DAYS * 86400,))
        if removed:
            logger.info("knowledge store pruned %d old messages", removed)
        return removed

    def prune(self, *, now: float | None = None) -> int:
        try:
            with self._connect() as conn:
                return self._prune(conn, now if now is not None else time.time())
        except sqlite3.Error as e:
            logger.warning("knowledge store prune failed: %s", e)
            return 0

    # -- reading -------------------------------------------------------------

    @staticmethod
    def _row_to_message(r: sqlite3.Row) -> Message:
        cols = set(r.keys())
        return Message(
            key=r["key"], source=r["source"], channel=r["channel"], sender=r["sender"],
            is_bot=bool(r["is_bot"]), ts=float(r["ts"]), text=r["text"], url=r["url"],
            first_seen=float(r["first_seen"]), last_seen=float(r["last_seen"]), seen=int(r["seen"]),
            conv=str(r["conv"] or "") if "conv" in cols else "",
            source_id=str(r["source_id"] or "") if "source_id" in cols else "",
        )

    # Conversations are worth recalling; a calendar or a Jira board is a live
    # view of *now* and an old snapshot of it would mislead.
    RECALL_SOURCES = ("slack", "email")

    def recall(
        self, *, since: float, exclude_keys: Iterable[str] = (), limit: int = 2000,
        sources: Iterable[str] = RECALL_SOURCES,
    ) -> list[Message]:
        """Messages read earlier that fall inside the briefing window but are
        not on screen right now — the briefing is built from memory, not from
        whatever happens to be visible this minute."""
        skip = set(exclude_keys)
        wanted = {s.lower() for s in sources}
        return [m for m in self.messages(since=since, limit=limit) if m.key not in skip and m.source in wanted]

    def messages(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        channel: str | None = None,
        sender: str | None = None,
        bots: bool | None = None,
        keys: Iterable[str] | None = None,
        limit: int = 1000,
        newest_first: bool = False,
    ) -> list[Message]:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("ts >= ?"); params.append(float(since))
        if until is not None:
            clauses.append("ts < ?"); params.append(float(until))
        if channel:
            clauses.append("channel = ? COLLATE NOCASE"); params.append(channel)
        if sender:
            clauses.append("sender = ? COLLATE NOCASE"); params.append(sender)
        if bots is not None:
            clauses.append("is_bot = ?"); params.append(1 if bots else 0)
        if keys is not None:
            key_list = list(keys)
            if not key_list:
                return []
            clauses.append(f"key IN ({','.join('?' * len(key_list))})"); params.extend(key_list)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "DESC" if newest_first else "ASC"
        sql = f"SELECT * FROM messages {where} ORDER BY ts {order}, first_seen {order} LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            with self._connect() as conn:
                return [self._row_to_message(r) for r in conn.execute(sql, params)]
        except sqlite3.Error as e:
            logger.warning("knowledge store read failed: %s", e)
            return []

    def search(self, terms: Iterable[str], *, since: float | None = None, limit: int = 20) -> list[Message]:
        """Messages containing *all* terms (case-insensitive substring match)."""
        words = [t.strip().lower() for t in terms if t and t.strip()]
        if not words:
            return []
        clauses = ["LOWER(text) LIKE ?" for _ in words]
        params: list[Any] = [f"%{w}%" for w in words]
        if since is not None:
            clauses.append("ts >= ?"); params.append(float(since))
        params.append(max(1, int(limit)))
        sql = f"SELECT * FROM messages WHERE {' AND '.join(clauses)} ORDER BY ts DESC LIMIT ?"
        try:
            with self._connect() as conn:
                return [self._row_to_message(r) for r in conn.execute(sql, params)]
        except sqlite3.Error as e:
            logger.warning("knowledge store search failed: %s", e)
            return []

    def channels(self, *, since: float | None = None) -> list[ChannelActivity]:
        where, params = ("WHERE ts >= ?", [float(since)]) if since is not None else ("", [])
        sql = (
            "SELECT channel, COUNT(*) AS total, SUM(CASE WHEN is_bot=0 THEN 1 ELSE 0 END) AS humans, "
            "SUM(is_bot) AS bots, COUNT(DISTINCT sender) AS senders, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
            f"FROM messages {where} GROUP BY channel ORDER BY total DESC"
        )
        try:
            with self._connect() as conn:
                return [
                    ChannelActivity(
                        channel=r["channel"], total=int(r["total"]), humans=int(r["humans"] or 0),
                        bots=int(r["bots"] or 0), senders=int(r["senders"] or 0),
                        first_ts=float(r["first_ts"]), last_ts=float(r["last_ts"]),
                    )
                    for r in conn.execute(sql, params)
                ]
        except sqlite3.Error as e:
            logger.warning("knowledge store channel stats failed: %s", e)
            return []

    def people(self, *, since: float | None = None, bots: bool = False) -> list[PersonActivity]:
        clauses = ["sender != ''", "is_bot = ?"]
        params: list[Any] = [1 if bots else 0]
        if since is not None:
            clauses.append("ts >= ?"); params.append(float(since))
        sql = (
            "SELECT sender, COUNT(*) AS total, COUNT(DISTINCT channel) AS channels, MAX(ts) AS last_ts "
            f"FROM messages WHERE {' AND '.join(clauses)} GROUP BY sender COLLATE NOCASE ORDER BY total DESC"
        )
        try:
            with self._connect() as conn:
                return [
                    PersonActivity(sender=r["sender"], total=int(r["total"]), channels=int(r["channels"]),
                                   last_ts=float(r["last_ts"]))
                    for r in conn.execute(sql, params)
                ]
        except sqlite3.Error as e:
            logger.warning("knowledge store people stats failed: %s", e)
            return []

    def daily_counts(self, *, days: int, now: float | None = None, channel: str | None = None) -> list[int]:
        """Messages per local day for the last ``days`` days, oldest first."""
        now = now if now is not None else time.time()
        start_day = datetime.fromtimestamp(now).astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        buckets = [0] * max(1, days)
        since = (start_day.timestamp()) - (days - 1) * 86400
        for m in self.messages(since=since, channel=channel, limit=self.max_messages):
            idx = int((m.ts - since) // 86400)
            if 0 <= idx < days:
                buckets[idx] += 1
        return buckets

    def stats(self, *, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
                    "COUNT(DISTINCT channel) AS channels, "
                    "COUNT(DISTINCT CASE WHEN is_bot=0 AND sender != '' THEN LOWER(sender) END) AS people "
                    "FROM messages"
                ).fetchone()
                week = conn.execute(
                    "SELECT COUNT(*) AS n, COUNT(DISTINCT channel) AS channels FROM messages WHERE ts >= ?",
                    (now - 7 * 86400,),
                ).fetchone()
                open_items = conn.execute(
                    "SELECT COUNT(*) FROM commitments WHERE status = 'open'"
                ).fetchone()[0]
        except sqlite3.Error as e:
            logger.warning("knowledge store stats failed: %s", e)
            return {"messages": 0, "channels": 0, "people": 0, "days": 0, "messages_7d": 0,
                    "channels_7d": 0, "open_commitments": 0, "first_ts": None, "last_ts": None}
        n = int(row["n"] or 0)
        first_ts = float(row["first_ts"]) if row["first_ts"] is not None else None
        last_ts = float(row["last_ts"]) if row["last_ts"] is not None else None
        days = 0
        if first_ts is not None and last_ts is not None:
            days = max(1, int((last_ts - first_ts + 1.0) // 86400) + 1)     # +1 s absorbs float jitter
        return {
            "messages": n,
            "channels": int(row["channels"] or 0),
            "people": int(row["people"] or 0),
            "days": days,
            "messages_7d": int(week["n"] or 0),
            "channels_7d": int(week["channels"] or 0),
            "open_commitments": int(open_items or 0),
            "first_ts": first_ts,
            "last_ts": last_ts,
        }

    # -- exact links ----------------------------------------------------------

    def permalink_for(self, *, channel: str, text: str, source: str = "slack", since: float | None = None) -> str:
        """The message-level link another reader stored for this same message ('' if none).

        The screen reader knows a message's channel but not its timestamp, so
        its link opens the channel; the API copy — seen in this refresh or an
        earlier one — carries the permalink. When the two were not read in the
        same refresh (a DM scrolled back to yesterday), memory still has it.
        """
        want = _norm_text(text)[:120]
        if len(want) < 8 or not channel:
            return ""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT text, url FROM messages WHERE source = ? AND channel = ? COLLATE NOCASE "
                    "AND url LIKE '%/archives/%/p%' AND ts >= ? ORDER BY ts DESC LIMIT 400",
                    (source, channel, float(since or 0.0)),
                ).fetchall()
        except sqlite3.Error as e:
            logger.debug("permalink lookup failed: %s", e)
            return ""
        for stored, url in rows:
            if _norm_text(stored)[:120] == want:
                return str(url or "")
        return ""

    def channel_link_for(self, *, channel: str, source: str = "slack") -> str:
        """A link that opens this conversation ('' if memory has no API copy from it).

        Derived from any permalink stored for the channel — ``…/archives/<id>/p…``
        → ``…/archives/<id>`` — so a message only the screen has seen at least
        lands in the right channel rather than just in the app.
        """
        if not channel:
            return ""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT url FROM messages WHERE source = ? AND channel = ? COLLATE NOCASE "
                    "AND url LIKE '%/archives/%/p%' ORDER BY ts DESC LIMIT 1",
                    (source, channel),
                ).fetchone()
        except sqlite3.Error as e:
            logger.debug("channel link lookup failed: %s", e)
            return ""
        m = _CHANNEL_OF_LINK.match(str(row[0]) if row else "")
        return m.group(1) if m else ""

    # -- LLM context ---------------------------------------------------------

    def context_for(
        self,
        *,
        channel: str,
        sender: str = "",
        before: float | None = None,
        exclude_keys: Iterable[str] = (),
        budget_chars: int = 1200,
        now: float | None = None,
    ) -> str:
        """
        Earlier messages that give a conversation its background: the last few
        in the same channel before it, plus what the same sender said elsewhere
        this week. Compact ``[Sep 10 08:19] alice: …`` lines within ``budget_chars``.
        """
        now = now if now is not None else time.time()
        excluded = set(exclude_keys)
        lines: list[str] = []
        used = 0

        def _add(prefix: str, msgs: list[Message]) -> None:
            nonlocal used
            block: list[str] = []
            for m in msgs:
                if m.key in excluded:
                    continue
                stamp = m.when.astimezone().strftime("%b %d %H:%M")
                who = m.sender or ("bot" if m.is_bot else "someone")
                body = re.sub(r"\s+", " ", m.text)[:160]
                block.append(f"[{stamp}] {who}: {body}")
            if not block:
                return
            text = prefix + "\n".join(block)
            if used + len(text) > budget_chars:
                text = text[: max(0, budget_chars - used)]
            if text.strip():
                lines.append(text)
                used += len(text)

        if channel:
            earlier = self.messages(channel=channel, until=before, since=now - 14 * 86400,
                                    limit=8, newest_first=True)
            _add(f"Earlier in {channel}:\n", list(reversed(earlier)))
        if sender and used < budget_chars:
            elsewhere = [
                m for m in self.messages(sender=sender, since=now - 7 * 86400, until=before,
                                         limit=12, newest_first=True)
                if m.channel.lower() != (channel or "").lower()
            ][:3]
            _add(f"{sender} elsewhere this week:\n", list(reversed(elsewhere)))
        return "\n\n".join(lines)

    # -- commitments ---------------------------------------------------------

    def upsert_commitment(self, row: dict[str, Any]) -> bool:
        """Insert a commitment; if it already exists only refresh mutable fields. Returns True if new."""
        now = float(row.get("updated") or time.time())
        try:
            with self._connect() as conn:
                exists = conn.execute("SELECT status FROM commitments WHERE id = ?", (row["id"],)).fetchone()
                if exists:
                    conn.execute(
                        "UPDATE commitments SET due = COALESCE(?, due), due_text = CASE WHEN ? != '' THEN ? ELSE due_text END, "
                        "updated = ?, confidence = MAX(confidence, ?), for_you = MAX(for_you, ?) WHERE id = ?",
                        (row.get("due"), row.get("due_text", ""), row.get("due_text", ""), now,
                         float(row.get("confidence", 0.5)), 1 if row.get("for_you") else 0, row["id"]),
                    )
                    return False
                conn.execute(
                    "INSERT INTO commitments (id, kind, who, what, channel, due, due_text, created, updated, "
                    "status, confidence, for_you, source_key, url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["id"], row["kind"], row.get("who", ""), row["what"], row.get("channel", ""),
                        row.get("due"), row.get("due_text", ""), float(row.get("created") or now), now,
                        row.get("status", "open"), float(row.get("confidence", 0.5)),
                        1 if row.get("for_you") else 0, row.get("source_key", ""), row.get("url", ""),
                    ),
                )
                return True
        except sqlite3.Error as e:
            logger.warning("knowledge store commitment write failed: %s", e)
            return False

    def commitments(self, *, status: str | None = "open", limit: int = 200) -> list[dict[str, Any]]:
        where, params = ("WHERE status = ?", [status]) if status else ("", [])
        sql = f"SELECT * FROM commitments {where} ORDER BY COALESCE(due, created + 1e12) ASC, created ASC LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            with self._connect() as conn:
                return [dict(r) for r in conn.execute(sql, params)]
        except sqlite3.Error as e:
            logger.warning("knowledge store commitment read failed: %s", e)
            return []

    def set_commitment_status(self, cid: str, status: str, *, now: float | None = None) -> bool:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE commitments SET status = ?, updated = ? WHERE id = ?",
                    (status, now if now is not None else time.time(), cid),
                )
                return bool(cur.rowcount)
        except sqlite3.Error as e:
            logger.warning("knowledge store commitment update failed: %s", e)
            return False

    # -- feedback: what you did with items, and the habits that fall out of it --

    def record_feedback(self, kind: str, *, item_id: str = "", channel: str = "", sender: str = "",
                        source: str = "", now: float | None = None) -> bool:
        """Remember one thing you did (open / expand / snooze / dismiss / clear) with an item."""
        kind = str(kind or "").strip().lower()
        if kind not in FEEDBACK_WEIGHTS:
            return False
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO feedback (ts, kind, item_id, channel, sender, source) VALUES (?, ?, ?, ?, ?, ?)",
                    (now if now is not None else time.time(), kind, str(item_id or "")[:200],
                     _norm_key(channel), _norm_key(sender), str(source or "")[:40]),
                )
                return True
        except sqlite3.Error as e:
            logger.warning("knowledge store feedback write failed: %s", e)
            return False

    def feedback_priors(self, *, now: float | None = None, days: int = FEEDBACK_DAYS) -> "FeedbackPriors":
        """
        Per-channel and per-sender habit scores in [-1, 1] from recent feedback.

        A score is the weighted sum of what you did, shrunk towards zero by
        ``FEEDBACK_SHRINK`` so two dismissals do not condemn a channel but
        twenty do: ``sum(w) / (n + shrink)`` clipped to [-1, 1]. Anything that
        happened to an item you never opened but dismissed within a few minutes
        of it appearing counts as a real "no"; a dismissal after an open counts
        for nothing (you read it and were done).
        """
        since = (now if now is not None else time.time()) - days * 86400.0
        chans: dict[str, tuple[float, int]] = {}
        senders: dict[str, tuple[float, int]] = {}
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT kind, item_id, channel, sender FROM feedback WHERE ts >= ? ORDER BY ts", (since,)
                ).fetchall()
        except sqlite3.Error as e:
            logger.warning("knowledge store feedback read failed: %s", e)
            return FeedbackPriors({}, {}, 0)
        opened: set[str] = set()
        for r in rows:
            kind, item_id, channel, sender = r["kind"], r["item_id"], r["channel"], r["sender"]
            if kind == "open" and item_id:
                opened.add(item_id)
            w = FEEDBACK_WEIGHTS.get(kind, 0.0)
            if kind in ("dismiss", "clear") and item_id and item_id in opened:
                w = 0.0                                  # read, then tidied away: not a "no"
            if channel:
                s, n = chans.get(channel, (0.0, 0))
                chans[channel] = (s + w, n + 1)
            if sender and kind != "clear":
                s, n = senders.get(sender, (0.0, 0))
                senders[sender] = (s + w, n + 1)

        def _score(sum_n: tuple[float, int]) -> float:
            s, n = sum_n
            return max(-1.0, min(1.0, s / (n + FEEDBACK_SHRINK)))

        return FeedbackPriors(
            channels={k: _score(v) for k, v in chans.items()},
            senders={k: _score(v) for k, v in senders.items()},
            events=len(rows),
        )

    # -- small key/value notes (learned facts such as the user's own names) ----

    def get_meta(self, key: str, default: str = "") -> str:
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
                return str(row["v"]) if row else default
        except sqlite3.Error:
            return default

    def set_meta(self, key: str, value: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, value))
        except sqlite3.Error as e:
            logger.debug("knowledge store meta write failed: %s", e)


_STORE: KnowledgeStore | None = None


def default_store() -> KnowledgeStore:
    """Process-wide store bound to the current data dir (re-bound if the dir changes)."""
    global _STORE
    target = paths.knowledge_db()
    if _STORE is None or _STORE.path != target:
        _STORE = KnowledgeStore(target)
    return _STORE
