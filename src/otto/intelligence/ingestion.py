from __future__ import annotations

"""
Ingestion pipeline — normalizes raw events, deduplicates, groups into
conversations, and publishes to the event bus.

Pipeline: RawEvent → normalize → dedup → thread → persist → publish
"""

import logging
from collections import OrderedDict
from typing import Any

from otto.adapters.base import RawEvent
from otto.core.event_bus import EventBus
from otto.storage.models import NormalizedEvent, NewEventsIngested
from otto.utils.content_parser import content_hash, html_to_text
from otto.utils.language import detect_language

logger = logging.getLogger("otto.intelligence.ingestion")

# Reader facts worth keeping past normalisation. Small, JSON-safe scalars and
# short lists only; anything else a reader puts in raw_metadata stays behind.
_META_KEYS = (
    "mentions_you", "is_dm", "reply_count", "reactions", "reply_users", "you_replied",
    "sender_title", "channel_purpose", "channel_topic", "extraction_mode", "is_edited",
)


def _carry_meta(raw_meta: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _META_KEYS:
        value = (raw_meta or {}).get(key)
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str):
            out[key] = value[:300]
        elif isinstance(value, (list, tuple)):
            out[key] = [str(v)[:80] for v in value[:20]]
    return out


class IngestionPipeline:
    """
    Transforms RawEvents from adapters into NormalizedEvents in the database.

    Steps:
    1. Normalize (HTML→text, language detect, content hash)
    2. Deduplicate (composite key: source+account+source_id+content_hash)
    3. Group into conversations (thread reconstruction)
    4. Persist via DatabaseWriter
    5. Publish NewEventsIngested to event bus
    """

    def __init__(
        self,
        event_bus: EventBus,
        db_writer: Any = None,
    ) -> None:
        self.event_bus = event_bus
        self._db_writer = db_writer
        self._seen_keys: OrderedDict[str, None] = OrderedDict()
        self._max_seen_keys = 100_000

    async def ingest(self, raw_events: list[RawEvent]) -> list[NormalizedEvent]:
        """
        Process a batch of raw events through the full pipeline.

        Returns list of new (non-duplicate) normalized events.
        """
        if not raw_events:
            return []

        normalized: list[NormalizedEvent] = []

        for raw in raw_events:
            try:
                event = self._normalize(raw)
                if event and not self._is_duplicate(event):
                    normalized.append(event)
            except Exception as e:
                logger.error("Failed to normalize event %s: %s", raw.source_id, e)

        if not normalized:
            return []

        # Persist
        if self._db_writer:
            for event in normalized:
                await self._db_writer.enqueue("insert_event", event)

        # Publish
        source_types = set(e.source for e in normalized)
        for source in source_types:
            source_events = [e for e in normalized if e.source == source]
            await self.event_bus.publish(
                NewEventsIngested(
                    source=source,
                    event_ids=[e.id for e in source_events],
                    count=len(source_events),
                )
            )

        logger.debug("Ingested %d new events from %d raw", len(normalized), len(raw_events))
        return normalized

    def _normalize(self, raw: RawEvent) -> NormalizedEvent:
        """Convert a RawEvent to a NormalizedEvent."""
        # Extract plain text from content blocks
        plain_text = raw.plain_text
        if not plain_text and raw.content_blocks:
            for block in raw.content_blocks:
                if block.text:
                    plain_text = block.text
                    break
                elif block.html:
                    plain_text = html_to_text(block.html)
                    break

        # Language detection
        lang = detect_language(plain_text) if plain_text else "en"

        # Content hash for dedup
        hash_input = f"{raw.source}:{raw.source_id}:{plain_text[:500]}"
        c_hash = content_hash(hash_input)

        return NormalizedEvent(
            source=raw.source,
            account_id=raw.sender_email or raw.sender_id or "",
            source_id=raw.source_id,
            source_url=raw.source_url,
            timestamp=raw.timestamp,
            title=raw.title,
            content_blocks=raw.content_blocks,
            plain_text_extract=plain_text[:2000] if plain_text else "",
            content_hash=c_hash,
            content_language=lang,
            is_auto_generated=raw.is_auto_generated,
            conversation_id=raw.thread_id,
            sender=raw.sender_name,
            meta=_carry_meta(raw.raw_metadata),
        )

    def _is_duplicate(self, event: NormalizedEvent) -> bool:
        """Check dedup via composite key with LRU eviction."""
        key = self._dedup_key(event)
        if key in self._seen_keys:
            logger.debug("Duplicate detected: %s", event.source_id)
            return True
        self._seen_keys[key] = None
        self._seen_keys.move_to_end(key)
        # Evict oldest keys if over capacity
        if len(self._seen_keys) > self._max_seen_keys:
            for _ in range(min(50_000, len(self._seen_keys))):
                self._seen_keys.popitem(last=False)
        return False

    def _dedup_key(self, event: NormalizedEvent) -> str:
        return f"{event.source}:{event.account_id}:{event.source_id}:{event.content_hash}"
