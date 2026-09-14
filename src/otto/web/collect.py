"""
Briefing data collection.

One :func:`collect_briefing_data` call = one refresh:

    adapters (concurrent, time-boxed) → ingestion → conversations
    → local heuristics → LLM enrichment (only for content not seen before)
    → noise filter → grouped, de-duplicated briefing items.

Everything here is designed to run every minute without becoming a
nuisance: adapters are polled concurrently with hard timeouts, LLM calls
are cached by content, and screenshots are only taken when a Slack item
changed and are pruned aggressively.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from otto import paths
from otto.web.security import safe_url

logger = logging.getLogger("otto.web.collect")

# Time boxes (seconds). A refresh must comfortably fit inside a 60 s cadence
# when there is nothing new; LLM work is the only thing allowed to be slow.
ADAPTER_CONNECT_TIMEOUT = 15.0
ADAPTER_POLL_TIMEOUT = 40.0
LLM_PHASE_TIMEOUT_REMOTE = 30.0
LLM_PHASE_TIMEOUT_LOCAL = 90.0
# Outer bound on one whole refresh (poll ≤ 40s, LLM ≤ 90s, the rest seconds).
REFRESH_HARD_TIMEOUT = 180.0
MAX_LLM_CONVERSATIONS_PER_REFRESH = 12
LLM_CONCURRENCY = 3
CONTEXT_MAX_CHARS = 6000          # history (72 h summaries) + memory ("earlier in this channel") per prompt
HANDLED_URGENCY = 0.65            # an ask somebody already reacted to: "for you", not "important"
ASKED_URGENCY_FLOOR = 0.7         # an ask of you needs you, whatever the words score locally
ADDRESSED_URGENCY_FLOOR = 0.5     # a DM, a mention, an answer to your question: at least "for you"
SYNTHESIS_TIMEOUT = 18.0          # the briefing-level digest, one model call per changed briefing

# Screenshot housekeeping
SCREENSHOT_TTL_SECONDS = 120
SCREENSHOT_MAX_FILES = 40
SCREENSHOT_MAX_AGE = timedelta(days=3)

MAX_ITEMS_PER_SOURCE = 40


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

_SIDEBAR_NOISE = {
    '#home', 'home', 'dms', 'dm', 'activity', 'files', 'more',
    'agents & tools', 'agents', 'admin', 'huddles', 'threads', 'later',
    'add channels', 'invite people', 'connect apps', 'messages',
    'add canvas', 'canvas', 'list', 'folder', 'preferences', 'profile',
    'sign out', 'search', 'directories', 'directory', 'compose',
    'inbox', 'starred', 'snoozed', 'sent', 'drafts', 'all mail',
    'spam', 'trash', 'categories', 'social', 'updates', 'promotions',
    'labels',
}

_AX_ARTIFACTS = [
    re.compile(r'\d+\s+characters?\s+remaining'),
    re.compile(r'Loading\s+\d+\s+more\s+(?:entr(?:y|ies)|repl(?:y|ies))'),
    re.compile(r'\d+\s+more\s+(?:entr|repl)(?:y|ies)'),
    re.compile(r'Unread messages?'),
    re.compile(r'Jump to bottom'),
]

_UI_CHROME_PREFIXES = (
    'this is your space', 'that looks like a github link', 'would you like to install',
    'invite teammates', 'channel or user name', 'slack works better',
)


def clean_text(raw: str) -> str:
    """Strip browser/AX navigation chrome from extracted text."""
    if not raw:
        return ""
    text = raw.replace('\r', '\n')
    try:
        from otto.utils.content_parser import is_slack_chrome_line
    except Exception:       # pragma: no cover — parser always ships with the collector
        def is_slack_chrome_line(_line: str) -> bool:
            return False
    lines: list[str] = []
    for line in text.split('\n'):
        line = line.strip()
        if not line or len(line) < 3:
            continue
        if line.lower() in _SIDEBAR_NOISE:
            continue
        if re.fullmatch(r'\d{1,2}:\d{2}(?:\s*(?:AM|PM|am|pm))?', line):
            continue
        if re.fullmatch(r'\d{1,4}', line):
            continue
        # Composer / thread-panel chrome that an older parse may have kept
        # (memory recalls the text as it was first read).
        if is_slack_chrome_line(line):
            continue
        lines.append(line)
    deduped: list[str] = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)
    return ' '.join(deduped[:20])


def clean_ax_text(text: str) -> str:
    """Strip Slack accessibility-tree artifacts from message text."""
    if not text:
        return ""
    for pat in _AX_ARTIFACTS:
        text = pat.sub('', text)
    return re.sub(r'\s+', ' ', text).strip()


def format_relative_time(value: Any) -> str:
    """Format a timestamp (datetime / iso / epoch) as a relative string."""
    try:
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif isinstance(value, datetime):
            dt = value
        elif isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        mins = int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
        if mins < 1:
            return "just now"
        if mins < 60:
            return f"{mins}m ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"
    except Exception:
        return ""


def is_noise(text: str, subject: str, topics: list[str], *, from_person: bool = False) -> bool:
    """
    UI chrome / empty / timestamp-only content that is not worth showing.

    Short text is usually chrome — unless a person wrote it: "can you review
    #42?" is twelve characters of real correspondence, so a named human sender
    lowers the length bar to a couple of words.
    """
    clean_t = (text or subject or "").strip()
    low = clean_t.lower()
    if len(clean_t) < 30 and not (from_person and len(clean_t) >= 8 and len(clean_t.split()) >= 2):
        return True
    if any(low.startswith(p) for p in _UI_CHROME_PREFIXES):
        return True
    if re.match(r'^\d+\+?\s+new\s+messages?\b', low):
        return True
    if low.startswith('channel '):
        return True
    if re.match(r'^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b', low) and len(clean_t) < 40:
        return True
    if 'slack-ui-noise' in topics or 'ui-artifact' in topics:
        return True
    # Routine bot notices that say nothing happened ("0 critical, 0 high",
    # "no findings") — the classifier tags them; they are not briefing material.
    if 'all-clear' in topics:
        return True
    return False


# ---------------------------------------------------------------------------
# Duplicate consolidation
# ---------------------------------------------------------------------------

_URGENCY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}


def _normalise_for_dedup(text: str) -> str:
    """Remove the parts of a message that change between repeated bot reports."""
    t = text.lower()
    t = re.sub(r'https?://\S+', ' ', t)
    t = re.sub(r'\d+(?:[.:]\d+)*\s*(?:ms|s|m|h|%|x)?', ' ', t)   # numbers, durations, percentages
    t = re.sub(r'\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\b', ' ', t)
    t = re.sub(r'\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b', ' ', t)
    t = re.sub(r'[^a-z\s]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def is_duplicate_item(item1: Dict[str, Any], item2: Dict[str, Any]) -> bool:
    """Two briefing items are the same thing if their text matches after normalising volatile parts."""
    t1 = (item1.get("text") or "").lower().strip()
    t2 = (item2.get("text") or "").lower().strip()
    if not t1 or not t2:
        return False
    if t1[:50] == t2[:50]:
        return True
    n1 = _normalise_for_dedup(t1[:160])
    n2 = _normalise_for_dedup(t2[:160])
    return bool(n1) and n1 == n2 and len(n1) > 15


_MESSAGE_LINK = re.compile(r"/archives/[A-Z0-9]+/p\d{10,}", re.IGNORECASE)
_CHANNEL_LINK = re.compile(r"/archives/[A-Za-z0-9_-]+/?$|app_redirect\?channel=|slack://channel\?", re.IGNORECASE)


def link_specificity(url: str) -> int:
    """2 = a particular message, 1 = a channel, 0 = the app or nothing.

    The same message often arrives twice — read off the screen (which knows
    only the channel) and through the API (which knows the exact message).
    When the two are folded into one item, the link that lands on the message
    must win, whichever copy came first.
    """
    u = (url or "").strip()
    if not u:
        return 0
    if _MESSAGE_LINK.search(u):
        return 2
    if _CHANNEL_LINK.search(u):
        return 1
    return 0 if u.startswith("slack://") else 1


def merge_duplicate_items(existing: Dict[str, Any], new_item: Dict[str, Any]) -> None:
    """Fold ``new_item`` into ``existing`` (counts, highest urgency, merged actions/topics, best link, every id)."""
    from otto.web.state import item_ids

    existing["occurrence_count"] = existing.get("occurrence_count", 1) + 1
    # Both copies' ids: a dismissal by either must hold when only one copy is
    # read next time (see state.item_ids).
    existing["ids"] = sorted(item_ids(existing) | item_ids(new_item))

    if _URGENCY_RANK.get(new_item.get("urgency"), 0) > _URGENCY_RANK.get(existing.get("urgency"), 0):
        existing["urgency"] = new_item["urgency"]

    # The link that lands on the message beats the one that lands on the channel.
    if link_specificity(new_item.get("source_url") or "") > link_specificity(existing.get("source_url") or ""):
        existing["source_url"] = new_item["source_url"]
    if new_item.get("external_url") and not existing.get("external_url"):
        existing["external_url"] = new_item["external_url"]
        if new_item.get("link_intelligence") and not existing.get("link_intelligence"):
            existing["link_intelligence"] = new_item["link_intelligence"]
    if new_item.get("screenshot") and not existing.get("screenshot"):
        existing["screenshot"] = new_item["screenshot"]

    actions = list(existing.get("action_items", []))
    for a in new_item.get("action_items", []):
        if a not in actions:
            actions.append(a)
    existing["action_items"] = actions

    existing["topics"] = list(dict.fromkeys(list(existing.get("topics", [])) + list(new_item.get("topics", []))))

    if (new_item.get("timestamp") or "") > (existing.get("timestamp") or ""):
        existing["timestamp"] = new_item["timestamp"]
        existing["time_display"] = new_item.get("time_display", existing.get("time_display", ""))

    new_ai = new_item.get("ai_analysis", "") or ""
    old_ai = existing.get("ai_analysis", "") or ""
    if new_item.get("matched_directive") and not existing.get("matched_directive"):
        existing["matched_directive"] = new_item["matched_directive"]
        if new_ai:
            existing["ai_analysis"] = new_ai
    elif len(new_ai) > len(old_ai):
        existing["ai_analysis"] = new_ai

    # Reasons are evidence: keep every distinct one, strongest first is preserved by order.
    seen_kinds = {r.get("kind") for r in existing.get("why") or [] if isinstance(r, dict)}
    for r in new_item.get("why") or []:
        if isinstance(r, dict) and r.get("kind") not in seen_kinds:
            existing.setdefault("why", []).append(r)
            seen_kinds.add(r.get("kind"))
    if new_item.get("for_you") and not existing.get("for_you"):
        existing["for_you"] = new_item["for_you"]


# ---------------------------------------------------------------------------
# Screenshots (Slack) — background, never steals focus
# ---------------------------------------------------------------------------

def screenshots_enabled() -> bool:
    return os.environ.get("OTTO_DISABLE_SCREENSHOTS") != "1"


def _get_slack_window_id() -> int | None:
    """Find Slack's on-screen window ID without activating or focusing it."""
    bin_path = paths.find_window_binary()
    if bin_path.exists() and os.access(bin_path, os.X_OK):
        try:
            res = subprocess.run([str(bin_path), "Slack"], capture_output=True, text=True, timeout=2)
            if res.returncode == 0 and res.stdout.strip().isdigit():
                return int(res.stdout.strip())
        except Exception:
            pass

    swift_bin = shutil.which("swift")
    if swift_bin:
        script = '''
import CoreGraphics
import Foundation

let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
for w in list {
    if let owner = w[kCGWindowOwnerName as String] as? String, owner.caseInsensitiveCompare("Slack") == .orderedSame,
       let bounds = w[kCGWindowBounds as String] as? [String: Any],
       let width = bounds["Width"] as? Double, width > 200,
       let id = w[kCGWindowNumber as String] as? Int {
        print(id)
        exit(0)
    }
}
exit(1)
'''
        try:
            res = subprocess.run([swift_bin, "-e", script], capture_output=True, text=True, timeout=3)
            if res.returncode == 0 and res.stdout.strip().isdigit():
                return int(res.stdout.strip())
        except Exception:
            pass
    return None


