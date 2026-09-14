from __future__ import annotations

"""
Conversation-level classification pipeline.

Subscribes to NewEventsIngested and runs:
1. Local heuristics (instant, always available)
2. LLM enrichment (async, budget-gated)

Classification operates at the CONVERSATION level, not event level.
"""

import json
import logging
import re
from typing import Any

from otto.core.event_bus import EventBus
from otto.llm.gateway import LLMGateway
from otto.llm.injection_defense import sanitize_for_llm
from otto.llm.prompts import CLASSIFY_CONVERSATION, CLASSIFY_WITH_CONTEXT, EXTRACT_ACTIONS
from otto.llm.write_detector import scan_for_write_intent
from otto.storage.models import (
    ActionItem,
    ActionStatus,
    ClassificationComplete,
    Conversation,
    Domain,
    NewEventsIngested,
    NormalizedEvent,
    SourceType,
)
from otto.utils.language import has_urgency_keywords
from otto.utils.pii_redactor import redact_pii

logger = logging.getLogger("otto.intelligence.classifier")

# A conversation whose newest message is the user's own never rates above
# "medium" on its own: the briefing tells people what *others* did.
OWN_WORDS_URGENCY_CAP = 0.35
# Your habits (what you open, what you dismiss unread) move a score by at most
# this much, and never lower something a direct mention or an ask already put
# at or above HABIT_PROTECTED_URGENCY.
HABIT_MAX = 0.15
HABIT_PROTECTED_URGENCY = 0.6


def _user_names() -> frozenset[str]:
    """The user's known display names, lower-cased (see utils.identity); computed once per classification."""
    try:
        from otto.utils.identity import self_names
        return frozenset(n.strip().lower() for n in self_names() if n and n.strip())
    except Exception:
        return frozenset()


def _written_by_user(event: NormalizedEvent, names: frozenset[str]) -> bool:
    """True when the event's sender is the user (a known name, or the reader's own "You")."""
    who = (getattr(event, "sender", "") or "").strip().lower()
    return bool(who) and (who in ("you", "me") or who in names)


def _user_profile() -> dict[str, Any]:
    """``{"role", "focus"}`` from config (see utils.identity); empty when unset."""
    try:
        from otto.utils.identity import profile
        return profile()
    except Exception:
        return {"role": "", "focus": []}


# Messages about something the user said they own are worth a look even
# when nothing in the wording is urgent: they land in "For you", not the fold.
FOCUS_URGENCY_FLOOR = 0.45
FOCUS_IMPORTANCE_FLOOR = 0.6


def _extract_json(text: str) -> Any:
    """Safely parse JSON from LLM output, stripping markdown fences if present."""
    clean = text.strip()
    if clean.startswith("```"):
        lines = clean.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        clean = "\n".join(lines).strip()

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        first_brace = clean.find("{")
        last_brace = clean.rfind("}")
        first_bracket = clean.find("[")
        last_bracket = clean.rfind("]")

        candidates = []
        if first_brace != -1 and last_brace > first_brace:
            candidates.append((first_brace, clean[first_brace:last_brace + 1]))
        if first_bracket != -1 and last_bracket > first_bracket:
            candidates.append((first_bracket, clean[first_bracket:last_bracket + 1]))

        candidates.sort(key=lambda x: x[0])
        for _, snippet in candidates:
            try:
                return json.loads(snippet)
            except json.JSONDecodeError:
                pass
        raise


# The briefing talks *to* its reader. Models still slip into "the user's
# directive"; these rewrites (verb agreement included for the common cases)
# turn that back into "your directive" wherever LLM text reaches a surface.
_THIRD_PERSON = [
    (re.compile(r"\b(?:the|this) user's\b", re.IGNORECASE), "your"),
    (re.compile(r"\b(?:the|this) user (?:is|was)\b", re.IGNORECASE), "you are"),
    (re.compile(r"\b(?:the|this) user has\b", re.IGNORECASE), "you have"),
    (re.compile(r"\b(?:the|this) user does\b", re.IGNORECASE), "you do"),
    # "the user owns the api" → "you own the api". After "the user " (no
    # apostrophe) an s-word is a verb in practice; the few nouns a model might
    # put there are listed so they are left alone ("the user status").
    (re.compile(r"\b(?:the|this) user (\w{3,}s)\b", re.IGNORECASE), "you {verb}"),
    (re.compile(r"\b(?:the|this) user\b", re.IGNORECASE), "you"),
]

_NOT_VERBS = frozenset({
    "status", "focus", "access", "address", "progress", "process", "business", "class", "alias", "settings",
    "news", "bugs", "issues", "findings", "things", "items", "messages", "hours", "days", "weeks", "months",
    "years", "results", "requests", "changes", "tasks", "projects", "reports", "channels", "threads", "teams",
    "users", "systems", "services", "tools", "logs", "metrics", "alerts", "notes", "docs", "files", "tests",
    "deadlines", "plus", "thus", "always", "perhaps", "unless", "across", "towards", "besides", "whereas",
})


def _plural_to_base(verb: str) -> str:
    """Third-person singular → base form for the regular cases: needs→need, watches→watch, tries→try."""
    if verb.lower() in _NOT_VERBS:
        return verb
    low = verb.lower()
    if low.endswith(("sses", "shes", "ches", "xes", "zzes")):
        return verb[:-2]
    if low.endswith("ies") and len(low) > 4:
        return verb[:-3] + "y"
    return verb[:-1]


