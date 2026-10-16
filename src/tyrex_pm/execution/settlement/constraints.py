"""Verified current trading constraints required before a live hold-profile entry.

Scenario fees and the research minimum-size assumption never reach a live order. An
entry is allowed only when, for the market being traded:

* the current fee schedule is known from provider metadata (``feeSchedule`` / ``fd``),
  parsed from one of the two observed shapes, taker-only, with an integer exponent;
* the strategy's fee curve equals that schedule (its economics priced the same fees);
* tick size and minimum order size are known and positive.

Anything unknown, unparsed or contradictory blocks the entry with a stable reason.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from tyrex_pm.domain.polymarket.fees import FeeCurveParams


@dataclass(frozen=True)
class VerifiedConstraints:
    ok: bool
    reasons: tuple[str, ...]
    fee_curve: FeeCurveParams | None = None
    tick_size: Decimal | None = None
    min_order_size: Decimal | None = None
    fee_source: str | None = None


def parse_fee_schedule(raw: Any) -> tuple[FeeCurveParams | None, str | None]:
    """Parse ``{"r","e","to"}`` (CLOB) or ``{"rate","exponent","takerOnly"}`` (Gamma)."""
    if not isinstance(raw, Mapping):
        return None, "FEE_SCHEDULE_MISSING"
    if "r" in raw and "e" in raw:
        rate, exponent, taker_only = raw["r"], raw["e"], raw.get("to")
    elif "rate" in raw and "exponent" in raw:
        rate, exponent, taker_only = raw["rate"], raw["exponent"], raw.get("takerOnly")
    else:
        return None, "FEE_SCHEDULE_UNPARSED"
    if taker_only is not True:
        return None, "FEE_SCHEDULE_NOT_TAKER_ONLY"
    try:
        rate_d, exponent_d = Decimal(str(rate)), Decimal(str(exponent))
    except (InvalidOperation, ValueError):
        return None, "FEE_SCHEDULE_UNPARSED"
    if rate_d < 0 or rate_d > 1 or exponent_d < 0 or exponent_d != exponent_d.to_integral_value():
        return None, "FEE_SCHEDULE_UNSUPPORTED"
    return FeeCurveParams(fee_rate=rate_d, exponent=exponent_d), None


def verify_constraints(
    *,
    fee_schedule: Any,
    strategy_fee_curve: FeeCurveParams | None,
    tick_size: Decimal | None,
    min_order_size: Decimal | None,
    fee_source: str,
) -> VerifiedConstraints:
    reasons: list[str] = []
    curve, problem = parse_fee_schedule(fee_schedule)
    if problem:
        reasons.append(problem)
    if curve is not None and strategy_fee_curve is not None and (
        curve.fee_rate != strategy_fee_curve.fee_rate
        or curve.exponent != strategy_fee_curve.exponent
    ):
        reasons.append("STRATEGY_FEE_CURVE_MISMATCH")
    if curve is not None and strategy_fee_curve is None:
        reasons.append("STRATEGY_FEE_CURVE_UNKNOWN")
    if tick_size is None or tick_size <= 0:
        reasons.append("TICK_SIZE_UNKNOWN")
    if min_order_size is None or min_order_size <= 0:
        reasons.append("MIN_ORDER_SIZE_UNKNOWN")
    return VerifiedConstraints(
        ok=not reasons,
        reasons=tuple(reasons),
        fee_curve=curve,
        tick_size=tick_size,
        min_order_size=min_order_size,
        fee_source=fee_source,
    )


FeeScheduleSource = Callable[[str, str], Awaitable[Any]]
"""``(window_slug, condition_id) -> raw fee schedule mapping or None``."""


async def gamma_fee_schedule(window_slug: str, condition_id: str) -> Any:
    """Current fee schedule from the public Gamma event (read-only, outside evaluate)."""
    import asyncio

    from tyrex_pm.adapters.polymarket.discovery import fetch_gamma_event

    event = await asyncio.to_thread(fetch_gamma_event, window_slug)
    markets = event.get("markets") if isinstance(event, Mapping) else None
    for market in markets or []:
        cid = market.get("conditionId") or market.get("condition_id")
        if str(cid).lower() == condition_id.lower():
            return market.get("feeSchedule") or market.get("fd") or market.get("fee_schedule")
    return None