# A thumbnail older than this is not shown: a stale picture of a channel is
# worse than none. Captures that fail (Screen Recording not granted, Slack on
# another Space, …) pause the feature instead of costing seconds every minute.
SCREENSHOT_STALE_SECONDS = 30 * 60
SCREENSHOT_FAILURE_PAUSE_SECONDS = 30 * 60
SCREEN_ACCESS_RECHECK_SECONDS = 10 * 60
_SCREENSHOT_STATE: Dict[str, Any] = {
    "paused_until": 0.0, "captured_this_refresh": False, "digests": {}, "hinted": False,
    "reason": "", "granted": None, "granted_checked": 0.0,
}


def reset_screenshot_state() -> None:
    _SCREENSHOT_STATE.update(
        paused_until=0.0, captured_this_refresh=False, digests={}, hinted=False,
        reason="", granted=None, granted_checked=0.0,
    )


def _pause_screenshots(reason: str) -> None:
    _SCREENSHOT_STATE["paused_until"] = time.time() + SCREENSHOT_FAILURE_PAUSE_SECONDS
    _SCREENSHOT_STATE["reason"] = reason
    if not _SCREENSHOT_STATE["hinted"]:
        _SCREENSHOT_STATE["hinted"] = True
        logger.info(
            "Slack thumbnails paused for %d min: %s (they need Screen Recording for the engine's interpreter — "
            "`otto permissions` walks through it; `screenshots = false` in config.toml turns them off for good)",
            SCREENSHOT_FAILURE_PAUSE_SECONDS // 60, reason,
        )
    else:
        logger.debug("Slack thumbnails paused again: %s", reason)


