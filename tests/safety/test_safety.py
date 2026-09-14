"""
Tests for the safety layer — the most critical tests in the project.

These tests verify the four-layer read-only guarantee:
- WriteGuard blocks all unauthorized write HTTP methods
- ScopeValidator rejects tokens with write scopes
- Audit log maintains tamper-evident hash chain integrity
"""

import os
import sqlite3
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.safety.audit import AuditEntry, AuditEventType, AuditLog
from otto.safety.scope_validator import (
    FORBIDDEN_WRITE_SCOPES,
    REQUIRED_READ_SCOPES,
    ScopeValidator,
)
from otto.safety.write_guard import InstrumentedHttpClient, WriteAttemptBlocked, WriteGuard


# =============================================================================
# WriteGuard Tests
# =============================================================================


class TestWriteGuard:
    """Test WriteGuard HTTP interceptor — Layer 4 of the read-only guarantee."""

    def setup_method(self):
        self.audit_log = MagicMock(spec=AuditLog)
        self.inner_transport = AsyncMock()
        self.inner_transport.handle_async_request = AsyncMock(
            return_value=MagicMock(status_code=200)
        )
        self.guard = WriteGuard(
            inner=self.inner_transport,
            audit_log=self.audit_log,
            source_name="test_source",
            allowlist=["https://oauth2.googleapis.com/token"],
        )

    @pytest.mark.asyncio
    async def test_get_request_allowed(self):
        """GET requests must always pass through."""
        request = MagicMock()
        request.method = "GET"
        request.url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"

        await self.guard.handle_async_request(request)

        self.inner_transport.handle_async_request.assert_called_once_with(request)
        self.audit_log.append.assert_called_once()
        call_kwargs = self.audit_log.append.call_args
        assert call_kwargs.kwargs.get("blocked") is False or call_kwargs[1].get("blocked") is False

    @pytest.mark.asyncio
    async def test_head_request_allowed(self):
        """HEAD requests must always pass through."""
        request = MagicMock()
        request.method = "HEAD"
        request.url = "https://example.com/health"
        await self.guard.handle_async_request(request)
        self.inner_transport.handle_async_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_options_request_allowed(self):
        """OPTIONS requests must always pass through."""
        request = MagicMock()
        request.method = "OPTIONS"
        request.url = "https://example.com"
        await self.guard.handle_async_request(request)
        self.inner_transport.handle_async_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_blocked(self):
        """POST to non-allowlisted URL must be blocked."""
        request = MagicMock()
        request.method = "POST"
        request.url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

        with pytest.raises(WriteAttemptBlocked):
            await self.guard.handle_async_request(request)

        self.inner_transport.handle_async_request.assert_not_called()
        self.audit_log.append.assert_called_once()

    @pytest.mark.asyncio
    async def test_put_blocked(self):
        """PUT requests must be blocked."""
        request = MagicMock()
        request.method = "PUT"
        request.url = "https://api.example.com/resource/123"

        with pytest.raises(WriteAttemptBlocked):
            await self.guard.handle_async_request(request)

    @pytest.mark.asyncio
    async def test_patch_blocked(self):
        """PATCH requests must be blocked."""
        request = MagicMock()
        request.method = "PATCH"
        request.url = "https://api.example.com/resource/123"

        with pytest.raises(WriteAttemptBlocked):
            await self.guard.handle_async_request(request)

    @pytest.mark.asyncio
    async def test_delete_blocked(self):
        """DELETE requests must ALWAYS be blocked (no allowlist override)."""
        request = MagicMock()
        request.method = "DELETE"
        request.url = "https://api.example.com/resource/123"

        with pytest.raises(WriteAttemptBlocked):
            await self.guard.handle_async_request(request)

    @pytest.mark.asyncio
    async def test_allowlisted_post_allowed(self):
        """POST to allowlisted URL (e.g., OAuth token refresh) must pass."""
        request = MagicMock()
        request.method = "POST"
        request.url = "https://oauth2.googleapis.com/token"

        await self.guard.handle_async_request(request)
        self.inner_transport.handle_async_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_allowlist_prefix_matching(self):
        """Allowlist should match URL prefixes."""
        request = MagicMock()
        request.method = "POST"
        request.url = "https://oauth2.googleapis.com/token?refresh=true"

        await self.guard.handle_async_request(request)
        self.inner_transport.handle_async_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_blocked_request_logged_to_audit(self):
        """Blocked requests must be logged in the audit system."""
        request = MagicMock()
        request.method = "POST"
        request.url = "https://api.slack.com/api/chat.postMessage"

        with pytest.raises(WriteAttemptBlocked):
            await self.guard.handle_async_request(request)

        self.audit_log.append.assert_called_once()
        call_kwargs = self.audit_log.append.call_args[1]
        assert call_kwargs["blocked"] is True
        assert call_kwargs["event_type"] == AuditEventType.http_blocked

    @pytest.mark.asyncio
    async def test_case_insensitive_method(self):
        """HTTP method comparison should be case-insensitive."""
        request = MagicMock()
        request.method = "post"
        request.url = "https://api.example.com/send"

        with pytest.raises(WriteAttemptBlocked):
            await self.guard.handle_async_request(request)

    @pytest.mark.asyncio
    async def test_empty_allowlist_blocks_all_writes(self):
        """With empty allowlist, ALL write methods must be blocked."""
        guard = WriteGuard(
            inner=self.inner_transport,
            audit_log=self.audit_log,
            source_name="strict",
            allowlist=[],
        )
        for method in ["POST", "PUT", "PATCH", "DELETE"]:
            request = MagicMock()
            request.method = method
            request.url = "https://any.api.com/anything"
            with pytest.raises(WriteAttemptBlocked):
                await guard.handle_async_request(request)


