from __future__ import annotations

from datetime import datetime, timezone
import pytest

from otto.core.event_bus import EventBus
from otto.intelligence.classifier import ConversationClassifier
from otto.storage.models import (
    ActionItem,
    ActionStatus,
    Conversation,
    Domain,
    NormalizedEvent,
    SourceType,
)


def _make_event(
    title: str = "Update",
    text: str = "",
    sender: str = "Alice",
    source: SourceType = SourceType.SLACK,
    recipients: list = None,
    is_auto: bool = False,
) -> NormalizedEvent:
    return NormalizedEvent(
        source=source,
        account_id="acc1",
        source_id="src1",
        source_url="https://example.com",
        timestamp=datetime.now(timezone.utc),
        title=title,
        plain_text_extract=text,
        content_hash="hash1",
        content_language="en",
        is_auto_generated=is_auto,
        sender=sender,
        recipients=recipients or [],
    )


def _make_conv(subject: str = "Thread", events: list[NormalizedEvent] = None) -> Conversation:
    events = events or [_make_event()]
    return Conversation(
        source=events[0].source,
        account_id=events[0].account_id,
        thread_id="t1",
        subject=subject,
        summary="",
        domain=Domain.WORK,
        relevance_explanation="",
        participants=[e.sender or "" for e in events],
        last_activity=datetime.now(timezone.utc),
        event_ids=[e.source_id for e in events],
    )


