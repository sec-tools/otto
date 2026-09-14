"""
HTTP Request Interceptor and Write Guard.

This module implements the core enforcement mechanism for the read-only guarantee
at the network boundary. All outbound HTTP traffic must pass through the WriteGuard
transport, which inspects the HTTP method and URL. Any unsafe method (POST, PUT, 
PATCH, DELETE) is blocked unless explicitly allowlisted (e.g., for OAuth token refresh
or specific read-only GraphQL/Search APIs that use POST).
"""
from __future__ import annotations

import httpx
import logging
from typing import List
from urllib.parse import urlparse

from otto.safety.audit import AuditLog, AuditEventType

logger = logging.getLogger(__name__)


class WriteAttemptBlocked(Exception):
    """Raised when an outbound HTTP request is blocked by the WriteGuard."""
    pass


class WriteGuard(httpx.AsyncBaseTransport):
    """
    An httpx Transport that intercepts all outbound requests and blocks 
    unauthorized write attempts based on HTTP methods and an allowlist.
    """

    # Methods that are generally considered safe (read-only)
    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    def __init__(self, inner: httpx.AsyncBaseTransport, audit_log: AuditLog, source_name: str, allowlist: List[str]):
        """
        :param inner: The underlying transport to use for allowed requests.
        :param audit_log: The audit log instance to record all actions.
        :param source_name: The name of the source (e.g., 'gmail', 'slack').
        :param allowlist: A list of exact URL strings or prefixes that are permitted to receive unsafe methods.
        """
        self.inner = inner
        self.audit_log = audit_log
        self.source_name = source_name
        self.allowlist = allowlist

    def _is_url_allowlisted(self, url_str: str) -> bool:
        """
        Check if a URL is allowlisted using strict scheme, host, and path prefix comparison.
        Prevents domain confusion (e.g. prefix matching api.com against api.com.evil.com).
        """
        try:
            req_parsed = urlparse(url_str)
        except Exception:
            return False

        for allowed in self.allowlist:
            try:
                allow_parsed = urlparse(allowed)
                # Scheme and hostname must match exactly
                if req_parsed.scheme != allow_parsed.scheme:
                    continue
                if req_parsed.netloc != allow_parsed.netloc:
                    continue
                # Path must either match exactly or be a valid sub-path prefix
                req_path = req_parsed.path or "/"
                allow_path = allow_parsed.path or "/"
                if req_path == allow_path:
                    return True
                if allow_path.endswith("/") and req_path.startswith(allow_path):
                    return True
                if req_path.startswith(allow_path + "/"):
                    return True
            except Exception:
                continue
        return False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        url_str = str(request.url)
        
        is_safe_method = method in self.SAFE_METHODS
        is_allowlisted = self._is_url_allowlisted(url_str)

        if not is_safe_method and not is_allowlisted:
            # Block the request
            self.audit_log.append(
                event_type=AuditEventType.http_blocked,
                source=self.source_name,
                method=method,
                url=url_str,
                blocked=True
            )
            # Emit an event or log extensively
            logger.error(f"WriteGuard BLOCKED {method} request to {url_str} for source {self.source_name}")
            
            raise WriteAttemptBlocked(
                f"Blocked attempt to use unsafe HTTP method '{method}' on non-allowlisted URL: {url_str}"
            )

        # Allow the request
        self.audit_log.append(
            event_type=AuditEventType.http_request,
            source=self.source_name,
            method=method,
            url=url_str,
            blocked=False
        )
        
        return await self.inner.handle_async_request(request)


def create_guarded_client(source_name: str, audit_log: AuditLog, allowlist: List[str], **client_kwargs) -> httpx.AsyncClient:
    """
    Factory function to create an httpx.AsyncClient protected by the WriteGuard.
    """
    # Create a default transport if none provided
    inner_transport = client_kwargs.pop("transport", httpx.AsyncHTTPTransport())
    
    # Wrap it with our WriteGuard
    guarded_transport = WriteGuard(
        inner=inner_transport,
        audit_log=audit_log,
        source_name=source_name,
        allowlist=allowlist
    )
    
    # Return the client using the guarded transport
    return httpx.AsyncClient(transport=guarded_transport, **client_kwargs)


class InstrumentedHttpClient:
    """
    A wrapper around the guarded httpx.AsyncClient that adapters should use.
    Provides standard HTTP methods but guarantees all traffic goes through WriteGuard.
    """
    
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        
    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.client.get(url, **kwargs)
        
    async def post(self, url: str, **kwargs) -> httpx.Response:
        """
        POST is only allowed if the URL is in the WriteGuard allowlist.
        Otherwise, WriteAttemptBlocked will be raised.
        """
        return await self.client.post(url, **kwargs)
        
    async def close(self) -> None:
        await self.client.aclose()
