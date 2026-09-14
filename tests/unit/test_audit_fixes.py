from __future__ import annotations

"""
Unit tests verifying all fixes from the Deep Audit:
- C1: Calendar & Jira browser adapter persistent deduplication across polls
- C2: IngestionPipeline true LRU FIFO eviction
- C3: AdapterManager active polling loop execution
- C4: AuditLog thread safety and concurrent hash chain integrity
- H1: Browser adapter bounded seen_ids LRU eviction
- H2: LLM cache key separation by temperature and max_tokens + cost tracking
- H3: WriteGuard strict URL parsing against domain confusion / SSRF bypasses
- H5: Async retry utility with backoff and Retry-After header handling
"""

import asyncio
import os
import tempfile
import threading
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from otto.adapters.base import AdapterManager, ConnectionStatus, RawEvent
from otto.adapters.browser.calendar_browser import BrowserCalendarAdapter
from otto.adapters.browser.jira_browser import BrowserJiraAdapter
from otto.adapters.browser.reader import BrowserContentReader, BrowserTab, ExtractedContent
from otto.core.event_bus import EventBus
from otto.intelligence.ingestion import IngestionPipeline
from otto.llm.gateway import LLMGateway, LLMProvider, LLMResponse
from otto.safety.audit import AuditEventType, AuditLog
from otto.safety.write_guard import WriteAttemptBlocked, WriteGuard
from otto.storage.models import ConnectionState, HealthStatus, SourceType
from otto.utils.retry import retry_async


# ============================================================================
# C1 & H1: Calendar & Jira Browser Adapter Deduplication
# ============================================================================

class TestCalendarAndJiraDedup:
    """Verify that Calendar and Jira browser adapters maintain state across polls."""

    @pytest.mark.asyncio
    async def test_calendar_adapter_persists_seen_ids_across_polls(self):
        reader = BrowserContentReader()
        adapter = BrowserCalendarAdapter(reader, "user@example.com")
        adapter._connected = True

        event_text = "Standup|||2026-08-29 09:00:00|||2026-08-29 09:30:00|||Zoom|||Daily sync|||Alice,Bob"

        with patch.object(reader, "extract_calendar_events", new_callable=AsyncMock) as mock_extract, \
             patch.object(reader, "list_all_tabs", new_callable=AsyncMock, return_value=[]):
            mock_extract.return_value = event_text
            # First poll: returns event
            events1 = await adapter.poll(datetime(2020, 1, 1, tzinfo=timezone.utc))
            assert len(events1) == 1
            assert events1[0].title == "Standup"

            # Second poll: same event is deduplicated, returns 0 events
            events2 = await adapter.poll(datetime(2020, 1, 1, tzinfo=timezone.utc))
            assert len(events2) == 0

    @pytest.mark.asyncio
    async def test_jira_adapter_persists_seen_ids_across_polls(self):
        reader = BrowserContentReader()
        adapter = BrowserJiraAdapter(reader, "jira-workspace")
        adapter._connected = True

        jira_tab = BrowserTab(
            browser="chrome",
            title="OTTO-123 Bug Fix",
            url="https://company.atlassian.net/browse/OTTO-123",
            window_index=1,
            tab_index=1,
        )

        jira_content = "OTTO-123\nBug Fix Summary\nStatus: In Progress\nPriority: High\nAssignee: Developer"

        with patch.object(reader, "list_all_tabs", new_callable=AsyncMock) as mock_tabs, \
             patch.object(reader, "extract_tab_content", new_callable=AsyncMock) as mock_content:
            mock_tabs.return_value = [jira_tab]
            mock_content.return_value = ExtractedContent(
                source="chrome",
                url=jira_tab.url,
                title=jira_tab.title,
                text=jira_content,
            )

            # First poll: returns issue
            events1 = await adapter.poll(datetime(2020, 1, 1, tzinfo=timezone.utc))
            assert len(events1) == 1
            assert events1[0].source_id == "OTTO-123"

            # Second poll: same issue is deduplicated, returns 0
            events2 = await adapter.poll(datetime(2020, 1, 1, tzinfo=timezone.utc))
            assert len(events2) == 0

    def test_calendar_seen_ids_bounded_lru(self):
        reader = BrowserContentReader()
        adapter = BrowserCalendarAdapter(reader, "user@example.com")
        adapter._max_seen_ids = 5

        for i in range(10):
            adapter._mark_seen(f"id_{i}")

        assert len(adapter._seen_ids) == 5
        # Oldest (id_0..id_4) evicted, newest (id_5..id_9) present
        assert "id_0" not in adapter._seen_ids
        assert "id_9" in adapter._seen_ids


