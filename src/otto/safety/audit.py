"""
Append-only audit log with cryptographic hash chain.

This module provides the AuditLog class, which maintains a tamper-evident,
append-only record of system events, focusing particularly on HTTP requests,
blocked write attempts, and scope validation failures. The read-only guarantee
of the system relies on this audit log to prove that no unauthorized actions
were taken, and that no records of blocked actions were removed.
"""
from __future__ import annotations

import sqlite3
import hashlib
import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

class AuditEventType(Enum):
    http_request = "http_request"
    http_blocked = "http_blocked"
    scope_validation = "scope_validation"
    write_intent_detected = "write_intent_detected"
    system_event = "system_event"


@dataclass
class AuditEntry:
    timestamp: str
    event_type: AuditEventType
    source: str
    method: Optional[str]
    url: Optional[str]
    blocked: bool
    previous_hash: str
    entry_hash: str = ""

    def calculate_hash(self) -> str:
        """
        Calculate the SHA-256 hash of this entry, including the previous_hash.
        This forms the cryptographic link in the hash chain.
        """
        data = {
            "timestamp": self.timestamp,
            "event_type": self.event_type.value,
            "source": self.source,
            "method": self.method,
            "url": self.url,
            "blocked": self.blocked,
            "previous_hash": self.previous_hash
        }
        # Serialize to a deterministic JSON string
        hash_str = json.dumps(data, sort_keys=True)
        return hashlib.sha256(hash_str.encode("utf-8")).hexdigest()


class AuditLog:
    """
    Append-only audit log with a tamper-evident hash chain.
    """

    def __init__(self, db_path: str = "audit.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        with self._lock:
            self._init_db()
            self._last_hash = self._get_last_hash()

    def _init_db(self) -> None:
        """Initialize the SQLite database schema if it doesn't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    method TEXT,
                    url TEXT,
                    blocked BOOLEAN NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL
                )
            """)
            conn.commit()

    def _get_last_hash(self) -> str:
        """Retrieve the hash of the most recent entry, or a genesis hash."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                return row["entry_hash"]
            # Genesis hash
            return "0" * 64

    def append(self, event_type: AuditEventType, source: str, method: Optional[str] = None, 
               url: Optional[str] = None, blocked: bool = False) -> AuditEntry:
        """
        Append a new entry to the audit log with thread-safe atomic hash chaining.
        """
        with self._lock:
            entry = AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type=event_type,
                source=source,
                method=method,
                url=url,
                blocked=blocked,
                previous_hash=self._last_hash
            )
            entry.entry_hash = entry.calculate_hash()
            
            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("""
                        INSERT INTO audit_log (timestamp, event_type, source, method, url, blocked, previous_hash, entry_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (entry.timestamp, entry.event_type.value, entry.source, entry.method, entry.url, entry.blocked, entry.previous_hash, entry.entry_hash))
                    conn.commit()
                
                self._last_hash = entry.entry_hash
                return entry
            except sqlite3.Error as e:
                logger.error(f"Failed to write to audit log: {e}")
                raise

    def verify_integrity(self) -> bool:
        """
        Walk the hash chain to verify that no entries have been removed or modified.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM audit_log ORDER BY id ASC")
            
            expected_prev_hash = "0" * 64
            
            for row in cursor:
                entry = AuditEntry(
                    timestamp=row["timestamp"],
                    event_type=AuditEventType(row["event_type"]),
                    source=row["source"],
                    method=row["method"],
                    url=row["url"],
                    blocked=bool(row["blocked"]),
                    previous_hash=row["previous_hash"]
                )
                
                # Verify link
                if entry.previous_hash != expected_prev_hash:
                    logger.error(f"Integrity check failed: Chain broken at entry ID {row['id']}")
                    return False
                
                # Verify content hash
                calculated_hash = entry.calculate_hash()
                if calculated_hash != row["entry_hash"]:
                    logger.error(f"Integrity check failed: Hash mismatch at entry ID {row['id']}")
                    return False
                
                expected_prev_hash = calculated_hash
                
        return True

    def generate_report(self, days: int = 7) -> str:
        """
        Generate a human-readable summary of events from the past N days.
        """
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_iso = cutoff_date.isoformat()
        
        report_lines = [f"Audit Report (Last {days} days)"]
        report_lines.append("=" * 40)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM audit_log WHERE timestamp >= ? ORDER BY timestamp DESC", 
                (cutoff_iso,)
            )
            
            blocked_count = 0
            total_count = 0
            
            for row in cursor:
                total_count += 1
                if row["blocked"]:
                    blocked_count += 1
                    report_lines.append(
                        f"[{row['timestamp']}] BLOCKED {row['event_type']} - "
                        f"Source: {row['source']}, URL: {row['url']}, Method: {row['method']}"
                    )
            
            report_lines.append("-" * 40)
            report_lines.append(f"Total events: {total_count}")
            report_lines.append(f"Total blocked actions: {blocked_count}")
            
        return "\n".join(report_lines)