def _screen_recording_granted() -> bool | None:
    """Cached, non-prompting Screen Recording preflight for *this* process (re-asked every 10 min)."""
    now = time.time()
    if now - float(_SCREENSHOT_STATE["granted_checked"]) > SCREEN_ACCESS_RECHECK_SECONDS:
        from otto.utils.screen import screen_recording_granted
        _SCREENSHOT_STATE["granted"] = screen_recording_granted()
        _SCREENSHOT_STATE["granted_checked"] = now
    return _SCREENSHOT_STATE["granted"]


def screenshot_status() -> Dict[str, Any]:
    """What the UI and ``otto status`` say about thumbnails."""
    now = time.time()
    paused_for = max(0, int(_SCREENSHOT_STATE["paused_until"] - now))
    return {
        "enabled": screenshots_enabled(),
        "granted": _SCREENSHOT_STATE["granted"],
        "paused_seconds": paused_for,
        "reason": _SCREENSHOT_STATE["reason"] if paused_for else "",
    }


def _same_channel(a: str, b: str) -> bool:
    def norm(name: str) -> str:
        n = (name or "").strip().lower()
        for prefix in ("dm-", "#", "@"):
            if n.startswith(prefix):
                n = n[len(prefix):]
        return n
    return bool(a) and bool(b) and norm(a) == norm(b)


def capture_slack_screenshot(channel_name: str = "") -> str:
    """
    Capture the Slack window by window-ID (``screencapture -l``) — no focus
    change, no interruption. Returns the filename or ``""``.

    The picture has to be *of the conversation*: a fresh capture is taken only
    while Slack is showing ``channel_name`` (the reader reports what is on
    screen); items from other conversations reuse their own recent capture
    (≤ 30 min) or show none. Budget: at most one fresh capture per refresh;
    a denied Screen Recording grant, a failed capture or a blank one pauses
    captures for half an hour with a hint in the log and in status.
    """
    if not screenshots_enabled():
        return ""
    try:
        sdir = paths.screenshots_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        fname = hashlib.sha256((channel_name or "slack").encode()).hexdigest()[:12] + ".png"
        fpath = sdir / fname
        now = time.time()
        age = now - fpath.stat().st_mtime if fpath.exists() else None
        if age is not None and age < SCREENSHOT_TTL_SECONDS:
            return fname
        fresh_enough = fname if age is not None and age < SCREENSHOT_STALE_SECONDS else ""

        if now < _SCREENSHOT_STATE["paused_until"] or _SCREENSHOT_STATE["captured_this_refresh"]:
            return fresh_enough
        from otto.adapters.browser import slack_browser
        if not _same_channel(slack_browser.visible_channel(), channel_name):
            return fresh_enough                      # Slack is showing something else right now
        if _screen_recording_granted() is False:
            _pause_screenshots("Screen Recording is not granted to the engine's interpreter")
            return fresh_enough
        _SCREENSHOT_STATE["captured_this_refresh"] = True

        win_id = _get_slack_window_id()
        if not win_id:
            return fresh_enough

        before = (fpath.stat().st_mtime, fpath.stat().st_size) if fpath.exists() else None
        res = subprocess.run(
            ["screencapture", f"-l{win_id}", "-o", "-x", str(fpath)],
            capture_output=True, timeout=4,
        )
        after = (fpath.stat().st_mtime, fpath.stat().st_size) if fpath.exists() else None
        if res.returncode != 0 or after is None or after == before or after[1] <= 1000:
            err = (res.stderr or b"").decode("utf-8", errors="replace").strip()[:120]
            _pause_screenshots(err or f"screencapture exit {res.returncode}, no image written")
            return fresh_enough
        # A capture byte-identical to *another* conversation's is not a picture
        # of this one (blank window without Screen Recording).
        digest = hashlib.sha256(fpath.read_bytes()).hexdigest()
        digests: Dict[str, str] = _SCREENSHOT_STATE["digests"]
        if any(d == digest for ch, d in digests.items() if ch != fname):
            fpath.unlink(missing_ok=True)
            _pause_screenshots("captures are identical for different conversations (blank window)")
            return ""
        digests[fname] = digest
        return fname
    except Exception as e:
        logger.debug("Background screenshot capture failed: %s", e)
        return ""


