from __future__ import annotations

"""
Conversation history and standing directives.

* Conversation history is a JSON-lines file so subsequent briefing runs can
  inject recent context into LLM prompts ("nuance based on what you have
  already seen"). Retention: 72 hours, pruned on every save.

* Standing directives are explicit, user-authored rules ("always flag
  anything about the payments migration"). They never expire and are only
  created through the CLI / API — Otto never invents directives from
  message content, because a message that merely *contains* "make sure to"
  is not an instruction from the user to Otto.

Storage lives under :func:`otto.paths.history_dir`.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from otto import paths

logger = logging.getLogger("otto.intelligence.history")

_DEFAULT_RETENTION_HOURS = 72


def _history_file() -> Path:
    return paths.history_dir() / "conversations.jsonl"


def _directives_file() -> Path:
    return paths.history_dir() / "directives.jsonl"


def _ensure_dir() -> None:
    paths.history_dir().mkdir(parents=True, exist_ok=True)


def _atomic_write_lines(path: Path, entries: list[dict[str, Any]]) -> None:
    """Write JSON lines atomically (temp file + rename)."""
    _ensure_dir()
    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, default=str) + "\n")
        tmp_path.replace(path)
    except OSError as e:
        logger.error("Failed to write %s: %s", path.name, e)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _read_lines(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not path.exists():
        return entries
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # skip corrupt lines
    except OSError as e:
        logger.warning("Failed to read %s: %s", path.name, e)
    return entries


# ---------------------------------------------------------------------------
# Standing directives
# ---------------------------------------------------------------------------

def config_directives() -> list[str]:
    """``[user] directives`` from config.toml — the place they are written now."""
    try:
        from otto.config import ConfigManager
        raw = ConfigManager().get_or("user.directives", []) or []
    except Exception as e:  # pragma: no cover - config is optional
        logger.debug("config directives unavailable: %s", e)
        return []
    out: list[str] = []
    for d in raw:
        cleaned = " ".join(str(d).split())
        if len(cleaned) >= 5:
            out.append(cleaned[:300])
    return out


def load_directives() -> list[dict[str, Any]]:
    """Standing directives: config.toml first, then any still in the old store (deduplicated)."""
    directives: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text in config_directives():
        if text.lower() not in seen:
            seen.add(text.lower())
            directives.append({"directive": text, "source": "config", "category": "", "priority": "medium"})
    for entry in _read_lines(_directives_file()):
        d_text = str(entry.get("directive", "")).strip().lower()
        if d_text and d_text not in seen:
            seen.add(d_text)
            directives.append(entry)
    return directives


def _looks_generated(text: str) -> bool:
    """Otto's own explanations that an older build stored back as directives
    ("Identified as directly relevant to standing directive: …", "High severity ·
    Directive: … · Personal action item"). Not something a person wrote."""
    low = text.lower()
    return low.startswith("identified as") or "directive:" in low or " · " in text


def retire_stored_directives() -> list[str]:
    """Hand the old store's directives over to config.toml: returns their text and
    renames the file (``directives.jsonl.migrated``) so it is not read twice."""
    path = _directives_file()
    texts: list[str] = []
    for e in _read_lines(path):
        t = " ".join(str(e.get("directive", "")).split())
        if not t or t.lower() in {x.lower() for x in texts} or _looks_generated(t):
            continue
        texts.append(t)
    if path.exists():
        try:
            path.rename(path.with_suffix(".jsonl.migrated"))
        except OSError as e:
            logger.warning("could not retire %s: %s", path.name, e)
            return []
    return texts


def _write_directives(directives: list[dict[str, Any]]) -> None:
    _atomic_write_lines(_directives_file(), directives)


def save_directive(
    directive: str,
    source: str = "user",
    category: str = "",
    priority: str = "medium",
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Save a standing user directive permanently. Returns False if it already exists."""
    cleaned = " ".join(directive.split())
    if not cleaned or len(cleaned) < 5:
        return False

    existing = load_directives()
    cleaned_lower = cleaned.lower()
    for d in existing:
        if str(d.get("directive", "")).strip().lower() == cleaned_lower:
            return False

    entry = {
        "directive": cleaned,
        "source": source,
        "category": category,
        "priority": priority,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **(metadata or {}),
    }
    existing.append(entry)
    _write_directives(existing)
    logger.info("Saved standing directive: %s", cleaned)
    return True


def remove_directive(directive_or_index: str | int) -> bool:
    """Remove a directive by exact text (case-insensitive) or 1-based index."""
    existing = load_directives()
    if isinstance(directive_or_index, int):
        idx = directive_or_index - 1
        if 0 <= idx < len(existing):
            del existing[idx]
            _write_directives(existing)
            return True
        return False
    target = directive_or_index.strip().lower()
    remaining = [d for d in existing if str(d.get("directive", "")).strip().lower() != target]
    if len(remaining) == len(existing):
        return False
    _write_directives(remaining)
    return True


def detect_directives_in_text(text: str) -> list[str]:
    """
    Suggest candidate directives from free text.

    A *suggestion* helper; nothing is saved automatically — directives are
    the ``[user] directives`` list in config.toml.
    """
    if not text:
        return []

    lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
    triggers = (
        "make sure to", "always look at", "always check", "always review",
        "always flag", "never fix", "do not fix", "don't fix", "priority is",
        "ensure to", "remind me",
    )
    # Text Otto itself generates must never be re-ingested as a directive.
    system_prefixes = (
        "identified as", "directive:", "relates to:", "act on directive:",
        "flagged for attention", "recommendation to", "proposed discussion",
    )

    found: list[str] = []
    for line in lines:
        lower = line.lower()
        if any(lower.startswith(p) or p in lower for p in system_prefixes):
            continue
        if any(t in lower for t in triggers) and 15 <= len(line) <= 200:
            found.append(line)
    return found


def get_standing_directives_text() -> str:
    """Format all standing directives for LLM prompt context."""
    directives = load_directives()
    if not directives:
        return "(No standing user directives recorded)"
    lines = []
    for d in directives:
        text = d.get("directive", "")
        cat = d.get("category", "")
        lines.append(f"- {text}{f' [{cat}]' if cat else ''}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------

def save_conversations(
    conversations: list[Any],
    *,
    retention_hours: int = _DEFAULT_RETENTION_HOURS,
) -> int:
    """
    Append classified conversations to the history file.

    Old entries (beyond ``retention_hours``) are pruned; entries are
    deduplicated by thread id (last write wins). Returns the number of
    conversations saved.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=retention_hours)

    existing: list[dict[str, Any]] = []
    for entry in _read_lines(_history_file()):
        ts_str = entry.get("_saved_at", "")
        try:
            if ts_str and datetime.fromisoformat(ts_str) >= cutoff:
                existing.append(entry)
        except ValueError:
            continue

    new_entries: list[dict[str, Any]] = []
    for conv in conversations:
        try:
            new_entries.append(_conversation_to_entry(conv, now))
        except Exception as e:
            logger.debug("Failed to serialize conversation: %s", e)

    seen: dict[str, dict[str, Any]] = {}
    for entry in existing + new_entries:
        key = entry.get("thread_id") or entry.get("id", "")
        seen[key] = entry
    deduped = list(seen.values())

    _atomic_write_lines(_history_file(), deduped)
    logger.debug(
        "Saved %d conversations to history (%d total after pruning)",
        len(new_entries), len(deduped),
    )
    return len(new_entries)


def get_recent_context(
    hours: int = _DEFAULT_RETENTION_HOURS,
    max_entries: int = 20,
) -> str:
    """
    Load recent conversation summaries for LLM context injection.

    Example line:
        [2h ago] #security-alerts: HIGH 7.8 CVE in keploy — Security finding (urgency=0.7, ...)
    """
    path = _history_file()
    if not path.exists():
        return "(No previous conversation history available)"

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    entries: list[dict[str, Any]] = []
    for entry in _read_lines(path):
        ts_str = entry.get("_saved_at", "")
        try:
            if ts_str and datetime.fromisoformat(ts_str) >= cutoff:
                entries.append(entry)
        except ValueError:
            continue

    if not entries:
        return "(No recent conversations in history)"

    entries.sort(key=lambda e: e.get("_saved_at", ""), reverse=True)
    entries = entries[:max_entries]

    lines: list[str] = []
    for entry in entries:
        saved = entry.get("_saved_at", "")
        time_ago = _relative_time(saved, now) if saved else "?"
        subject = str(entry.get("subject", "Unknown"))[:60]
        summary = str(entry.get("summary", ""))[:80]
        ai = str(entry.get("ai_analysis", ""))[:80]
        topics = entry.get("topics", []) or []
        detail = summary or ai or ""
        topic_str = f" [{', '.join(topics[:3])}]" if topics else ""
        lines.append(
            f"[{time_ago}] {subject}: {detail}{topic_str} "
            f"(urgency={float(entry.get('urgency', 0.0)):.1f}, "
            f"importance={float(entry.get('importance', 0.0)):.1f}, "
            f"source={entry.get('source', '')}, domain={entry.get('domain', '')})"
        )
    return "\n".join(lines)


def _conversation_to_entry(conv: Any, now: datetime) -> dict[str, Any]:
    """Convert a Conversation dataclass (or dict) to a serializable dict."""
    if hasattr(conv, "__dataclass_fields__"):
        return {
            "id": getattr(conv, "id", ""),
            "thread_id": getattr(conv, "thread_id", ""),
            "source": str(getattr(conv, "source", "")),
            "subject": getattr(conv, "subject", ""),
            "summary": getattr(conv, "summary", ""),
            "domain": str(getattr(conv, "domain", "")),
            "urgency": getattr(conv, "urgency", 0.0),
            "importance": getattr(conv, "importance", 0.0),
            "opportunity_score": getattr(conv, "opportunity_score", 0.0),
            "opportunity_type": getattr(conv, "opportunity_type", ""),
            "opportunity_description": getattr(conv, "opportunity_description", ""),
            "relevance_explanation": getattr(conv, "relevance_explanation", ""),
            "ai_analysis": getattr(conv, "ai_analysis", ""),
            "topics": getattr(conv, "topics", []),
            "source_url": getattr(conv, "source_url", ""),
            "message_count": getattr(conv, "message_count", 0),
            "_saved_at": now.isoformat(),
        }
    if isinstance(conv, dict):
        entry = dict(conv)
        entry["_saved_at"] = now.isoformat()
        return entry
    raise TypeError(f"Cannot serialize {type(conv)} to history entry")


def _relative_time(iso_str: str, now: datetime) -> str:
    """Convert ISO timestamp to a relative time string."""
    try:
        dt = datetime.fromisoformat(iso_str)
        mins = int((now - dt).total_seconds() / 60)
        if mins < 1:
            return "just now"
        if mins < 60:
            return f"{mins}m ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"
    except (ValueError, TypeError):
        return "?"
