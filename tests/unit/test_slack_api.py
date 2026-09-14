"""
Slack Web API adapter — hermetic tests over a fake HTTP client.

Nothing here touches the network: ``FakeSlack`` answers by URL and records
every request so the tests can assert on *what Otto asks for* — GET only,
read methods only, budgeted, back-off honoured.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from otto.adapters import slack as slack_api
from otto.adapters.slack import RequestLedger, SlackAdapter
from otto.storage.models import ConnectionState, HealthStatus, SourceType

T0 = 1_800_000_000.0        # a fixed "now" (2027-01-15T08:00:00Z)


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200, headers: dict | None = None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload


class FakeSlack:
    """A tiny Slack: users, channels, per-channel history and threads."""

    def __init__(self) -> None:
        self.users = {
            "U1": {"id": "U1", "name": "alice", "profile": {"display_name": "Alice", "real_name": "Alice Kim"}},
            "U2": {"id": "U2", "name": "bob", "profile": {"display_name": "", "real_name": "Bob Lee"}},
            "UME": {"id": "UME", "name": "me", "profile": {"display_name": "Sam", "real_name": "Sam Rivera"}},
            "UBOT": {"id": "UBOT", "name": "deploybot", "is_bot": True, "profile": {"real_name": "Deploy Bot"}},
        }
        self.channels = [
            {"id": "C1", "name": "eng", "is_member": True},
            {"id": "C2", "name": "random", "is_member": True},
            {"id": "C3", "name": "not-mine", "is_member": False},
            {"id": "C4", "name": "old", "is_member": True, "is_archived": True},
            {"id": "G1", "name": "leads", "is_member": True, "is_private": True},
            {"id": "D1", "is_im": True, "user": "U1"},
            {"id": "M1", "name": "mpdm-alice--bob--me-1", "is_mpim": True},
        ]
        self.history: dict[str, list[dict]] = {cid: [] for cid in ("C1", "C2", "G1", "D1", "M1")}
        self.threads: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_with: dict[str, Any] = {}      # method → payload or FakeResponse
        self.auth = {"ok": True, "user": "me", "user_id": "UME", "team": "Acme", "url": "https://acme.slack.com/"}

    def add(self, cid: str, ts: float, user: str, text: str, **extra: Any) -> dict:
        msg = {"ts": f"{ts:.6f}", "user": user, "text": text, **extra}
        self.history.setdefault(cid, []).append(msg)
        return msg

    def add_reply(self, cid: str, parent_ts: str, ts: float, user: str, text: str) -> dict:
        msg = {"ts": f"{ts:.6f}", "user": user, "text": text, "thread_ts": parent_ts}
        self.threads.setdefault(f"{cid}:{parent_ts}", []).append(msg)
        return msg

    async def get(self, url: str, params: dict | None = None, **_: Any) -> FakeResponse:
        params = dict(params or {})
        method = urlparse(url).path.rsplit("/", 1)[-1]
        self.calls.append(("GET", method, params))
        if method in self.fail_with:
            f = self.fail_with[method]
            return f if isinstance(f, FakeResponse) else FakeResponse(f)
        if method == "auth.test":
            return FakeResponse(self.auth)
        if method == "users.list":
            return FakeResponse({"ok": True, "members": list(self.users.values())})
        if method == "users.info":
            u = self.users.get(str(params.get("user")))
            return FakeResponse({"ok": True, "user": u} if u else {"ok": False, "error": "user_not_found"})
        if method == "conversations.list":
            types = str(params.get("types", "")).split(",")
            chans = [c for c in self.channels
                     if (c.get("is_im") and "im" in types) or (c.get("is_mpim") and "mpim" in types)
                     or (not c.get("is_im") and not c.get("is_mpim")
                         and (("private_channel" in types) if c.get("is_private") else ("public_channel" in types)))]
            if params.get("exclude_archived") == "true":
                chans = [c for c in chans if not c.get("is_archived")]
            return FakeResponse({"ok": True, "channels": chans})
        if method == "conversations.history":
            cid = str(params.get("channel"))
            oldest = float(params.get("oldest", 0) or 0)
            msgs = [m for m in self.history.get(cid, []) if float(m["ts"]) > oldest]
            msgs.sort(key=lambda m: -float(m["ts"]))
            return FakeResponse({"ok": True, "messages": msgs})
        if method == "conversations.replies":
            key = f"{params.get('channel')}:{params.get('ts')}"
            parent = next((m for m in self.history.get(str(params.get("channel")), []) if m["ts"] == params.get("ts")), None)
            return FakeResponse({"ok": True, "messages": ([parent] if parent else []) + self.threads.get(key, [])})
        return FakeResponse({"ok": False, "error": "unknown_method"})

    def methods(self) -> list[str]:
        return [m for _, m, _ in self.calls]


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def world():
    fake = FakeSlack()
    clock = Clock()
    adapter = SlackAdapter(fake, "default", clock=clock)
    return fake, clock, adapter


def _since(hours: float = 6) -> datetime:
    return datetime.fromtimestamp(T0, tz=timezone.utc) - timedelta(hours=hours)


class TestReadOnlyContract:
    def test_no_write_methods_on_the_class(self, world):
        _fake, _clock, adapter = world
        write_words = {"send", "post", "write", "update", "delete", "modify", "react", "upload", "create"}
        for m in (x for x in dir(adapter) if not x.startswith("_")):
            assert not any(w in m.lower() for w in write_words), m

    @pytest.mark.asyncio
    async def test_only_get_to_read_methods(self, world):
        fake, _clock, adapter = world
        fake.add("C1", T0 - 60, "U1", "hello")
        await adapter.connect()
        await adapter.poll(_since())
        assert {verb for verb, _, _ in fake.calls} == {"GET"}
        assert set(fake.methods()) <= {"auth.test", "users.list", "users.info", "conversations.list",
                                       "conversations.history", "conversations.replies"}

    @pytest.mark.asyncio
    async def test_guarded_client_refuses_writes(self):
        """The real transport: a POST is blocked before it reaches the wire."""
        import httpx

        from otto.safety.write_guard import WriteAttemptBlocked

        ledger = RequestLedger()
        client = slack_api.build_slack_http_client("xoxp-test-token-000000000000", ledger=ledger)
        assert isinstance(client, httpx.AsyncClient)
        assert client.headers["Authorization"] == "Bearer xoxp-test-token-000000000000"
        try:
            with pytest.raises(WriteAttemptBlocked):
                await client.post("https://slack.com/api/chat.postMessage")
            assert ledger.blocked == 1 and ledger.requests == 0
        finally:
            await client.aclose()

    def test_ledger_counts_by_method(self):
        ledger = RequestLedger()
        ledger.append(method="GET", url="https://slack.com/api/conversations.history?channel=C1")
        ledger.append(method="GET", url="https://slack.com/api/conversations.history?channel=C2")
        ledger.append(method="GET", url="https://slack.com/api/auth.test")
        assert ledger.requests == 3 and ledger.per_minute() == 3
        assert ledger.by_method == {"conversations.history": 2, "auth.test": 1}


class TestConnect:
    @pytest.mark.asyncio
    async def test_connect_learns_identity_and_channels(self, world):
        fake, _clock, adapter = world
        status = await adapter.connect()
        assert status.state == ConnectionState.HEALTHY
        assert adapter.name == "api_slack:acme"
        assert adapter.source_type == SourceType.SLACK
        # Self: auth.test user name + users.list display/real names, shared with the radar.
        assert set(adapter.self_names) == {"me", "Sam", "Sam Rivera"}
        from otto.utils.identity import learned_self_names
        assert set(learned_self_names()) >= {"Sam", "Sam Rivera"}
        # Channels: members only, archived dropped, DMs named after the person.
        assert adapter._channels == {"C1": "#eng", "C2": "#random", "G1": "#leads", "D1": "@Alice", "M1": "@alice, bob, me"}
        assert adapter.channel_count == 5

    @pytest.mark.asyncio
    async def test_connect_is_cached_and_reverified_later(self, world):
        fake, clock, adapter = world
        await adapter.connect()
        n = len(fake.calls)
        await adapter.connect()
        assert len(fake.calls) == n                       # no network within the TTL
        clock.tick(slack_api.CONNECT_TTL_S + 1)
        await adapter.connect()
        assert fake.methods().count("auth.test") == 2
        assert fake.methods().count("users.list") == 1    # users have their own, longer TTL

    @pytest.mark.asyncio
    async def test_bad_token_fails_with_reason(self, world):
        fake, _clock, adapter = world
        fake.fail_with["auth.test"] = {"ok": False, "error": "invalid_auth"}
        status = await adapter.connect()
        assert status.state == ConnectionState.FAILED
        assert "invalid_auth" in status.error
        assert await adapter.poll(_since()) == []
        assert await adapter.health_check() == HealthStatus.UNHEALTHY

    @pytest.mark.asyncio
    async def test_network_error_fails_softly(self, world):
        fake, _clock, adapter = world

        async def boom(url, **kw):
            raise ConnectionError("down")

        fake.get = boom  # type: ignore[assignment]
        status = await adapter.connect()
        assert status.state == ConnectionState.FAILED and "ConnectionError" in status.error

    @pytest.mark.asyncio
    async def test_listing_narrows_when_im_scope_is_missing(self, world):
        fake, _clock, adapter = world
        real_get = fake.get

        async def get(url, params=None, **kw):
            if url.endswith("conversations.list") and "im" in str((params or {}).get("types", "")):
                fake.calls.append(("GET", "conversations.list", dict(params or {})))
                return FakeResponse({"ok": False, "error": "missing_scope", "needed": "im:read"})
            return await real_get(url, params=params, **kw)

        fake.get = get  # type: ignore[assignment]
        await adapter.connect()
        assert set(adapter._channels) == {"C1", "C2", "G1"}   # public + private, no DMs
        assert adapter.last_error == ""                        # the successful listing cleared it
        assert adapter.scope_note == "token cannot list DMs, group DMs"
        assert adapter._listable_types == ["public_channel", "private_channel"]
        assert fake.methods().count("conversations.list") == 3   # full → minus im → minus mpim


class TestPolling:
    @pytest.mark.asyncio
    async def test_messages_become_events_with_resolved_names(self, world):
        fake, _clock, adapter = world
        fake.add("C1", T0 - 300, "U2", "Hey <@U1> can you review <https://github.com/acme/x/pull/7|PR 7> by tomorrow? &amp; thanks")
        fake.add("C1", T0 - 200, "U1", "on it <#C2|random> <!here>")
        fake.add("C1", T0 - 100, "UBOT", "", attachments=[{"title": "Deploy finished", "text": "api v1.2.3 → prod"}])
        fake.add("C1", T0 - 50, "U1", "joined", subtype="channel_join")
        await adapter.connect()
        events = await adapter.poll(_since())
        assert [e.sender_name for e in events] == ["Bob Lee", "Alice", "Deploy Bot"]
        assert events[0].plain_text == "Hey @Alice can you review PR 7 (https://github.com/acme/x/pull/7) by tomorrow? & thanks"
        assert events[0].raw_metadata["has_mention"] is True and events[0].raw_metadata["mentions_you"] is False
        assert events[1].plain_text == "on it #random @here"
        assert events[2].plain_text == "Deploy finished — api v1.2.3 → prod" and events[2].is_auto_generated
        assert all(e.title == "#eng" for e in events)
        assert events[0].source_url == "https://acme.slack.com/archives/C1/p1799999700000000"
        assert events[0].timestamp == datetime.fromtimestamp(T0 - 300, tz=timezone.utc)

    @pytest.mark.asyncio
    async def test_self_mention_is_rendered_with_your_name(self, world):
        fake, _clock, adapter = world
        fake.add("C1", T0 - 30, "U1", "<@UME> can you own the retro?")
        await adapter.connect()
        (ev,) = await adapter.poll(_since())
        assert ev.plain_text == "@me can you own the retro?"
        assert ev.raw_metadata["mentions_you"] is True

    @pytest.mark.asyncio
    async def test_dms_and_group_dms_are_read(self, world):
        fake, _clock, adapter = world
        fake.add("D1", T0 - 30, "U1", "lunch?")
        fake.add("M1", T0 - 20, "U2", "standup moved to 10")
        await adapter.connect()
        events = await adapter.poll(_since())
        by_title = {e.title: e for e in events}
        assert by_title["@Alice"].raw_metadata["is_dm"] is True
        assert by_title["@alice, bob, me"].plain_text == "standup moved to 10"

    @pytest.mark.asyncio
    async def test_second_poll_only_fetches_new_and_returns_the_window(self, world):
        fake, clock, adapter = world
        fake.add("C1", T0 - 300, "U1", "first")
        await adapter.connect()
        first = await adapter.poll(_since())
        assert [e.plain_text for e in first] == ["first"]
        hist = [p for _, m, p in fake.calls if m == "conversations.history" and p["channel"] == "C1"]
        assert float(hist[-1]["oldest"]) == pytest.approx(_since().timestamp())
        clock.tick(60)
        fake.add("C1", T0 + 30, "U2", "second")
        second = await adapter.poll(_since())
        assert [e.plain_text for e in second] == ["first", "second"]     # cached + new
        hist = [p for _, m, p in fake.calls if m == "conversations.history" and p["channel"] == "C1"]
        assert float(hist[-1]["oldest"]) == pytest.approx(T0 - slack_api.OVERLAP_S)
        assert adapter.stats["new"] == 1

    @pytest.mark.asyncio
    async def test_thread_replies_are_fetched_and_grouped(self, world):
        fake, _clock, adapter = world
        parent = fake.add("C1", T0 - 600, "U1", "Design review thread", reply_count=2, latest_reply=f"{T0 - 100:.6f}")
        fake.add_reply("C1", parent["ts"], T0 - 200, "U2", "LGTM")
        fake.add_reply("C1", parent["ts"], T0 - 100, "UME", "one nit")
        await adapter.connect()
        events = await adapter.poll(_since())
        assert [e.plain_text for e in events] == ["Design review thread", "LGTM", "one nit"]
        assert {e.thread_id for e in events} == {f"C1:{parent['ts']}"}
        assert "thread_ts=" in events[1].source_url
        assert fake.methods().count("conversations.replies") == 1
        assert adapter.stats["threads"] == 1

    @pytest.mark.asyncio
    async def test_unknown_senders_are_looked_up(self, world):
        fake, _clock, adapter = world
        fake.users["UNEW"] = {"id": "UNEW", "name": "newbie", "profile": {"real_name": "Nina New"}}
        del fake.users["UNEW"]      # not in users.list …
        fake.add("C1", T0 - 30, "UNEW", "hi all")
        await adapter.connect()
        fake.users["UNEW"] = {"id": "UNEW", "name": "newbie", "profile": {"real_name": "Nina New"}}  # … but users.info knows
        (ev,) = await adapter.poll(_since())
        assert ev.sender_name == "Nina New"
        assert "users.info" in fake.methods()

    @pytest.mark.asyncio
    async def test_rate_limit_pauses_and_keeps_what_it_has(self, world):
        fake, clock, adapter = world
        fake.add("C1", T0 - 300, "U1", "before the limit")
        await adapter.connect()
        await adapter.poll(_since())
        fake.fail_with["conversations.history"] = FakeResponse({"ok": False, "error": "ratelimited"}, 429, {"Retry-After": "45"})
        clock.tick(60)
        events = await adapter.poll(_since())
        assert [e.plain_text for e in events] == ["before the limit"]   # cache survives
        assert adapter.last_error.startswith("rate limited")
        assert fake.methods().count("conversations.history") == 5 + 1   # first poll: 5 channels; then one 429 and stop
        clock.tick(10)
        n = len(fake.calls)
        await adapter.poll(_since())
        assert len(fake.calls) == n                                       # still paused: zero requests
        status = await adapter.connect()
        assert status.state == ConnectionState.DEGRADED                  # collector still calls poll (cached events)
        clock.tick(40)
        del fake.fail_with["conversations.history"]
        await adapter.poll(_since())
        assert len(fake.calls) > n                                        # resumed

    @pytest.mark.asyncio
    async def test_missing_history_scope_disables_that_kind_only(self, world):
        fake, clock, adapter = world
        real_get = fake.get

        async def get(url, params=None, **kw):
            if url.endswith("conversations.history") and str((params or {}).get("channel", "")).startswith("D"):
                fake.calls.append(("GET", "conversations.history", dict(params or {})))
                return FakeResponse({"ok": False, "error": "missing_scope", "needed": "im:history"})
            return await real_get(url, params=params, **kw)

        fake.get = get  # type: ignore[assignment]
        fake.add("C1", T0 - 30, "U1", "public still works")
        await adapter.connect()
        events = await adapter.poll(_since())
        assert [e.plain_text for e in events] == ["public still works"]
        assert "history:im" in adapter._missing_scopes
        clock.tick(60)
        before = fake.methods().count("conversations.history")
        await adapter.poll(_since())
        dm_reads = [p for _, m, p in fake.calls[-(fake.methods().count("conversations.history") - before):] if m == "conversations.history" and p["channel"] == "D1"]
        assert dm_reads == []                                             # DMs are no longer attempted

    @pytest.mark.asyncio
    async def test_revoked_token_mid_poll_disconnects(self, world, caplog):
        fake, _clock, adapter = world
        await adapter.connect()
        fake.fail_with["conversations.history"] = {"ok": False, "error": "token_revoked"}
        with caplog.at_level(logging.WARNING, logger="otto.adapters.slack"):
            assert await adapter.poll(_since()) == []
        assert adapter._connected is False and "token_revoked" in adapter.last_error
        # The first refusal ends the poll: no channel after it is tried, one warning, no user look-ups.
        assert fake.methods().count("conversations.history") == 1
        assert [m for m in fake.methods() if m in ("conversations.replies", "users.info")] == []
        assert sum("token rejected" in r.getMessage() for r in caplog.records) == 1
        assert "otto slack connect" in caplog.text
        fake.fail_with["auth.test"] = {"ok": False, "error": "token_revoked"}
        assert (await adapter.connect()).state == ConnectionState.FAILED

    @pytest.mark.asyncio
    async def test_rejected_token_is_rechecked_every_ten_minutes_not_every_poll(self, world):
        fake, clock, adapter = world
        fake.fail_with["auth.test"] = {"ok": False, "error": "invalid_auth"}
        status = await adapter.connect()
        assert status.state == ConnectionState.FAILED and "invalid_auth" in status.error
        calls_after_first = len(fake.calls)
        for _ in range(9):                      # nine more refreshes inside the park: silent, no traffic
            clock.tick(60)
            status = await adapter.connect()
            assert status.state == ConnectionState.FAILED and "invalid_auth" in status.error
            assert await adapter.poll(_since()) == []
        assert len(fake.calls) == calls_after_first
        clock.tick(60)                           # 10 min later: one auth.test, then parked again
        assert (await adapter.connect()).state == ConnectionState.FAILED
        assert fake.methods().count("auth.test") == 2
        # The user replaces the token (a fresh adapter after `otto slack connect`) — or Slack
        # accepts the old one again: the next check reconnects normally.
        del fake.fail_with["auth.test"]
        clock.tick(slack_api.AUTH_RECHECK_S)
        assert (await adapter.connect()).state == ConnectionState.HEALTHY
        assert adapter._connected is True

    @pytest.mark.asyncio
    async def test_poll_never_raises(self, world):
        fake, _clock, adapter = world
        await adapter.connect()

        async def boom(url, **kw):
            raise RuntimeError("socket exploded")

        fake.get = boom  # type: ignore[assignment]
        assert await adapter.poll(_since()) == []
        assert "socket exploded" in adapter.last_error


class TestBudget:
    def _many_channels(self, fake: FakeSlack, n: int) -> None:
        fake.channels = [{"id": f"C{i}", "name": f"chan{i}", "is_member": True} for i in range(n)]
        fake.history = {f"C{i}": [] for i in range(n)}

    @pytest.mark.asyncio
    async def test_calls_per_poll_are_capped_and_rotate(self, world):
        fake, clock, adapter = world
        self._many_channels(fake, 120)
        await adapter.connect()
        await adapter.poll(_since())
        first = {p["channel"] for _, m, p in fake.calls if m == "conversations.history"}
        assert len(first) <= slack_api.MAX_CALLS_PER_POLL
        assert adapter.stats["calls"] <= slack_api.MAX_CALLS_PER_POLL
        n = len(fake.calls)
        clock.tick(60)
        await adapter.poll(_since())
        second = {p["channel"] for _, m, p in fake.calls[n:] if m == "conversations.history"}
        assert second and not (first & second)                            # cold start walks new channels first
        # Every channel gets read within a few refreshes.
        seen = first | second
        for _ in range(6):
            clock.tick(60)
            await adapter.poll(_since())
            seen |= {p["channel"] for _, m, p in fake.calls if m == "conversations.history"}
        assert seen == {f"C{i}" for i in range(120)}

    @pytest.mark.asyncio
    async def test_hot_channels_are_read_every_poll_quiet_ones_round_robin(self, world):
        fake, clock, adapter = world
        self._many_channels(fake, 60)
        for i in range(10):
            fake.add(f"C{i}", T0 - 100 - i, "U1", f"busy {i}")     # ten hot channels
        await adapter.connect()
        for _ in range(3):                                          # cover every channel once
            await adapter.poll(_since())
            clock.tick(60)
        n = len(fake.calls)
        await adapter.poll(_since())
        read = [p["channel"] for _, m, p in fake.calls[n:] if m == "conversations.history"]
        assert {f"C{i}" for i in range(10)} <= set(read)            # all hot channels every poll
        assert len(read) > 10                                       # and a slice of the quiet ones

    def test_plan_reserves_budget_for_quiet_channels(self, world):
        _fake, _clock, adapter = world
        adapter._channels = {f"C{i}": f"#c{i}" for i in range(50)}
        adapter._channel_meta = {c: {} for c in adapter._channels}
        for c in adapter._channels:
            adapter._last_polled[c] = T0 - 3600
        for i in range(40):
            adapter._activity[f"C{i}"] = T0 - 60                    # 40 hot, 10 quiet
        plan = adapter._plan(T0, 20)
        assert len(plan) == 20
        assert len([c for c in plan if int(c[1:]) >= 40]) == 5     # a quarter of the budget for quiet channels

    @pytest.mark.asyncio
    async def test_deadline_stops_the_poll(self, world):
        fake, clock, adapter = world
        self._many_channels(fake, 30)
        await adapter.connect()
        real_get = fake.get

        async def slow(url, params=None, **kw):
            clock.tick(slack_api.POLL_DEADLINE_S / 4)               # every call eats a quarter of the budget
            return await real_get(url, params=params, **kw)

        fake.get = slow  # type: ignore[assignment]
        await adapter.poll(_since())
        assert adapter.stats["polled"] <= 4


class TestRendering:
    def test_render_text_tokens(self, world):
        _fake, _clock, adapter = world
        adapter._users = {"U1": {"name": "Alice", "is_bot": False}}
        adapter._channels = {"C9": "#ops"}
        adapter._self_id, adapter._self_names = "UME", ["Sam"]
        r = adapter.render_text
        assert r("<@U1> <@U2> <@U3|carol> <@UME>") == "@Alice @U2 @carol @Sam"
        assert r("see <#C9> and <#C10|dev>") == "see #ops and #dev"
        assert r("<!channel> <!subteam^S1|@platform> <!date^1700000000^{date}|Nov 14>") == "@channel @platform Nov 14"
        assert r("<https://a.b/c> <https://a.b/c|https://a.b/c> <mailto:x@y.z|x@y.z>") == "https://a.b/c https://a.b/c x@y.z"
        assert r("a &lt; b &amp; c &gt; d") == "a < b & c > d"

    def test_message_to_event_edge_cases(self, world):
        _fake, _clock, adapter = world
        assert adapter._message_to_event({"ts": "1.0", "subtype": "channel_join", "user": "U1", "text": "joined"}, "C1", "eng") is None
        assert adapter._message_to_event({"ts": "1.0", "user": "U1", "text": ""}, "C1", "eng") is None
        ev = adapter._message_to_event({"ts": "1.5", "user": "U1", "text": "x", "files": [{"name": "plan.pdf"}]}, "C1", "eng")
        assert ev is not None and ev.plain_text == "x\n[file: plan.pdf]" and ev.has_attachments
        bot = adapter._message_to_event({"ts": "2.0", "bot_id": "B1", "username": "jira", "text": "PROJ-1 moved to Done"}, "C1", "eng")
        assert bot is not None and bot.is_auto_generated and bot.sender_name == "jira"
        blocks = adapter._message_to_event(
            {"ts": "3.0", "bot_id": "B2", "text": "", "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "*Build failed* <https://ci/1|logs>"}}]},
            "C1", "ci")
        assert blocks is not None and blocks.plain_text == "*Build failed* logs (https://ci/1)"
        parent = adapter._message_to_event({"ts": "4.0", "user": "U1", "text": "thread root", "reply_count": 3}, "C1", "eng")
        assert parent is not None and parent.thread_id == "C1:4.0"
        bad_ts = adapter._message_to_event({"ts": "not-a-number", "user": "U1", "text": "hm"}, "C1", "eng")
        assert bad_ts is not None

    def test_retry_after_parsing(self):
        assert slack_api._retry_after(FakeResponse({}, 429, {"Retry-After": "12"})) == 12.0
        assert slack_api._retry_after(FakeResponse({}, 429, {"Retry-After": "1"})) == 5.0      # floor
        assert slack_api._retry_after(FakeResponse({}, 429, {"Retry-After": "99999"})) == 900.0  # ceiling
        assert slack_api._retry_after(FakeResponse({}, 429, {"Retry-After": "soon"})) == 30.0
        assert slack_api._retry_after(FakeResponse({}, 429)) == 30.0


class TestKeysAndWiring:
    def test_slack_tokens_are_recognised_and_never_treated_as_llm_keys(self, monkeypatch):
        from otto.llm.gateway import _detect_provider
        from otto.utils import keys as K

        assert K.is_slack_token("xoxp-1234567890-1234567890-abcdefghij")
        assert K.is_slack_token("xoxb-1234567890-1234567890-abcdefghij")
        assert K.is_slack_token("xoxe.xoxp-1-abcdefghijklmnopqrstuvwxyz")
        assert not K.is_slack_token("xoxc-browser-session-token-0000000")
        assert not K.is_slack_token("sk-or-v1-abcdefghijklmnopqrstuvwxyz")
        assert not K.is_slack_token("xoxp-short")
        assert _detect_provider("xoxp-1234567890-1234567890-abcdefghij") is None
        monkeypatch.setenv("SLACK_TOKEN", "xoxb-1234567890-1234567890-abcdefghij")
        monkeypatch.setenv("OTTO_API_KEY", "xoxp-1234567890-1234567890-abcdefghij")
        tok = K.slack_token()
        assert tok is not None and tok.key.startswith("xoxp-")                # user tokens preferred
        from otto.llm.gateway import LLMGateway
        gw = LLMGateway()
        gw.setup_from_discovered_keys()
        assert not gw._providers

    def test_api_adapters_come_from_the_key_store_and_persist(self, monkeypatch):
        from otto.web import collect

        assert collect.api_adapters() == []
        monkeypatch.setenv("SLACK_TOKEN", "xoxp-1234567890-1234567890-abcdefghij")
        (a,) = collect.api_adapters()
        assert isinstance(a, SlackAdapter) and a.name == "api_slack:default"
        (b,) = collect.api_adapters()
        assert a is b                                                          # same instance across refreshes
        monkeypatch.setenv("SLACK_TOKEN", "xoxp-1234567890-1234567890-zzzzzzzzzz")
        (c,) = collect.api_adapters()
        assert c is not a                                                      # a new token retires the old client
        collect.reset_api_adapters()
        assert collect._source_label(a.name) == "slack api"

    @pytest.mark.asyncio
    async def test_collector_reports_channel_counts(self, world):
        from otto.web import collect

        fake, _clock, adapter = world
        fake.add("C1", T0 - 30, "U1", "hello")
        name, events, status = await collect._poll_adapter(adapter, _since(), backoff=collect.SourceBackoff())
        assert name == "api_slack:default" and len(events) == 1       # the workspace name is learnt on connect
        assert adapter.name == "api_slack:acme"
        assert status["source"] == "slack api" and status["ok"] and status["channels"] == 5 and status["calls"] >= 1

    def test_key_cli_labels_slack_tokens(self, capsys, monkeypatch, tmp_path):
        from otto import cli

        monkeypatch.setattr(cli, "_api", lambda *a, **k: None)
        assert cli.main(["key", "add", "xoxp-1234567890-1234567890-abcdefghij"]) == 0
        out = capsys.readouterr().out
        assert "Slack read-only token — user" in out and "nothing is ever written" in out
        assert "xoxp-1234567890-1234567890-abcdefghij" not in out              # masked
        assert cli.main(["key", "list"]) == 0
        assert "(slack, read-only)" in capsys.readouterr().out

    def test_status_reports_token_state(self, monkeypatch):
        """What `otto doctor` used to say about the token now comes from `otto status` (slack_connect.status)
        and the engine's problem list (core/health.py)."""
        from otto.core import health, slack_connect

        out = []
        assert slack_connect.status(out=out.append) == 1 and any("no token" in line for line in out)
        monkeypatch.setenv("SLACK_TOKEN", "xoxb-1234567890-1234567890-abcdefghij")
        out.clear()
        slack_connect.status(client=object(), engine_status={"source_status": [
            {"source": "slack api", "ok": True, "channels": 42, "items": 7}]}, out=out.append)
        text = "\n".join(out)
        assert "bot token" in text and "abcdefghij" not in text and "reading 42 channels" in text
        rejected = {"source_status": [{"source": "slack api", "ok": False, "error": "Slack token rejected (invalid_auth)"}]}
        found = health.problems(rejected, menubar_installed=False)
        assert [p["title"] for p in found] == ["Slack token was rejected"] and found[0]["action"] == "slack"
        assert health.problems({"source_status": [{"source": "slack api", "ok": True, "channels": 42}]},
                               menubar_installed=False) == []

    def test_parse_qs_sanity(self):
        # Guard for the fake: Slack params travel as query strings in the real client.
        assert parse_qs("channel=C1&oldest=1.5") == {"channel": ["C1"], "oldest": ["1.5"]}
