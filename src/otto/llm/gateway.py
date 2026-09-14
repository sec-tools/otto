from __future__ import annotations
"""
LLM gateway with automatic failover chain.

Priority order:
  1. User's primary provider (OpenAI / Anthropic / etc.)
  2. User's secondary provider (if configured)
  3. Local Ollama (always available if installed)
  4. None — fall back to local heuristics only

Failover is automatic and instant. The UI shows the current tier.
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
import sys
if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum
    class StrEnum(str, Enum):
        pass
from typing import Any

logger = logging.getLogger("otto.llm.gateway")


class LLMTier(StrEnum):
    """Active LLM tier indicator for UI status."""
    FULL = "full"          # Primary provider active
    BACKUP = "backup"      # Secondary provider active
    LOCAL = "local"        # Ollama active
    HEURISTIC = "heuristic"  # No LLM — heuristics only


class ModelTier(StrEnum):
    """Task-level model tier assignment."""
    CHEAP = "cheap"        # GPT-4o-mini, Haiku, Llama 3.1 8B
    CAPABLE = "capable"    # GPT-4o, Sonnet, Llama 3.1 70B
    EMBEDDING = "embedding"


@dataclass
class LLMResponse:
    """Response from an LLM call."""
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cached: bool = False


class LLMHTTPError(RuntimeError):
    """Non-200 from a provider, with the status kept so callers can classify it."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body or ""

    @property
    def is_auth(self) -> bool:
        """Key rejected / account problem — will not fix itself within minutes."""
        low = self.body.lower()
        return self.status in (401, 403) or "incorrect api key" in low or "invalid api key" in low

    @property
    def is_quota(self) -> bool:
        low = self.body.lower()
        return self.status == 429 or "quota" in low or "billing" in low or "insufficient" in low

    @property
    def is_model(self) -> bool:
        """The *model id* is the problem (retired, renamed, no endpoints) — try another."""
        low = self.body.lower()
        return self.status == 404 or (self.status == 400 and "model" in low) or "no endpoints found" in low


@dataclass
class LLMProvider:
    """A configured LLM provider."""
    name: str
    model_cheap: str
    model_capable: str
    is_local: bool = False
    consecutive_failures: int = 0
    last_failure: float = 0.0
    # Alternatives per tier, tried in order when a model id stops existing.
    model_options: dict = field(default_factory=dict)
    # Long cool-down for problems that need a human (bad key, billing).
    disabled_until: float = 0.0
    disabled_reason: str = ""

    @property
    def is_healthy(self) -> bool:
        """Healthy unless disabled, or failed 3+ times in the last 5 minutes."""
        if self.disabled_until and time.time() < self.disabled_until:
            return False
        if self.consecutive_failures < 3:
            return True
        # After 3 failures, wait 5 minutes before retrying
        return (time.time() - self.last_failure) > 300

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.disabled_until = 0.0
        self.disabled_reason = ""

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.last_failure = time.time()

    def disable(self, seconds: float, reason: str) -> None:
        self.disabled_until = time.time() + seconds
        self.disabled_reason = reason
        self.last_failure = time.time()

    def model_for(self, tier: "ModelTier") -> str:
        return self.model_capable if tier == ModelTier.CAPABLE else self.model_cheap

    def rotate_model(self, tier: "ModelTier", failed: str) -> str | None:
        """Retire *failed* for this tier and return the next candidate (or None)."""
        options = list(self.model_options.get(tier.value, []))
        if failed in options:
            options.remove(failed)
        self.model_options[tier.value] = options
        nxt = options[0] if options else None
        if nxt:
            if tier == ModelTier.CAPABLE:
                self.model_capable = nxt
            else:
                self.model_cheap = nxt
        return nxt

    @property
    def status(self) -> str:
        """Human-readable health: ``ok`` or the reason it is not being used."""
        now = time.time()
        if self.disabled_until and now < self.disabled_until:
            return self.disabled_reason or "disabled"
        if self.consecutive_failures >= 3 and (now - self.last_failure) <= 300:
            return f"cooling down after {self.consecutive_failures} failures"
        return "ok"


