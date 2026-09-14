from __future__ import annotations
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import List

from aiosqlite import Connection

from otto.storage.database import OttoDatabase
from otto.storage.models import (
    NormalizedEvent,
    Conversation,
    Person,
    FeedbackRecord
)

logger = logging.getLogger(__name__)

class WriteOp(ABC):
    """Protocol for write operations to be executed by the single writer."""
    
    @abstractmethod
    async def execute(self, db: Connection) -> None:
        """Execute the write operation."""
        pass


class InsertEvent(WriteOp):
    def __init__(self, event: NormalizedEvent):
        self.event = event

    async def execute(self, db: Connection) -> None:
        import json
        await db.execute("""
            INSERT OR REPLACE INTO events (
                id, conversation_id, source, account_id, source_id, source_url, 
                timestamp, title, plain_text_extract, content_hash, content_language, 
                is_auto_generated, ingested_at, content_blocks, sender, recipients, 
                has_attachments, attachment_summaries, entities, embedding
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            self.event.id, self.event.conversation_id, self.event.source, self.event.account_id,
            self.event.source_id, self.event.source_url, self.event.timestamp.isoformat(),
            self.event.title, self.event.plain_text_extract, self.event.content_hash,
            self.event.content_language, self.event.is_auto_generated,
            self.event.ingested_at.isoformat(),
            json.dumps([cb.__dict__ for cb in self.event.content_blocks]),
            self.event.sender, json.dumps(self.event.recipients),
            self.event.has_attachments, json.dumps(self.event.attachment_summaries),
            json.dumps([e.__dict__ for e in self.event.entities]),
            json.dumps(self.event.embedding) if self.event.embedding else None
        ))


class InsertConversation(WriteOp):
    def __init__(self, conv: Conversation):
        self.conv = conv

    async def execute(self, db: Connection) -> None:
        import json
        await db.execute("""
            INSERT OR REPLACE INTO conversations (
                id, source, account_id, thread_id, subject, summary, domain,
                relevance_explanation, started, last_activity, message_count,
                is_active, participants, initiator, user_role, topic_arc, urgency,
                importance, opportunity_score, open_actions, resolved_actions,
                event_ids, related_conversation_ids, related_ticket_ids,
                related_calendar_event_ids, user_feedback, is_dismissed, dismissed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            self.conv.id, self.conv.source, self.conv.account_id, self.conv.thread_id, self.conv.subject,
            self.conv.summary, self.conv.domain, self.conv.relevance_explanation,
            self.conv.started.isoformat(), self.conv.last_activity.isoformat(),
            self.conv.message_count, self.conv.is_active, json.dumps(self.conv.participants),
            self.conv.initiator, self.conv.user_role, json.dumps(self.conv.topic_arc),
            self.conv.urgency, self.conv.importance, self.conv.opportunity_score,
            json.dumps(self.conv.open_actions), json.dumps(self.conv.resolved_actions),
            json.dumps(self.conv.event_ids), json.dumps(self.conv.related_conversation_ids),
            json.dumps(self.conv.related_ticket_ids), json.dumps(self.conv.related_calendar_event_ids),
            self.conv.user_feedback, self.conv.is_dismissed, 
            self.conv.dismissed_at.isoformat() if self.conv.dismissed_at else None
        ))


class UpdateConversation(WriteOp):
    def __init__(self, conv_id: str, **kwargs):
        self.conv_id = conv_id
        self.kwargs = kwargs

    async def execute(self, db: Connection) -> None:
        import json
        if not self.kwargs:
            return
            
        set_clauses = []
        values = []
        for k, v in self.kwargs.items():
            set_clauses.append(f"{k} = ?")
            if isinstance(v, (dict, list)):
                values.append(json.dumps(v))
            else:
                values.append(v)
                
        values.append(self.conv_id)
        
        query = f"UPDATE conversations SET {', '.join(set_clauses)} WHERE id = ?"
        await db.execute(query, tuple(values))


class UpsertPerson(WriteOp):
    def __init__(self, person: Person):
        self.person = person

    async def execute(self, db: Connection) -> None:
        import json
        await db.execute("""
            INSERT OR REPLACE INTO persons (
                id, display_name, email, slack_id, jira_id, role_to_user,
                interaction_count_30d, last_interaction, domains, importance_score,
                topics, last_discussed, sentiment_trend, response_time_avg_hours,
                blockers_involving, context_embedding
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            self.person.id, self.person.display_name, self.person.email, self.person.slack_id,
            self.person.jira_id, self.person.role_to_user, self.person.interaction_count_30d,
            self.person.last_interaction.isoformat(), json.dumps(list(self.person.domains)),
            self.person.importance_score, json.dumps(self.person.topics),
            json.dumps([(t, d.isoformat()) for t, d in self.person.last_discussed]),
            self.person.sentiment_trend, self.person.response_time_avg_hours,
            json.dumps(self.person.blockers_involving), 
            json.dumps(self.person.context_embedding) if self.person.context_embedding else None
        ))


class InsertFeedback(WriteOp):
    def __init__(self, feedback: FeedbackRecord):
        self.feedback = feedback

    async def execute(self, db: Connection) -> None:
        await db.execute("""
            INSERT OR REPLACE INTO feedback (
                id, feedback_type, event_id, conversation_id, reason, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            self.feedback.id, self.feedback.feedback_type, self.feedback.event_id,
            self.feedback.conversation_id, self.feedback.reason, self.feedback.timestamp.isoformat()
        ))


class DatabaseWriter:
    """Serialized database writer that drains WriteOps from a queue in batches."""

    def __init__(self, db_manager: OttoDatabase, max_queue_size: int = 10000):
        self.db_manager = db_manager
        self.queue: asyncio.Queue[WriteOp] = asyncio.Queue(maxsize=max_queue_size)
        self.running = False
        self._task: asyncio.Task | None = None

    def start(self):
        """Start the single writer loop."""
        if not self.running:
            self.running = True
            self._task = asyncio.create_task(self._writer_loop())

    async def stop(self):
        """Gracefully shutdown and drain remaining queue on stop."""
        self.running = False
        if self._task:
            await self._task

    async def enqueue(self, op: WriteOp) -> None:
        """Enqueue a write operation, blocking if full."""
        await self.queue.put(op)

    async def _writer_loop(self):
        while self.running or not self.queue.empty():
            ops: List[WriteOp] = []
            
            # Drain up to 100 ops or 0.5s timeout
            try:
                # Wait for at least one item or short timeout
                if self.queue.empty() and self.running:
                    try:
                        first_op = await asyncio.wait_for(self.queue.get(), timeout=0.5)
                        ops.append(first_op)
                        self.queue.task_done()
                    except asyncio.TimeoutError:
                        continue
                        
                # Drain the rest up to 100
                while len(ops) < 100 and not self.queue.empty():
                    ops.append(self.queue.get_nowait())
                    self.queue.task_done()
                    
            except asyncio.CancelledError:
                break
                
            if ops and self.db_manager.db:
                try:
                    async with self.db_manager.transaction() as db:
                        for op in ops:
                            await op.execute(db)
                except Exception as e:
                    logger.error(f"Error executing write batch: {e}")
