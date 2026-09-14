"""
Post-processing write-intent detector for LLM output.

Scans LLM responses for content that looks like draft messages,
replies, or sendable content. Quarantines and flags for audit.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass

logger = logging.getLogger("otto.llm.write_detector")

# Patterns suggesting the LLM produced sendable content
WRITE_INTENT_PATTERNS = [
    r"(?i)^(hi|hey|hello|dear)\s+\w+",          # Greeting patterns
    r"(?i)^subject\s*:",                          # Email subject lines
    r"(?i)(best\s+regards|sincerely|thanks|cheers),?\s*$",  # Email sign-offs
    r"(?i)i('d| would)\s+suggest\s+(reply|respond|send)ing",
    r"(?i)here'?s?\s+(a\s+)?(draft|template|response|reply)",
    r"(?i)you\s+(could|should|might)\s+(reply|respond|send|post|write)",
    r"(?i)copy\s+and\s+paste\s+this",
    r"(?i)forward\s+this\s+to",
    r"(?i)send\s+this\s+(to|email|message)",
]

_compiled = [re.compile(p, re.MULTILINE) for p in WRITE_INTENT_PATTERNS]


@dataclass
class WriteIntentResult:
    """Result of write-intent scanning."""
    has_write_intent: bool
    matched_patterns: list[str]
    content: str


def scan_for_write_intent(llm_output: str) -> WriteIntentResult:
    """
    Scan LLM output for write-intent patterns.

    If detected, the output should be quarantined and the item
    classified using local heuristics instead.

    Returns:
        WriteIntentResult with detection status and matched patterns.
    """
    matched = []
    for pattern in _compiled:
        match = pattern.search(llm_output)
        if match:
            matched.append(match.group())

    if matched:
        logger.warning(
            "Write intent detected in LLM output: %s",
            matched,
            extra={"event_type": "write_intent_detected"},
        )

    return WriteIntentResult(
        has_write_intent=len(matched) > 0,
        matched_patterns=matched,
        content=llm_output,
    )
