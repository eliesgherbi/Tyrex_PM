from __future__ import annotations

from typing import Any

from tyrex_pm.reporting.schema_v2 import OMS_RESULT_PAYLOAD_KEY


def get_oms_result_text(payload: dict[str, Any] | None) -> str | None:
    """Canonical OMS response summary; accepts legacy `shadow_result` from older facts."""
    if not payload:
        return None
    v = payload.get(OMS_RESULT_PAYLOAD_KEY)
    if v is not None:
        return str(v)
    leg = payload.get("shadow_result")
    if leg is not None:
        return str(leg)
    return None