# ============================================================================
# C2: IngestionPipeline True FIFO LRU Eviction
# ============================================================================

class TestIngestionLRUEviction:
    """Verify IngestionPipeline evicts in true FIFO order."""

    @pytest.mark.asyncio
    async def test_fifo_eviction_under_capacity_limit(self):
        bus = EventBus()
        pipeline = IngestionPipeline(event_bus=bus)
        pipeline._max_seen_keys = 100

        # Ingest 120 unique events
        raw_events = [
            RawEvent(
                source=SourceType.EMAIL,
                source_id=f"msg_{i}",
                source_url="",
                timestamp=datetime.now(timezone.utc),
                title=f"Msg {i}",
                plain_text=f"Body {i}",
            )
            for i in range(120)
        ]

        normalized = await pipeline.ingest(raw_events)
        assert len(normalized) == 120

        # Keys should be capped
        assert len(pipeline._seen_keys) <= 100

        # Oldest keys should have been evicted, newest retained
        oldest_event = normalized[0]
        newest_event = normalized[-1]
        assert not pipeline._is_duplicate(oldest_event)  # Was evicted, so not duplicate
        assert pipeline._is_duplicate(newest_event)  # Was retained, so duplicate


# ============================================================================
# C3: AdapterManager Active Polling Loop
# ============================================================================

class TestAdapterManagerPolling:
    """Verify AdapterManager starts active background polling tasks."""

    @pytest.mark.asyncio
    async def test_adapter_manager_starts_polling_loop(self):
        ingested = []
        mock_pipeline = MagicMock()
        mock_pipeline.ingest = AsyncMock(side_effect=lambda evts: ingested.extend(evts))

        manager = AdapterManager(pipeline=mock_pipeline, poll_interval=0.05)

        mock_adapter = MagicMock()
        mock_adapter.name = "test_src"
        mock_adapter.connect = AsyncMock(return_value=ConnectionStatus(state=ConnectionState.HEALTHY))
        mock_adapter.disconnect = AsyncMock()
        mock_adapter.health_check = AsyncMock(return_value=HealthStatus.HEALTHY)

        test_event = RawEvent(
            source=SourceType.EMAIL,
            source_id="test_msg",
            source_url="",
            timestamp=datetime.now(timezone.utc),
            title="Poll test",
        )
        mock_adapter.poll = AsyncMock(return_value=[test_event])

        manager.register(mock_adapter)
        await manager.start()

        # Check that task was created
        assert "test_src" in manager._poll_tasks
        assert not manager._poll_tasks["test_src"].done()

        # Allow poll loop to execute
        await asyncio.sleep(0.12)

        assert mock_adapter.poll.called
        assert len(ingested) >= 1

        await manager.stop()
        assert len(manager._poll_tasks) == 0


# ============================================================================
# C4: AuditLog Concurrency & Hash Chain
# ============================================================================

class TestAuditLogConcurrency:
    """Verify AuditLog thread safety and hash chain integrity under concurrent appends."""

    def test_concurrent_appends_maintain_hash_chain_integrity(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            audit = AuditLog(db_path=db_path)

            def worker(worker_id: int):
                for i in range(20):
                    audit.append(
                        event_type=AuditEventType.http_request,
                        source=f"worker_{worker_id}",
                        method="GET",
                        url=f"https://api.example.com/item/{i}",
                        blocked=False,
                    )

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # Verify integrity across all 100 entries
            assert audit.verify_integrity() is True
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)


