"""
Persistent cache of LLM classification results, keyed by *content*.

Otto refreshes every minute. Without this cache every refresh would send
every visible conversation to the LLM again — slow, expensive, and a
privacy cost with no benefit, because the content has not changed.

The key is a hash of the conversation identity plus the text that was
classified, so a new message in a thread invalidates the entry naturally.
Entries expire after ``ttl`` and the file is capped at ``max_entries``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from otto import paths

logger = logging.getLogger("otto.intelligence.classification_cache")

_FIELDS = (
    "urgency", "importance", "opportunity_score", "domain", "summary",
    "relevance_explanation", "ai_analysis", "topics", "opportunity_type",
    "opportunity_description", "action_items", "matched_directive", "for_you", "llm_enriched",
)


def content_key(source: str, thread_id: str, text: str, directives_fingerprint: str = "") -> str:
    """Stable key for a conversation's classified content."""
    h = hashlib.sha256()
    h.update(f"{source}\x1f{thread_id}\x1f{directives_fingerprint}\x1f".encode("utf-8"))
    h.update((text or "")[:4000].encode("utf-8", errors="replace"))
    return h.hexdigest()


class ClassificationCache:
    def __init__(
        self,
        path: Path | None = None,
        *,
        ttl_seconds: float = 7 * 24 * 3600,
        max_entries: int = 2000,
    ) -> None:
        self._path = path
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] | None = None
        self.hits = 0
        self.misses = 0

    @property
    def path(self) -> Path:
        return self._path or paths.classification_cache_file()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._data is not None:
            return self._data
        data: dict[str, dict[str, Any]] = {}
        p = self.path
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    data = {k: v for k, v in raw.items() if isinstance(v, dict)}
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("Classification cache unreadable (%s); starting fresh", e)
        self._data = self._prune(data)
        return self._data

    def _prune(self, data: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        now = time.time()
        fresh = {k: v for k, v in data.items() if now - float(v.get("_ts", 0)) < self._ttl}
        if len(fresh) > self._max:
            ordered = sorted(fresh.items(), key=lambda kv: float(kv[1].get("_ts", 0)), reverse=True)
            fresh = dict(ordered[: self._max])
        return fresh

    def _save(self) -> None:
        if self._data is None:
            return
        p = self.path
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
            os.replace(tmp, p)
        except OSError as e:
            logger.warning("Could not write classification cache: %s", e)

    # -- API ------------------------------------------------------------------

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._load().get(key)
            if entry is None or time.time() - float(entry.get("_ts", 0)) >= self._ttl:
                self.misses += 1
                return None
            if not entry.get("llm_enriched", False):
                # Written before Otto tracked whether the LLM really answered —
                # possibly a heuristics-only result stored by mistake. Re-classify.
                self.misses += 1
                return None
            self.hits += 1
            return {k: v for k, v in entry.items() if not k.startswith("_")}

    def put(self, key: str, values: dict[str, Any], *, thread: str = "") -> None:
        with self._lock:
            data = self._load()
            entry = {k: values[k] for k in _FIELDS if k in values}
            entry["_ts"] = time.time()
            if thread:
                entry["_thread"] = thread
            data[key] = entry
            if len(data) > self._max:
                self._data = self._prune(data)
            self._save()

    def previous(self, thread: str, *, exclude_key: str = "") -> dict[str, Any] | None:
        """
        Otto's most recent earlier read of the same thread, if any.

        A thread that grows (a reply lands) gets a new content key and a fresh
        verdict; handing the model its previous summary lets the new one say
        what *changed* instead of starting from scratch. Returns the cached
        fields plus ``age_seconds``, or None.
        """
        if not thread:
            return None
        with self._lock:
            best: tuple[float, dict[str, Any]] | None = None
            for key, entry in self._load().items():
                if key == exclude_key or entry.get("_thread") != thread or not entry.get("llm_enriched"):
                    continue
                ts = float(entry.get("_ts", 0))
                if best is None or ts > best[0]:
                    best = (ts, entry)
            if best is None:
                return None
            out = {k: v for k, v in best[1].items() if not k.startswith("_")}
            out["age_seconds"] = max(0.0, time.time() - best[0])
            return out

    def apply(self, conversation: Any, cached: dict[str, Any]) -> None:
        """Copy cached classification fields onto a Conversation.

        The local pass has already run on *conversation*; its structural tags
        (``all-clear``, noise markers, directive match) are kept in front of
        the cached topics so the noise filter sees them even for entries
        written before those tags existed.
        """
        from otto.intelligence.classifier import STICKY_TOPICS
        from otto.storage.models import Domain
        for field_name, value in cached.items():
            if field_name == "domain":
                try:
                    conversation.domain = Domain(value)
                except ValueError:
                    continue
            elif field_name == "topics":
                sticky = [t for t in (getattr(conversation, "topics", None) or []) if t in STICKY_TOPICS]
                conversation.topics = sticky + [t for t in (value or []) if t not in sticky]
            elif hasattr(conversation, field_name):
                setattr(conversation, field_name, value)

    @staticmethod
    def snapshot(conversation: Any) -> dict[str, Any]:
        """Extract the cacheable fields from a classified Conversation."""
        out: dict[str, Any] = {}
        for field_name in _FIELDS:
            val = getattr(conversation, field_name, None)
            if val is None:
                continue
            if field_name == "domain":
                val = getattr(val, "value", str(val))
            out[field_name] = val
        return out

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._load()), "hits": self.hits, "misses": self.misses}
