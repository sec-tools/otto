"""
Why this is for you — every reason a card shows must come from something in
the data the user can check, ordered by how directly it ties the item to them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from otto.intelligence import relevance
from otto.intelligence.relevance import (
    MAX_REASONS, PeopleIndex, evidence_text, focus_hits, header_why, opportunity_kind, reasons_for,
    severity_label, why_line,
)
from otto.storage.models import Conversation, Domain, NormalizedEvent, SourceType

NAMES = ["Sam"]
PROFILE = {"role": "security engineer", "focus": ["runner image", "sandbox"]}


def _event(text: str, sender: str, *, is_auto: bool = False, minutes_ago: int = 0, sid: str = "") -> NormalizedEvent:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return NormalizedEvent(
        source=SourceType.SLACK, account_id="acc", source_id=sid or f"s-{abs(hash(text)) % 10**6}",
        source_url="", timestamp=ts, title="#eng", plain_text_extract=text, content_hash="h",
        content_language="en", is_auto_generated=is_auto, sender=sender,
    )


def _conv(events: list[NormalizedEvent], **fields) -> Conversation:
    conv = Conversation(
        source=SourceType.SLACK, account_id="acc", thread_id="t1", subject="#eng", summary="",
        domain=Domain.WORK, relevance_explanation="", participants=[e.sender or "" for e in events],
        last_activity=datetime.now(timezone.utc), event_ids=[e.source_id for e in events],
    )
    for k, v in fields.items():
        setattr(conv, k, v)
    return conv


def _kinds(reasons) -> list[str]:
    return [r.kind for r in reasons]


class TestYourInvolvement:
    def test_an_ask_aimed_at_you_leads_with_its_due_date(self):
        ev = _event("Sam can you review the runner image PR by Friday?", "alice")
        rs = reasons_for(_conv([ev]), [ev], names=NAMES, profile=PROFILE)
        assert rs[0].kind == "asked" and rs[0].label == "Asked of you"
        assert "review the runner image PR" in rs[0].detail and "due" in rs[0].detail
        assert "mention" not in _kinds(rs)          # the ask already says you were named

    def test_a_reply_in_your_thread_says_who_and_when(self):
        mine = _event("can someone check the runner image?", "Sam", minutes_ago=30, sid="a")
        theirs = _event("looked, it is the base image — fix coming", "alice", sid="b")
        rs = reasons_for(_conv([mine, theirs]), [mine, theirs], names=NAMES)
        # You asked a question; the reply is an *answer*, which says more than "a reply".
        assert rs[0].kind == "answered" and rs[0].label == "Reply to your question"
        assert "alice answered" in rs[0].detail and "you asked 30 min ago" in rs[0].detail
        assert "thread" not in _kinds(rs) and "reply" not in _kinds(rs)

    def test_a_reply_to_a_statement_of_yours_is_a_plain_reply(self):
        mine = _event("pushed the runner image fix to staging", "Sam", minutes_ago=30, sid="a")
        theirs = _event("thanks — deploying it now", "alice", sid="b")
        rs = reasons_for(_conv([mine, theirs]), [mine, theirs], names=NAMES)
        assert rs[0].kind == "reply" and rs[0].label == "New reply in your thread"
        assert "alice replied" in rs[0].detail and "you wrote 30 min ago" in rs[0].detail

    def test_your_own_message_is_labelled_as_yours(self):
        bot = _event("scan complete, 0 findings", "scanner", is_auto=True, minutes_ago=20, sid="a")
        mine = _event("build failed twice this morning, could be upstream", "Sam", sid="b")
        rs = reasons_for(_conv([bot, mine]), [bot, mine], names=NAMES)
        assert _kinds(rs) == ["own"]
        assert "you replied" in rs[0].detail and "thread of 2" in rs[0].detail

    def test_a_direct_message_and_a_mention_are_addressed_to_you(self):
        dm = _event("hey, got a minute?", "alice")
        rs = reasons_for(_conv([dm]), [dm], names=NAMES, channel="@alice")
        assert rs[0].kind == "dm"
        ping = _event("@Sam the deploy window moved", "bob")
        rs = reasons_for(_conv([ping]), [ping], names=NAMES)
        assert rs[0].kind == "mention" and "bob named you" in rs[0].detail

    def test_here_ping_is_quiet_evidence(self):
        ev = _event("@here deploy freeze starts at 5", "bob")
        rs = reasons_for(_conv([ev]), [ev], names=NAMES)
        assert any(r.kind == "everyone" and r.label == "@here" and r.tone == "quiet" for r in rs)


class TestWhatYouToldOtto:
    def test_directive_and_focus_are_named_with_their_evidence(self):
        ev = _event("Scan complete — sandbox main. Severity : 4 critical, 2 high, 0 medium", "scanner", is_auto=True)
        rs = reasons_for(_conv([ev], matched_directive="always review payments migration bugs"), [ev], names=NAMES, profile=PROFILE)
        by_kind = {r.kind: r for r in rs}
        assert by_kind["directive"].label == "Your directive" and by_kind["directive"].detail == "always review payments migration bugs"
        assert by_kind["focus"].label == "Your focus: sandbox" and "[user] focus" in by_kind["focus"].detail
        assert by_kind["severity"].label == "4 critical · 2 high"
        assert _kinds(rs)[0] == "directive"            # what you asked for outranks what the bot said

    def test_focus_terms_match_whole_words_only(self):
        assert focus_hits("the sandboxed runner", ["sandbox"]) == []
        assert focus_hits("deployed to payments-api today", ["payments-api"]) == ["payments-api"]
        assert focus_hits("Runner Image rebuilt", ["runner image", "billing"]) == ["runner image"]
        assert focus_hits("anything", ["ab"]) == []       # too short to mean anything


class TestWhatTheMessageProves:
    def test_severity_label_ignores_zero_counts(self):
        assert severity_label("Severity : 0 critical, 2 high, 0 medium") == "2 high"
        assert severity_label("0 critical, 0 high, 0 medium") == ""
        assert severity_label("[HIGH 8.0] Command Injection") == "HIGH 8.0"
        assert severity_label("CVSS 9.8 remote code execution") == "CVSS 9.8"
        assert severity_label("CVSS 3.1 informational") == ""

    def test_deadlines_and_promises_from_others(self):
        due = _event("Reminder: the security review is due Monday", "notify", is_auto=True)
        rs = reasons_for(_conv([due]), [due], names=NAMES)
        assert rs and rs[0].kind == "deadline" and rs[0].label.startswith("Due ")
        promise = _event("I'll send the report by tomorrow", "alice")
        rs = reasons_for(_conv([promise]), [promise], names=NAMES)
        assert rs[0].kind == "waiting" and rs[0].label == "alice promised" and "due tomorrow" in rs[0].detail

    def test_opportunity_reads_as_plain_language(self):
        ev = _event("hi all, joining the platform team this week, happy to pair on infra", "carol")
        conv = _conv([ev], opportunity_score=0.6, opportunity_type="tool_recommendation", opportunity_description="pairing offer")
        rs = reasons_for(conv, [ev], names=NAMES)
        assert any(r.kind == "opportunity" and r.label == "Opportunity · tool worth a look" for r in rs)
        assert opportunity_kind("role_opening") == "role opening" and opportunity_kind("null") == ""
        low = _conv([ev], opportunity_score=0.2, opportunity_type="collaboration")
        assert "opportunity" not in _kinds(reasons_for(low, [ev], names=NAMES))

    def test_your_own_words_are_never_an_opportunity_for_you(self):
        mine = _event("we could automate the nightly triage with a small script", "Sam")
        conv = _conv([mine], opportunity_score=0.8, opportunity_type="process_improvement", opportunity_description="automation")
        assert _kinds(reasons_for(conv, [mine], names=NAMES)) == ["own"]


class TestWhoItIsFrom:
    def test_regulars_and_newcomers(self):
        people = PeopleIndex(week={"alice": 12}, known={"alice", "bob"})
        ev = _event("quick update on the migration", "alice")
        rs = reasons_for(_conv([ev]), [ev], names=NAMES, people=people)
        assert any(r.label == "alice · 12 messages this week" for r in rs)
        first = _event("quick update on the migration", "carol")
        rs = reasons_for(_conv([first]), [first], names=NAMES, people=people)
        assert any(r.kind == "new_person" and r.label == "First message from carol" for r in rs)
        bot = _event("quick update on the migration", "scanner", is_auto=True)
        assert not any(r.kind in ("person", "new_person") for r in reasons_for(_conv([bot]), [bot], names=NAMES, people=people))

    def test_people_index_survives_a_broken_store(self):
        class Broken:
            def people(self, **_):
                raise RuntimeError("locked")
        idx = PeopleIndex.from_store(Broken())
        assert idx.weekly("alice") == 0 and not idx.is_known("alice")
        assert PeopleIndex.from_store(None).week == {}

    def test_a_thread_without_you_is_still_a_thread(self):
        a = _event("who owns the runner image now?", "alice", minutes_ago=10, sid="a")
        b = _event("bob does, since last sprint", "carol", sid="b")
        rs = reasons_for(_conv([a, b]), [a, b], names=NAMES)
        assert any(r.kind == "thread" and r.label == "Thread · 2 messages" for r in rs)


class TestShape:
    def test_at_most_four_reasons_strongest_first(self):
        people = PeopleIndex(week={"alice": 12}, known={"alice"})
        ev = _event("Sam please review the sandbox scan by Friday: 4 critical, 2 high. @here", "alice")
        conv = _conv([ev], matched_directive="always review payments migration bugs", opportunity_score=0.7, opportunity_type="collaboration")
        rs = reasons_for(conv, [ev], names=NAMES, profile=PROFILE, people=people)
        assert len(rs) <= MAX_REASONS
        weights = [r.weight for r in rs]
        assert weights == sorted(weights, reverse=True) and rs[0].kind == "asked"

    def test_empty_and_malformed_inputs(self):
        assert reasons_for(_conv([]), [], names=NAMES) == []
        assert why_line([]) == "" and evidence_text([]) == "" and header_why([]) == ""

    def test_serialization_carries_tone_and_detail(self):
        ev = _event("Sam can you review this?", "alice")
        d = reasons_for(_conv([ev]), [ev], names=NAMES)[0].to_dict()
        assert d["kind"] == "asked" and d["tone"] == "you" and d["label"] == "Asked of you" and d["detail"]

    def test_text_surfaces(self):
        ev = _event("Sam can you review the runner image PR?", "alice")
        rs = reasons_for(_conv([ev]), [ev], names=NAMES, profile=PROFILE)
        assert why_line(rs).startswith("Asked of you · Your focus: runner image")
        assert why_line([{"label": "A"}, {"label": ""}, {"label": "B"}]) == "A · B"
        text = evidence_text(rs)
        assert text.startswith("- Asked of you: review the runner image PR")

    def test_header_tallies_kinds_across_items(self):
        items = [
            {"why": [{"kind": "asked", "label": "Asked of you"}]},
            {"why": [{"kind": "asked", "label": "Asked of you"}, {"kind": "directive", "label": "Your directive"}]},
            {"why": [{"kind": "reply", "label": "New reply in your thread"}]},
            {"why": [{"kind": "opportunity", "label": "Opportunity"}]},
            {"why": []},
            {},
        ]
        line = header_why(items)
        assert line == ("2 ask you for something · 1 new reply in a thread of yours · "
                        "1 touches a directive of yours · 1 opportunity")
        assert header_why(items, limit=1) == "2 ask you for something"

    def test_reasons_never_raise_on_odd_events(self, monkeypatch):
        ev = _event("Sam can you review this?", "alice")
        monkeypatch.setattr(relevance.cm, "extract", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        rs = reasons_for(_conv([ev]), [ev], names=NAMES)
        assert any(r.kind == "mention" for r in rs)     # the rest of the evidence still comes through