def to_second_person(text: str) -> str:
    """``"the user's directive"`` → ``"your directive"``; capitalisation at a sentence start is kept."""
    if not text or "user" not in text.lower():
        return text
    out = text
    for pattern, repl in _THIRD_PERSON:
        def _sub(m: re.Match, repl: str = repl) -> str:
            new = repl.replace("{verb}", _plural_to_base(m.group(1))) if "{verb}" in repl else repl
            return new[0].upper() + new[1:] if m.group(0)[0].isupper() else new
        out = pattern.sub(_sub, out)
    return out


# ---------------------------------------------------------------------------
# Local heuristics shared by classify_local (kept module-level so they can be
# unit-tested without a classifier instance)
# ---------------------------------------------------------------------------

_ZERO_COUNT = re.compile(r'\b(?:0|zero|no)\s+(?:critical|high|medium|low|urgent|blocker)s?\b', re.IGNORECASE)
_ALL_CLEAR = re.compile(
    r'\bno (?:report-eligible |new |open )?(?:findings|issues|vulnerabilities|failures|errors)\b'
    r'|\b0 critical,?\s+0 high\b'
    r'|\b(?:0|zero) (?:new |open )?(?:findings|vulnerabilities)\b'          # "Report 3 generated with 0 findings"
    r'|\ball (?:checks|tests) passed\b'
    r'|\bnothing (?:to report|found)\b',
    re.IGNORECASE,
)
_FINDING_LINE = re.compile(r'\[(critical|high|medium)(?:\s+[\d.]+)?\]\s*([^\n\[]{4,120})', re.IGNORECASE)
_NEW_FINDING = re.compile(
    r'new finding:\s*(?P<what>[^\n]{4,160}?)\s*\((?:cvss|score)[:\s]*(?P<score>\d+(?:\.\d+)?)\)'
    r'(?:\s+in\s+(?:https?://)?(?:www\.)?github\.com/(?P<repo>[\w.-]+/[\w.-]+))?',
    re.IGNORECASE,
)
_DIRECTIVE_STOPWORDS = {
    "the", "a", "an", "to", "of", "and", "or", "that", "this", "these", "those", "do", "not",
    "will", "make", "sure", "always", "never", "look", "at", "get", "gets", "be", "is", "are",
    "was", "were", "for", "in", "on", "with", "within", "by", "it", "we", "you", "your", "our",
    "if", "any", "all", "as", "from", "so", "can", "should", "must", "please", "when", "into",
    "about", "up", "out", "also", "very", "just", "than", "then", "there", "their", "them",
    "they", "who", "what", "which", "keep", "check", "watch", "want", "need", "like", "have",
    "has", "had", "does", "did", "done", "let", "me", "my", "i", "us",
}
_SECURITY_DIRECTIVE = re.compile(r'\b(?:severity|vuln\w*|cve|security|bugs?|exploit\w*|findings?)\b', re.IGNORECASE)

# Tags set by the local pass that the UI/noise filter depends on. An LLM
# reply replaces the descriptive topics but never these.
STICKY_TOPICS = frozenset({"all-clear", "slack-ui-noise", "ui-artifact", "directive-match", "your-message", "your-focus",
                           "habit-skip", "habit-open"})


def _neutralize_zero_counts(text: str) -> str:
    """Drop "0 critical" / "no high" style phrases so they cannot read as findings."""
    return _ZERO_COUNT.sub(" ", text or "")


def _is_all_clear_notice(text: str) -> bool:
    """A routine "scan complete, nothing found" style message."""
    return bool(_ALL_CLEAR.search(text or ""))


