"""Reporting views of UTC instants. Accounting stays UTC; Paris time is presentation only."""

from __future__ import annotations

from datetime import datetime, timezone

REPORTING_TIMEZONE = "Europe/Paris"


def paris_iso(value: datetime | str | None) -> str | None:
    """ISO-8601 Paris local time of a UTC instant (None passes through)."""
    if value is None:
        return None
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return instant.astimezone(ZoneInfo(REPORTING_TIMEZONE)).isoformat()
    except Exception:  # noqa: BLE001 - missing tz database: say so instead of guessing
        return f"{instant.astimezone(timezone.utc).isoformat()} (Paris time unavailable)"
