"""
Tests for utility modules — content parser, PII redactor, language,
time helpers, credentials, and configuration.
"""

import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

from otto.utils.content_parser import (
    content_hash,
    html_to_text,
    strip_email_signature,
    truncate_for_llm,
)
from otto.utils.language import detect_language, has_urgency_keywords
from otto.utils.pii_redactor import contains_pii, redact_pii
from otto.utils.time import format_relative, hours_since, is_within_hours, now_utc


# =============================================================================
# Content Parser Tests
# =============================================================================


class TestHtmlToText:
    """Test HTML to text extraction."""

    def test_simple_html(self):
        result = html_to_text("<p>Hello World</p>")
        assert "Hello World" in result

    def test_strips_tags(self):
        result = html_to_text("<b>bold</b> and <i>italic</i>")
        assert "<b>" not in result
        assert "bold" in result
        assert "italic" in result

    def test_strips_script_tags(self):
        result = html_to_text("<p>Text</p><script>alert('xss')</script><p>More</p>")
        assert "alert" not in result
        assert "Text" in result
        assert "More" in result

    def test_strips_style_tags(self):
        result = html_to_text("<style>body{color:red}</style><p>Content</p>")
        assert "color" not in result
        assert "Content" in result

    def test_preserves_line_breaks(self):
        result = html_to_text("<p>Line 1</p><p>Line 2</p>")
        assert "Line 1" in result
        assert "Line 2" in result

    def test_decodes_entities(self):
        result = html_to_text("&amp; &lt; &gt; &quot;")
        assert "&" in result
        assert "<" in result

    def test_nested_html(self):
        result = html_to_text("<div><p><span>Deep text</span></p></div>")
        assert "Deep text" in result

    def test_empty_html(self):
        assert html_to_text("") == ""

    def test_none_safe(self):
        """Should handle None-like empty input."""
        assert html_to_text("") == ""

    def test_email_html(self):
        """Should handle typical email HTML."""
        html = """<html><body>
        <div dir="ltr">Hi team,<br><br>
        Please review the <b>Q3 report</b>.<br>
        <a href="http://example.com">Link</a><br><br>
        Thanks,<br>Alice</div></body></html>"""
        result = html_to_text(html)
        assert "Q3 report" in result
        assert "Alice" in result
        assert "<div>" not in result


class TestStripEmailSignature:
    """Test email signature stripping."""

    def test_standard_delimiter(self):
        text = "Main content here.\n\n-- \nAlice\nSr. Engineer"
        result = strip_email_signature(text)
        assert "Main content" in result
        assert "Sr. Engineer" not in result

    def test_sent_from_iphone(self):
        text = "Quick note.\n\nSent from my iPhone"
        result = strip_email_signature(text)
        assert "Quick note" in result
        assert "iPhone" not in result

    def test_outlook_signature(self):
        text = "Meeting at 3pm.\n\nGet Outlook for iOS"
        result = strip_email_signature(text)
        assert "Meeting at 3pm" in result
        assert "Outlook" not in result

    def test_disclaimer(self):
        text = "Important info.\n\nDISCLAIMER: This email is confidential."
        result = strip_email_signature(text)
        assert "Important info" in result
        assert "DISCLAIMER" not in result

    def test_no_signature(self):
        text = "Just a plain message."
        result = strip_email_signature(text)
        assert result == text

    def test_underscore_divider(self):
        text = "Content above.\n__________\nFooter below."
        result = strip_email_signature(text)
        assert "Content above" in result
        assert "Footer below" not in result


class TestContentHash:
    """Test content hashing for deduplication."""

    def test_deterministic(self):
        assert content_hash("hello") == content_hash("hello")

    def test_different_content(self):
        assert content_hash("hello") != content_hash("world")

    def test_returns_hex_string(self):
        result = content_hash("test")
        assert len(result) == 64  # SHA-256 hex
        assert all(c in "0123456789abcdef" for c in result)


class TestTruncateForLlm:
    """Test LLM-friendly text truncation."""

    def test_short_text_unchanged(self):
        text = "Short text."
        assert truncate_for_llm(text) == text

    def test_truncates_at_sentence_boundary(self):
        # Place a sentence boundary at ~80% of max_chars so it's above the 70% threshold
        text = ("x" * 75) + ". " + ("y" * 3000)
        result = truncate_for_llm(text, max_chars=100)
        # Should break at the period (position 76 > 100*0.7=70)
        assert result.endswith(".")
        assert len(result) <= 100

    def test_truncates_with_ellipsis_if_no_boundary(self):
        text = "a" * 3000  # No sentence boundaries
        result = truncate_for_llm(text, max_chars=100)
        assert result.endswith("...")

    def test_max_chars_respected(self):
        text = "word " * 1000
        result = truncate_for_llm(text, max_chars=200)
        assert len(result) <= 203  # +3 for "..."


