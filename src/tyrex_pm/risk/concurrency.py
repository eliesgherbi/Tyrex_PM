from __future__ import annotations

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.models import RiskContext


def check_concurrency(ctx: RiskContext, *, max_in_flight: int) -> tuple[bool, str | None]:
    if ctx.in_flight_order_count >= max_in_flight:
        return False, rc.CONCURRENCY_LIMIT
    return True, None
