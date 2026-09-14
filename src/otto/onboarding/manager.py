from __future__ import annotations

"""
Onboarding flow — progressive, checkpointed, resumable.

Guides the user from install to first value in < 30 seconds.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import sys
if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum
    class StrEnum(str, Enum):
        pass

logger = logging.getLogger("otto.onboarding")


class OnboardingStep(StrEnum):
    """Onboarding progress steps (each is checkpointed)."""
    WELCOME = "welcome"
    API_KEY = "api_key"
    SOURCE_EMAIL = "source_email"
    SOURCE_SLACK = "source_slack"
    SOURCE_JIRA = "source_jira"
    SOURCE_CALENDAR = "source_calendar"
    OBSERVING = "observing"
    CALIBRATING = "calibrating"
    STABILIZING = "stabilizing"
    STEADY_STATE = "steady_state"


class AdaptivePhase(StrEnum):
    """The adaptive ramp phases from the original spec."""
    OBSERVATION = "observation"       # 0–1 hour
    CALIBRATION = "calibration"       # 1h–1 day
    STABILIZATION = "stabilization"   # 1d–1 week
    STEADY_STATE = "steady_state"     # 1 week+


@dataclass
class OnboardingState:
    """Checkpointed onboarding state (survives restart)."""
    current_step: OnboardingStep = OnboardingStep.WELCOME
    completed_steps: list[str] = field(default_factory=list)
    skipped_steps: list[str] = field(default_factory=list)
    api_key_configured: bool = False
    connected_sources: list[str] = field(default_factory=list)
    first_value_delivered: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_checkpoint: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    adaptive_phase: AdaptivePhase = AdaptivePhase.OBSERVATION
    feedback_count: int = 0


class OnboardingManager:
    """
    Manages the onboarding flow.

    Key principles:
    1. Time to first value: < 30 seconds
    2. One source is enough to start
    3. Steps are skippable and resumable
    4. OAuth friction is mitigated with pre-flight checks
    5. Progressive source connection (suggests based on detected references)
    """

    # Messages for each step
    MESSAGES = {
        OnboardingStep.WELCOME: {
            "title": "Welcome to Otto",
            "subtitle": "Your personal AI assistant. Read-only. Private. Local.",
        },
        OnboardingStep.API_KEY: {
            "title": "AI Configuration",
            "subtitle": "Otto can run AI locally — no API key needed.",
            "options": ["Use Local AI (Ollama)", "Enter API Key"],
        },
        OnboardingStep.SOURCE_EMAIL: {
            "title": "Connect your email",
            "subtitle": "Let's start with email. You can add more sources anytime.",
        },
        OnboardingStep.SOURCE_SLACK: {
            "title": "Add Slack?",
            "subtitle": "I notice Slack threads in your emails. Connect Slack for full context?",
        },
        OnboardingStep.SOURCE_JIRA: {
            "title": "Add Jira?",
            "subtitle": "I see Jira ticket references. Connect Jira to track your tickets?",
        },
        OnboardingStep.SOURCE_CALENDAR: {
            "title": "Add Calendar?",
            "subtitle": "Connect your calendar for meeting prep and schedule awareness.",
        },
        OnboardingStep.OBSERVING: {
            "title": "Getting to know you ☕",
            "subtitle": "Reading your emails... Otto will show first results within 30 seconds.",
        },
    }

    # Empty states during ingestion
    INGESTION_MESSAGES = [
        "Reading your emails... getting to know you ☕",
        "Building your relationship graph...",
        "Analyzing conversation patterns...",
        "Almost ready...",
    ]

    def __init__(
        self,
        state: OnboardingState | None = None,
        checkpoint_path: str | Any | None = None,
    ) -> None:
        self._state = state or OnboardingState()
        self._checkpoint_path = str(checkpoint_path) if checkpoint_path else None

    @property
    def state(self) -> OnboardingState:
        return self._state

    @property
    def is_complete(self) -> bool:
        return self._state.current_step == OnboardingStep.STEADY_STATE

    @property
    def current_message(self) -> dict[str, str]:
        return self.MESSAGES.get(self._state.current_step, {
            "title": "Otto",
            "subtitle": "Setting up...",
        })

    def advance(self) -> OnboardingStep:
        """Advance to the next step."""
        step_order = list(OnboardingStep)
        current_idx = step_order.index(self._state.current_step)

        self._state.completed_steps.append(self._state.current_step.value)
        if current_idx + 1 < len(step_order):
            self._state.current_step = step_order[current_idx + 1]
        self._checkpoint()

        logger.info("Onboarding advanced to: %s", self._state.current_step)
        return self._state.current_step

    def skip(self) -> OnboardingStep:
        """Skip the current step."""
        self._state.skipped_steps.append(self._state.current_step.value)
        return self.advance()

    def complete_api_key(self, provider: str = "local") -> None:
        """Mark API key setup as complete."""
        self._state.api_key_configured = True
        self._checkpoint()

    def complete_source(self, source: str) -> None:
        """Mark a source connection as complete."""
        if source not in self._state.connected_sources:
            self._state.connected_sources.append(source)
        self._checkpoint()

    def mark_first_value(self) -> None:
        """Mark that first useful content was shown to the user."""
        self._state.first_value_delivered = True
        elapsed = (datetime.now(timezone.utc) - self._state.started_at).total_seconds()
        logger.info("First value delivered in %.1f seconds", elapsed)
        self._checkpoint()

    def update_adaptive_phase(self) -> AdaptivePhase:
        """
        Update the adaptive ramp phase based on time and feedback.

        Phases:
        - Observation (0–1h): Conservative, build initial model
        - Calibration (1h–1d): >80% confidence only, solicit feedback
        - Stabilization (1d–1w): Normal operation, opportunity detection
        - Steady State (1w+): Full autonomous, passive adaptation
        """
        elapsed = datetime.now(timezone.utc) - self._state.started_at

        if elapsed < timedelta(hours=1):
            self._state.adaptive_phase = AdaptivePhase.OBSERVATION
        elif elapsed < timedelta(days=1):
            self._state.adaptive_phase = AdaptivePhase.CALIBRATION
        elif elapsed < timedelta(weeks=1):
            self._state.adaptive_phase = AdaptivePhase.STABILIZATION
        else:
            self._state.adaptive_phase = AdaptivePhase.STEADY_STATE
            if self._state.current_step in (OnboardingStep.OBSERVING, OnboardingStep.CALIBRATING, OnboardingStep.STABILIZING):
                self._state.current_step = OnboardingStep.STEADY_STATE

        self._checkpoint()
        return self._state.adaptive_phase

    def get_confidence_threshold(self) -> float:
        """
        Get the minimum confidence threshold for the current phase.

        Conservative early on, relaxes over time.
        """
        thresholds = {
            AdaptivePhase.OBSERVATION: 0.9,
            AdaptivePhase.CALIBRATION: 0.8,
            AdaptivePhase.STABILIZATION: 0.6,
            AdaptivePhase.STEADY_STATE: 0.5,
        }
        return thresholds.get(self._state.adaptive_phase, 0.8)

    def record_feedback(self) -> None:
        """Track feedback count for phase progression."""
        self._state.feedback_count += 1
        self._checkpoint()

    def should_suggest_source(self, source: str, evidence: str = "") -> bool:
        """
        Check if we should suggest connecting a new source.

        Returns True if:
        - Source is not yet connected
        - Not skipped in onboarding
        - Evidence was found (e.g., Jira ticket IDs in emails)
        """
        return (
            source not in self._state.connected_sources
            and source not in self._state.skipped_steps
            and bool(evidence)
        )

    def get_resume_message(self) -> str:
        """Get a message for resuming after restart."""
        sources = ", ".join(self._state.connected_sources)
        if sources:
            return f"Welcome back! {sources} connected. Ready to go."
        return "Welcome back! Pick up where you left off."

    def _checkpoint(self) -> None:
        """Save state checkpoint."""
        self._state.last_checkpoint = datetime.now(timezone.utc)
        if self._checkpoint_path:
            try:
                self.save_checkpoint(self._checkpoint_path)
            except Exception as e:
                logger.error("Failed to write onboarding checkpoint: %s", e)

    def to_dict(self) -> dict[str, Any]:
        """Serialize state for persistence."""
        return {
            "current_step": self._state.current_step.value,
            "completed_steps": self._state.completed_steps,
            "skipped_steps": self._state.skipped_steps,
            "api_key_configured": self._state.api_key_configured,
            "connected_sources": self._state.connected_sources,
            "first_value_delivered": self._state.first_value_delivered,
            "started_at": self._state.started_at.isoformat(),
            "last_checkpoint": self._state.last_checkpoint.isoformat(),
            "adaptive_phase": self._state.adaptive_phase.value,
            "feedback_count": self._state.feedback_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], checkpoint_path: str | Any | None = None) -> OnboardingManager:
        """Reconstruct OnboardingManager from serialized dict."""
        try:
            started_at = datetime.fromisoformat(data["started_at"])
        except (KeyError, ValueError, TypeError):
            started_at = datetime.now(timezone.utc)

        try:
            last_checkpoint = datetime.fromisoformat(data["last_checkpoint"])
        except (KeyError, ValueError, TypeError):
            last_checkpoint = datetime.now(timezone.utc)

        try:
            current_step = OnboardingStep(data.get("current_step", OnboardingStep.WELCOME.value))
        except ValueError:
            current_step = OnboardingStep.WELCOME

        try:
            adaptive_phase = AdaptivePhase(data.get("adaptive_phase", AdaptivePhase.OBSERVATION.value))
        except ValueError:
            adaptive_phase = AdaptivePhase.OBSERVATION

        state = OnboardingState(
            current_step=current_step,
            completed_steps=data.get("completed_steps", []),
            skipped_steps=data.get("skipped_steps", []),
            api_key_configured=data.get("api_key_configured", False),
            connected_sources=data.get("connected_sources", []),
            first_value_delivered=data.get("first_value_delivered", False),
            started_at=started_at,
            last_checkpoint=last_checkpoint,
            adaptive_phase=adaptive_phase,
            feedback_count=data.get("feedback_count", 0),
        )
        return cls(state=state, checkpoint_path=checkpoint_path)

    def save_checkpoint(self, path: str | Any) -> None:
        """Save onboarding state to JSON file."""
        import json
        import os
        from pathlib import Path

        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
        os.replace(tmp_path, file_path)

    @classmethod
    def load_checkpoint(cls, path: str | Any) -> OnboardingManager:
        """Load onboarding state from JSON file."""
        import json
        from pathlib import Path

        file_path = Path(path)
        if not file_path.exists():
            return cls(checkpoint_path=str(path))

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data, checkpoint_path=str(path))
