"""
PII redaction for LLM calls.

Strips personally identifiable information from content before
sending to LLM APIs. Default-on; user can opt into full context per source.
"""

import re
import logging

logger = logging.getLogger("otto.utils.pii_redactor")

# Patterns to redact
_PATTERNS = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    "phone_us": re.compile(r"\b(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "phone_intl": re.compile(r"\+\d{1,3}[-.\s]?\d{1,14}"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "credit_card": re.compile(
        r"\b(?:\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}|\d{4}[-\s]?\d{6}[-\s]?\d{5})\b"
    ),
    "ip_address": re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    "street_address": re.compile(
        r"\b\d{1,5}\s+[A-Za-z]+\s+(Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct)\b",
        re.IGNORECASE,
    ),
}

_REPLACEMENT = "[REDACTED]"


def redact_pii(text: str) -> str:
    """
    Strip PII patterns from text.

    Replaces email addresses, phone numbers, SSNs, credit card numbers,
    IP addresses, and street addresses with [REDACTED].

    Args:
        text: Raw text content.

    Returns:
        Text with PII patterns replaced.
    """
    result = text
    redacted_count = 0
    for name, pattern in _PATTERNS.items():
        new_result, count = pattern.subn(_REPLACEMENT, result)
        if count > 0:
            redacted_count += count
            result = new_result

    if redacted_count > 0:
        logger.debug(
            "Redacted %d PII patterns",
            redacted_count,
            extra={"event_type": "pii_redaction"},
        )

    return result


def contains_pii(text: str) -> bool:
    """Quick check whether text contains PII patterns."""
    return any(p.search(text) for p in _PATTERNS.values())