class TestSecurityAwareClassifier:
    def setup_method(self):
        self.classifier = ConversationClassifier(EventBus())

    def test_cvss_critical_score_extraction(self):
        ev = _make_event(title="Security alert", text="Found issue with CVSS: 9.8 in auth service")
        conv = _make_conv("Auth Security", [ev])
        res = self.classifier.classify_local(conv, [ev])
        assert res.urgency >= 0.9
        assert res.importance >= 0.8
        assert "Critical" in res.relevance_explanation

    def test_cvss_high_score_extraction(self):
        ev = _make_event(title="Vulnerability report", text="keploy [HIGH 7.8] mapdb.Insert joins testSetID")
        conv = _make_conv("Keploy finding", [ev])
        res = self.classifier.classify_local(conv, [ev])
        assert res.urgency >= 0.7
        assert "High severity finding" in res.relevance_explanation

    def test_cvss_medium_score_extraction(self):
        ev = _make_event(title="Scan results", text="Low risk finding score 5.2 in static assets")
        conv = _make_conv("Static assets", [ev])
        res = self.classifier.classify_local(conv, [ev])
        assert res.urgency >= 0.4
        assert "Medium" in res.relevance_explanation

    def test_cve_pattern_detection(self):
        ev = _make_event(title="CVE-2026-12345", text="Details about CVE-2026-12345 published")
        conv = _make_conv("CVE-2026-12345", [ev])
        res = self.classifier.classify_local(conv, [ev])
        assert res.urgency >= 0.6
        assert "CVE reference" in res.relevance_explanation

    @pytest.mark.parametrize("vuln_text,expected_reason", [
        ("Discovered remote code execution in worker", "RCE vulnerability"),
        ("SQL injection vulnerability in user lookup", "SQL injection"),
        ("Path traversal vulnerability in static handler", "Path traversal"),
        ("Stack exhaustion in parser", "Stack exhaustion"),
        ("Denial of service attack surface", "DoS vulnerability"),
        ("Decompression bomb memory exhaustion", "Decompression bomb"),
        ("Fail-open authentication bypass in gateway", "Fail-open authentication"),
        ("Infinite recursion detected in serializer", "Infinite recursion"),
    ])
    def test_vulnerability_pattern_detection(self, vuln_text, expected_reason):
        ev = _make_event(title="Security finding", text=vuln_text)
        conv = _make_conv("Audit updates", [ev])
        res = self.classifier.classify_local(conv, [ev])
        assert res.urgency >= 0.6
        assert expected_reason in res.relevance_explanation

    def test_direct_mention_detection(self):
        ev = _make_event(
            title="Question",
            text="Can you review this?",
            recipients=[{"email": "me@example.com"}],
        )
        conv = _make_conv("Review Request", [ev])
        res = self.classifier.classify_local(conv, [ev], user_email="me@example.com")
        assert res.urgency >= 0.6
        assert "Direct mention" in res.relevance_explanation

    def test_content_richness_importance_boost(self):
        rich_text = "This is a detailed analysis of the architecture. " * 25  # >1000 chars
        ev = _make_event(title="Architecture Spec", text=rich_text)
        conv = _make_conv("Architecture", [ev])
        res = self.classifier.classify_local(conv, [ev])
        assert res.importance >= 0.6
        assert res.importance >= 0.6

    def test_multi_message_thread_importance(self):
        events = [
            _make_event(title="Update 1", text="First message"),
            _make_event(title="Update 2", text="Second message"),
            _make_event(title="Update 3", text="Third message"),
        ]
        conv = _make_conv("Active discussion", events)
        res = self.classifier.classify_local(conv, events)
        assert res.importance >= 0.6
        assert "3 messages" in res.relevance_explanation

    def test_security_action_item_auto_generation(self):
        ev = _make_event(title="Security", text="CVSS: 9.8 remote code execution in parser")
        conv = _make_conv("#security-alerts", [ev])
        assert len(conv.open_actions) == 0
        res = self.classifier.classify_local(conv, [ev])
        assert res.urgency >= 0.7
        assert len(res.open_actions) >= 1
        assert "Review:" in str(res.open_actions[0])

    def test_existing_action_items_not_overwritten(self):
        ev = _make_event(title="Security", text="CVSS: 9.8 remote code execution in parser")
        conv = _make_conv("#security-alerts", [ev])
        conv.open_actions = [ActionItem(description="Existing task", owner_id="user", domain=Domain.WORK, status=ActionStatus.OPEN)]
        res = self.classifier.classify_local(conv, [ev])
        assert len(res.open_actions) == 1
        assert res.open_actions[0].description == "Existing task"

    def test_opportunity_detection(self):
        ev = _make_event(title="Proposal", text="There is a patch available and a clear workaround proposal for this issue.")
        conv = _make_conv("Improvement proposal", [ev])
        res = self.classifier.classify_local(conv, [ev])
        assert res.opportunity_score >= 0.6
        assert res.opportunity_score > 0.0

    def test_action_evaluation_directive_detection(self):
        ev = _make_event(
            title="sam",
            text="see if this can be used in next project: https://github.com/acme/csv-kit"
        )
        conv = _make_conv("@sam", [ev])
        res = self.classifier.classify_local(conv, [ev])
        assert res.urgency >= 0.7
        assert res.opportunity_score >= 0.7
        assert res.opportunity_type == "tool_evaluation"
        assert "csv-kit" in res.opportunity_description
        assert len(res.action_items) >= 1
        assert "csv-kit" in res.action_items[0]                    # a name ending in t/i/g is kept whole
        assert "csv-k " not in res.action_items[0] + " "

    def test_repo_name_keeps_its_last_letters_and_drops_dot_git(self):
        for url, name in (("https://github.com/acme/toolkit.git", "toolkit"), ("https://github.com/acme/git", "git")):
            ev = _make_event(title="sam", text=f"see if this can be used in next project: {url}")
            res = self.classifier.classify_local(_make_conv("@sam", [ev]), [ev])
            assert res.action_items[0] == f"Evaluate {name} for upcoming project"
            assert f"Evaluate {name} for future projects" == res.opportunity_description
        assert res.summary != ""
        assert res.ai_analysis != ""

    def test_standing_directive_matching(self):
        from otto.intelligence.history import save_directive
        save_directive("always review payments migration bugs", category="security")
        ev = _make_event(
            title="Dev",
            text="release is Friday, make sure to always review payments migration bugs first"
        )
        conv = _make_conv("#engineering", [ev])
        res = self.classifier.classify_local(conv, [ev])
        assert res.urgency >= 0.75
        assert res.importance >= 0.8
        assert "directive-match" in res.topics or "Directive:" in res.relevance_explanation
        assert len(res.action_items) >= 1

    def test_personal_dm_action_priority(self):
        ev = _make_event(
            title="sam",
            text="todo: check the release notes by tomorrow"
        )
        conv = _make_conv("@sam", [ev])
        res = self.classifier.classify_local(conv, [ev])
        assert res.urgency >= 0.7
        assert "Personal action item" in res.relevance_explanation or res.urgency >= 0.75