def prune_screenshots(
    max_files: int = SCREENSHOT_MAX_FILES,
    max_age: timedelta = SCREENSHOT_MAX_AGE,
    keep: set[str] | None = None,
) -> int:
    """Delete stale screenshots. Returns number removed."""
    sdir = paths.screenshots_dir()
    if not sdir.exists():
        return 0
    keep = keep or set()
    removed = 0
    now = time.time()
    files = sorted(sdir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for idx, f in enumerate(files):
        too_old = now - f.stat().st_mtime > max_age.total_seconds()
        over_cap = idx >= max_files
        if (too_old or over_cap) and f.name not in keep:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def _item_id(src: str, channel: str, text: str) -> str:
    # md5 is used purely as a stable, short identifier (not for security);
    # kept for continuity with users' existing dismissed/snoozed state.
    return hashlib.md5(f"{src}:{channel}:{text[:80]}".encode()).hexdigest()[:10]  # noqa: S324


def _ago(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    if seconds < 90:
        return "a minute"
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h"
    return f"{int(seconds // 86400)} d"


def thread_continuity(cache: Any, thread_key: str, ckey: str, events: list) -> list[str]:
    """
    Otto's earlier read of the same thread, as one context line, or [].

    A thread that grew since (a reply landed) gets a fresh verdict; handing
    the model its previous summary lets the new one say what *changed*.
    """
    previous = getattr(cache, "previous", None)
    if previous is None:
        return []
    try:
        prev = previous(thread_key, exclude_key=ckey)
    except Exception:
        return []
    if not prev or not str(prev.get("summary") or "").strip():
        return []
    line = f"Otto's earlier read of this thread ({_ago(prev.get('age_seconds', 0))} ago): {str(prev['summary']).strip()[:400]}"
    tie = str(prev.get("for_you") or "").strip()
    if tie:
        line += f" For you then: {tie[:160]}"
    return [line]


def slack_context_lines(events: list) -> list[str]:
    """Channel purpose and people's titles, when the reader supplied them (Slack token)."""
    purpose = topic = ""
    titles: Dict[str, str] = {}
    for e in events:
        meta = getattr(e, "meta", None) or {}
        if not purpose and meta.get("channel_purpose"):
            purpose = str(meta["channel_purpose"])[:200]
        if not topic and meta.get("channel_topic"):
            topic = str(meta["channel_topic"])[:120]
        who = (getattr(e, "sender", "") or "").strip()
        title = str(meta.get("sender_title") or "").strip()
        if who and title and who not in titles:
            titles[who] = title[:80]
    lines: list[str] = []
    if purpose or topic:
        lines.append("Channel purpose: " + " · ".join(b for b in (purpose, topic) if b))
    if titles:
        lines.append("People: " + "; ".join(f"{w} — {t}" for w, t in list(titles.items())[:6]))
    return lines


def _urgency_label(urgency: float) -> str:
    if urgency >= 0.9:
        return "critical"
    if urgency >= 0.7:
        return "high"
    if urgency >= 0.4:
        return "medium"
    return "info"


def _source_label(adapter_name: str) -> str:
    """``browser_slack`` → ``slack``; ``native_calendar`` → ``calendar``; ``api_slack`` → ``slack api``."""
    n = adapter_name.lower().split(":", 1)[0]
    for prefix in ("browser_", "native_"):
        if n.startswith(prefix):
            n = n[len(prefix):]
    if n.startswith("api_"):
        n = n[len("api_"):] + " api"
    return n or adapter_name.lower()


def _explain_adapter_error(err: str) -> str:
    """Turn adapter/osascript errors into one human sentence."""
    e = (err or "").lower()
    if "assistive" in e or "1719" in e or "25211" in e or "accessibility" in e:
        return "needs Accessibility permission (System Settings → Privacy & Security → Accessibility)"
    if "not allowed" in e or "1743" in e or "automation" in e:
        return "needs Automation permission — allow the macOS prompt"
    if "timed out" in e or "timeout" in e:
        return "timed out (usually a pending macOS permission prompt)"
    if ("not running" in e or "no windows" in e or "no browser" in e or "not found" in e
            or "no slack" in e or "tabs found" in e or "tab found" in e or "not open" in e):
        return "app not open"
    return err[:120] if err else "unavailable"


def _permission_problem(err: str) -> str:
    """The explanation if *err* is a macOS permission denial or a hang on a prompt, else ``""``."""
    explained = _explain_adapter_error(err)
    if explained.startswith("needs ") or explained.startswith("timed out"):
        return explained
    return ""


_FINDING_WORDS = re.compile(
    r"\b(?:cvss|vulnerabilit(?:y|ies)|(?:critical|high|medium)[- ]severity|severity findings?|"
    r"security (?:bug|finding|findings|scan|audit|issue)|remote code execution|rce|"
    r"(?:command|sql|code) injection|insecure deserialization|cve-\d{4}-\d+)\b",
    re.IGNORECASE,
)


def is_security_finding(conv: Any, text: str) -> bool:
    """Is this conversation a vulnerability / scan-finding report?

    Uses the local findings summariser on the raw text plus the classifier's
    own words (summary, analysis, actions, topics), so it holds whether the
    conversation was enriched by the LLM or not.
    """
    from otto.intelligence.classifier import summarize_findings

    if summarize_findings(text or ""):
        return True
    blob = " ".join([
        getattr(conv, "summary", "") or "",
        getattr(conv, "relevance_explanation", "") or "",
        " ".join(getattr(conv, "action_items", None) or []),
        " ".join(getattr(conv, "topics", None) or []),
    ])
    return bool(_FINDING_WORDS.search(blob))


class SourceBackoff:
    """Per-source retry schedule for sources that keep failing.

    A source that times out or is denied is retried after 2, 4, 8 … minutes
    (capped) instead of every refresh. This matters on macOS: each attempt
    can raise a permission dialog, and re-prompting every 60 s while the user
    is away is hostile. A manual refresh (``reset``) tries everything again.
    """

    BASE = 120.0
    CAP = 15 * 60.0

    def __init__(self) -> None:
        self._failures: Dict[str, int] = {}
        self._next_try: Dict[str, float] = {}
        self._last_error: Dict[str, str] = {}

    def should_skip(self, name: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self._next_try.get(name, 0.0) > now

    def retry_in(self, name: str, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        return max(0, int(self._next_try.get(name, 0.0) - now))

    def last_error(self, name: str) -> str:
        return self._last_error.get(name, "")

    def record_failure(self, name: str, error: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        n = self._failures.get(name, 0) + 1
        self._failures[name] = n
        self._last_error[name] = error
        self._next_try[name] = now + min(self.CAP, self.BASE * (2 ** (n - 1)))

    def record_success(self, name: str) -> None:
        self._failures.pop(name, None)
        self._next_try.pop(name, None)
        self._last_error.pop(name, None)

    def reset(self) -> None:
        self._failures.clear()
        self._next_try.clear()
        self._last_error.clear()


_BACKOFF = SourceBackoff()


def reset_source_backoff() -> None:
    """Forget every source back-off (used by manual refresh)."""
    _BACKOFF.reset()


async def _poll_adapter(adapter: Any, since: datetime, backoff: SourceBackoff | None = None) -> tuple[str, list, dict]:
    """Connect + poll one adapter under hard timeouts. Never raises.

    Returns ``(name, events, status)`` where ``status`` is
    ``{"source", "ok", "state", "error", "items"}`` so the UI can tell
    "nothing to report" from "could not read this source".
    """
    backoff = _BACKOFF if backoff is None else backoff
    name = getattr(adapter, "name", type(adapter).__name__)
    status: dict = {"source": _source_label(name), "adapter": name, "ok": False, "state": "unknown", "error": "", "items": 0, "seconds": 0.0}

    if backoff.should_skip(name):
        status.update(state="backoff", error=backoff.last_error(name) or "unavailable", retry_in=backoff.retry_in(name))
        return name, [], status

    t0 = time.monotonic()
    try:
        return await _poll_adapter_inner(adapter, since, backoff, name, status)
    finally:
        status["seconds"] = round(time.monotonic() - t0, 2)
        timings = getattr(adapter, "timings", None)
        if isinstance(timings, dict) and timings:
            status["timings"] = {k: float(v) for k, v in timings.items()}


async def _poll_adapter_inner(adapter: Any, since: datetime, backoff: SourceBackoff, name: str, status: dict) -> tuple[str, list, dict]:
    from otto.storage.models import ConnectionState

    try:
        conn = await asyncio.wait_for(adapter.connect(), timeout=ADAPTER_CONNECT_TIMEOUT)
        status["state"] = getattr(conn.state, "value", str(conn.state))
        if conn.state not in (ConnectionState.HEALTHY, ConnectionState.DEGRADED):
            status["error"] = _explain_adapter_error(getattr(conn, "error", "") or status["state"])
            # "App not open" is cheap to re-check and prompts nothing: no back-off.
            if status["error"] != "app not open":
                backoff.record_failure(name, status["error"])
            return name, [], status
        events = list(await asyncio.wait_for(adapter.poll(since), timeout=ADAPTER_POLL_TIMEOUT) or [])
        if not events:
            # Adapters swallow read errors and return []; a permission denial
            # must still surface as "could not read", not "nothing new".
            denied = _permission_problem(getattr(adapter, "last_error", "") or "")
            if denied:
                status.update(ok=False, state="failed", error=denied)
                backoff.record_failure(name, denied)
                return name, [], status
        status.update(ok=True, items=len(events))
        stats = getattr(adapter, "stats", None)
        if isinstance(stats, dict) and stats.get("channels"):
            status["channels"] = int(stats["channels"])
            status["calls"] = int(stats.get("calls") or 0)
        note = getattr(adapter, "scope_note", "")
        if note:
            status["note"] = str(note)
        # The screen reader's account of Slack's sidebar: which conversations
        # were read, which are sitting unread (see slack_browser.slack_coverage).
        coverage = getattr(adapter, "coverage", None)
        if isinstance(coverage, dict) and coverage:
            status["coverage"] = coverage
        backoff.record_success(name)
        return name, events, status
    except asyncio.TimeoutError:
        logger.warning("Adapter %s: timed out; skipping this refresh", name)
        status.update(state="timeout", error=_explain_adapter_error("timed out"))
        backoff.record_failure(name, status["error"])
    except Exception as e:
        logger.debug("Adapter %s skipped: %s", name, e)
        status.update(state="error", error=_explain_adapter_error(str(e)))
        backoff.record_failure(name, status["error"])
    return name, [], status


UNREAD_ROWS_MAX = 12


def _add_unread_slack(radar_payload: Dict[str, Any], source_status: list) -> None:
    """Conversations Slack marks unread that nobody has opened → radar rows and one note.

    Only the screen reader has this gap: it reads what is on screen, and the
    sidebar tells it what is waiting elsewhere. With the API reader those
    messages were read in full, so nothing is added. Never raises.
    """
    try:
        if any(s.get("source") == "slack api" and s.get("ok") for s in source_status):
            return
        coverage = next((s.get("coverage") or {} for s in source_status if s.get("source") == "slack"), {})
        unseen = [u for u in (coverage.get("unseen") or []) if u.get("title")]
        if not unseen:
            return
        rows = radar_payload.setdefault("unread", [])
        for u in unseen[:UNREAD_ROWS_MAX]:
            badge = int(u.get("badge") or 0)
            why = (f"{badge} mention{'s' if badge != 1 else ''}" if badge else "mentions you") if u.get("mention") else "unread"
            rows.append({
                "id": "unread:" + hashlib.sha256(str(u.get("key") or u["title"]).encode("utf-8")).hexdigest()[:12],
                "kind": "unread", "who": "", "what": str(u["title"]), "channel": why,
                "url": str(u.get("url") or ""), "due_label": "",
            })
        names = ", ".join(str(u["title"]) for u in unseen[:3])
        if len(unseen) > 3:
            names += f" and {len(unseen) - 3} more"
        note = f"Slack shows unread in {names} — not opened yet, so nothing from there is in this briefing."
        radar_payload.setdefault("attention", []).insert(0, note)
    except Exception as e:  # pragma: no cover - a coverage note must never fail a refresh
        logger.debug("unread Slack rows skipped: %s", e)


_API_ADAPTERS: dict[str, Any] = {}


def api_adapters() -> list:
    """Read-only API readers configured from the key store — today: Slack (``xoxp-``/``xoxb-``).

    Instances persist across refreshes (the screen readers are recreated every
    minute; these keep channel/user caches, the poll rotation and rate-limit
    back-off, so a 60 s refresh costs a few dozen GETs, not a full re-sync).
    """
    from otto.utils import keys as K

    out: list = []
    try:
        token = K.slack_token()
    except Exception as e:
        logger.debug("key discovery failed: %s", e)
        token = None
    if token is None:
        return out
    fp = hashlib.sha256(token.key.encode()).hexdigest()[:16]
    adapter = _API_ADAPTERS.get(fp)
    if adapter is None:
        try:
            from otto.adapters.slack import RequestLedger, SlackAdapter, build_slack_http_client
            ledger = RequestLedger()
            adapter = SlackAdapter(build_slack_http_client(token.key, ledger=ledger), "default", ledger=ledger)
            _API_ADAPTERS.clear()           # a replaced token retires the old client
            _API_ADAPTERS[fp] = adapter
            logger.info("Slack API reader configured from %s (read-only)", token.source)
        except Exception as e:
            logger.warning("Slack API reader unavailable: %s", e)
            return out
    out.append(adapter)
    return out


def reset_api_adapters() -> None:
    _API_ADAPTERS.clear()


def build_llm_gateway() -> Any:
    """LLM gateway configured from every discoverable key (may have zero providers).

    Daily budgets come from ``[llm]`` in config.toml (``daily_token_limit``,
    ``daily_cost_limit_usd``); past either, the day finishes on heuristics.
    """
    from otto.llm.gateway import LLMGateway

    tokens, cost = 500_000, 2.00
    try:
        from otto.config import ConfigManager
        cfg = ConfigManager()
        tokens = int(cfg.get_or("llm.daily_token_limit", tokens))
        cost = float(cfg.get_or("llm.daily_cost_limit_usd", cost))
    except Exception as e:
        logger.debug("LLM budget config unreadable, using defaults: %s", e)
    llm = LLMGateway(daily_token_limit=tokens, daily_cost_limit_usd=cost)
    llm.setup_from_discovered_keys()
    return llm


def _lookback_hours() -> int:
    """How far back each refresh looks (``engine.lookback_hours``, 1..168, default 24)."""
    try:
        from otto.config import ConfigManager
        hours = int(ConfigManager().get_or("engine.lookback_hours", 24))
    except Exception:
        hours = 24
    return max(1, min(168, hours))


# Messages remembered inside the window but not on screen right now are part
# of the briefing (``engine.recall = false`` turns this off: on-screen only).
RECALL_MAX_MESSAGES = 1500


def _recall_enabled() -> bool:
    if os.environ.get("OTTO_DISABLE_RECALL") == "1":
        return False
    try:
        from otto.config import ConfigManager
        return bool(ConfigManager().get_or("engine.recall", True))
    except Exception:
        return True


def _in_window(event: Any, since: datetime) -> bool:
    """True when the event is inside the lookback window (undated → keep)."""
    ts = getattr(event, "timestamp", None)
    if not isinstance(ts, datetime):
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= since


async def _collect(
    *,
    adapters: list | None,
    llm: Any | None,
    classification_cache: Any | None,
    force: bool = False,
) -> Dict[str, Any]:
    from otto.adapters.browser.factory import AdapterFactory
    from otto.adapters.browser.reader import BrowserContentReader
    from otto.core.event_bus import EventBus
    from otto.intelligence.classification_cache import ClassificationCache, content_key
    from otto.intelligence.classifier import ConversationClassifier, to_second_person
    from otto.intelligence.history import (
        get_recent_context,
        get_standing_directives_text,
        save_conversations,
    )
    from otto.intelligence.ingestion import IngestionPipeline
    from otto.llm.injection_defense import sanitize_for_llm
    from otto.storage.models import Conversation, Domain
    from otto.utils.pii_redactor import redact_pii

    started = time.monotonic()
    event_bus = EventBus()
    pipeline = IngestionPipeline(event_bus=event_bus)

    if adapters is None:
        factory = AdapterFactory(reader=BrowserContentReader())
        adapters = factory.create_all_adapters()
        adapters.extend(api_adapters())

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=_lookback_hours())

    if force:
        _BACKOFF.reset()

    # Where the seconds go, per refresh — logged and exposed so a slow source
    # is diagnosable from `otto status` instead of guessed at.
    phases: Dict[str, float] = {}
    _t = time.monotonic()

    def _lap(name: str) -> None:
        nonlocal _t
        t = time.monotonic()
        phases[name] = round(phases.get(name, 0.0) + (t - _t), 2)
        _t = t

    # 1. Poll all adapters concurrently, each time-boxed.
    results = await asyncio.gather(*(_poll_adapter(a, since) for a in adapters))
    all_raw: list = []
    sources_polled: list[str] = []
    source_status: list[dict] = []
    for name, events, st in results:
        sources_polled.append(name)
        source_status.append(st)
        all_raw.extend(events)
    _lap("poll")

    # 2. Normalise + dedupe.
    normalized = await pipeline.ingest(all_raw)

    # 3. LLM availability (never blocks on network here).
    if llm is None:
        llm = build_llm_gateway()
    has_llm = bool(llm.ensure_chat_provider())
    llm_is_local = has_llm and bool(getattr(llm, "_providers", None)) and all(
        getattr(p, "is_local", False) for p in llm._providers if p.is_healthy
    )
    classifier = ConversationClassifier(event_bus=event_bus, llm_gateway=llm if has_llm else None)
    cache = classification_cache or ClassificationCache()

    recent_context = ""
    try:
        recent_context = get_recent_context(hours=72, max_entries=20)
    except Exception as e:
        logger.debug("history unavailable: %s", e)
    # Directives and the user's role/focus shape the verdict, so a change to
    # either re-classifies (the cache key carries their fingerprint).
    try:
        from otto.utils.identity import profile_text
        profile_fp_text = profile_text()
    except Exception:
        profile_fp_text = ""
    directives_fp = hashlib.sha256((get_standing_directives_text() + "\n" + profile_fp_text).encode()).hexdigest()[:12]

    # Otto's memory: remember everything read *before* classification so the
    # LLM can be handed the earlier messages of the same channel/sender, and
    # the radar (commitments, deadlines, recurring series) advances every refresh.
    radar_payload: Dict[str, Any] = {}
    memory_context: Any = None
    memory_store: Any = None
    habits_text = ""
    recalled = 0
    # The briefing covers the lookback window — the same window for every
    # reader. What is on screen but older than that (a channel scrolled back
    # to last week) is remembered but is not "new" and is not shown as if it
    # were; what is inside the window but no longer on screen (a channel you
    # scrolled past at 9:00, a tab you closed) is recalled from memory below
    # and stays until it ages out or you dismiss it.
    live_all = list(normalized)
    normalized = [e for e in live_all if _in_window(e, since)]
    aged_out = len(live_all) - len(normalized)
    from otto.intelligence.relevance import PeopleIndex, evidence_text, reasons_for
    from otto.utils.identity import profile as user_profile, self_names
    people_index = PeopleIndex.empty()
    try:
        own_names, profile = self_names(), user_profile()
    except Exception:
        own_names, profile = [], {}

    def _why(conv: Any, events: list, channel: str = "") -> list:
        """Provable ties between a conversation and the user — never raises."""
        try:
            return reasons_for(conv, events, names=own_names, channel=channel, profile=profile, people=people_index)
        except Exception as e:
            logger.debug("relevance failed for %s: %s", getattr(conv, "thread_id", "?"), e)
            return []
    try:
        from otto.intelligence import knowledge, radar
        radar_payload = radar.update(live_all)
        memory_store = knowledge.default_store()
        people_index = PeopleIndex.from_store(memory_store)
        # What you opened, snoozed or dismissed unread shapes today's scores a
        # little and tells the model your habits in one line.
        try:
            habits = memory_store.feedback_priors()
            classifier.set_habits(habits)
            habits_text = habits.habits()
        except Exception as e:
            logger.debug("feedback priors unavailable: %s", e)

        # Recalled messages keep their reader ids, so they group and cache
        # exactly like live ones (no extra LLM calls).
        if _recall_enabled():
            live_keys = set(knowledge.keys_for(live_all))
            for m in memory_store.recall(since=since.timestamp(), exclude_keys=live_keys, limit=RECALL_MAX_MESSAGES):
                normalized.append(m.to_event())
                recalled += 1

        def _memory_context(conv: Any, events: list) -> str:
            first, latest = events[0], events[-1]
            return memory_store.context_for(
                channel=conv.subject or "",
                sender=getattr(latest, "sender", "") or "",
                before=first.timestamp.timestamp() if first.timestamp else None,
                exclude_keys=knowledge.keys_for(events),
            )

        memory_context = _memory_context
    except Exception as e:
        logger.warning("memory/radar unavailable this refresh: %s", e)
    _add_unread_slack(radar_payload, source_status)
    _lap("memory")

    # 4. Group into conversations.
    thread_map: Dict[str, list] = {}
    for event in normalized:
        key = getattr(event, 'conversation_id', None) or event.source_id
        thread_map.setdefault(key, []).append(event)

    convs: list[tuple[Any, list, str, str]] = []  # (conv, events, clean_text, cache_key)
    for thread_id, events in thread_map.items():
        events.sort(key=lambda e: e.timestamp)
        latest, first = events[-1], events[0]
        title = (latest.title or "").replace('\r', ' ').replace('\n', ' ').strip()[:80]
        clean = clean_text(latest.plain_text_extract or "")

        conv = Conversation(
            source=latest.source,
            account_id=latest.account_id or "",
            thread_id=thread_id,
            subject=title or "General",
            summary="",
            domain=Domain.UNKNOWN,
            relevance_explanation="",
            source_url=safe_url(latest.source_url or ""),
            started=first.timestamp,
            last_activity=latest.timestamp,
            message_count=len(events),
        )
        conv = classifier.classify_local(conv, events)
        if conv.importance < 0.3:
            conv.importance = 0.3

        src_val = conv.source.value if hasattr(conv.source, "value") else str(conv.source)
        ckey = content_key(src_val, thread_id, " ".join((e.plain_text_extract or "") for e in events[-3:]), directives_fp)
        convs.append((conv, events, clean, ckey))

    # 5. LLM enrichment — cached by content; only new material costs a call.
    llm_calls = 0
    if has_llm and convs:
        pending: list[tuple[Any, list, str]] = []
        for conv, events, _clean, ckey in convs:
            cached = cache.get(ckey)
            if cached is not None:
                cache.apply(conv, cached)
            else:
                pending.append((conv, events, ckey))

        # Most urgent (by local heuristics) first; bound the work per refresh.
        pending.sort(key=lambda t: t[0].urgency, reverse=True)
        pending = pending[:MAX_LLM_CONVERSATIONS_PER_REFRESH]
        sem = asyncio.Semaphore(LLM_CONCURRENCY)

        async def _enrich(conv: Any, events: list, ckey: str) -> None:
            nonlocal llm_calls
            src_val = conv.source.value if hasattr(conv.source, "value") else str(conv.source)
            thread_key = f"{src_val}:{conv.thread_id}"
            async with sem:
                try:
                    context = recent_context
                    if memory_context is not None:
                        try:
                            earlier = memory_context(conv, events)
                            if earlier:
                                context = f"{recent_context}\n\n{earlier}" if recent_context else earlier
                        except Exception as e:
                            logger.debug("memory context failed: %s", e)
                    # Compounding: Otto's own earlier read of this thread (so
                    # the new summary says what changed) and your habits.
                    lead = thread_continuity(cache, thread_key, ckey, events)
                    if habits_text:
                        lead.append("Your habits (from what you opened, snoozed or dismissed unread): " + habits_text)
                    lead.extend(slack_context_lines(events))
                    if lead:
                        context = "\n".join(lead) + ("\n\n" + context if context else "")
                    # Remembered text is Slack text: same PII redaction and
                    # injection scrub as the conversation itself before it
                    # leaves the machine.
                    if context:
                        context = sanitize_for_llm(redact_pii(context), max_length=CONTEXT_MAX_CHARS)
                    await classifier.classify_llm(
                        conv, events, recent_context=context, evidence=evidence_text(_why(conv, events)),
                    )
                    llm_calls += 1
                    if not conv.llm_enriched:
                        return          # every provider failed — keep the local result, retry next refresh
                    if conv.urgency >= 0.5:
                        actions = await classifier.extract_actions(conv, events)
                        llm_calls += 1
                        if actions:
                            conv.action_items = [a.description for a in actions if a.description]
                    cache.put(ckey, ClassificationCache.snapshot(conv), thread=thread_key)
                except Exception as e:
                    logger.warning("LLM classification failed for %s: %s", conv.thread_id, e)

        budget = LLM_PHASE_TIMEOUT_LOCAL if llm_is_local else LLM_PHASE_TIMEOUT_REMOTE
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*(_enrich(*p) for p in pending)), timeout=budget)
            except asyncio.TimeoutError:
                logger.info("LLM phase hit its %.0fs budget; remaining items will be enriched next refresh", budget)
    _lap("classify")

    # 6. Filter noise, build items grouped by source/channel.
    _SCREENSHOT_STATE["captured_this_refresh"] = False
    source_icons = {"slack": "💬", "email": "📧", "gmail": "📧", "calendar": "📅", "jira": "📋"}
    sources: Dict[str, Dict[str, list]] = {}
    opportunities: list = []
    screenshots_used: set[str] = set()
    all_conversations: list = []

    for conv, events, clean, _ckey in convs:
        all_conversations.append(conv)
        newest = events[-1]
        from_person = bool(getattr(newest, "sender", "")) and not getattr(newest, "is_auto_generated", False)
        if is_noise(clean, conv.subject, conv.topics or [], from_person=from_person) and conv.urgency < 0.7:
            continue

        src = (conv.source.value if hasattr(conv.source, 'value') else str(conv.source)).lower()
        channel = conv.subject or "General"
        if src == "slack" and not channel.startswith(('#', '@', 'dm-')):
            channel = f"#{channel}"

        _t_shot = time.monotonic()
        screenshot_file = capture_slack_screenshot(channel) if src == "slack" else ""
        phases["screenshots"] = round(phases.get("screenshots", 0.0) + (time.monotonic() - _t_shot), 2)
        if screenshot_file:
            screenshots_used.add(screenshot_file)

        # A repo named in a vulnerability report is the *subject* of the finding,
        # not a tool to evaluate: no "check the README before adopting" card and
        # no opportunity framing, whatever the LLM felt like calling it.
        finding = is_security_finding(conv, clean)
        if finding:
            conv.opportunity_score = min(float(conv.opportunity_score or 0.0), 0.4)
            conv.opportunity_type = ""
            conv.opportunity_description = ""

        link_intel = None
        external_url = ""
        try:
            from otto.intelligence.link_intelligence import analyze_url, extract_urls
            urls = extract_urls(f"{clean or ''} {conv.subject or ''}")
            if urls:
                external_url = safe_url(urls[0])
                if external_url and not finding:
                    link_intel = analyze_url(external_url)
                    if link_intel and not conv.opportunity_description:
                        conv.opportunity_description = link_intel.get("summary", "")
        except Exception as e:
            logger.debug("Link intelligence failed: %s", e)

        latest = events[-1]
        text = clean_ax_text(clean[:300]) if clean else conv.subject
        why = _why(conv, events, channel)
        # What is provably for you never sits at the bottom, model or no
        # model: an ask of you needs you; a DM, a mention or an answer to a
        # question of yours at least belongs under "For you". Nothing is
        # lowered here, and a bot's all-clear stays quiet.
        kinds = {r.kind for r in why}
        if "own" not in kinds and "all-clear" not in (conv.topics or []):
            floor = 0.0
            if "asked" in kinds:
                floor = ASKED_URGENCY_FLOOR
            elif kinds & {"dm", "mention", "answered"} and not getattr(latest, "is_auto_generated", False):
                floor = ADDRESSED_URGENCY_FLOOR
            if floor and conv.urgency < floor:
                conv.urgency = floor
                conv.importance = max(float(conv.importance or 0.0), floor)
        # An ask somebody has already reacted to (✅ 👀) is picked up: it stays
        # in front of you but below what nobody has touched — unless a real
        # severity or a directive of yours says otherwise.
        if any(r.kind == "handled" for r in why) and conv.urgency >= 0.7 and not finding and not conv.matched_directive:
            conv.urgency = HANDLED_URGENCY
        # Cached classifications predate the second-person rule; fix them here too.
        for field_name in ("for_you", "relevance_explanation", "ai_analysis", "summary", "opportunity_description"):
            value = getattr(conv, field_name, "") or ""
            if value:
                setattr(conv, field_name, to_second_person(value))
        conv.action_items = [to_second_person(a) if isinstance(a, str) else a for a in (conv.action_items or [])]
        item = {
            "text": text,
            "why": [r.to_dict() for r in why],
            "for_you": conv.for_you or "",
            "sender": clean_ax_text(getattr(latest, 'sender', '') or latest.account_id or ""),
            "source_url": conv.source_url or "",
            "timestamp": latest.timestamp.isoformat() if latest.timestamp else "",
            "time_display": format_relative_time(latest.timestamp) if latest.timestamp else "",
            "urgency": _urgency_label(conv.urgency),
            "urgency_score": round(float(conv.urgency), 3),
            "importance_score": round(float(conv.importance), 3),
            "relevance": conv.relevance_explanation or "",
            "ai_analysis": conv.ai_analysis or "",
            "summary": conv.summary or "",
            "topics": conv.topics or [],
            "matched_directive": conv.matched_directive or "",
            "opportunity_score": conv.opportunity_score,
            "opportunity_type": conv.opportunity_type or "",
            "opportunity_description": conv.opportunity_description or "",
            "action_items": conv.action_items or [],
            "screenshot": screenshot_file,
            "link_intelligence": link_intel,
            "external_url": external_url,
        }
        item["id"] = _item_id(src, channel, item["text"])

        chan_items = sources.setdefault(src, {}).setdefault(channel, [])
        for existing in chan_items:
            if is_duplicate_item(existing, item):
                merge_duplicate_items(existing, item)
                break
        else:
            item["occurrence_count"] = 1
            chan_items.append(item)

        if conv.opportunity_score > 0.4:
            opportunities.append({
                "subject": conv.subject,
                "score": conv.opportunity_score,
                "type": conv.opportunity_type or "",
                "description": conv.opportunity_description or conv.relevance_explanation or "",
                "source_url": conv.source_url or "",
                "external_url": external_url,
                "link_intelligence": link_intel,
            })

    # 6b. Exact links. An item read only off the screen links to its channel;
    # if the API copy of the same message is in memory (this refresh or an
    # earlier one), take its permalink so a click lands on the message.
    if memory_store is not None:
        for src, channels in sources.items():
            if src != "slack":
                continue
            for channel, chan_items in channels.items():
                for item in chan_items:
                    have = link_specificity(item.get("source_url") or "")
                    if have >= 2:
                        continue
                    try:
                        url = memory_store.permalink_for(channel=channel, text=item.get("text") or "",
                                                         since=since.timestamp() - 7 * 86400)
                        if not url and have < 1:
                            # No exact copy known: at least land in the right conversation.
                            url = memory_store.channel_link_for(channel=channel)
                    except Exception as e:  # pragma: no cover - a link is never worth a failed refresh
                        logger.debug("permalink lookup failed: %s", e)
                        url = ""
                    if url:
                        item["source_url"] = url

    # 7. Sections.
    sections = []
    total = 0
    for src in sorted(sources):
        channels = sources[src]
        channel_list = []
        count = 0
        for ch_name in sorted(channels):
            items = channels[ch_name][:MAX_ITEMS_PER_SOURCE]
            count += len(items)
            channel_list.append({"name": ch_name, "items": items})
        total += count
        icon = source_icons.get(src, "📄")
        sections.append({
            "source": src, "icon": icon, "title": f"{icon} {src.title()}",
            "channels": channel_list, "count": count,
        })

    _lap("build")
    phases["build"] = round(max(0.0, phases["build"] - phases.get("screenshots", 0.0)), 2)

    # 8. The digest: what all of this adds up to, in two sentences, plus the
    # connections and predictions a colleague would point out. One model
    # call per *changed* briefing (cached by content); local words otherwise.
    digest: Dict[str, Any] = {}
    try:
        from otto.intelligence import synthesis
        from otto.web.render import flatten_items
        from otto.web.state import hidden_ids
        partial = {"sections": sections, "radar": radar_payload, "source_status": source_status}
        visible = flatten_items(partial, hidden_ids())
        digest = await asyncio.wait_for(
            synthesis.synthesize(llm if has_llm else None, visible, radar_payload, data=partial,
                                 profile_salt=directives_fp),
            timeout=SYNTHESIS_TIMEOUT,
        )
        if digest.get("source") == "model" and not digest.get("cached"):
            llm_calls += 1
    except asyncio.TimeoutError:
        logger.info("digest hit its %.0fs budget; local words this refresh", SYNTHESIS_TIMEOUT)
    except Exception as e:
        logger.debug("digest unavailable: %s", e)
    _lap("digest")

    try:
        if all_conversations:
            save_conversations(all_conversations)
    except Exception as e:
        logger.warning("Failed to save conversation history: %s", e)

    try:
        prune_screenshots(keep=screenshots_used)
    except Exception as e:
        logger.debug("Screenshot pruning failed: %s", e)
    _lap("save")

    elapsed = time.monotonic() - started
    enriched = sum(1 for conv, *_ in convs if getattr(conv, "llm_enriched", False))
    llm_status = llm.provider_status() if hasattr(llm, "provider_status") else []
    mem = radar_payload.get("memory") or {}
    slow_sources = ", ".join(
        f"{s['source']} {s['seconds']}s" + (
            " (" + " ".join(f"{k}={v}s" for k, v in s["timings"].items()) + ")" if s.get("timings") else "")
        for s in source_status if float(s.get("seconds") or 0) >= 3.0
    )
    logger.info(
        "Refresh: %d raw → %d events (%d recalled, %d older than the window) → %d conversations → %d items in %.1fs "
        "(llm=%s, calls=%d, enriched=%d/%d, cache=%s, "
        "radar: todo=%d waiting=%d upcoming=%d recurring=%d, memory=%s msgs/%s days; phases %s%s)",
        len(all_raw), len(normalized), recalled, aged_out, len(convs), total, elapsed,
        "on" if has_llm else "off", llm_calls, enriched, len(convs), cache.stats(),
        len(radar_payload.get("todo") or []), len(radar_payload.get("waiting") or []),
        len(radar_payload.get("upcoming") or []), len(radar_payload.get("patterns") or []),
        mem.get("messages", 0), mem.get("days", 0),
        " ".join(f"{k}={v}s" for k, v in phases.items() if v), f"; slow: {slow_sources}" if slow_sources else "",
    )

    return {
        "generated_at": now.isoformat(),
        "generated_at_human": now.astimezone().strftime("%A, %B %d %Y · %I:%M %p"),
        "total_items": total,
        "sections": sections,
        "opportunities": opportunities,
        # "AI-analyzed" only when an LLM result was really applied this refresh
        # (fresh or cached); a configured-but-failing provider is not AI.
        "ai_powered": bool(has_llm and (enriched > 0 or not convs)),
        "llm": {
            "configured": has_llm,
            "enriched": enriched,
            "conversations": len(convs),
            "providers": llm_status,
        },
        "sources_polled": sources_polled,
        "source_status": source_status,
        "sources_failed": [s["source"] for s in source_status if not s["ok"]],
        "refresh_seconds": round(elapsed, 2),
        "phases": phases,
        "recalled": recalled,
        "aged_out": aged_out,
        "llm_calls": llm_calls,
        "screenshots": screenshot_status(),
        # Cross-day intelligence: your open loops, what you are waiting on,
        # upcoming deadlines, recurring series and what deserves attention.
        "radar": radar_payload,
        # What it all adds up to: digest, connections between items,
        # predictions and heads-ups (see intelligence/synthesis.py).
        "digest": digest,
    }


def collect_briefing_data(
    *,
    adapters: list | None = None,
    llm: Any | None = None,
    classification_cache: Any | None = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Synchronous entry point (runs its own event loop). Safe to call from a thread.

    ``force=True`` (manual refresh) retries sources that are in back-off.
    Bounded by ``REFRESH_HARD_TIMEOUT`` as a last line of defence: every step
    has its own time-box, but a refresh that somehow outlives them all fails
    loudly (and the engine backs off) instead of wedging the loop.
    """
    async def _bounded() -> Dict[str, Any]:
        return await asyncio.wait_for(
            _collect(adapters=adapters, llm=llm, classification_cache=classification_cache, force=force),
            timeout=REFRESH_HARD_TIMEOUT,
        )

    return asyncio.run(_bounded())