class TestInstrumentedHttpClient:
    """Test the adapter-facing HTTP client wrapper."""

    def setup_method(self):
        self.mock_client = AsyncMock()

    @pytest.mark.asyncio
    async def test_get_delegates(self):
        """get() should delegate to underlying client."""
        client = InstrumentedHttpClient(self.mock_client)
        await client.get("https://example.com")
        self.mock_client.get.assert_called_once_with("https://example.com")

    @pytest.mark.asyncio
    async def test_post_delegates(self):
        """post() should delegate (WriteGuard on transport layer handles blocking)."""
        client = InstrumentedHttpClient(self.mock_client)
        await client.post("https://example.com", json={"key": "value"})
        self.mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_close(self):
        """close() should close the underlying client."""
        client = InstrumentedHttpClient(self.mock_client)
        await client.close()
        self.mock_client.aclose.assert_called_once()


# =============================================================================
# ScopeValidator Tests
# =============================================================================


class TestScopeValidator:
    """Test OAuth scope validation — Layer 1 of the read-only guarantee."""

    def test_gmail_valid_read_only_scopes(self):
        """Valid Gmail read-only scopes should pass."""
        result = ScopeValidator.validate_scopes(
            "gmail",
            {"https://www.googleapis.com/auth/gmail.readonly"},
        )
        assert result.is_valid is True
        assert len(result.missing_read_scopes) == 0
        assert len(result.present_write_scopes) == 0

    def test_gmail_rejects_send_scope(self):
        """Gmail send scope must be rejected as forbidden."""
        result = ScopeValidator.validate_scopes(
            "gmail",
            {
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send",
            },
        )
        assert result.is_valid is False
        assert "https://www.googleapis.com/auth/gmail.send" in result.present_write_scopes

    def test_gmail_rejects_modify_scope(self):
        """Gmail modify scope must be rejected."""
        result = ScopeValidator.validate_scopes(
            "gmail",
            {
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.modify",
            },
        )
        assert result.is_valid is False

    def test_gmail_rejects_full_mail_scope(self):
        """Full Gmail scope must be rejected."""
        result = ScopeValidator.validate_scopes(
            "gmail",
            {
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://mail.google.com/",
            },
        )
        assert result.is_valid is False

    def test_gmail_missing_read_scope(self):
        """Missing required read scope should fail validation."""
        result = ScopeValidator.validate_scopes("gmail", set())
        assert result.is_valid is False
        assert len(result.missing_read_scopes) > 0

    def test_slack_valid_scopes(self):
        """Valid Slack read-only scopes should pass."""
        result = ScopeValidator.validate_scopes(
            "slack",
            {"channels:history", "channels:read", "users:read", "team:read"},
        )
        assert result.is_valid is True

    def test_slack_rejects_chat_write(self):
        """Slack chat:write must be rejected."""
        result = ScopeValidator.validate_scopes(
            "slack",
            {"channels:read", "users:read", "team:read", "channels:history", "chat:write"},
        )
        assert result.is_valid is False
        assert "chat:write" in result.present_write_scopes

    def test_slack_rejects_all_write_scopes(self):
        """All Slack write scopes must be individually rejected."""
        write_scopes = FORBIDDEN_WRITE_SCOPES["slack"]
        for scope in write_scopes:
            result = ScopeValidator.validate_scopes(
                "slack",
                REQUIRED_READ_SCOPES["slack"] | {scope},
            )
            assert result.is_valid is False, f"Should reject scope: {scope}"
            assert scope in result.present_write_scopes

    def test_jira_valid_scopes(self):
        """Valid Jira read-only scopes should pass."""
        result = ScopeValidator.validate_scopes(
            "jira", {"read:jira-work", "read:jira-user"}
        )
        assert result.is_valid is True

    def test_jira_rejects_write(self):
        """Jira write scope must be rejected."""
        result = ScopeValidator.validate_scopes(
            "jira", {"read:jira-work", "read:jira-user", "write:jira-work"}
        )
        assert result.is_valid is False

    def test_calendar_valid_scopes(self):
        """Valid Calendar read-only scopes should pass."""
        result = ScopeValidator.validate_scopes(
            "calendar",
            {"https://www.googleapis.com/auth/calendar.readonly"},
        )
        assert result.is_valid is True

    def test_calendar_rejects_full_scope(self):
        """Full Calendar scope (which includes write) must be rejected."""
        result = ScopeValidator.validate_scopes(
            "calendar",
            {
                "https://www.googleapis.com/auth/calendar.readonly",
                "https://www.googleapis.com/auth/calendar",
            },
        )
        assert result.is_valid is False

    def test_unknown_source_denied(self):
        """Unknown source should be denied by default."""
        result = ScopeValidator.validate_scopes("unknown_service", {"some:scope"})
        assert result.is_valid is False

    def test_validate_all_sources(self):
        """validate_all should check all sources at once."""
        results = ScopeValidator.validate_all({
            "gmail": {"https://www.googleapis.com/auth/gmail.readonly"},
            "slack": {"channels:history", "channels:read", "users:read", "team:read"},
        })
        assert results["gmail"].is_valid is True
        assert results["slack"].is_valid is True

    def test_validate_all_catches_violations(self):
        """validate_all should catch write scope violations across sources."""
        results = ScopeValidator.validate_all({
            "gmail": {"https://www.googleapis.com/auth/gmail.readonly"},
            "slack": {"channels:read", "chat:write"},  # Violation
        })
        assert results["gmail"].is_valid is True
        assert results["slack"].is_valid is False

    def test_error_message_on_write_scope(self):
        """Error message should be clear about which write scopes are present."""
        result = ScopeValidator.validate_scopes(
            "gmail",
            {
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send",
            },
        )
        assert "CRITICAL" in result.error_message
        assert "gmail.send" in result.error_message

    def test_all_sources_have_scope_definitions(self):
        """Every source in REQUIRED must also exist in FORBIDDEN."""
        assert set(REQUIRED_READ_SCOPES.keys()) == set(FORBIDDEN_WRITE_SCOPES.keys())