# =============================================================================
# PII Redactor Tests
# =============================================================================


class TestPiiRedactor:
    """Test PII detection and redaction."""

    def test_redacts_email_address(self):
        text = "Contact alice@example.com for details."
        result = redact_pii(text)
        assert "[REDACTED]" in result
        assert "alice@example.com" not in result

    def test_redacts_us_phone(self):
        text = "Call me at 555-123-4567."
        result = redact_pii(text)
        assert "[REDACTED]" in result
        assert "555-123-4567" not in result

    def test_redacts_intl_phone(self):
        text = "Reach out at +44-20-7946-0958."
        result = redact_pii(text)
        assert "[REDACTED]" in result

    def test_redacts_ssn(self):
        text = "SSN: 123-45-6789"
        result = redact_pii(text)
        assert "[REDACTED]" in result
        assert "123-45-6789" not in result

    def test_redacts_credit_card(self):
        text = "Card: 4111-2222-3333-4444"
        result = redact_pii(text)
        assert "[REDACTED]" in result

    def test_redacts_ip_address(self):
        text = "Server at 192.168.1.100"
        result = redact_pii(text)
        assert "[REDACTED]" in result

    def test_redacts_street_address(self):
        text = "Office at 123 Main Street"
        result = redact_pii(text)
        assert "[REDACTED]" in result

    def test_preserves_clean_text(self):
        text = "The project deadline is next Friday."
        result = redact_pii(text)
        assert result == text

    def test_multiple_pii_in_one_text(self):
        text = "Email alice@test.example, call 555-123-4567, SSN 123-45-6789"
        result = redact_pii(text)
        assert result.count("[REDACTED]") >= 3

    def test_contains_pii_positive(self):
        assert contains_pii("Contact bob@example.com") is True

    def test_contains_pii_negative(self):
        assert contains_pii("The weather is nice today") is False


# =============================================================================
# Language Tests
# =============================================================================


class TestLanguageDetection:
    """Test language detection and urgency keyword matching."""

    def test_default_language(self):
        """Short/empty text should default to 'en'."""
        assert detect_language("") == "en"
        assert detect_language("Hi") == "en"

    def test_english_urgency_keywords(self):
        assert has_urgency_keywords("This is urgent!", "en") is True
        assert has_urgency_keywords("ASAP please", "en") is True
        assert has_urgency_keywords("P0 blocker", "en") is True
        assert has_urgency_keywords("deadline approaching", "en") is True

    def test_no_urgency_keywords(self):
        assert has_urgency_keywords("Let's discuss next week", "en") is False

    def test_german_urgency_keywords(self):
        assert has_urgency_keywords("Dies ist dringend!", "de") is True

    def test_french_urgency_keywords(self):
        assert has_urgency_keywords("C'est urgent", "fr") is True

    def test_japanese_urgency_keywords(self):
        assert has_urgency_keywords("これは緊急です", "ja") is True

    def test_cross_language_urgency_detection(self):
        """Without specifying language, should detect urgency in any language."""
        assert has_urgency_keywords("dringend bitte", None) is True

    def test_case_insensitive_keywords(self):
        assert has_urgency_keywords("URGENT!", "en") is True
        assert has_urgency_keywords("Critical issue", "en") is True


# =============================================================================
# Time Utility Tests
# =============================================================================


