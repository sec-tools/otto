"""
OAuth Scope Validation.

This module is responsible for enforcing read-only constraints at the OAuth level.
It verifies that any tokens granted to the application DO NOT contain scopes that
would allow data modification or destructive actions. It is a key component of the
defense-in-depth strategy for the read-only guarantee.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Set

logger = logging.getLogger(__name__)

# Required scopes that are necessary for the application to function.
# These must all be read-only scopes.
REQUIRED_READ_SCOPES: Dict[str, Set[str]] = {
    "gmail": {"https://www.googleapis.com/auth/gmail.readonly"},
    "slack": {"channels:history", "channels:read", "users:read", "team:read"},
    "jira": {"read:jira-work", "read:jira-user"},
    "calendar": {"https://www.googleapis.com/auth/calendar.readonly"}
}

# Forbidden scopes that explicitly grant write, modify, or delete access.
# If ANY of these are present, the token must be rejected immediately.
FORBIDDEN_WRITE_SCOPES: Dict[str, Set[str]] = {
    "gmail": {
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.send"
    },
    "slack": {
        "chat:write", "chat:write.public", "chat:write.customize",
        "files:write", "groups:write", "channels:manage"
    },
    "jira": {
        "write:jira-work", "manage:jira-project", "manage:jira-configuration"
    },
    "calendar": {
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events"
    }
}


@dataclass
class ScopeValidationResult:
    is_valid: bool
    missing_read_scopes: Set[str]
    present_write_scopes: Set[str]
    error_message: str = ""


class ScopeValidator:
    """Validates OAuth scopes against defined read-only policies."""

    @staticmethod
    def validate_scopes(source: str, granted_scopes: Set[str]) -> ScopeValidationResult:
        """
        Validate the scopes granted by a specific source.
        Fails if any forbidden write scope is present, or if required read scopes are missing.
        """
        if source not in REQUIRED_READ_SCOPES or source not in FORBIDDEN_WRITE_SCOPES:
            logger.warning(f"Unknown source '{source}' in scope validation. Denying by default.")
            return ScopeValidationResult(
                is_valid=False,
                missing_read_scopes=set(),
                present_write_scopes=set(),
                error_message=f"Unknown source: {source}"
            )

        required = REQUIRED_READ_SCOPES[source]
        forbidden = FORBIDDEN_WRITE_SCOPES[source]

        missing_read = required - granted_scopes
        present_write = forbidden & granted_scopes

        is_valid = len(missing_read) == 0 and len(present_write) == 0
        error_message = ""

        if len(present_write) > 0:
            error_message = f"CRITICAL: Forbidden write scopes detected for {source}: {present_write}. " \
                            f"The read-only guarantee is compromised."
            logger.error(error_message)
        elif len(missing_read) > 0:
            error_message = f"Missing required read scopes for {source}: {missing_read}."
            logger.warning(error_message)

        return ScopeValidationResult(
            is_valid=is_valid,
            missing_read_scopes=missing_read,
            present_write_scopes=present_write,
            error_message=error_message
        )

    @classmethod
    def validate_all(cls, sources_and_scopes: Dict[str, Set[str]]) -> Dict[str, ScopeValidationResult]:
        """
        Validate scopes for all connected sources.
        """
        results = {}
        for source, scopes in sources_and_scopes.items():
            results[source] = cls.validate_scopes(source, scopes)
        return results
