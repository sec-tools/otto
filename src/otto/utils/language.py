from __future__ import annotations
"""
Language detection for multi-language support.

Detects the language of event content for proper LLM handling
and multi-language keyword matching.
"""

import logging

logger = logging.getLogger("otto.utils.language")

# Urgency keywords per language (top 10 languages)
URGENCY_KEYWORDS: dict[str, list[str]] = {
    "en": ["urgent", "asap", "deadline", "blocker", "critical", "emergency", "immediately", "p0", "p1"],
    "de": ["dringend", "sofort", "frist", "blockiert", "kritisch", "notfall"],
    "fr": ["urgent", "immédiatement", "délai", "critique", "bloqueur"],
    "es": ["urgente", "inmediatamente", "plazo", "crítico", "bloqueador"],
    "pt": ["urgente", "imediatamente", "prazo", "crítico", "bloqueador"],
    "ja": ["緊急", "至急", "期限", "ブロッカー", "重要"],
    "zh": ["紧急", "立即", "截止", "阻塞", "关键"],
    "ko": ["긴급", "즉시", "마감", "차단", "중요"],
    "it": ["urgente", "immediatamente", "scadenza", "critico", "bloccante"],
    "nl": ["dringend", "onmiddellijk", "deadline", "kritiek", "blokkerend"],
}


def detect_language(text: str) -> str:
    """
    Detect the language of text content.

    Args:
        text: Text to analyze.

    Returns:
        ISO 639-1 language code (e.g., 'en', 'de', 'ja').
        Returns 'en' as default if detection fails.
    """
    if not text or len(text.strip()) < 10:
        return "en"

    try:
        from langdetect import detect
        return detect(text)
    except ImportError:
        logger.debug("langdetect not installed, defaulting to 'en'")
        return "en"
    except Exception:
        return "en"


def has_urgency_keywords(text: str, language: str | None = None) -> bool:
    """
    Check if text contains urgency keywords.

    If language is provided, checks that language's keywords.
    Otherwise, checks all languages.

    Args:
        text: Text to scan.
        language: Optional ISO 639-1 code.

    Returns:
        True if urgency keywords found.
    """
    text_lower = text.lower()

    if language and language in URGENCY_KEYWORDS:
        return any(kw in text_lower for kw in URGENCY_KEYWORDS[language])

    # Check all languages
    for keywords in URGENCY_KEYWORDS.values():
        if any(kw in text_lower for kw in keywords):
            return True
    return False