class TestTimeUtils:
    """Test timezone handling and relative formatting."""

    def test_now_utc_is_utc(self):
        result = now_utc()
        assert result.tzinfo is not None

    def test_hours_since(self):
        past = now_utc() - timedelta(hours=3)
        result = hours_since(past)
        assert 2.9 <= result <= 3.1

    def test_hours_since_future(self):
        """Future timestamp should return negative hours."""
        future = now_utc() + timedelta(hours=2)
        result = hours_since(future)
        assert result < 0

    def test_is_within_hours_true(self):
        recent = now_utc() - timedelta(hours=1)
        assert is_within_hours(recent, 2) is True

    def test_is_within_hours_false(self):
        old = now_utc() - timedelta(hours=5)
        assert is_within_hours(old, 2) is False

    def test_format_relative_just_now(self):
        now = now_utc()
        result = format_relative(now, now)
        assert result == "just now"

    def test_format_relative_minutes(self):
        now = now_utc()
        past = now - timedelta(minutes=5)
        result = format_relative(past, now)
        assert "5 minutes ago" == result

    def test_format_relative_hours(self):
        now = now_utc()
        past = now - timedelta(hours=3)
        result = format_relative(past, now)
        assert "3 hours ago" == result

    def test_format_relative_yesterday(self):
        now = now_utc()
        past = now - timedelta(hours=30)
        result = format_relative(past, now)
        assert result == "yesterday"

    def test_format_relative_days(self):
        now = now_utc()
        past = now - timedelta(days=5)
        result = format_relative(past, now)
        assert "5 days ago" == result

    def test_format_relative_1_minute(self):
        """Singular form for 1 minute."""
        now = now_utc()
        past = now - timedelta(minutes=1)
        result = format_relative(past, now)
        assert "1 minute ago" == result

    def test_format_relative_1_hour(self):
        """Singular form for 1 hour."""
        now = now_utc()
        past = now - timedelta(hours=1)
        result = format_relative(past, now)
        assert "1 hour ago" == result


# =============================================================================
# Config Tests
# =============================================================================


class TestConfig:
    """Test layered configuration system."""

    def test_default_values(self):
        from otto.config import ConfigManager
        manager = ConfigManager(config_path=Path("/nonexistent/config.toml"))
        assert manager.get("debug_mode") is False

    def test_layer_override(self):
        from otto.config import ConfigLayer, ConfigManager
        manager = ConfigManager(config_path=Path("/nonexistent/config.toml"))
        manager.set(ConfigLayer.CLI, "debug_mode", True)
        assert manager.get("debug_mode") is True

    def test_cli_overrides_file(self):
        from otto.config import ConfigLayer, ConfigManager
        manager = ConfigManager(config_path=Path("/nonexistent/config.toml"))
        manager.set(ConfigLayer.FILE, "debug_mode", False)
        manager.set(ConfigLayer.CLI, "debug_mode", True)
        assert manager.get("debug_mode") is True

    def test_learned_overrides_ui(self):
        from otto.config import ConfigLayer, ConfigManager
        manager = ConfigManager(config_path=Path("/nonexistent/config.toml"))
        manager.set(ConfigLayer.UI, "log_level", "WARNING")
        manager.set(ConfigLayer.LEARNED, "log_level", "DEBUG")
        assert manager.get("log_level") == "DEBUG"

    def test_missing_key_raises(self):
        from otto.config import ConfigManager
        manager = ConfigManager(config_path=Path("/nonexistent/config.toml"))
        with pytest.raises(KeyError):
            manager.get("nonexistent_key")

    def test_reset_layer(self):
        from otto.config import ConfigLayer, ConfigManager
        manager = ConfigManager(config_path=Path("/nonexistent/config.toml"))
        manager.set(ConfigLayer.CLI, "debug_mode", True)
        assert manager.get("debug_mode") is True
        manager.reset_layer(ConfigLayer.CLI)
        assert manager.get("debug_mode") is False  # Falls back to default

    def test_get_effective(self):
        from otto.config import ConfigManager
        manager = ConfigManager(config_path=Path("/nonexistent/config.toml"))
        effective = manager.get_effective()
        assert "debug_mode" in effective
        assert effective["debug_mode"]["source"] == "DEFAULTS"

    def test_file_config_loading(self):
        """Should load from a TOML config file."""
        from otto.config import ConfigManager
        tmp = tempfile.NamedTemporaryFile(suffix=".toml", mode="w", delete=False)
        tmp.write('[notifications]\nmax_per_hour = 10\n')
        tmp.close()

        manager = ConfigManager(config_path=Path(tmp.name))
        assert manager.get("notifications.max_per_hour") == 10

    def test_invalid_config_file_handled(self):
        """Invalid config file should not crash, just warn."""
        from otto.config import ConfigManager
        tmp = tempfile.NamedTemporaryFile(suffix=".toml", mode="w", delete=False)
        tmp.write("this is not valid toml {{{")
        tmp.close()

        # Should not raise
        manager = ConfigManager(config_path=Path(tmp.name))
        assert manager.get("debug_mode") is False
