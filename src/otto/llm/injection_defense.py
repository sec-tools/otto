"""
Prompt injection defense for LLM inputs.

Sanitizes user-provided content before inclusion in LLM prompts to
prevent prompt injection attacks from malicious email/Slack content.
"""

import re
import logging

logger = logging.getLogger("otto.llm.injection_defense")

# Patterns commonly used in prompt injection attacks
INJECTION_PATTERNS = [
    r"(?i)ignore\s+(all\s+)?previous\s+instructions",
    r"(?i)ignore\s+(all\s+)?above",
    r"(?i)you\s+are\s+now\s+(an?\s+)?unrestricted",
    r"(?i)disregard\s+(all\s+)?(prior|previous)",
    r"(?i)system\s*:\s*you\s+are",
    r"(?i)forget\s+(everything|all|your\s+instructions)",
    r"(?i)new\s+instructions?\s*:",
    r"(?i)override\s+(your\s+)?instructions",
    r"(?i)act\s+as\s+(if\s+you\s+are|an?\s+)",
    r"(?i)pretend\s+(you\s+are|to\s+be)",
    r"(?i)\bdo\s+not\s+follow\s+(your|the)\s+rules\b",
    r"(?i)\bjailbreak\b",
    r"(?i)output\s+the\s+(full|entire|complete)\s+(text|content|email)",
    r"(?i)reveal\s+(your|the)\s+(system\s+)?prompt",
]

_compiled_patterns = [re.compile(p) for p in INJECTION_PATTERNS]


def sanitize_for_llm(content: str, max_length: int = 2000) -> str:
    """
    Sanitize user-provided content before including it in an LLM prompt.

    1. Truncates to max_length
    2. Strips known prompt injection patterns
    3. Escapes control-like sequences

    Args:
        content: Raw content from email/Slack/Jira.
        max_length: Maximum allowed character length.

    Returns:
        Sanitized content safe for LLM prompt inclusion.
    """
    if not content:
        return ""

    # Truncate
    sanitized = content[:max_length]

    # Strip injection patterns
    injection_found = False
    for pattern in _compiled_patterns:
        if pattern.search(sanitized):
            injection_found = True
            sanitized = pattern.sub("[REDACTED]", sanitized)

    if injection_found:
        logger.warning(
            "Prompt injection pattern detected and redacted",
            extra={"event_type": "prompt_injection_detected"},
        )

    # Collapse excessive whitespace (sometimes used to hide injections)
    sanitized = re.sub(r"\n{5,}", "\n\n", sanitized)
    sanitized = re.sub(r" {10,}", " ", sanitized)

    return sanitized


def is_suspicious(content: str) -> bool:
    """Quick check whether content contains potential injection patterns."""
    return any(p.search(content) for p in _compiled_patterns)
