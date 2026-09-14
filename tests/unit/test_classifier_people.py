"""
Who said it matters: the user's own words are never an alert in themselves,
and the LLM is told who wrote each line (a bot, a colleague, or the user).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from otto.core.event_bus import EventBus
from otto.intelligence.classifier import OWN_WORDS_URGENCY_CAP, ConversationClassifier
from otto.llm.gateway import LLMResponse
from otto.storage.models import Conversation, Domain, NormalizedEvent, SourceType
from otto.utils.identity import remember_self_name


def _event(text: str, sender: str, *, is_auto: bool = False, minutes_ago: int = 0, sid: str = "") -> NormalizedEvent:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return NormalizedEvent(
        source=SourceType.SLACK, account_id="acc", source_id=sid or f"s-{abs(hash(text)) % 10**6}",
        source_url="", timestamp=ts, title="#eng", plain_text_extract=text, content_hash="h",
        content_language="en", is_auto_generated=is_auto, sender=sender,
    )


def _conv(events: list[NormalizedEvent]) -> Conversation:
    return Conversation(
        source=SourceType.SLACK, account_id="acc", thread_id="t1", subject="#eng", summary="",
        domain=Domain.WORK, relevance_explanation="", participants=[e.sender or "" for e in events],
        last_activity=datetime.now(timezone.utc), event_ids=[e.source_id for e in events],
    )


class _RecordingLLM:
    """Stands in for the gateway: returns a fixed verdict and keeps what it was asked."""

    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.user_messages: list[str] = []

    async def complete(self, *, task: str, system_prompt: str, user_message: str, **_: object) -> LLMResponse:
        self.user_messages.append(user_message)
        return LLMResponse(content=self.verdict, model="fake", input_tokens=1, output_tokens=1, latency_ms=1.0)


class TestOwnWords:
    def test_the_users_own_reply_is_not_an_alert(self):
        remember_self_name("Sam")
        ev = _event("please make sure everyone looks into the build errors before standup!!", "Sam")
        conv = _conv([ev])
        ConversationClassifier(EventBus()).classify_local(conv, [ev])
        assert conv.urgency <= OWN_WORDS_URGENCY_CAP
        assert "your-message" in conv.topics

    def test_the_same_words_from_a_colleague_do_rate(self):
        remember_self_name("Sam")
        ev = _event("please make sure everyone looks into the build errors before standup!!", "Alice")
        conv = _conv([ev])
        ConversationClassifier(EventBus()).classify_local(conv, [ev])
        assert conv.urgency >= 0.7
        assert "your-message" not in conv.topics

    def test_a_colleagues_answer_to_you_is_theirs_to_rate(self):
        remember_self_name("Sam")
        mine = _event("can someone check the runner image?", "Sam", minutes_ago=10, sid="a")
        theirs = _event("Sam — please look into this today, it is blocking the release", "Alice", sid="b")
        conv = _conv([mine, theirs])
        ConversationClassifier(EventBus()).classify_local(conv, [mine, theirs])
        assert conv.urgency >= 0.7

    def test_a_directive_still_wins_over_the_cap(self, monkeypatch):
        remember_self_name("Sam")
        import otto.intelligence.history as history
        monkeypatch.setattr(history, "load_directives", lambda: [{"directive": "always flag runner image changes", "category": "ops"}])
        ev = _event("switching the runner image to the new base tonight", "Sam")
        conv = _conv([ev])
        ConversationClassifier(EventBus()).classify_local(conv, [ev])
        assert conv.urgency >= 0.8

    def test_llm_cannot_lift_your_own_message_above_the_cap(self):
        remember_self_name("Sam")
        llm = _RecordingLLM('{"urgency": 0.95, "importance": 0.9, "summary": "x", "domain": "work"}')
        ev = _event("switching the runner image tonight, heads up", "Sam")
        conv = _conv([ev])
        clf = ConversationClassifier(EventBus(), llm_gateway=llm)
        clf.classify_local(conv, [ev])
        asyncio.run(clf.classify_llm(conv, [ev]))
        assert conv.llm_enriched
        assert conv.urgency <= OWN_WORDS_URGENCY_CAP


class TestWhoSaidWhat:
    def test_llm_sees_senders_with_you_and_bot_markers_in_thread_order(self):
        remember_self_name("Sam")
        llm = _RecordingLLM('{"urgency": 0.2, "importance": 0.3, "summary": "x", "domain": "work"}')
        parent = _event("Report 10 generated with 0 findings", "scanner", is_auto=True, minutes_ago=60, sid="p")
        reply = _event("build failed twice this morning, could be the runner image", "Sam", sid="r")
        conv = _conv([parent, reply])
        clf = ConversationClassifier(EventBus(), llm_gateway=llm)
        asyncio.run(clf.classify_llm(conv, [parent, reply]))
        sent = llm.user_messages[0]
        assert "scanner (bot)" in sent
        assert "Sam (you)" in sent
        assert sent.index("Report 10") < sent.index("build failed twice")
        assert "Thread, oldest first" in sent
        assert "user's own messages" in sent

    def test_no_markers_when_nobody_is_you(self):
        llm = _RecordingLLM('{"urgency": 0.2, "importance": 0.3, "summary": "x", "domain": "work"}')
        ev = _event("lunch at noon?", "Alice")
        conv = _conv([ev])
        clf = ConversationClassifier(EventBus(), llm_gateway=llm)
        asyncio.run(clf.classify_llm(conv, [ev]))
        sent = llm.user_messages[0]
        assert "— Alice" in sent
        assert "(you)" not in sent and "user's own messages" not in sent and "Thread," not in sent