def _stem(word: str) -> str:
    for suffix in ("ies", "ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def _content_terms(text: str) -> set[str]:
    return {
        _stem(w) for w in re.findall(r"[a-z][a-z0-9]+", (text or "").lower())
        if w not in _DIRECTIVE_STOPWORDS and len(w) > 2
    }


def directive_matches(directive: str, text: str, *, security_signal: bool = False) -> bool:
    """
    Does *text* relate to a standing directive?

    The directive's content words (stemmed, stop-words removed) must overlap
    the text: at least two of them and at least half of them. A directive
    about severity / bugs / vulnerabilities also matches any conversation
    that carries a real HIGH/CRITICAL security signal and shares one term —
    "high severity bugs …" should light up on a HIGH finding.
    """
    d = (directive or "").lower().strip()
    t = (text or "").lower()
    if not d or not t:
        return False
    if d in t:
        return True
    terms = _content_terms(d)
    if not terms:
        return False
    overlap = terms & _content_terms(t)
    if len(terms) == 1:
        return bool(overlap)
    needed = max(2, (len(terms) + 1) // 2)
    if len(overlap) >= needed:
        return True
    return bool(security_signal and overlap and _SECURITY_DIRECTIVE.search(d))


def summarize_findings(text: str) -> str:
    """
    One line for a security notice: ``"2 HIGH findings — Command Injection (api)"``.
    Empty when the text has no bracketed severity lines.
    """
    single = _NEW_FINDING.search(text or "")
    found = _FINDING_LINE.findall(text or "")
    if single and not found:
        score = float(single.group("score"))
        level = "CRITICAL" if score >= 9.0 else "HIGH" if score >= 7.0 else "MEDIUM" if score >= 4.0 else "LOW"
        what = single.group("what").strip(" .:-—")
        repo = single.group("repo")
        return f"New {level} finding (CVSS {score:g}): {what}" + (f" — {repo}" if repo else "") + "."
    if not found:
        return ""
    by_level: dict[str, list[str]] = {}
    for level, desc in found:
        desc = re.sub(r'^\s*cross-audit pattern hit:\s*', '', desc.strip(), flags=re.IGNORECASE).strip(" .:-—")
        by_level.setdefault(level.upper(), []).append(desc)
    order = [lvl for lvl in ("CRITICAL", "HIGH", "MEDIUM") if lvl in by_level]
    parts = []
    for lvl in order:
        descs = by_level[lvl]
        unique = list(dict.fromkeys(descs))
        noun = "finding" if len(descs) == 1 else "findings"
        parts.append(f"{len(descs)} {lvl} {noun} — {'; '.join(unique[:3])}")
    target = re.search(r'scan complete\s*[—-]\s*([^\n]{1,40})', text or "", re.IGNORECASE)
    suffix = f" ({target.group(1).strip()})" if target else ""
    return "; ".join(parts) + suffix + "."


class ConversationClassifier:
    """
    Two-stage conversation classification pipeline.

    Stage 1 (Local Heuristics — instant, always available):
      - Urgency keyword boost (multi-language)
      - Sender importance (learned weights)
      - Thread participation analysis (To/CC/@-mention)
      - Auto-generated message detection
      - Statistical patterns from history

    Stage 2 (LLM Enrichment — async, budget-gated):
      - Full classification (urgency, importance, opportunity, domain)
      - Relevance explanation
      - Action item extraction
      - Conversation summary update
    """

    def __init__(
        self,
        event_bus: EventBus,
        llm_gateway: LLMGateway | None = None,
        db: Any = None,
    ) -> None:
        self.event_bus = event_bus
        self._llm = llm_gateway
        self._db = db
        self._sender_weights: dict[str, float] = {}
        self._habits: Any = None          # FeedbackPriors from the knowledge store, per refresh

    def set_habits(self, priors: Any) -> None:
        """What you did with earlier items (opened / dismissed / snoozed), as channel and sender priors."""
        self._habits = priors

    def habit_adjustment(self, channel: str, sender: str) -> float:
        """
        Signed nudge in [-HABIT_MAX, +HABIT_MAX] from your habits for this channel/sender.

        Channels weigh more than people (a person can be interesting in one
        channel and noisy in another). Returns 0.0 when nothing is known.
        """
        priors = self._habits
        if priors is None:
            return 0.0
        try:
            c = float(priors.channel(channel or ""))
            s = float(priors.sender(sender or ""))
        except Exception:
            return 0.0
        raw = 0.65 * c + 0.35 * s
        return max(-HABIT_MAX, min(HABIT_MAX, HABIT_MAX * raw))

    async def start(self) -> None:
        """Subscribe to ingestion events."""
        await self.event_bus.subscribe(NewEventsIngested, self._on_new_events)
        logger.info("Classifier subscribed to NewEventsIngested")

    async def stop(self) -> None:
        await self.event_bus.unsubscribe(NewEventsIngested, self._on_new_events)

    async def _on_new_events(self, event: NewEventsIngested) -> None:
        """Handler for new events — triggers classification."""
        processed_ids = []
        for event_id in event.event_ids:
            try:
                await self._classify_event(event_id, event.source)
                processed_ids.append(event_id)
            except Exception as e:
                logger.error("Classification failed for %s: %s", event_id, e)

        if processed_ids:
            await self.event_bus.publish(
                ClassificationComplete(event_ids=processed_ids, conversation_ids=[])
            )

    async def _classify_event(self, event_id: str, source: SourceType) -> None:
        """Classify a single event (or its parent conversation)."""
        logger.debug("Classified event %s from %s", event_id, source.value)

    def classify_local(
        self,
        conversation: Conversation,
        events: list[NormalizedEvent],
        user_email: str = "",
    ) -> Conversation:
        """
        Stage 1: Local heuristic classification (instant, no LLM).

        Applies security-aware, context-sensitive classification.
        Modifies the conversation in place and returns it.
        """
        import re as _re

        if not events:
            return conversation

        latest = max(events, key=lambda e: e.timestamp)
        original_text = " ".join(
            (e.plain_text_extract or "") + " " + (e.title or "")
            for e in events
        )
        raw_all_text = original_text.lower()
        # "0 critical, 0 high" is the *absence* of a finding — keep the zero
        # counts from lighting up the severity / urgency keywords below.
        all_text = _neutralize_zero_counts(raw_all_text)
        # "Nothing found" is only routine while it is the bot talking: a person
        # replying under such a notice makes it a conversation.
        all_clear = _is_all_clear_notice(raw_all_text) and (latest.is_auto_generated or not (latest.sender or "").strip())

        # --- Urgency, Importance, and Opportunity Detection ---
        urgency_boost = 0.0
        urgency_reasons: list[str] = []
        importance_boost = 0.0
        importance_reasons: list[str] = []
        opportunity_score = 0.0

        # Standard urgency keywords
        if has_urgency_keywords(_neutralize_zero_counts(latest.plain_text_extract or ""), latest.content_language):
            urgency_boost = max(urgency_boost, 0.3)
            urgency_reasons.append("urgency keywords")

        # Security severity detection (CVSS, HIGH/CRITICAL)
        cvss_match = _re.search(r'\b(?:cvss|score)[:\s]*(\d+\.?\d*)', all_text)
        if cvss_match:
            score = float(cvss_match.group(1))
            if score >= 9.0:
                urgency_boost = max(urgency_boost, 0.9)
                urgency_reasons.append(f"CVSS {score} (Critical)")
            elif score >= 7.0:
                urgency_boost = max(urgency_boost, 0.7)
                urgency_reasons.append(f"CVSS {score} (High)")
            elif score >= 4.0:
                urgency_boost = max(urgency_boost, 0.4)
                urgency_reasons.append(f"CVSS {score} (Medium)")

        # Severity tags: [HIGH 7.8], [CRITICAL], etc.
        severity_patterns = [
            (r'\[critical\b', 0.9, "Critical severity"),
            (r'\[high\s+\d', 0.7, "High severity finding"),
            (r'\[high\]', 0.7, "High severity"),
            (r'\[medium\b', 0.4, "Medium severity"),
            (r'\bcve-\d{4}-\d+', 0.6, "CVE reference"),
            (r'\brce\b|\bremote code execution\b', 0.9, "RCE vulnerability"),
            (r'\bsql injection\b|\bsqli\b', 0.8, "SQL injection"),
            (r'\bpath traversal\b|\bdirectory traversal\b', 0.7, "Path traversal"),
            (r'\bstack exhaustion\b|\bstack overflow\b', 0.6, "Stack exhaustion"),
            (r'\bdenial.?of.?service\b|\bdos\b', 0.6, "DoS vulnerability"),
            (r'\bdecompression bomb\b|\bzip bomb\b', 0.7, "Decompression bomb"),
            (r'\bmemory exhaustion\b|\boom\b', 0.6, "Memory exhaustion"),
            (r'\bfail.?open\b', 0.8, "Fail-open authentication"),
            (r'\binfinite\s+recursion\b|\binfinite\s+loop\b', 0.6, "Infinite recursion"),
        ]
        # Unbracketed severity and time-bound patterns
        additional_severity_patterns = [
            (r'\bhigh\s+severity\b', 0.75, "High severity"),
            (r'\bcritical\s+severity\b', 0.9, "Critical severity"),
            (r'\bmedium\s+severity\b', 0.4, "Medium severity"),
            (r'\bfixed\s+within\s+\d+\s+(?:days?|weeks?)\b', 0.75, "Time-bound fix requirement"),
        ]
        all_severity_patterns = severity_patterns + additional_severity_patterns
        matched_security = bool(cvss_match)
        for pattern, boost, reason in all_severity_patterns:
            if _re.search(pattern, all_text):
                matched_security = True
                if boost > urgency_boost:
                    urgency_boost = boost
                    urgency_reasons.append(reason)

        findings = summarize_findings(original_text) if matched_security else ""

        # Standing Directives cross-referencing
        matched_directive = None
        try:
            from otto.intelligence.history import load_directives
            directives = load_directives()
            for d in directives:
                d_text = (d.get("directive", "")).lower()
                if directive_matches(d_text, all_text, security_signal=matched_security and urgency_boost >= 0.7):
                    matched_directive = d.get("directive", "")
                    urgency_boost = max(urgency_boost, 0.8)
                    importance_boost = max(importance_boost, 0.85)
                    urgency_reasons.append(f"Directive: {matched_directive[:40]}")
                    if "directive-match" not in conversation.topics:
                        conversation.topics.append("directive-match")
                    cat = d.get("category", "")
                    if cat and cat not in conversation.topics:
                        conversation.topics.append(cat)
                    break
        except Exception:
            pass

        conversation.matched_directive = matched_directive or ""

        # The things the user said they own or watch ([user] focus).
        try:
            from otto.intelligence.relevance import focus_hits
            hits = focus_hits(original_text, _user_profile().get("focus") or [])
        except Exception:
            hits = []
        if hits:
            urgency_boost = max(urgency_boost, FOCUS_URGENCY_FLOOR)
            importance_boost = max(importance_boost, FOCUS_IMPORTANCE_FLOOR)
            importance_reasons.append(f"Your focus: {hits[0]}")
            if "your-focus" not in conversation.topics:
                conversation.topics.append("your-focus")

        # Action Directives & Evaluation Requests
        action_patterns = [
            (r'\bsee\s+if\s+(?:this|it|we)\s+can\s+be\s+used\b', 0.75, "Project evaluation request"),
            (r'\bsee\s+if\b', 0.7, "Action item: see if"),
            (r'\bcheck\s+(?:if|out|whether)\b', 0.7, "Action item: check"),
            (r'\blook\s+into\b', 0.7, "Action item: look into"),
            (r'\binvestigate\b', 0.7, "Action item: investigate"),
            (r'\bevaluate\b', 0.7, "Action item: evaluate"),
            (r'\bexplore\b', 0.65, "Action item: explore"),
            (r'\btry\s+(?:out|this)\b', 0.7, "Action item: try out"),
            (r'\btest\s+(?:if|whether|this|out)\b', 0.7, "Action item: test"),
            (r'\bmake\s+sure\b', 0.75, "Directive: make sure"),
            (r'\bremember\s+to\b', 0.7, "Action reminder: remember to"),
            (r'\bdon\'?t\s+forget\b', 0.7, "Action reminder: don't forget"),
            (r'\btodo\b', 0.75, "To-do item"),
            (r'\baction\s+item\b', 0.75, "Action item"),
            (r'\bplease\s+(?:review|check|test|fix|update|look)\b', 0.7, "Review request"),
            (r'\bnext\s+project\b', 0.7, "Next project consideration"),
            (r'\bfor\s+(?:the\s+)?next\s+project\b', 0.75, "Next project requirement"),
            (r'\buseful\s+to\s+go\s+over\b', 0.75, "Meeting discussion proposal"),
            (r'\bteam\s+meeting\b', 0.75, "Team meeting agenda item"),
            (r'\bgo\s+over\s+in\b', 0.7, "Discussion item"),
            (r'\bnext\s+week\b', 0.7, "Upcoming schedule"),
            (r'\bby\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|eod|tomorrow)\b', 0.7, "Deadline specified"),
        ]
        has_action = False
        for pattern, boost, reason in action_patterns:
            if _re.search(pattern, all_text):
                has_action = True
                if boost > urgency_boost:
                    urgency_boost = boost
                    urgency_reasons.append(reason)
                importance_boost = max(importance_boost, boost)

        # Developer / Tool / Repository Opportunities. A repository named inside
        # a security finding is the *subject* of the finding, not a tool to try.
        gh_match = _re.search(r'github\.com\/([a-zA-Z0-9_\-]+)\/([a-zA-Z0-9_\-\.]+)', all_text)
        if gh_match and matched_security:
            gh_match = None
        repo_name = ""
        if gh_match:
            repo_name = gh_match.group(2)
            if repo_name.endswith(".git"):
                repo_name = repo_name[:-4]
            eval_indicator = bool(_re.search(
                r'\b(can\s+be\s+used|used\s+in|project|cool|player|function|feature|check|try|tool|library|framework|awesome|recommend|promising)\b',
                all_text
            ))
            score = 0.75 if eval_indicator else 0.5
            opportunity_score = max(opportunity_score, score)
            urgency_boost = max(urgency_boost, 0.75 if eval_indicator else 0.5)
            importance_boost = max(importance_boost, 0.75)
            if eval_indicator:
                urgency_reasons.append(f"Tool candidate: {repo_name}")
            conversation.opportunity_type = "tool_evaluation"
            conversation.opportunity_description = f"Evaluate {repo_name} for future projects"
            if "tool-evaluation" not in conversation.topics:
                conversation.topics.append("tool-evaluation")
            if "github" not in conversation.topics:
                conversation.topics.append("github")

        # Learning / reading material shared for discussion (papers, articles, talks)
        learning_url = _re.search(
            r'https?://(?:[a-z0-9-]+\.)*(arxiv\.org|paperswithcode\.com|[a-z0-9-]+\.edu|openreview\.net|acm\.org|ieee\.org)\S*',
            all_text,
        )
        learning_words = bool(_re.search(
            r'\b(?:paper|article|blog\s*post|talk|reading|worth\s+(?:a\s+)?(?:read|look|watch))\b', all_text
        ))
        discussion_intent = bool(_re.search(
            r'\b(?:go\s+over|discuss|team\s+meeting|reading\s+group|share\s+with\s+the\s+team|useful\s+to)\b', all_text
        ))
        learning_match = bool(learning_url) or (learning_words and discussion_intent)
        learning_source = learning_url.group(1) if learning_url else "shared article"
        if learning_match:
            score = 0.75 if discussion_intent else 0.55
            opportunity_score = max(opportunity_score, score)
            urgency_boost = max(urgency_boost, score)
            importance_boost = max(importance_boost, 0.7)
            urgency_reasons.append(f"Learning resource: {learning_source}")
            conversation.opportunity_type = "team_learning"
            conversation.opportunity_description = (
                f"Review the {learning_source} material"
                + (" before the team discussion" if discussion_intent else "")
            )
            if "team-learning" not in conversation.topics:
                conversation.topics.append("team-learning")

        # Direct message / personal space note awareness
        subj = (conversation.subject or "").lower()
        is_personal_dm = subj.startswith(('@', 'dm-'))
        if is_personal_dm and (has_action or gh_match or matched_directive):
            urgency_boost = max(urgency_boost, 0.75)
            importance_boost = max(importance_boost, 0.75)
            if "Personal action item" not in urgency_reasons:
                urgency_reasons.append("Personal action item")

        # Direct message / mention
        if user_email:
            for event in events:
                recips = getattr(event, "recipients", [])
                if any(
                    (r.get("email", "") if isinstance(r, dict) else str(r)).lower() == user_email.lower()
                    for r in recips
                ):
                    urgency_boost = max(urgency_boost, 0.6)
                    urgency_reasons.append("Direct mention")

        # --- Additional Importance Detection ---

        # Sender importance
        sender_boost = 0.0
        for event in events:
            weight = self._sender_weights.get(event.account_id, 0.0)
            sender_boost = max(sender_boost, weight)
        if sender_boost > 0:
            importance_boost = max(importance_boost, sender_boost)
            importance_reasons.append("Known sender")

        # Content richness = more important
        total_text = sum(len(e.plain_text_extract or "") for e in events)
        if total_text > 500:
            importance_boost = max(importance_boost, 0.5)
        if total_text > 1000:
            importance_boost = max(importance_boost, 0.6)

        # Thread with multiple messages = important
        if len(events) >= 3:
            importance_boost = max(importance_boost, 0.6)
            importance_reasons.append(f"{len(events)} messages")

        # Security findings are inherently important
        if matched_security:
            if urgency_boost >= 0.6:
                importance_boost = max(importance_boost, 0.8)
            importance_reasons.append("Security finding")

        # --- Opportunity Detection ---
        opportunity_patterns = [
            (r'\bopportunity\b|\bpotential\b', 0.4),
            (r'\bnew\s+finding\b|\bnew\s+issue\b', 0.5),
            (r'\bfix\s+available\b|\bpatch\s+available\b', 0.6),
            (r'\bworkaround\b|\bmitigation\b', 0.5),
            (r'\bproposal\b|\bsuggestion\b', 0.4),
        ]
        for pattern, score in opportunity_patterns:
            if _re.search(pattern, all_text):
                opportunity_score = max(opportunity_score, score)

        # --- Auto-generated penalty ---
        # Bots are how security findings arrive, so a real severity signal is
        # exempt; routine "scan complete, nothing found" notices are demoted.
        auto_count = sum(1 for e in events if e.is_auto_generated)
        if auto_count > len(events) * 0.5 and not matched_security:
            urgency_boost = max(0, urgency_boost - 0.2)
        if all_clear and not matched_security and not matched_directive:
            urgency_boost = min(urgency_boost, 0.1)
            if "all-clear" not in conversation.topics:
                conversation.topics.append("all-clear")

        # --- Your own words ---
        # The last thing said was said by the user: nothing to alert them to
        # (they know), unless a directive or a real severity signal is in play.
        own_words = _written_by_user(latest, _user_names()) and not matched_directive and not matched_security
        if own_words:
            urgency_boost = min(urgency_boost, OWN_WORDS_URGENCY_CAP)
            if "your-message" not in conversation.topics:
                conversation.topics.append("your-message")

        # --- Apply scores ---
        conversation.urgency = max(conversation.urgency, min(1.0, urgency_boost))
        if own_words:
            conversation.urgency = min(conversation.urgency, OWN_WORDS_URGENCY_CAP)
        conversation.importance = max(conversation.importance, min(1.0, importance_boost))

        # --- Your habits ---
        # What you did with earlier items from this channel and sender nudges
        # the score a little: a channel you keep opening rises, one you keep
        # dismissing unread sinks. A nudge never overrides a hard signal — a
        # real severity, a directive or a mention stays where it is.
        habit = self.habit_adjustment(conversation.subject or "", getattr(latest, "sender", "") or "")
        if habit and not matched_security and not matched_directive:
            if habit > 0 or urgency_boost < HABIT_PROTECTED_URGENCY:
                conversation.urgency = max(0.0, min(1.0, conversation.urgency + habit))
            conversation.importance = max(0.0, min(1.0, conversation.importance + habit))
            if habit < 0 and "habit-skip" not in conversation.topics:
                conversation.topics.append("habit-skip")
            elif habit > 0 and "habit-open" not in conversation.topics:
                conversation.topics.append("habit-open")
        conversation.opportunity_score = max(conversation.opportunity_score, opportunity_score)
        conversation.last_activity = latest.timestamp

        # Build intelligent relevance explanation
        reasons = urgency_reasons + importance_reasons
        if reasons:
            conversation.relevance_explanation = " · ".join(reasons[:3])
        elif conversation.summary:
            conversation.relevance_explanation = conversation.summary

        # Auto-generate action items
        if not conversation.action_items:
            if findings:
                conversation.action_items = [f"Triage: {findings.rstrip('.')}"]
            elif gh_match:
                conversation.action_items = [f"Evaluate {repo_name} for upcoming project"]
            elif learning_match:
                conversation.action_items = [conversation.opportunity_description or "Review the shared material"]
            elif matched_directive:
                conversation.action_items = [f"Act on directive: {matched_directive[:60]}"]
            elif urgency_boost >= 0.7:
                match_action = _re.search(r'(?:see\s+if|check\s+(?:if|out)|make\s+sure\s+to|remember\s+to|todo|useful\s+to\s+go\s+over)[:\s]+([^.!?\n]{5,80})', all_text)
                if match_action:
                    act_txt = match_action.group(0).strip()
                    conversation.action_items = [act_txt[0].upper() + act_txt[1:]]
                else:
                    conversation.action_items = [f"Review: {conversation.subject}"]

        if not conversation.open_actions and conversation.action_items:
            conversation.open_actions = list(conversation.action_items)
        elif urgency_boost >= 0.7 and not conversation.open_actions:
            conversation.open_actions = [
                f"Review: {conversation.subject}",
            ]

        # Local summary / AI analysis fallback when LLM is unavailable
        if not conversation.summary or conversation.summary.startswith("from ") or not conversation.ai_analysis:
            fallback_text = ""
            if findings:
                fallback_text = findings + (
                    f' Relevant to your directive: "{matched_directive}".' if matched_directive else ""
                )
            elif gh_match:
                fallback_text = f"Recommendation to evaluate {repo_name} for use in upcoming projects."
            elif learning_match:
                fallback_text = f"Reading material shared ({learning_source})" + (
                    " — proposed for team discussion." if discussion_intent else "."
                )
            elif matched_directive:
                fallback_text = f"Identified as directly relevant to standing directive: \"{matched_directive}\"."
            elif urgency_reasons:
                fallback_text = f"Flagged for attention: {' · '.join(urgency_reasons[:2])}."

            if fallback_text:
                if not conversation.summary or conversation.summary.startswith("from "):
                    conversation.summary = fallback_text
                if not conversation.ai_analysis:
                    conversation.ai_analysis = fallback_text

        # Domain detection
        if not conversation.domain or conversation.domain == Domain.UNKNOWN:
            conversation.domain = self._detect_domain_local(events)

        return conversation

    async def classify_llm(
        self,
        conversation: Conversation,
        events: list[NormalizedEvent],
        recent_context: str = "",
        standing_directives: str = "",
        evidence: str = "",
    ) -> Conversation:
        """
        Stage 2: LLM enrichment (async, budget-gated).

        Enhances classification with LLM-powered analysis.
        Falls back to local-only if LLM unavailable.

        Args:
            conversation: The conversation to enrich.
            events: Normalized events in this conversation.
            recent_context: Optional recent conversation history text
                for context-aware classification.
            standing_directives: Optional standing user directives/preferences.
            evidence: Verified ties between this conversation and the user
                (see :func:`otto.intelligence.relevance.evidence_text`) — the
                model explains in these terms instead of inventing involvement.
        """
        if not self._llm or not events:
            return conversation

        # Build context for LLM: capture original thread context (first 2) and recent updates (last 3)
        selected_events = events[:2] + events[-3:] if len(events) > 5 else events
        seen_event_ids: set[str] = set()
        unique_events: list[NormalizedEvent] = []
        for e in selected_events:
            if e.id not in seen_event_ids:
                seen_event_ids.add(e.id)
                unique_events.append(e)

        # Who said what matters as much as what was said: a bot's routine
        # notice, a colleague's ask and the user's own reply read differently.
        context_parts: list[str] = []
        any_own = False
        names = _user_names()
        for event in unique_events:
            sanitized = sanitize_for_llm(
                redact_pii(event.plain_text_extract)
            )
            who = (event.sender or "").strip()
            if _written_by_user(event, names):
                who, any_own = f"{who} (you)" if who else "you", True
            elif event.is_auto_generated:
                who = f"{who} (bot)" if who else "bot"
            head = f"[{event.timestamp.isoformat()}] {event.title}" + (f" — {who}" if who else "")
            context_parts.append(f"{head}\n{sanitized}")
        context = "\n---\n".join(context_parts)
        if len(unique_events) > 1:
            context = "Thread, oldest first; the last message is the newest.\n\n" + context
        if any_own:
            context = (
                "Lines marked (you) are the user's own messages: never urgent in themselves — judge by what "
                "others need from the user, or what the user is now waiting on.\n\n" + context
            )

        # Load standing directives if not explicitly passed
        if not standing_directives:
            try:
                from otto.intelligence.history import get_standing_directives_text
                standing_directives = get_standing_directives_text()
            except Exception:
                standing_directives = ""

        # Who the user is: role and focus from config, plus the ties Otto
        # verified in the data (asked, mentioned, their thread, a directive…).
        profile_block = ""
        try:
            from otto.utils.identity import profile_text
            profile_block = profile_text(_user_profile())
        except Exception:
            profile_block = ""
        if evidence:
            profile_block = (profile_block + "\n" if profile_block else "") + \
                "Verified ties between this conversation and the user (from the data, not guesses):\n" + evidence

        # Choose prompt: use context-aware variant if history, directives or a profile are available
        has_history = bool(recent_context and recent_context != "(No previous conversation history available)")
        has_directives = bool(standing_directives and standing_directives != "(No standing user directives recorded)")

        if has_history or has_directives or profile_block:
            prompt = CLASSIFY_WITH_CONTEXT
            prompt = prompt.replace("{user_profile}", profile_block or "(Nothing recorded beyond the directives below)")
            prompt = prompt.replace("{standing_directives}", standing_directives or "(No standing user directives recorded)")
            prompt = prompt.replace("{recent_context}", recent_context or "(No previous conversation history available)")
        else:
            prompt = CLASSIFY_CONVERSATION

        # Classify
        response = await self._llm.complete(
            task="conversation_classify",
            system_prompt=prompt,
            user_message=context,
        )

        if not response:
            return conversation  # Budget exceeded or all providers failed

        # Validate output (no write intent)
        write_check = scan_for_write_intent(response.content)
        if write_check.has_write_intent:
            logger.warning("Write intent in classification output — quarantined")
            return conversation

        # Parse JSON response
        try:
            data = _extract_json(response.content)

            # Urgency & importance: always take the max of local and LLM. The
            # model's numbers start from scratch, so a "you usually skip this"
            # habit is applied to them the same way the local pass applied it.
            llm_urgency = float(data.get("urgency", 0))
            llm_importance = float(data.get("importance", 0))
            if "habit-skip" in conversation.topics:
                habit = self.habit_adjustment(conversation.subject or "", getattr(unique_events[-1], "sender", "") or "")
                if habit < 0 and llm_urgency < HABIT_PROTECTED_URGENCY:
                    llm_urgency = max(0.0, llm_urgency + habit)
                if habit < 0:
                    llm_importance = max(0.0, llm_importance + habit)
            conversation.urgency = max(conversation.urgency, llm_urgency)
            if "your-message" in conversation.topics:
                conversation.urgency = min(conversation.urgency, OWN_WORDS_URGENCY_CAP)
            conversation.importance = max(conversation.importance, llm_importance)

            # Opportunity: use max() to preserve local heuristic score if LLM returns lower
            conversation.opportunity_score = max(
                conversation.opportunity_score, float(data.get("opportunity_score", 0))
            )

            domain_str = data.get("domain", "unknown")
            try:
                conversation.domain = Domain(domain_str)
            except ValueError:
                pass

            # LLM-generated explanations (overwrite local with richer LLM output)
            llm_relevance = data.get("relevance_explanation", "")
            if isinstance(llm_relevance, str) and llm_relevance:
                conversation.relevance_explanation = to_second_person(llm_relevance)

            llm_summary = data.get("summary", "")
            if isinstance(llm_summary, str) and llm_summary:
                conversation.summary = to_second_person(llm_summary)

            # Topics: the LLM's are richer, but Otto's structural tags (noise,
            # all-clear, directive match) drive filtering and must survive.
            llm_topics = [t.strip() for t in data.get("topics") or [] if isinstance(t, str) and t.strip()]
            if llm_topics:
                sticky = [t for t in conversation.topics if t in STICKY_TOPICS]
                conversation.topics = sticky + [t for t in llm_topics if t not in sticky][:8]

            # Opportunity metadata
            opp_type = data.get("opportunity_type")
            if opp_type and opp_type != "null":
                conversation.opportunity_type = opp_type
            opp_desc = data.get("opportunity_description")
            if isinstance(opp_desc, str) and opp_desc and opp_desc != "null":
                conversation.opportunity_description = to_second_person(opp_desc)

            # AI analysis — nuanced "why this matters" reasoning
            ai_analysis = data.get("ai_analysis", "")
            if isinstance(ai_analysis, str) and ai_analysis:
                conversation.ai_analysis = to_second_person(ai_analysis)

            # One sentence in the user's terms; a "null"/empty answer means
            # the model found no personal tie, which is itself informative.
            for_you = data.get("for_you", "")
            if isinstance(for_you, str) and for_you.strip() and for_you.strip().lower() not in ("null", "none", "n/a"):
                conversation.for_you = to_second_person(re.sub(r"\s+", " ", for_you.strip())[:220])
            else:
                conversation.for_you = ""

            conversation.llm_enriched = True

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning("Failed to parse LLM classification: %s", e)

        return conversation

    async def extract_actions(
        self,
        conversation: Conversation,
        events: list[NormalizedEvent],
    ) -> list[ActionItem]:
        """Extract action items from a conversation using LLM."""
        if not self._llm or not events:
            return []

        context_parts = []
        for event in events[-5:]:
            sanitized = sanitize_for_llm(redact_pii(event.plain_text_extract))
            context_parts.append(f"{event.title}\n{sanitized}")
        context = "\n---\n".join(context_parts)

        response = await self._llm.complete(
            task="action_extract",
            system_prompt=EXTRACT_ACTIONS,
            user_message=context,
        )

        if not response:
            return []

        write_check = scan_for_write_intent(response.content)
        if write_check.has_write_intent:
            return []

        try:
            data = _extract_json(response.content)
            actions: list[ActionItem] = []
            for item in data.get("actions", []):
                actions.append(ActionItem(
                    description=item.get("description", ""),
                    owner_id=item.get("owner", ""),
                    domain=conversation.domain or Domain.UNKNOWN,
                    source_conversation_ids=[conversation.thread_id or ""],
                    status=ActionStatus.OPEN,
                ))
            return actions
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Failed to parse action extraction: %s", e)
            return []

    def learn_sender_weight(self, sender_id: str, weight: float) -> None:
        """Update learned sender importance weight."""
        self._sender_weights[sender_id] = min(1.0, max(0.0, weight))

    def _detect_domain_local(self, events: list[NormalizedEvent]) -> Domain:
        """Simple domain detection from event metadata."""
        work_signals = 0
        personal_signals = 0

        for event in events:
            text = (event.title + " " + event.plain_text_extract).lower()
            if any(kw in text for kw in ["sprint", "jira", "deploy", "standup", "okr", "meeting", "project"]):
                work_signals += 1
            if any(kw in text for kw in ["birthday", "family", "vacation", "personal"]):
                personal_signals += 1

        if work_signals > personal_signals:
            return Domain.WORK
        elif personal_signals > work_signals:
            return Domain.PERSONAL
        return Domain.UNKNOWN