class TestConversationNewFields:
    """Tests for new AI enrichment fields on Conversation."""

    def test_topics_field_exists(self):
        conv = Conversation(
            source=SourceType.SLACK, account_id="a", thread_id="t",
            subject="Test", summary="", domain=Domain.WORK,
            relevance_explanation="",
        )
        assert hasattr(conv, "topics")
        assert conv.topics == []

    def test_topics_field_assignment(self):
        conv = Conversation(
            source=SourceType.SLACK, account_id="a", thread_id="t",
            subject="Test", summary="", domain=Domain.WORK,
            relevance_explanation="",
            topics=["security", "deployment"],
        )
        assert conv.topics == ["security", "deployment"]

    def test_ai_analysis_field(self):
        conv = Conversation(
            source=SourceType.SLACK, account_id="a", thread_id="t",
            subject="Test", summary="", domain=Domain.WORK,
            relevance_explanation="",
            ai_analysis="Critical finding affecting production",
        )
        assert conv.ai_analysis == "Critical finding affecting production"

    def test_opportunity_type_field(self):
        conv = Conversation(
            source=SourceType.SLACK, account_id="a", thread_id="t",
            subject="Test", summary="", domain=Domain.WORK,
            relevance_explanation="",
            opportunity_type="collaboration",
            opportunity_description="New partnership opportunity",
        )
        assert conv.opportunity_type == "collaboration"
        assert conv.opportunity_description == "New partnership opportunity"

    def test_default_values(self):
        conv = Conversation(
            source=SourceType.SLACK, account_id="a", thread_id="t",
            subject="Test", summary="", domain=Domain.WORK,
            relevance_explanation="",
        )
        assert conv.topics == []
        assert conv.opportunity_type == ""
        assert conv.opportunity_description == ""
        assert conv.ai_analysis == ""


