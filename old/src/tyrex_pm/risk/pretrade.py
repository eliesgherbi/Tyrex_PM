from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.models import EnterIntent, ExitIntent, ReduceIntent


def estimate_notional(intent: EnterIntent | ExitIntent | ReduceIntent) -> Decimal:
    if intent.limit_price is None:
        return Decimal("0")
    return intent.size * intent.limit_price


def clip_intent_to_max_notional(
    intent: EnterIntent | ExitIntent | ReduceIntent,
    max_usd: Decimal,
) -> EnterIntent | ExitIntent | ReduceIntent:
    price = intent.limit_price
    if price is None or price <= 0:
        return intent
    new_size = max_usd / price
    return replace(intent, size=new_size)


def apply_notional_min_max(
    intent: EnterIntent | ExitIntent | ReduceIntent,
    *,
    min_usd: Decimal,
    max_usd: Decimal,
    max_policy: str,
) -> tuple[EnterIntent | ExitIntent | ReduceIntent, dict[str, Any], str | None]:
    """
    Enforce min/max notional. When notional > max_usd and max_policy is cap, clip intent size.

    Returns (intent_for_downstream_checks, extensions merged into risk_decision facts, deny_reason).
    """
    ext: dict[str, Any] = {
        "notional_max_policy": max_policy,
        "notional_max_usd": str(max_usd),
    }
    work: EnterIntent | ExitIntent | ReduceIntent = intent
    n0 = estimate_notional(intent)
    if n0 < min_usd:
        ext["order_notional_usd"] = str(n0)
        return work, ext, rc.NOTIONAL_BELOW_MIN

    if n0 > max_usd:
        if max_policy != "cap":
            ext["order_notional_usd"] = str(n0)
            ext["notional_denied_above_max"] = True
            return intent, ext, rc.NOTIONAL_ABOVE_MAX
        work = clip_intent_to_max_notional(intent, max_usd)
        n1 = estimate_notional(work)
        ext["notional_capped"] = True
        ext["prior_notional_usd"] = str(n0)
        ext["order_notional_usd"] = str(n1)
        if n1 < min_usd:
            return work, ext, rc.NOTIONAL_BELOW_MIN
    else:
        ext["order_notional_usd"] = str(n0)

    return work, ext, None
