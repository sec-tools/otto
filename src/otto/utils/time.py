from __future__ import annotations
"""
Time utilities — timezone handling and signal decay.

All internal timestamps are UTC. User-facing times are converted
to the user's detected timezone.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def now_utc() -> datetime:
    """Current time in UTC."""
    return datetime.now(timezone.utc)


def to_user_timezone(dt: datetime, tz_name: str | None = None) -> datetime:
    """
    Convert a UTC datetime to the user's timezone.

    Args:
        dt: UTC datetime.
        tz_name: IANA timezone name. Auto-detected if None.

    Returns:
        Datetime in user's local timezone.
    """
    if tz_name is None:
        tz_name = _detect_local_timezone()
    try:
        local_tz = ZoneInfo(tz_name)
        return dt.astimezone(local_tz)
    except Exception:
        return dt  # Return UTC if timezone is invalid or unsupported


def _detect_local_timezone() -> str:
    """Detect the system's local timezone."""
    try:
        import subprocess
        result = subprocess.run(
            ["systemsetup", "-gettimezone"],
            capture_output=True, text=True, timeout=5,
        )
        # Output: "Time Zone: America/Los_Angeles"
        if ":" in result.stdout:
            tz = result.stdout.split(":", 1)[1].strip()
            ZoneInfo(tz)  # validate
            return tz
    except Exception:
        pass

    # Fallback
    try:
        import time
        tz = time.tzname[0]
        ZoneInfo(tz)  # validate
        return tz
    except Exception:
        return "UTC"


def hours_since(dt: datetime, now: datetime | None = None) -> float:
    """Hours elapsed since a datetime."""
    if now is None:
        now = now_utc()
    return (now - dt).total_seconds() / 3600


def is_within_hours(dt: datetime, hours: float, now: datetime | None = None) -> bool:
    """Check if a datetime is within N hours of now."""
    return hours_since(dt, now) <= hours


def format_relative(dt: datetime, now: datetime | None = None) -> str:
    """
    Format a datetime as a human-readable relative string.

    Examples: "2 minutes ago", "3 hours ago", "yesterday", "3 days ago"
    """
    if now is None:
        now = now_utc()

    delta = now - dt
    seconds = int(delta.total_seconds())

    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    elif seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    elif seconds < 172800:
        return "yesterday"
    else:
        days = seconds // 86400
        return f"{days} days ago"
