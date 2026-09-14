from __future__ import annotations
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Optional

import aiosqlite

from otto.storage.models import NormalizedEvent, Conversation

logger = logging.getLogger(__name__)

class OttoDatabase:
    """Database manager for Otto using aiosqlite."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self):
        """Creates/opens the SQLite database and sets WAL mode."""
        # Ensure parent directories exist
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        
        # Use WAL journal mode for better concurrent read performance
        await self.db.execute("PRAGMA journal_mode = WAL")
        await self.db.execute("PRAGMA synchronous = NORMAL")
        await self.db.execute("PRAGMA foreign_keys = ON")
        
        # Note: SQLCipher encryption support point.
        # If using pysqlcipher3, we would run:
        # await self.db.execute(f"PRAGMA key = '{encryption_key}'")
        
        await self._init_schema()

    async def close(self):
        """Close connection and perform WAL checkpoint."""
        if self.db:
            try:
                # WAL checkpoint
                await self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as e:
                logger.warning(f"Error during WAL checkpoint: {e}")
            await self.db.close()
            self.db = None

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Context manager for transactions."""
        if not self.db:
            raise RuntimeError("Database not connected")
            
        async with self._lock:
            await self.db.execute("BEGIN TRANSACTION")
            try:
                yield self.db
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

    async def _init_schema(self):
        """Schema creation for all models from models.py."""
        if not self.db:
            raise RuntimeError("Database not connected")

        # Create tables
        schema = """
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            source TEXT,
            account_id TEXT,
            thread_id TEXT,
            subject TEXT,
            summary TEXT,
            domain TEXT,
            relevance_explanation TEXT,
            started TEXT,
            last_activity TEXT,
            message_count INTEGER,
            is_active BOOLEAN,
            participants TEXT,
            initiator TEXT,
            user_role TEXT,
            topic_arc TEXT,
            urgency REAL,
            importance REAL,
            opportunity_score REAL,
            open_actions TEXT,
            resolved_actions TEXT,
            event_ids TEXT,
            related_conversation_ids TEXT,
            related_ticket_ids TEXT,
            related_calendar_event_ids TEXT,
            user_feedback TEXT,
            is_dismissed BOOLEAN,
            dismissed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            conversation_id TEXT,
            source TEXT,
            account_id TEXT,
            source_id TEXT,
            source_url TEXT,
            timestamp TEXT,
            title TEXT,
            plain_text_extract TEXT,
            content_hash TEXT,
            content_language TEXT,
            is_auto_generated BOOLEAN,
            ingested_at TEXT,
            content_blocks TEXT,
            sender TEXT,
            recipients TEXT,
            has_attachments BOOLEAN,
            attachment_summaries TEXT,
            entities TEXT,
            embedding TEXT,
            FOREIGN KEY(conversation_id) REFERENCES conversations(id)
        );
        
        CREATE INDEX IF NOT EXISTS idx_events_conversation_id ON events(conversation_id);

        CREATE TABLE IF NOT EXISTS persons (
            id TEXT PRIMARY KEY,
            display_name TEXT,
            email TEXT,
            slack_id TEXT,
            jira_id TEXT,
            role_to_user TEXT,
            interaction_count_30d INTEGER,
            last_interaction TEXT,
            domains TEXT,
            importance_score REAL,
            topics TEXT,
            last_discussed TEXT,
            sentiment_trend REAL,
            response_time_avg_hours REAL,
            blockers_involving TEXT,
            context_embedding TEXT
        );

        CREATE TABLE IF NOT EXISTS action_items (
            id TEXT PRIMARY KEY,
            description TEXT,
            owner_id TEXT,
            domain TEXT,
            source_conversation_ids TEXT,
            first_seen TEXT,
            last_mentioned TEXT,
            status TEXT,
            deadline TEXT,
            completion_evidence TEXT,
            staleness_days INTEGER,
            urgency REAL
        );

        CREATE TABLE IF NOT EXISTS briefings (
            id TEXT PRIMARY KEY,
            type TEXT,
            content_hash TEXT,
            valid_until TEXT,
            generated_at TEXT,
            sections TEXT,
            calendar_event_id TEXT,
            time_range TEXT,
            source_conversation_ids TEXT,
            source_action_item_ids TEXT,
            read_at TEXT,
            time_spent_seconds REAL,
            feedback TEXT,
            is_stale BOOLEAN
        );

        CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY,
            feedback_type TEXT,
            event_id TEXT,
            conversation_id TEXT,
            reason TEXT,
            timestamp TEXT
        );
        """
        await self.db.executescript(schema)
        await self.db.commit()

    # Basic CRUD async methods
    async def insert_event(self, event: NormalizedEvent) -> None:
        if not self.db:
            return
            
        await self.db.execute("""
            INSERT OR REPLACE INTO events (
                id, conversation_id, source, account_id, source_id, source_url, 
                timestamp, title, plain_text_extract, content_hash, content_language, 
                is_auto_generated, ingested_at, content_blocks, sender, recipients, 
                has_attachments, attachment_summaries, entities, embedding
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event.id, event.conversation_id, event.source, event.account_id,
            event.source_id, event.source_url, event.timestamp.isoformat(),
            event.title, event.plain_text_extract, event.content_hash,
            event.content_language, event.is_auto_generated,
            event.ingested_at.isoformat(),
            json.dumps([cb.__dict__ for cb in event.content_blocks]),
            event.sender, json.dumps(event.recipients),
            event.has_attachments, json.dumps(event.attachment_summaries),
            json.dumps([e.__dict__ for e in event.entities]),
            json.dumps(event.embedding) if event.embedding else None
        ))
        await self.db.commit()

    async def insert_conversation(self, conv: Conversation) -> None:
        if not self.db:
            return
            
        await self.db.execute("""
            INSERT OR REPLACE INTO conversations (
                id, source, account_id, thread_id, subject, summary, domain,
                relevance_explanation, started, last_activity, message_count,
                is_active, participants, initiator, user_role, topic_arc, urgency,
                importance, opportunity_score, open_actions, resolved_actions,
                event_ids, related_conversation_ids, related_ticket_ids,
                related_calendar_event_ids, user_feedback, is_dismissed, dismissed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            conv.id, conv.source, conv.account_id, conv.thread_id, conv.subject,
            conv.summary, conv.domain, conv.relevance_explanation,
            conv.started.isoformat(), conv.last_activity.isoformat(),
            conv.message_count, conv.is_active, json.dumps(conv.participants),
            conv.initiator, conv.user_role, json.dumps(conv.topic_arc),
            conv.urgency, conv.importance, conv.opportunity_score,
            json.dumps(conv.open_actions), json.dumps(conv.resolved_actions),
            json.dumps(conv.event_ids), json.dumps(conv.related_conversation_ids),
            json.dumps(conv.related_ticket_ids), json.dumps(conv.related_calendar_event_ids),
            conv.user_feedback, conv.is_dismissed, 
            conv.dismissed_at.isoformat() if conv.dismissed_at else None
        ))
        await self.db.commit()

    async def get_conversation(self, conv_id: str) -> Optional[aiosqlite.Row]:
        if not self.db:
            return None
            
        async with self.db.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)) as cursor:
            return await cursor.fetchone()

    async def query_events_since(self, since: datetime) -> list[aiosqlite.Row]:
        if not self.db:
            return []
            
        async with self.db.execute("SELECT * FROM events WHERE timestamp > ?", (since.isoformat(),)) as cursor:
            return await cursor.fetchall()
