"""
Who the user is — ``[user] role`` and ``focus`` — reaches the model and the
local scorer, and the model's one-line "for you" verdict is kept only when it
says something.
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from otto.config import ConfigManager
from otto.core.event_bus import EventBus
from otto.intelligence.classification_cache import ClassificationCache
from otto.intelligence.classifier import FOCUS_IMPORTANCE_FLOOR, FOCUS_URGENCY_FLOOR, ConversationClassifier
from otto.llm.gateway import LLMResponse
from otto.storage.models import Conversation, Domain, NormalizedEvent, SourceType
from otto.utils import identity

PROFILE = {"role": "security engineer on the platform team", "focus": ["runner image", "sandbox"]}


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
    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.system_prompts: list[str] = []
        self.user_messages: list[str] = []

    async def complete(self, *, task: str, system_prompt: str, user_message: str, **_: object) -> LLMResponse:
        self.system_prompts.append(system_prompt)
        self.user_messages.append(user_message)
        return LLMResponse(content=self.verdict, model="fake", input_tokens=1, output_tokens=1, latency_ms=1.0)


class TestConfig:
    def test_role_and_focus_come_from_the_user_section(self, monkeypatch):
        with tempfile.NamedTemporaryFile(suffix=".toml", mode="w", delete=False) as f:
            f.write('[user]\nname = "Sam"\nrole = "security engineer"\nfocus = ["runner image", "  ", "ab", "sandbox", "Sandbox"]\n')
            path = Path(f.name)
        try:
            monkeypatch.setattr(identity, "ConfigManager", lambda: ConfigManager(config_path=path), raising=False)
            import otto.config as config_mod
            monkeypatch.setattr(config_mod, "ConfigManager", lambda *a, **k: ConfigManager(config_path=path))
            p = identity.profile()
            assert p["role"] == "security engineer"
            assert p["focus"] == ["runner image", "sandbox"]          # blanks, 2-letter terms and duplicates dropped
            text = identity.profile_text(p)
            assert text == "Role: security engineer\nFocus (projects, systems, topics they own or watch): runner image, sandbox"
        finally:
            path.unlink(missing_ok=True)

    def test_defaults_are_empty_and_render_to_nothing(self, monkeypatch):
        import otto.config as config_mod
        monkeypatch.setattr(config_mod, "ConfigManager", lambda *a, **k: ConfigManager(config_path=Path("/nonexistent/config.toml")))
        assert identity.profile() == {"role": "", "focus": []}
        assert identity.profile_text({"role": "", "focus": []}) == ""


class TestLocalScoring:
    def test_a_message_about_your_focus_is_lifted_out_of_the_fold(self, monkeypatch):
        monkeypatch.setattr(identity, "profile", lambda: PROFILE)
        ev = _event("fyi the runner image rebuild moved to the nightly job", "alice")
        conv = _conv([ev])
        ConversationClassifier(EventBus()).classify_local(conv, [ev])
        assert conv.urgency >= FOCUS_URGENCY_FLOOR and conv.importance >= FOCUS_IMPORTANCE_FLOOR
        assert "your-focus" in conv.topics
        assert "Your focus: runner image" in conv.relevance_explanation

    def test_the_same_message_without_a_profile_stays_quiet(self, monkeypatch):
        monkeypatch.setattr(identity, "profile", lambda: {"role": "", "focus": []})
        ev = _event("fyi the runner image rebuild moved to the nightly job", "alice")
        conv = _conv([ev])
        ConversationClassifier(EventBus()).classify_local(conv, [ev])
        assert conv.urgency < FOCUS_URGENCY_FLOOR and "your-focus" not in conv.topics


class TestModelContext:
    def test_role_focus_and_verified_ties_reach_the_model(self, monkeypatch):
        monkeypatch.setattr(identity, "profile", lambda: PROFILE)
        llm = _RecordingLLM('{"urgency": 0.6, "importance": 0.6, "summary": "x", "domain": "work", '
                            '"for_you": "  Alice needs your review of the runner image PR   by Friday. "}')
        ev = _event("Sam can you review the runner image PR by Friday?", "alice")
        conv = _conv([ev])
        clf = ConversationClassifier(EventBus(), llm_gateway=llm)
        asyncio.run(clf.classify_llm(conv, [ev], evidence="- Asked of you: review the runner image PR · due Fri"))
        prompt = llm.system_prompts[0]
        assert "## Who the user is" in prompt
        assert "Role: security engineer on the platform team" in prompt
        assert "runner image, sandbox" in prompt
        assert "Verified ties between this conversation and the user" in prompt
        assert "- Asked of you: review the runner image PR · due Fri" in prompt
        assert '"for_you"' in prompt and "never invent involvement" in prompt
        assert conv.for_you == "Alice needs your review of the runner image PR by Friday."

    def test_no_profile_no_evidence_no_context_uses_the_plain_prompt(self, monkeypatch):
        monkeypatch.setattr(identity, "profile", lambda: {"role": "", "focus": []})
        import otto.intelligence.history as history
        monkeypatch.setattr(history, "get_standing_directives_text", lambda: "(No standing user directives recorded)")
        llm = _RecordingLLM('{"urgency": 0.2, "importance": 0.3, "summary": "x", "domain": "work", "for_you": null}')
        ev = _event("lunch at noon?", "alice")
        conv = _conv([ev])
        clf = ConversationClassifier(EventBus(), llm_gateway=llm)
        asyncio.run(clf.classify_llm(conv, [ev]))
        prompt = llm.system_prompts[0]
        assert "## Who the user is" not in prompt and '"for_you"' in prompt
        assert conv.for_you == ""

    def test_null_and_filler_verdicts_leave_for_you_empty(self, monkeypatch):
        monkeypatch.setattr(identity, "profile", lambda: PROFILE)
        for verdict in ('"null"', '"None"', '"  "', "123"):
            llm = _RecordingLLM('{"urgency": 0.2, "importance": 0.3, "summary": "x", "domain": "work", "for_you": %s}' % verdict)
            ev = _event("lunch at noon?", "alice")
            conv = _conv([ev])
            conv.for_you = "stale"
            clf = ConversationClassifier(EventBus(), llm_gateway=llm)
            asyncio.run(clf.classify_llm(conv, [ev]))
            assert conv.llm_enriched and conv.for_you == "", verdict

    def test_for_you_is_cached_with_the_verdict(self):
        conv = _conv([_event("x", "alice")])
        conv.for_you = "Alice needs your review by Friday."
        snap = ClassificationCache.snapshot(conv)
        assert snap["for_you"] == "Alice needs your review by Friday."
        fresh = _conv([_event("x", "alice")])
        ClassificationCache().apply(fresh, snap)
        assert fresh.for_you == "Alice needs your review by Friday."
        ClassificationCache().apply(fresh, {"urgency": 0.5})          # older entries simply lack the key
        assert fresh.for_you == "Alice needs your review by Friday."
