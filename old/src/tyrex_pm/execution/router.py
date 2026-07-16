from __future__ import annotations

from tyrex_pm.core.models import ApprovedIntent


def route_approved(ap: ApprovedIntent) -> str:
    """Returns routing label for facts."""
    return f"{ap.intent.token_id}:{ap.intent.side.value}"