# ============================================================================
# H2: LLM Gateway Cache Key & Cost Tracking
# ============================================================================

class TestLLMGatewayCacheAndCost:
    """Verify cache keys account for temperature and max_tokens, and usage tracks cost."""

    def test_cache_keys_differ_by_temperature_and_max_tokens(self):
        gw = LLMGateway()
        k1 = gw._cache_key("gpt-4o", "sys", "user", temperature=0.0, max_tokens=1000)
        k2 = gw._cache_key("gpt-4o", "sys", "user", temperature=0.7, max_tokens=1000)
        k3 = gw._cache_key("gpt-4o", "sys", "user", temperature=0.0, max_tokens=500)

        assert k1 != k2
        assert k1 != k3
        assert k2 != k3

    @pytest.mark.asyncio
    async def test_cost_tracking_updated_on_completion(self):
        gw = LLMGateway()
        provider = LLMProvider("test", "test/cheap", "test/capable")
        gw.add_provider(provider)

        mock_resp = LLMResponse(
            content="Hello",
            model="test/cheap",
            input_tokens=1000,
            output_tokens=500,
            latency_ms=10.0,
        )

        with patch.object(gw, "_call_provider", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = mock_resp
            resp = await gw.complete(
                task="conversation_classify",
                system_prompt="sys",
                user_message="user",
            )
            assert resp is not None
            assert gw._usage.input_tokens == 1000
            assert gw._usage.output_tokens == 500
            assert gw._usage.total_cost_usd > 0.0


# ============================================================================
# H3: WriteGuard Strict URL Parsing Against Domain Confusion
# ============================================================================

class TestWriteGuardStrictUrls:
    """Verify WriteGuard blocks domain confusion and SSRF bypass attempts."""

    @pytest.mark.asyncio
    async def test_domain_confusion_url_blocked(self):
        audit = MagicMock(spec=AuditLog)
        transport = AsyncMock()
        guard = WriteGuard(
            inner=transport,
            audit_log=audit,
            source_name="google",
            allowlist=["https://oauth2.googleapis.com/token"],
        )

        # Attack URL with domain suffix
        evil_request = MagicMock()
        evil_request.method = "POST"
        evil_request.url = httpx.URL("https://oauth2.googleapis.com.evil.com/token")

        with pytest.raises(WriteAttemptBlocked):
            await guard.handle_async_request(evil_request)

        # Legitimate URL with query parameter allowed
        good_request = MagicMock()
        good_request.method = "POST"
        good_request.url = httpx.URL("https://oauth2.googleapis.com/token?client_id=123")

        await guard.handle_async_request(good_request)
        transport.handle_async_request.assert_called_once_with(good_request)


# ============================================================================
# H5: Async Retry Utility
# ============================================================================

class TestAsyncRetryUtility:
    """Verify retry_async handles backoff, rate limits, and Retry-After."""

    @pytest.mark.asyncio
    async def test_retries_on_transient_server_error(self):
        calls = 0

        async def unstable_op():
            nonlocal calls
            calls += 1
            if calls < 3:
                resp = MagicMock()
                resp.status_code = 503
                return resp
            resp = MagicMock()
            resp.status_code = 200
            return resp

        result = await retry_async(unstable_op, max_retries=3, base_delay=0.01)
        assert result.status_code == 200
        assert calls == 3

    @pytest.mark.asyncio
    async def test_retries_on_exception(self):
        calls = 0

        async def error_op():
            nonlocal calls
            calls += 1
            if calls < 2:
                raise httpx.ConnectError("Network dropped")
            return "success"

        result = await retry_async(error_op, max_retries=3, base_delay=0.01)
        assert result == "success"
        assert calls == 2