@dataclass
class TokenUsage:
    """Daily token usage tracker."""
    date: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0
    call_count: int = 0


# A rejected key or a billing problem is parked this long before retrying.
AUTH_FAILURE_COOLDOWN = 6 * 3600


def _key_fingerprint(provider: "LLMProvider") -> str:
    """Short, non-reversible id for the provider's key — a replaced key gets a clean slate."""
    key = getattr(provider, "api_key", "") or ""
    return hashlib.sha256(f"{provider.name}:{key}".encode("utf-8")).hexdigest()[:16]


def _provider_health_file():
    from otto import paths
    return paths.data_dir() / "provider_health.json"


def load_provider_health() -> dict[str, dict[str, Any]]:
    """``{fingerprint: {"name", "disabled_until", "reason"}}`` for parked providers."""
    try:
        with open(_provider_health_file(), "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def clear_provider_health() -> None:
    """Forget every parked provider (an explicit `otto restart` means "try again")."""
    try:
        _provider_health_file().unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.debug("Could not clear provider health: %s", e)


def save_provider_health(entries: dict[str, dict[str, Any]]) -> None:
    import os
    path = _provider_health_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f)
        os.replace(tmp, path)
    except OSError as e:
        logger.debug("Could not persist provider health: %s", e)


class LLMGateway:
    """
    LLM abstraction with automatic failover and token budget enforcement.

    Features:
    - Failover chain across providers
    - Per-task model tier assignment
    - Daily token budget enforcement
    - Response caching
    - Auto-promote back when primary recovers
    """

    # Per-task model tier assignments (NEVER implicit)
    TASK_TIERS: dict[str, ModelTier] = {
        "conversation_classify": ModelTier.CHEAP,
        "conversation_summarize": ModelTier.CHEAP,
        "action_extract": ModelTier.CHEAP,
        "relevance_explain": ModelTier.CHEAP,
        "briefing_generate": ModelTier.CAPABLE,
        "briefing_synthesize": ModelTier.CAPABLE,   # one call per changed briefing: the "what's going on" digest
        "opportunity_detect": ModelTier.CAPABLE,
    }

    def __init__(
        self,
        daily_token_limit: int = 500_000,
        daily_cost_limit_usd: float = 2.00,
    ) -> None:
        self._providers: list[LLMProvider] = []
        self._daily_token_limit = daily_token_limit
        self._daily_cost_limit = daily_cost_limit_usd
        self._usage = TokenUsage()
        self._cache: dict[str, LLMResponse] = {}
        self._cache_max_size = 500
        self._lock: asyncio.Lock | None = None
        self._in_flight_count = 0
        self._current_provider: LLMProvider | None = None
        self._failed_calls = 0
        self._ok_calls = 0
        self._last_success_provider = ""

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def name(self) -> str:
        return "llm_gateway"

    @property
    def active_tier(self) -> LLMTier:
        """Current active tier for UI status display."""
        for i, provider in enumerate(self._providers):
            if provider.is_healthy:
                if i == 0:
                    return LLMTier.FULL
                elif provider.is_local:
                    return LLMTier.LOCAL
                else:
                    return LLMTier.BACKUP
        return LLMTier.HEURISTIC

    def add_provider(self, provider: LLMProvider) -> None:
        """Add a provider to the failover chain (order matters).

        A provider parked in an earlier engine session (rejected key, billing)
        stays parked: the cool-down survives restarts, so `otto status` and
        `otto status` are right immediately and no refresh pays for a call
        that is known to fail. Replacing the key (new fingerprint) clears it.
        """
        self._providers.append(provider)
        logger.info("LLM provider added: %s (local=%s)", provider.name, provider.is_local)
        if provider.is_local:
            return
        saved = load_provider_health().get(_key_fingerprint(provider))
        if saved:
            until = float(saved.get("disabled_until") or 0)
            if until > time.time():
                provider.disabled_until = until
                provider.disabled_reason = str(saved.get("reason") or "disabled")
                logger.warning(
                    "Provider %s still parked (%s) for %d min — fix the key or account, then `otto restart` "
                    "to retry now.", provider.name, provider.disabled_reason, int((until - time.time()) // 60),
                )

    def _remember_provider_health(self) -> None:
        """Persist which providers are parked (and why) for the next engine start."""
        now = time.time()
        entries = {
            _key_fingerprint(p): {"name": p.name, "disabled_until": p.disabled_until, "reason": p.disabled_reason}
            for p in self._providers if not p.is_local and p.disabled_until > now
        }
        save_provider_health(entries)

    def _get_model_for_task(self, provider: LLMProvider, task: str) -> str:
        """Resolve the model name for a given task and provider."""
        tier = self.TASK_TIERS.get(task, ModelTier.CHEAP)
        if tier == ModelTier.CAPABLE:
            return provider.model_capable
        return provider.model_cheap

    def _cache_key(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.0,
        max_tokens: int = 1000,
    ) -> str:
        """Generate a cache key from input parameters."""
        data = f"{model}:{system_prompt}:{user_message}:{temperature}:{max_tokens}"
        return hashlib.sha256(data.encode()).hexdigest()

    def _check_budget(self, estimated_tokens: int) -> bool:
        """Within today's token *and* cost budget? False means: heuristics only until tomorrow."""
        today = time.strftime("%Y-%m-%d")
        if self._usage.date != today:
            self._usage = TokenUsage(date=today)
        within_tokens = (
            self._usage.input_tokens + self._usage.output_tokens + estimated_tokens
            < self._daily_token_limit
        )
        within_cost = self._usage.total_cost_usd < self._daily_cost_limit
        return within_tokens and within_cost

    async def complete(
        self,
        task: str,
        system_prompt: str,
        user_message: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1000,
        use_cache: bool = True,
    ) -> LLMResponse | None:
        """
        Send a completion request with automatic failover.

        Args:
            task: Task identifier (must be in TASK_TIERS).
            system_prompt: System prompt text.
            user_message: User message text.
            temperature: Sampling temperature.
            max_tokens: Maximum output tokens.
            use_cache: Whether to use response caching.

        Returns:
            LLMResponse if successful, None if all providers failed and budget exceeded.
        """
        async with self._ensure_lock():
            if not self._check_budget(max_tokens):
                logger.warning("Daily token budget exceeded. Falling back to heuristics.")
                return None
            self._in_flight_count += 1

        try:
            # Try cache first
            # Try chat-capable providers first (Ollama, OpenRouter, etc.),
            # then fall back to Devin session API
            sorted_providers = sorted(
                self._providers,
                key=lambda p: 1 if getattr(p, "is_devin", False) else 0,
            )

            for provider in sorted_providers:
                if not provider.is_healthy:
                    continue

                tier = self.TASK_TIERS.get(task, ModelTier.CHEAP)
                model = self._get_model_for_task(provider, task)

                if use_cache:
                    cache_key = self._cache_key(
                        model, system_prompt, user_message, temperature, max_tokens
                    )
                    async with self._ensure_lock():
                        if cache_key in self._cache:
                            logger.debug("Cache hit for task %s", task)
                            return self._cache[cache_key]

                # Attempt the call; a retired model id is retried on the same
                # provider with the next candidate, anything else moves on.
                try:
                    self._current_provider = provider
                    start = time.monotonic()
                    attempts = 0
                    while True:
                        attempts += 1
                        try:
                            response = await self._call_provider(
                                provider=provider,
                                model=model,
                                system_prompt=system_prompt,
                                user_message=user_message,
                                temperature=temperature,
                                max_tokens=max_tokens,
                            )
                            break
                        except LLMHTTPError as http_err:
                            if http_err.is_model and attempts < 4:
                                nxt = provider.rotate_model(tier, model)
                                if nxt:
                                    logger.warning(
                                        "Provider %s: model %s unavailable (%s) — switching to %s",
                                        provider.name, model, http_err.body[:80], nxt,
                                    )
                                    model = nxt
                                    continue
                            raise
                    latency = (time.monotonic() - start) * 1000
                    response.latency_ms = latency

                    was_parked = bool(provider.disabled_until)
                    provider.record_success()
                    if was_parked:
                        self._remember_provider_health()
                    self._ok_calls += 1
                    self._last_success_provider = provider.name

                    # Update usage and cost
                    cost = (response.input_tokens * 0.0000015) + (response.output_tokens * 0.000006)
                    async with self._ensure_lock():
                        self._usage.input_tokens += response.input_tokens
                        self._usage.output_tokens += response.output_tokens
                        self._usage.total_cost_usd += cost
                        self._usage.call_count += 1

                        # Cache the response
                        if use_cache and len(self._cache) < self._cache_max_size:
                            self._cache[cache_key] = response

                    return response

                except LLMHTTPError as e:
                    if e.is_auth or e.is_quota:
                        # Needs a human: a rejected key or an empty account does
                        # not recover in the next minute, so stop paying the
                        # latency on every refresh and say so once.
                        reason = (
                            f"API key rejected (HTTP {e.status})" if e.is_auth and not e.is_quota
                            else f"quota / billing problem (HTTP {e.status})"
                        )
                        already = bool(provider.disabled_until and time.time() < provider.disabled_until)
                        provider.disable(AUTH_FAILURE_COOLDOWN, reason)
                        if not already:      # concurrent calls all see the same 401 — say it once
                            logger.warning(
                                "Provider %s disabled for %d min: %s — check `otto key list` / the account, "
                                "then `otto restart`.", provider.name, AUTH_FAILURE_COOLDOWN // 60, reason,
                            )
                            self._remember_provider_health()
                        continue
                    provider.record_failure()
                    logger.warning(
                        "Provider %s failed for task %s: [%s] %s. Trying next.",
                        provider.name, task, type(e).__name__, e,
                    )
                    continue
                except Exception as e:
                    provider.record_failure()
                    logger.warning(
                        "Provider %s failed for task %s: [%s] %s. Trying next.",
                        provider.name, task, type(e).__name__, e,
                    )
                    continue

            self._failed_calls += 1
            if self._failed_calls in (1, 10, 100) or self._failed_calls % 500 == 0:
                logger.error("All LLM providers failed for task %s (%s)", task, self.status_line())
            else:
                logger.debug("All LLM providers failed for task %s", task)
            return None
        finally:
            async with self._ensure_lock():
                self._in_flight_count = max(0, self._in_flight_count - 1)

    async def _call_provider(
        self,
        provider: LLMProvider,
        model: str,
        system_prompt: str,
        user_message: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        """Route to the appropriate API based on provider type."""
        is_devin = getattr(provider, "is_devin", False)
        if is_devin:
            return await self._call_devin(provider, system_prompt, user_message)
        else:
            return await self._call_openai_compatible(
                provider, model, system_prompt, user_message,
                temperature, max_tokens,
            )

    async def _call_openai_compatible(
        self,
        provider: LLMProvider,
        model: str,
        system_prompt: str,
        user_message: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        """Call an OpenAI-compatible endpoint (OpenRouter, OpenAI, etc.)."""
        import httpx

        base_url = getattr(provider, "base_url", "https://openrouter.ai/api/v1")
        api_key = getattr(provider, "api_key", "")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://otto-assistant.local",
            "X-Title": "Otto AI Assistant",
        }

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # Local providers (Ollama) need more time — model loading + inference
        # phi3:mini can take 120-160s for complex classification prompts
        timeout = 180.0 if getattr(provider, "is_local", False) else 30.0

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
            )

        if resp.status_code != 200:
            raise LLMHTTPError(resp.status_code, resp.text)

        data = resp.json()

        choices = data.get("choices")
        if not choices:
            error_msg = data.get("error", {}).get("message", "No choices returned from LLM provider")
            raise RuntimeError(f"LLM provider error: {error_msg}")
        choice = choices[0]
        usage = data.get("usage", {})

        return LLMResponse(
            content=choice.get("message", {}).get("content") or "",
            model=data.get("model", model),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            latency_ms=0,
        )

    async def _call_devin(
        self,
        provider: LLMProvider,
        system_prompt: str,
        user_message: str,
    ) -> LLMResponse:
        """
        Use Devin's session API as a completions equivalent.

        Creates a lightweight session, polls until Devin responds, then
        extracts the response from session messages. Devin responds in
        seconds for simple classification prompts.

        API: POST /v1/sessions → GET /v1/session/{id} (messages inline)
        Status flow: running → blocked (= Devin replied, waiting for input)
        """
        import httpx

        base_url = getattr(provider, "base_url", "https://api.devin.ai/v1")
        api_key = getattr(provider, "api_key", "")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # Combine system + user prompt for Devin
        prompt = f"{system_prompt}\n\n---\n\n{user_message}"

        # Create session with minimal ACU budget
        async with httpx.AsyncClient(timeout=30.0) as client:
            create_resp = await client.post(
                f"{base_url}/sessions",
                headers=headers,
                json={
                    "prompt": prompt,
                    "title": "Otto classify",
                    "max_acu_limit": 1,
                },
            )

            if create_resp.status_code != 200:
                # Keeps the status so a 401/403 (bad key, out of quota) parks
                # the provider instead of costing a session attempt per call.
                raise LLMHTTPError(create_resp.status_code, f"Devin session create failed: {create_resp.text}")

            session_data = create_resp.json()
            session_id = session_data.get("session_id", "")

            if not session_id:
                raise RuntimeError("Devin returned no session_id")

            logger.info("Devin session created: %s", session_id)

        # Poll for Devin's response (max ~120s with 5s intervals)
        content = ""
        async with httpx.AsyncClient(timeout=15.0) as client:
            for attempt in range(24):
                await asyncio.sleep(5)

                try:
                    status_resp = await client.get(
                        f"{base_url}/session/{session_id}",
                        headers=headers,
                    )
                except httpx.HTTPError:
                    continue

                if status_resp.status_code != 200:
                    continue

                status_data = status_resp.json()
                status = status_data.get("status_enum", status_data.get("status", ""))

                # Messages are inline in the session response
                messages = status_data.get("messages", [])

                # Check structured_output first (if prompt requested it)
                structured = status_data.get("structured_output")
                if structured:
                    content = json.dumps(structured) if not isinstance(structured, str) else structured
                    logger.info("Devin structured_output received on attempt %d", attempt + 1)
                    break

                # Look for devin_message responses
                for msg in reversed(messages):
                    msg_type = msg.get("type", "")
                    if msg_type == "devin_message":
                        content = msg.get("message", "")
                        if content:
                            logger.info(
                                "Devin response received on attempt %d: %d chars",
                                attempt + 1, len(content),
                            )
                            break

                if content:
                    break

                # "blocked" with a devin_message means Devin replied
                # and is waiting for user input — we're done
                if status == "blocked" and not content:
                    # No devin_message yet, keep polling
                    logger.debug(
                        "Devin session %s blocked, attempt %d, %d messages",
                        session_id, attempt + 1, len(messages),
                    )

                # Session truly finished
                if status in ("finished", "stopped"):
                    break

        if not content:
            raise RuntimeError(
                f"Devin session {session_id} did not produce output after {attempt + 1} polls"
            )

        return LLMResponse(
            content=content,
            model="devin",
            input_tokens=len(prompt.split()),
            output_tokens=len(content.split()),
            latency_ms=0,
        )

    def get_usage_summary(self) -> dict[str, Any]:
        """Return current daily usage for health dashboard."""
        return {
            "date": self._usage.date,
            "input_tokens": self._usage.input_tokens,
            "output_tokens": self._usage.output_tokens,
            "total_tokens": self._usage.input_tokens + self._usage.output_tokens,
            "call_count": self._usage.call_count,
            "daily_limit": self._daily_token_limit,
            "active_tier": self.active_tier.value,
        }

    # -- health reporting ---------------------------------------------------

    def provider_status(self) -> list[dict[str, Any]]:
        """One entry per provider: name, current cheap model, ``ok`` or why not."""
        out = []
        for p in self._providers:
            out.append({
                "name": p.name,
                "model": p.model_cheap,
                "local": bool(p.is_local),
                "state": p.status,
                "last_used": p.name == self._last_success_provider,
            })
        return out

    def status_line(self) -> str:
        """``openrouter ok · openai API key rejected (HTTP 401)`` — for logs and `otto status`."""
        if not self._providers:
            return "no LLM provider configured"
        return " · ".join(f"{p['name']} {p['state']}" for p in self.provider_status())

    @property
    def has_working_provider(self) -> bool:
        """Any provider that is neither disabled nor cooling down."""
        return any(p.is_healthy for p in self._providers)

    @property
    def successful_calls(self) -> int:
        return self._ok_calls

    # Subsystem protocol
    async def start(self) -> None:
        logger.info("LLM Gateway started. Providers: %d", len(self._providers))

    async def stop(self) -> None:
        logger.info("LLM Gateway stopped.")

    async def drain(self, timeout: float = 5.0) -> None:
        """Drain in-flight calls (for graceful shutdown)."""
        deadline = time.monotonic() + timeout
        while self._in_flight_count > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if self._in_flight_count > 0:
            logger.warning("LLM Gateway drain timed out with %d calls in flight", self._in_flight_count)
        else:
            logger.info("LLM Gateway drained successfully.")

    async def health_check(self):
        from otto.storage.models import HealthStatus
        if any(p.is_healthy for p in self._providers):
            return HealthStatus.HEALTHY
        return HealthStatus.DEGRADED

    def setup_from_key_file(self, key_file: str) -> bool:
        """Load API keys from a file and configure providers. Returns True if any loaded."""
        providers = load_api_keys_from_file(key_file)
        for p in providers:
            self.add_provider(p)
        return len(providers) > 0

    def setup_from_discovered_keys(self) -> bool:
        """
        Configure providers from every key Otto can discover
        (env → Keychain → data-dir key file → legacy files).

        See :mod:`otto.utils.keys` for the resolution order. Returns True if
        at least one provider was configured.
        """
        from otto.utils.keys import discover_keys

        added = 0
        existing_keys = {getattr(p, "api_key", None) for p in self._providers}
        for found in discover_keys():
            if found.key in existing_keys:
                continue
            provider = _detect_provider(found.key)
            if provider:
                self.add_provider(provider)
                existing_keys.add(found.key)
                added += 1
                logger.info("Configured %s from %s", provider.name, found.source)
        return added > 0

    def sync_discovered_keys(self) -> tuple[int, int]:
        """Follow the key store: add providers for new keys, drop providers whose key is gone.

        Called when config.toml changes, so a key pasted into ``[keys]`` (or
        deleted from it) takes effect on the next refresh without a restart.
        Local providers (Ollama) have no key and stay. Returns ``(added, removed)``.
        """
        from otto.utils.keys import discover_keys

        try:
            live = {k.key for k in discover_keys()}
        except Exception as e:
            logger.debug("key discovery failed during sync: %s", e)
            return 0, 0
        before = len(self._providers)
        self._providers = [
            p for p in self._providers
            if p.is_local or getattr(p, "api_key", None) in (None, "") or getattr(p, "api_key", None) in live
        ]
        removed = before - len(self._providers)
        if removed:
            self._current_provider = None
            logger.info("Dropped %d model provider(s) whose key was removed", removed)
        had = len(self._providers)
        self.setup_from_discovered_keys()
        return len(self._providers) - had, removed

    def ensure_chat_provider(self) -> bool:
        """
        Ensure at least one LLM provider capable of completions is configured.

        Prefers chat-native providers (Ollama, OpenRouter, Gemini) for speed,
        but accepts Devin session API as a valid (slower) fallback.

        Returns True if any completion-capable provider is available.
        """
        # Check if we already have a chat-native provider (fast)
        has_chat = any(
            not getattr(p, "is_devin", False) for p in self._providers
        )
        if has_chat:
            return True

        # Try to auto-detect local Ollama
        ollama_provider = _detect_ollama()
        if ollama_provider:
            self.add_provider(ollama_provider)
            return True

        # Devin session API works as a completions equivalent (slower but valid)
        has_devin = any(
            getattr(p, "is_devin", False) and p.is_healthy for p in self._providers
        )
        if has_devin:
            logger.info(
                "Using Devin session API for completions (slower, uses ACUs). "
                "For faster inference, install Ollama or add an OpenRouter key."
            )
            return True

        logger.info(
            "No LLM provider found. "
            "Add an OpenRouter/OpenAI/Gemini key to api.key, "
            "or install Ollama for local inference."
        )
        return False


def _detect_ollama() -> LLMProvider | None:
    """
    Probe localhost for a running Ollama instance and return a provider if found.

    Checks Ollama's /api/tags endpoint to discover available models,
    then configures the best available model.
    """
    import urllib.request
    import urllib.error

    ollama_url = "http://localhost:11434"

    try:
        req = urllib.request.Request(
            f"{ollama_url}/api/tags",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status != 200:
                return None
            import json as _json
            data = _json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None

    models = data.get("models", [])
    if not models:
        logger.debug("Ollama running but no models installed")
        return None

    # Rank models by capability: prefer larger models for "capable" tier
    model_names = [m.get("name", "") for m in models]
    logger.info("Ollama detected with models: %s", ", ".join(model_names))

    # Select best cheap and capable models from what's available
    # Priority order for each tier
    cheap_prefs = [
        "phi3:mini", "phi3", "tinyllama", "gemma2:2b", "qwen2:1.5b",
    ]
    capable_prefs = [
        "llama3.1:8b", "llama3:8b", "mistral", "phi3:mini", "phi3",
        "gemma2:9b", "qwen2:7b",
    ]

    def _find_best(prefs: list[str], available: list[str]) -> str:
        for pref in prefs:
            for avail in available:
                if avail.startswith(pref):
                    return avail
        return available[0]  # Fallback to first available

    cheap = _find_best(cheap_prefs, model_names)
    capable = _find_best(capable_prefs, model_names)

    p = LLMProvider(
        name="ollama",
        model_cheap=cheap,
        model_capable=capable,
        is_local=True,
    )
    p.base_url = f"{ollama_url}/v1"  # type: ignore[attr-defined]
    p.api_key = "ollama"  # type: ignore[attr-defined]  # Ollama doesn't need a key
    logger.info(
        "Auto-configured Ollama (cheap=%s, capable=%s)",
        cheap, capable,
    )
    return p


def load_api_keys_from_file(filepath: str) -> list[LLMProvider]:
    """
    Read API keys from a key file and auto-detect provider type.

    Supports:
      - OpenRouter keys (sk-or-*)        → openrouter.ai
      - OpenAI keys (sk-*)               → api.openai.com
      - Anthropic keys (sk-ant-*)        → api.anthropic.com
      - Gemini keys (AIza*)              → generativelanguage.googleapis.com
      - Devin keys (apk_*)              → api.devin.ai (session only)

    File format: one key per line, blank lines and #comments ignored.
    Lines can optionally be 'provider:key' format.
    """
    import os

    if not os.path.isfile(filepath):
        logger.debug("API key file not found: %s", filepath)
        return []

    providers: list[LLMProvider] = []

    try:
        with open(filepath) as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue

                # Support "provider:key" format
                if ':' in line and not line.startswith('sk-') and not line.startswith('apk_') and not line.startswith('AIza'):
                    provider_hint, _, key = line.partition(':')
                    key = key.strip()
                    provider_hint = provider_hint.strip().lower()
                else:
                    key = line
                    provider_hint = ""

                provider = _detect_provider(key, provider_hint)
                if provider:
                    providers.append(provider)
                    logger.info(
                        "Loaded API key: %s (%s...)",
                        provider.name, key[:12],
                    )
    except Exception as e:
        logger.error("Failed to read API key file %s: %s", filepath, e)

    return providers


def provider_name_for_key(key: str) -> str:
    """``"openrouter"`` / ``"openai"`` / … for a key's prefix, ``""`` if unknown — no logging, no provider built.

    For status lines and health checks that run every few seconds;
    :func:`_detect_provider` logs as it configures and belongs at start-up.
    """
    from otto.utils.keys import is_slack_token

    k = (key or "").strip()
    if not k or is_slack_token(k):
        return ""
    if k.startswith("apk_"):
        return "devin"
    if k.startswith("AIza"):
        return "gemini"
    if k.startswith("sk-or-"):
        return "openrouter"
    if k.startswith("sk-ant-"):
        return "anthropic"
    if k.startswith("sk-"):
        return "openai"
    return ""


def _detect_provider(key: str, hint: str = "") -> LLMProvider | None:
    """Auto-detect provider from API key prefix."""
    from otto.utils.keys import is_slack_token

    # Slack tokens live in the same key store but are for the read-only Slack
    # reader, not for inference — never send them to an LLM endpoint.
    if is_slack_token(key):
        return None

    # Devin platform keys (apk_user_*, apk_*) — session-based agentic API
    # These enable reading Devin session data but NOT LLM inference
    if hint == "devin" or key.startswith("apk_user_") or key.startswith("apk_"):
        p = LLMProvider(
            name="devin",
            model_cheap="devin",
            model_capable="devin",
        )
        p.base_url = "https://api.devin.ai/v1"  # type: ignore[attr-defined]
        p.api_key = key  # type: ignore[attr-defined]
        p.is_devin = True  # type: ignore[attr-defined]
        logger.info(
            "Devin API key loaded — enables reading session data. "
            "Will auto-detect local Ollama for LLM inference."
        )
        return p

    # Google Gemini keys (AIza*)
    if hint == "gemini" or key.startswith("AIza"):
        p = LLMProvider(
            name="gemini",
            model_cheap="gemini-2.0-flash",
            model_capable="gemini-2.0-flash",
            model_options={
                "cheap": ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-flash-latest"],
                "capable": ["gemini-2.0-flash", "gemini-2.5-pro", "gemini-pro-latest"],
            },
        )
        p.base_url = "https://generativelanguage.googleapis.com/v1beta/openai"  # type: ignore[attr-defined]
        p.api_key = key  # type: ignore[attr-defined]
        return p

    # OpenRouter keys (sk-or-*)
    if hint == "openrouter" or key.startswith("sk-or-"):
        # Model ids retire; the list is walked on 404 and `openrouter/auto`
        # (the router itself) is the durable last resort.
        p = LLMProvider(
            name="openrouter",
            model_cheap="google/gemini-2.5-flash",
            model_capable="anthropic/claude-sonnet-4",
            model_options={
                "cheap": ["google/gemini-2.5-flash", "google/gemini-2.0-flash-001", "openai/gpt-4o-mini",
                          "openai/gpt-4.1-mini", "openrouter/auto"],
                "capable": ["anthropic/claude-sonnet-4", "anthropic/claude-3.7-sonnet", "openai/gpt-4o",
                            "openrouter/auto"],
            },
        )
        p.base_url = "https://openrouter.ai/api/v1"  # type: ignore[attr-defined]
        p.api_key = key  # type: ignore[attr-defined]
        return p

    if hint == "openai" or (key.startswith("sk-") and not key.startswith("sk-ant-")):
        p = LLMProvider(
            name="openai",
            model_cheap="gpt-4o-mini",
            model_capable="gpt-4o",
            model_options={
                "cheap": ["gpt-4o-mini", "gpt-4.1-mini", "gpt-5-mini"],
                "capable": ["gpt-4o", "gpt-4.1", "gpt-5"],
            },
        )
        p.base_url = "https://api.openai.com/v1"  # type: ignore[attr-defined]
        p.api_key = key  # type: ignore[attr-defined]
        return p

    if hint == "anthropic" or key.startswith("sk-ant-"):
        p = LLMProvider(
            name="anthropic",
            model_cheap="claude-3-5-haiku-20241022",
            model_capable="claude-sonnet-4-20250514",
            model_options={
                "cheap": ["claude-3-5-haiku-20241022", "claude-3-5-haiku-latest", "claude-haiku-4-5"],
                "capable": ["claude-sonnet-4-20250514", "claude-sonnet-4-5", "claude-3-7-sonnet-latest"],
            },
        )
        p.base_url = "https://api.anthropic.com/v1"  # type: ignore[attr-defined]
        p.api_key = key  # type: ignore[attr-defined]
        return p

    logger.warning("Unknown API key format: %s...", key[:12])
    return None