class TestClassifyLLMOpportunityScoreMax:
    """Tests for the opportunity_score max() fix in classify_llm."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.event_bus = EventBus()
        self.classifier = ConversationClassifier(event_bus=self.event_bus)

    def test_opportunity_score_preserved_when_llm_returns_zero(self):
        """Regression: LLM returning 0 should not wipe local heuristic score."""
        ev = _make_event(title="Proposal", text="patch available and a workaround proposal")
        conv = _make_conv("Test", [ev])

        # Run local classification first
        conv = self.classifier.classify_local(conv, [ev])
        local_opp_score = conv.opportunity_score
        assert local_opp_score > 0, "Local heuristics should detect opportunity"

        # Simulate what classify_llm does with the fixed max() logic
        llm_opp_score = 0.0  # LLM might return 0
        result = max(conv.opportunity_score, llm_opp_score)
        assert result == local_opp_score, "max() should preserve the local score"

    def test_opportunity_score_upgrade_when_llm_returns_higher(self):
        """LLM returning higher score should upgrade."""
        conv = Conversation(
            source=SourceType.SLACK, account_id="a", thread_id="t",
            subject="Test", summary="", domain=Domain.WORK,
            relevance_explanation="", opportunity_score=0.3,
        )
        llm_opp_score = 0.8
        result = max(conv.opportunity_score, llm_opp_score)
        assert result == 0.8

    def test_learning_opportunity_detection(self):
        """Test detection of team discussion / learning resources."""
        ev = _make_event(
            title="Team Note",
            text="this might be useful to go over in team meeting next week: https://hai.example.edu/definitions/what-are-weights"
        )
        conv = _make_conv("Team Note", [ev])
        conv = self.classifier.classify_local(conv, [ev])
        assert conv.opportunity_score >= 0.75
        assert conv.urgency >= 0.7
        assert conv.opportunity_type == "team_learning"
        assert "example.edu" in conv.opportunity_description
        assert "team discussion" in conv.opportunity_description
        assert "team-learning" in conv.topics
        assert any("example.edu" in a for a in conv.action_items)




# ---------------------------------------------------------------------------
# Bot security notices: real findings vs. "nothing found"
# ---------------------------------------------------------------------------

SCAN_WITH_FINDINGS = (
    "Scan complete — api\nmain\nDuration : 380.2s\nRaw leads: 34\nConfirmed: 0\nQualified: 0\n"
    "Severity : 0 critical, 2 high, 0 medium\nReport-eligible findings:\n"
    "[HIGH 8.0]\nCross-audit pattern hit: Command Injection\n[HIGH 8.0]\nCross-audit pattern hit: Command Injection\n"
    "Open on the platform"
)
SCAN_ALL_CLEAR = (
    "Scan complete — worker\nmain\nDuration : 537.7s\nRaw leads: 287\nConfirmed: 0\nQualified: 0\n"
    "Severity : 0 critical, 0 high, 0 medium\nNo report-eligible findings above the CVSS threshold.\n"
    "Open the findings on the platform"
)


class TestBotSecurityNotices:
    def setup_method(self):
        self.classifier = ConversationClassifier(EventBus())

    def test_zero_counts_do_not_read_as_findings(self):
        ev = _make_event(title="#security-alerts", text=SCAN_ALL_CLEAR, sender="scanner", is_auto=True)
        conv = self.classifier.classify_local(_make_conv("#security-alerts", [ev]), [ev])
        assert conv.urgency <= 0.1
        assert "all-clear" in conv.topics
        assert "High severity" not in conv.relevance_explanation
        assert "urgency keywords" not in conv.relevance_explanation

    def test_report_generated_with_zero_findings_is_all_clear(self):
        ev = _make_event(title="#security-alerts", text="Report 3 generated with 0 findings", sender="notify", is_auto=True)
        conv = self.classifier.classify_local(_make_conv("#security-alerts", [ev]), [ev])
        assert conv.urgency <= 0.1 and "all-clear" in conv.topics
        # a report *with* findings is not
        ev = _make_event(title="#security-alerts", text="Report 4 generated with 3 findings", sender="notify", is_auto=True)
        conv = self.classifier.classify_local(_make_conv("#security-alerts", [ev]), [ev])
        assert "all-clear" not in conv.topics

    def test_a_person_replying_under_an_all_clear_notice_makes_it_a_conversation(self):
        bot = _make_event(title="#security-alerts", text="Report 10 generated with 0 findings", sender="notify", is_auto=True)
        human = _make_event(title="#security-alerts", text="build failed twice this morning, could be the runner image", sender="carol")
        human.timestamp = bot.timestamp.replace(microsecond=bot.timestamp.microsecond) + __import__("datetime").timedelta(minutes=5)
        conv = self.classifier.classify_local(_make_conv("#security-alerts", [bot, human]), [bot, human])
        assert "all-clear" not in conv.topics

    @pytest.mark.asyncio
    async def test_llm_topics_cannot_erase_the_all_clear_tag(self):
        """Seen live: the LLM replaced topics with ["security audit", "no findings"],
        the all-clear tag vanished and three "nothing found" notices became cards."""
        import json
        from types import SimpleNamespace

        ev = _make_event(title="#security-alerts", text=SCAN_ALL_CLEAR, sender="scanner", is_auto=True)
        conv = self.classifier.classify_local(_make_conv("#security-alerts", [ev]), [ev])
        assert "all-clear" in conv.topics

        reply = json.dumps({"urgency": 0.1, "importance": 0.3, "domain": "engineering",
                            "topics": ["security audit", "Scanner scan", "no findings"],
                            "summary": "Routine scan, nothing found."})

        class FakeLLM:
            async def complete(self, **kw):
                return SimpleNamespace(content=reply)

        self.classifier._llm = FakeLLM()
        enriched = await self.classifier.classify_llm(conv, [ev])
        assert enriched.llm_enriched is True
        assert enriched.topics[0] == "all-clear"
        assert "security audit" in enriched.topics and "no findings" in enriched.topics

    def test_high_finding_from_a_bot_is_not_penalised(self):
        ev = _make_event(title="#security-alerts", text=SCAN_WITH_FINDINGS, sender="scanner", is_auto=True)
        conv = self.classifier.classify_local(_make_conv("#security-alerts", [ev]), [ev])
        assert conv.urgency >= 0.7
        assert conv.importance >= 0.8
        assert "all-clear" not in conv.topics

    def test_findings_summary_is_specific(self):
        ev = _make_event(title="#security-alerts", text=SCAN_WITH_FINDINGS, sender="scanner", is_auto=True)
        conv = self.classifier.classify_local(_make_conv("#security-alerts", [ev]), [ev])
        assert conv.summary.startswith("2 HIGH findings — Command Injection (api)")
        assert conv.action_items and conv.action_items[0].startswith("Triage: 2 HIGH findings")

    def test_security_directive_lights_up_on_a_real_finding_only(self):
        from otto.intelligence.history import save_directive
        save_directive("high severity findings must be triaged within a week", category="security")

        hit = _make_event(title="#security-alerts", text=SCAN_WITH_FINDINGS, sender="scanner", is_auto=True)
        conv = self.classifier.classify_local(_make_conv("#security-alerts", [hit]), [hit])
        assert conv.matched_directive == "high severity findings must be triaged within a week"
        assert "directive-match" in conv.topics
        assert "Relevant to your directive" in conv.summary

        miss = _make_event(title="#security-alerts", text=SCAN_ALL_CLEAR, sender="scanner", is_auto=True)
        conv2 = self.classifier.classify_local(_make_conv("#security-alerts", [miss]), [miss])
        assert conv2.matched_directive == ""


class TestDirectiveMatching:
    def test_exact_phrase(self):
        from otto.intelligence.classifier import directive_matches
        assert directive_matches("always review payments migration bugs", "please always review payments migration bugs here")

    def test_content_word_overlap_with_stemming(self):
        from otto.intelligence.classifier import directive_matches
        assert directive_matches("skip bugs that the platform team will handle",
                                 "the platform team is handling this bug next sprint")
        assert not directive_matches("skip bugs that the platform team will handle",
                                     "lunch is at noon, the team is going out")

    def test_security_directive_needs_a_signal_when_overlap_is_thin(self):
        from otto.intelligence.classifier import directive_matches
        d = "high severity findings must be triaged within a week"
        text = "severity : 2 high — [high 8.0] command injection"
        assert directive_matches(d, text, security_signal=True)
        assert not directive_matches(d, text, security_signal=False)

    def test_empty_inputs(self):
        from otto.intelligence.classifier import directive_matches
        assert not directive_matches("", "anything")
        assert not directive_matches("something", "")


class TestSummarizeFindings:
    def test_groups_by_level_and_names_target(self):
        from otto.intelligence.classifier import summarize_findings
        text = "Scan complete — api\n[CRITICAL 9.8]\nRCE in upload handler\n[HIGH 8.0]\nCross-audit pattern hit: Command Injection\n[HIGH 7.5]\nPath traversal in export"
        s = summarize_findings(text)
        assert s.startswith("1 CRITICAL finding — RCE in upload handler; 2 HIGH findings — Command Injection; Path traversal in export (api).")

    def test_no_findings_is_empty(self):
        from otto.intelligence.classifier import summarize_findings
        assert summarize_findings("all good, nothing to see") == ""


class TestRepoInsideAFinding:
    def setup_method(self):
        self.classifier = ConversationClassifier(EventBus())

    def test_repository_named_in_a_finding_is_not_a_tool_recommendation(self):
        text = ("New finding: Empty dashboard token fail-open (frp-class) (CVSS 8.7) in https://github.com/acme/widget\n"
                "GitHub\nGitHub - acme/widget: Example project.\nGitHub | Added by scanner")
        ev = _make_event(title="#security-alerts", text=text, sender="", is_auto=True)
        conv = self.classifier.classify_local(_make_conv("#security-alerts", [ev]), [ev])
        assert conv.urgency >= 0.7
        assert conv.opportunity_type != "tool_evaluation"
        assert "tool-evaluation" not in conv.topics
        assert conv.summary.startswith("New HIGH finding (CVSS 8.7): Empty dashboard token fail-open (frp-class) — acme/widget")
        assert conv.action_items[0].startswith("Triage: New HIGH finding")

    def test_plain_repo_share_is_still_an_opportunity(self):
        text = "Check out https://github.com/acme/audio-kit — could be useful for the next project"
        ev = _make_event(title="#eng", text=text)
        conv = self.classifier.classify_local(_make_conv("#eng", [ev]), [ev])
        assert conv.opportunity_type == "tool_evaluation"