# =============================================================================
# AuditLog Tests
# =============================================================================


class TestAuditLog:
    """Test append-only audit log with hash chain integrity."""

    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_audit.db")
        self.log = AuditLog(db_path=self.db_path)

    def test_append_entry(self):
        """Should append an entry and return it with a hash."""
        entry = self.log.append(
            event_type=AuditEventType.http_request,
            source="gmail",
            method="GET",
            url="https://gmail.googleapis.com/v1/messages",
            blocked=False,
        )
        assert entry.entry_hash != ""
        assert entry.previous_hash == "0" * 64  # Genesis hash for first entry

    def test_hash_chain_links(self):
        """Each entry's previous_hash should equal the prior entry's entry_hash."""
        e1 = self.log.append(
            event_type=AuditEventType.http_request, source="gmail",
            method="GET", url="https://a.com", blocked=False,
        )
        e2 = self.log.append(
            event_type=AuditEventType.http_blocked, source="slack",
            method="POST", url="https://b.com", blocked=True,
        )
        assert e2.previous_hash == e1.entry_hash

    def test_hash_chain_10_entries(self):
        """Hash chain should be valid across 10 entries."""
        entries = []
        for i in range(10):
            entry = self.log.append(
                event_type=AuditEventType.http_request,
                source=f"source_{i}",
                method="GET",
                url=f"https://api.example.com/{i}",
                blocked=False,
            )
            entries.append(entry)

        # Verify chain
        for i in range(1, len(entries)):
            assert entries[i].previous_hash == entries[i - 1].entry_hash

    def test_verify_integrity_clean(self):
        """Integrity check should pass on untampered log."""
        for i in range(5):
            self.log.append(
                event_type=AuditEventType.http_request, source="test",
                method="GET", url=f"https://api.com/{i}", blocked=False,
            )
        assert self.log.verify_integrity() is True

    def test_verify_integrity_detects_tampered_hash(self):
        """Integrity check should fail if an entry's hash is modified."""
        for i in range(5):
            self.log.append(
                event_type=AuditEventType.http_request, source="test",
                method="GET", url=f"https://api.com/{i}", blocked=False,
            )

        # Tamper with an entry's hash
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE audit_log SET entry_hash = 'tampered' WHERE id = 3"
            )

        assert self.log.verify_integrity() is False

    def test_verify_integrity_detects_deleted_entry(self):
        """Integrity check should fail if an entry is deleted."""
        for i in range(5):
            self.log.append(
                event_type=AuditEventType.http_request, source="test",
                method="GET", url=f"https://api.com/{i}", blocked=False,
            )

        # Delete middle entry
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM audit_log WHERE id = 3")

        assert self.log.verify_integrity() is False

    def test_verify_integrity_detects_modified_content(self):
        """Integrity check should fail if an entry's content is modified."""
        for i in range(3):
            self.log.append(
                event_type=AuditEventType.http_request, source="test",
                method="GET", url=f"https://api.com/{i}", blocked=False,
            )

        # Modify content but keep the hash
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE audit_log SET url = 'https://evil.com/hack' WHERE id = 2"
            )

        assert self.log.verify_integrity() is False

    def test_empty_log_passes_integrity(self):
        """Empty log should pass integrity check."""
        assert self.log.verify_integrity() is True

    def test_generate_report(self):
        """Report should include blocked entries."""
        self.log.append(
            event_type=AuditEventType.http_request, source="gmail",
            method="GET", url="https://a.com", blocked=False,
        )
        self.log.append(
            event_type=AuditEventType.http_blocked, source="slack",
            method="POST", url="https://b.com/send", blocked=True,
        )

        report = self.log.generate_report(days=1)
        assert "BLOCKED" in report
        assert "https://b.com/send" in report
        assert "Total blocked actions: 1" in report

    def test_report_empty_period(self):
        """Report with no entries should show zero counts."""
        report = self.log.generate_report(days=0)
        assert "Total events: 0" in report

    def test_entry_hash_is_deterministic(self):
        """Same input should produce the same hash."""
        entry1 = AuditEntry(
            timestamp="2026-01-01T00:00:00Z",
            event_type=AuditEventType.http_request,
            source="test",
            method="GET",
            url="https://example.com",
            blocked=False,
            previous_hash="abc123",
        )
        entry2 = AuditEntry(
            timestamp="2026-01-01T00:00:00Z",
            event_type=AuditEventType.http_request,
            source="test",
            method="GET",
            url="https://example.com",
            blocked=False,
            previous_hash="abc123",
        )
        assert entry1.calculate_hash() == entry2.calculate_hash()

    def test_different_inputs_produce_different_hashes(self):
        """Different inputs must produce different hashes."""
        entry1 = AuditEntry(
            timestamp="2026-01-01T00:00:00Z",
            event_type=AuditEventType.http_request,
            source="test", method="GET", url="https://a.com",
            blocked=False, previous_hash="abc",
        )
        entry2 = AuditEntry(
            timestamp="2026-01-01T00:00:00Z",
            event_type=AuditEventType.http_request,
            source="test", method="GET", url="https://b.com",
            blocked=False, previous_hash="abc",
        )
        assert entry1.calculate_hash() != entry2.calculate_hash()

    def test_blocked_flag_affects_hash(self):
        """Changing blocked flag must change the hash."""
        base = dict(
            timestamp="2026-01-01T00:00:00Z",
            event_type=AuditEventType.http_request,
            source="test", method="GET", url="https://a.com",
            previous_hash="abc",
        )
        e1 = AuditEntry(**base, blocked=False)
        e2 = AuditEntry(**base, blocked=True)
        assert e1.calculate_hash() != e2.calculate_hash()

    def test_audit_event_types_cover_all_scenarios(self):
        """All defined event types should be usable."""
        for event_type in AuditEventType:
            entry = self.log.append(
                event_type=event_type, source="test",
                method="GET", url="https://example.com", blocked=False,
            )
            assert entry.entry_hash != ""

    def test_persistence_across_instances(self):
        """Closing and reopening the audit log should preserve entries."""
        self.log.append(
            event_type=AuditEventType.http_request, source="test",
            method="GET", url="https://a.com", blocked=False,
        )

        # Create new instance with same DB
        log2 = AuditLog(db_path=self.db_path)
        assert log2.verify_integrity() is True

        # New entry should chain from the last one
        e2 = log2.append(
            event_type=AuditEventType.http_request, source="test",
            method="GET", url="https://b.com", blocked=False,
        )
        assert e2.previous_hash != "0" * 64  # Not genesis
