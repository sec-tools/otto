from __future__ import annotations

"""
Briefing generator — produces structured briefings from classified conversations.

Briefing types:
- Morning: Full day preview with priorities
- Meeting prep: Context packet for upcoming meetings
- EOD: End of day summary
- Catch-me-up: Delta since last checked
- Weekly: Week review with patterns
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from otto.core.event_bus import EventBus
from otto.llm.gateway import LLMGateway
from otto.storage.models import (
    ActionItem,
    Briefing,
    BriefingReady,
    BriefingSection,
    BriefingType,
    Conversation,
)
from otto.utils.content_parser import content_hash
from otto.utils.time import format_relative, now_utc

logger = logging.getLogger("otto.briefings.generator")


class BriefingGenerator:
    """
    Generates structured briefings from classified conversations.

    Briefings are idempotent — generating the same briefing from the
    same data produces the same content_hash (no redundant notifications).
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        llm_gateway: LLMGateway | None = None,
    ) -> None:
        self.event_bus = event_bus or EventBus()
        self._llm = llm_gateway
        self._cache: dict[str, tuple[datetime, Briefing]] = {}

    async def generate_morning_briefing(
        self,
        conversations: list[Conversation],
        actions: list[ActionItem],
        now: datetime | None = None,
    ) -> Briefing:
        """
        Generate a morning briefing.

        Sections:
        1. Action required (urgent items needing attention)
        2. Opportunities (new opportunities detected)
        3. Today's agenda (calendar items + related context)
        4. Overnight updates (what happened while sleeping)
        5. Open action items (pending tasks)
        """
        if now is None:
            now = now_utc()

        sections: list[BriefingSection] = []

        # 1. Action required
        urgent = [c for c in conversations if c.urgency >= 0.7 and c.open_actions]
        if urgent:
            items = []
            for conv in sorted(urgent, key=lambda c: c.urgency, reverse=True)[:5]:
                reason = conv.relevance_explanation or conv.summary or ""
                items.append(f"• {conv.subject}: {reason}" if reason else f"• {conv.subject}")
            sections.append(BriefingSection(
                title="🔴 Needs Your Attention",
                content="\n".join(items),
                priority=1,
            ))

        # 2. Opportunities
        opportunity_convs = [c for c in conversations if c.opportunity_score > 0.5]
        if opportunity_convs:
            items = []
            for conv in sorted(opportunity_convs, key=lambda c: c.opportunity_score, reverse=True)[:3]:
                items.append(f"• {conv.subject} (confidence: {conv.opportunity_score:.0%})")
            sections.append(BriefingSection(
                title="✨ Opportunities",
                content="\n".join(items),
                priority=2,
            ))

        # 3. Overnight updates (exclude items already in urgent section)
        urgent_ids = {c.id for c in urgent} if urgent else set()
        overnight_cutoff = now - timedelta(hours=10)
        overnight = [c for c in conversations
                     if c.last_activity and c.last_activity > overnight_cutoff
                     and c.importance > 0.4
                     and c.id not in urgent_ids]
        if overnight:
            items = []
            for conv in sorted(overnight, key=lambda c: c.importance, reverse=True)[:5]:
                time_str = format_relative(conv.last_activity, now) if conv.last_activity else ""
                reason = conv.relevance_explanation or conv.summary or ""
                detail = f": {reason}" if reason else ""
                items.append(f"• {conv.subject}{detail} — {time_str}")
            sections.append(BriefingSection(
                title="🌙 While You Were Away",
                content="\n".join(items),
                priority=3,
            ))

        # 4. Open actions
        open_actions = [a for a in actions if a.status.value in ("open", "in_progress")]
        if open_actions:
            items = []
            for action in sorted(open_actions, key=lambda a: a.deadline or datetime.max.replace(tzinfo=timezone.utc))[:5]:
                deadline_str = ""
                if action.deadline:
                    deadline_str = f" (due {format_relative(action.deadline, now)})"
                items.append(f"• {action.description}{deadline_str}")
            sections.append(BriefingSection(
                title="📋 Open Actions",
                content="\n".join(items),
                priority=4,
            ))

        # 5. All clear
        if not sections:
            sections.append(BriefingSection(
                title="✅ All Clear",
                content="Nothing needs your attention right now. Have a great day!",
                priority=5,
            ))

        briefing = await self._get_or_create_briefing(
            briefing_type=BriefingType.MORNING,
            sections=sections,
            valid_until=now + timedelta(hours=4),
            now=now,
        )
        return briefing

    async def generate_catch_me_up(
        self,
        conversations: list[Conversation],
        since: datetime,
        now: datetime | None = None,
    ) -> Briefing:
        """
        Generate a delta briefing since the user last checked.

        Shows only what's new and important since `since`.
        """
        if now is None:
            now = now_utc()

        new_convs = [
            c for c in conversations
            if c.last_activity and c.last_activity > since
        ]

        sections: list[BriefingSection] = []

        # Important new activity
        important = [c for c in new_convs if c.importance >= 0.4 or c.urgency >= 0.4]
        if important:
            items = []
            for conv in sorted(important, key=lambda c: max(c.urgency, c.importance), reverse=True)[:8]:
                items.append(f"• {conv.subject} — {conv.summary or conv.relevance_explanation or ''}")
            sections.append(BriefingSection(
                title=f"Since {format_relative(since, now)}",
                content="\n".join(items),
                priority=1,
            ))

        # Noise filter summary
        noise_count = len(new_convs) - len(important)
        if noise_count > 0:
            sections.append(BriefingSection(
                title="Filtered",
                content=f"{noise_count} low-priority items filtered. View in Feed.",
                priority=5,
            ))

        if not sections:
            sections.append(BriefingSection(
                title="Nothing New",
                content=f"No significant updates since {format_relative(since, now)}.",
                priority=5,
            ))

        briefing = await self._get_or_create_briefing(
            briefing_type=BriefingType.CATCH_ME_UP,
            sections=sections,
            valid_until=now + timedelta(hours=1),
            now=now,
        )
        return briefing

    async def generate_meeting_prep(
        self,
        meeting_title: str,
        participants: list[str],
        conversations: list[Conversation],
        actions: list[ActionItem],
        now: datetime | None = None,
    ) -> Briefing:
        """
        Generate a meeting prep packet.

        Gathers all relevant context from conversations involving
        the meeting participants and related topics.
        """
        if now is None:
            now = now_utc()

        sections: list[BriefingSection] = []
        participant_set = {p.lower() for p in participants}

        # Related conversations (participant overlap)
        related = []
        for conv in conversations:
            if conv.participants:
                conv_participants = {p.lower() for p in conv.participants}
                if conv_participants & participant_set:
                    related.append(conv)

        if related:
            items = []
            for conv in sorted(related, key=lambda c: c.last_activity or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:5]:
                items.append(f"• {conv.subject} — {conv.summary or ''}")
            sections.append(BriefingSection(
                title=f"Context for: {meeting_title}",
                content="\n".join(items),
                priority=1,
            ))

        # Related action items
        related_actions = [a for a in actions if a.owner_id.lower() in participant_set]
        if related_actions:
            items = [f"• {a.description} ({a.status.value})" for a in related_actions[:5]]
            sections.append(BriefingSection(
                title="Open Action Items",
                content="\n".join(items),
                priority=2,
            ))

        if not sections:
            sections.append(BriefingSection(
                title=f"Meeting: {meeting_title}",
                content="No related context found in your conversations.",
                priority=1,
            ))

        briefing = await self._get_or_create_briefing(
            briefing_type=BriefingType.MEETING_PREP,
            sections=sections,
            valid_until=now + timedelta(hours=2),
            now=now,
        )
        return briefing

    async def generate_eod_recap(
        self,
        conversations: list[Conversation],
        actions: list[ActionItem],
        now: datetime | None = None,
    ) -> Briefing:
        """
        Generate an End-of-Day (EOD) recap briefing.

        Sections:
        1. Accomplished today (completed action items)
        2. Today's movement (active conversations)
        3. Pending for tomorrow
        """
        if now is None:
            now = now_utc()

        sections: list[BriefingSection] = []

        # 1. Accomplished
        completed = [a for a in actions if a.status.value == "completed"]
        if completed:
            items = [f"✓ {a.description}" for a in completed[:5]]
            sections.append(BriefingSection(
                title="✅ Completed Today",
                content="\n".join(items),
                priority=1,
            ))

        # 2. Movement
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_convs = [
            c for c in conversations
            if c.last_activity and c.last_activity >= today_start
        ]
        if today_convs:
            items = []
            for conv in sorted(today_convs, key=lambda c: c.urgency, reverse=True)[:5]:
                items.append(f"• {conv.subject} ({conv.source.value})")
            sections.append(BriefingSection(
                title="📊 Today's Activity",
                content="\n".join(items),
                priority=2,
            ))

        # 3. Pending
        open_items = [a for a in actions if a.status.value in ("open", "in_progress")]
        if open_items:
            items = [f"• {a.description}" for a in open_items[:5]]
            sections.append(BriefingSection(
                title="⏳ Pending for Tomorrow",
                content="\n".join(items),
                priority=3,
            ))

        if not sections:
            sections.append(BriefingSection(
                title="✨ Day Complete",
                content="No active items tracked today. Rest well!",
                priority=1,
            ))

        briefing = await self._get_or_create_briefing(
            briefing_type=BriefingType.EOD,
            sections=sections,
            valid_until=now + timedelta(hours=8),
            now=now,
        )
        return briefing

    async def generate_weekly_digest(
        self,
        conversations: list[Conversation],
        actions: list[ActionItem],
        top_people: list[Any] | None = None,
        now: datetime | None = None,
    ) -> Briefing:
        """
        Generate a weekly digest briefing.

        Sections:
        1. Week in review highlights
        2. Top collaborators
        3. Opportunities pipeline
        4. Pending action items
        """
        if now is None:
            now = now_utc()

        sections: list[BriefingSection] = []

        # 1. Week in review
        week_ago = now - timedelta(days=7)
        week_convs = [
            c for c in conversations
            if c.last_activity and c.last_activity >= week_ago and c.importance > 0.5
        ]
        if week_convs:
            items = [f"• {c.subject}" for c in week_convs[:6]]
            sections.append(BriefingSection(
                title="📅 Week in Review Highlights",
                content="\n".join(items),
                priority=1,
            ))

        # 2. Top collaborators
        if top_people:
            people_names = [getattr(p, "display_name", str(p)) for p in top_people[:5]]
            sections.append(BriefingSection(
                title="👥 Top Collaborators This Week",
                content=", ".join(people_names),
                priority=2,
            ))

        # 3. Opportunities
        opps = [c for c in conversations if c.opportunity_score > 0.5]
        if opps:
            items = [f"• {c.subject} ({c.opportunity_score:.0%})" for c in opps[:4]]
            sections.append(BriefingSection(
                title="🌟 Opportunity Pipeline",
                content="\n".join(items),
                priority=3,
            ))

        # 4. Open actions
        open_items = [a for a in actions if a.status.value in ("open", "in_progress")]
        if open_items:
            items = [f"• {a.description}" for a in open_items[:5]]
            sections.append(BriefingSection(
                title="📋 Open Action Items",
                content="\n".join(items),
                priority=4,
            ))

        if not sections:
            sections.append(BriefingSection(
                title="📊 Weekly Summary",
                content="A calm week with no unresolved high-priority items.",
                priority=1,
            ))

        briefing = await self._get_or_create_briefing(
            briefing_type=BriefingType.WEEKLY,
            sections=sections,
            valid_until=now + timedelta(days=2),
            now=now,
        )
        return briefing

    async def _get_or_create_briefing(
        self,
        briefing_type: BriefingType,
        sections: list[BriefingSection],
        valid_until: datetime,
        now: datetime,
    ) -> Briefing:
        """Check 5-minute idempotency cache or create and publish new briefing."""
        chash = self._compute_hash(sections)
        cache_key = f"{briefing_type.value}:{chash}"

        # 5-minute (300s) idempotency check from the original spec
        if cache_key in self._cache:
            cached_time, cached_briefing = self._cache[cache_key]
            if (now - cached_time).total_seconds() < 300:
                logger.info("Serving cached briefing for %s (hash: %s)", briefing_type.value, chash[:8])
                return cached_briefing

        briefing = Briefing(
            type=briefing_type,
            sections=sections,
            content_hash=chash,
            valid_until=valid_until,
            generated_at=now,
        )

        self._cache[cache_key] = (now, briefing)

        # Evict cache entries older than 1 hour
        cutoff = now - timedelta(hours=1)
        self._cache = {k: v for k, v in self._cache.items() if v[0] > cutoff}

        await self.event_bus.publish(
            BriefingReady(briefing_id=briefing.id, briefing_type=briefing_type)
        )

        logger.info("Generated %s briefing: %d sections", briefing_type.value, len(sections))
        return briefing

    def _compute_hash(self, sections: list[BriefingSection]) -> str:
        """Compute content hash for idempotency."""
        content = "|".join(f"{s.title}:{s.content}" for s in sections)
        return content_hash(content)
