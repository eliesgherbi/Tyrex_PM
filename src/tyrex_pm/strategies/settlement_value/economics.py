"""Pure settlement_value economics: probability interval, depth cost and size.

No I/O, no clocks. Prices, shares and money are Decimal; probability arithmetic is
float and converted explicitly. Buy fees are charged in shares:
``net = gross * (1 - phi(p) / p)`` at each level walked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Iterable

from tyrex_pm.core.snapshots import BookLevel
from tyrex_pm.domain.polymarket.fees import FeeCurveParams, phi_taker_fee_per_share
from tyrex_pm.indicators.binary_fair_value import FairValueInput, compute_fair_value
from tyrex_pm.indicators.ewma_volatility import SIGMA_UNITS_PER_SQRT_SECOND
from tyrex_pm.strategies.settlement_value.config import (
    SettlementEntryConfig,
    SettlementModelConfig,
    SettlementSizingConfig,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True, kw_only=True)
class ProbabilityEstimate:
    ready: bool
    reason: str | None
    backend: str
    p_up_point: float | None = None
    p_up_low: float | None = None
    p_up_high: float | None = None
    z: float | None = None
    sigma: float | None = None
    tau_s: float | None = None
    interval_method: str = "sigma_and_reference_band_v1"

    def conservative(self, *, up: bool) -> float | None:
        """Lower bound for the side bought: p_low for Up, 1 - p_high for Down."""
        if not self.ready or self.p_up_low is None or self.p_up_high is None:
            return None
        return self.p_up_low if up else 1.0 - self.p_up_high

    def point(self, *, up: bool) -> float | None:
        if not self.ready or self.p_up_point is None:
            return None
        return self.p_up_point if up else 1.0 - self.p_up_point


def endpoint_probability(
    *,
    S: Decimal | None,
    K: Decimal | None,
    sigma: float | None,
    tau_s: float | None,
    model: SettlementModelConfig,
) -> ProbabilityEstimate:
    """Endpoint diffusion point estimate plus a sigma/reference band interval."""

    def price(s: Decimal | None, sig: float | None):
        return compute_fair_value(
            FairValueInput(
                S=s,
                K=K,
                sigma=sig,
                tau_s=tau_s,
                sigma_units=SIGMA_UNITS_PER_SQRT_SECOND,
                tau_floor_s=model.tau_floor_s,
            )
        )

    point = price(S, sigma)
    if point.p_up is None or S is None or sigma is None:
        return ProbabilityEstimate(
            ready=False, reason=point.reject_reason or "model_not_ready", backend=model.backend
        )
    shift = Decimal(str(model.reference_band_bps)) / Decimal("10000")
    sigmas = (sigma * (1.0 - model.sigma_band), sigma, sigma * (1.0 + model.sigma_band))
    spots = (S * (_ONE - shift), S, S * (_ONE + shift))
    grid = [
        p.p_up
        for p in (price(s, sig) for s in spots for sig in sigmas if sig > 0)
        if p.p_up is not None
    ]
    return ProbabilityEstimate(
        ready=True,
        reason=None,
        backend=model.backend,
        p_up_point=point.p_up,
        p_up_low=min(grid),
        p_up_high=max(grid),
        z=point.z,
        sigma=sigma,
        tau_s=tau_s,
    )


@dataclass(frozen=True, kw_only=True)
class SideCandidate:
    up: bool
    feasible: bool
    reason: str
    binding_constraint: str
    p_conservative: float | None
    p_point: float | None
    best_ask: Decimal | None
    gross_shares: Decimal = _ZERO
    net_shares: Decimal = _ZERO
    debit: Decimal = _ZERO
    worst_price: Decimal | None = None
    vwap: Decimal | None = None
    ev_conservative: Decimal = _ZERO
    ev_point: Decimal = _ZERO
    first_level_edge: Decimal | None = None
    levels_used: int = 0
    target_feasible: bool | None = None

    @property
    def side(self) -> str:
        return "UP" if self.up else "DOWN"


def _floor(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _prob(value: float) -> Decimal:
    return Decimal(repr(min(max(value, 0.0), 1.0)))


def size_side(
    asks: Iterable[BookLevel],
    *,
    up: bool,
    p_conservative: float | None,
    p_point: float | None,
    entry: SettlementEntryConfig,
    sizing: SettlementSizingConfig,
    fee_curve: FeeCurveParams,
    min_order_shares: Decimal,
) -> SideCandidate:
    """Walk asks while the marginal conservative edge clears the minimum.

    The marginal conservative value of one gross share bought at price p is
    ``p_cons * (1 - phi(p)/p) - p``; it falls as the walk climbs the book, so the
    greedy walk maximizes conservative EV under the debit cap (``cap_only``) and
    finds the smallest size reaching the target (``require_target``).
    """
    levels = sorted(asks, key=lambda lv: lv.price)[: sizing.max_levels]
    best_ask = levels[0].price if levels else None
    if p_conservative is None or p_point is None:
        return SideCandidate(
            up=up, feasible=False, reason="MODEL_NOT_READY", binding_constraint="model",
            p_conservative=p_conservative, p_point=p_point, best_ask=best_ask,
        )
    if not levels:
        return SideCandidate(
            up=up, feasible=False, reason="DEPTH_INSUFFICIENT", binding_constraint="no_asks",
            p_conservative=p_conservative, p_point=p_point, best_ask=None,
        )
    p_cons = _prob(p_conservative)
    p_pt = _prob(p_point)
    budget = min(sizing.max_debit_usdc, sizing.max_loss_usdc)
    require_target = entry.target_mode == "require_target"
    gross = net = debit = ev_cons = ev_pt = _ZERO
    worst: Decimal | None = None
    used = 0
    binding = "depth"
    first_edge: Decimal | None = None
    reached_target = False
    for level in levels:
        price = level.price
        if price <= 0 or price >= 1:
            continue
        if price > entry.max_entry_price:
            binding = "max_entry_price"
            break
        net_ratio = _ONE - phi_taker_fee_per_share(price, fee_curve) / price
        marginal = p_cons * net_ratio - price
        if first_edge is None:
            first_edge = marginal
        if marginal < entry.min_edge_per_share:
            binding = "min_edge"
            break
        available = level.quantity * sizing.depth_participation
        by_budget = (budget - debit) / price
        take = min(available, by_budget)
        if require_target and marginal > 0:
            needed = _ceil((entry.target_profit_usdc - ev_cons) / marginal, sizing.share_increment)
            if needed <= take:
                take, reached_target = needed, True
        take = _floor(take, sizing.share_increment)
        if take <= 0:
            binding = "max_debit"
            break
        gross += take
        net += take * net_ratio
        debit += take * price
        ev_cons += take * marginal
        ev_pt += take * (p_pt * net_ratio - price)
        worst = price
        used += 1
        if reached_target:
            binding = "target_reached"
            break
        if by_budget <= available:
            binding = "max_debit"
            break
    common = dict(
        up=up,
        p_conservative=p_conservative,
        p_point=p_point,
        best_ask=best_ask,
        gross_shares=gross,
        net_shares=net,
        debit=debit,
        worst_price=worst,
        vwap=None if gross == 0 else debit / gross,
        ev_conservative=ev_cons,
        ev_point=ev_pt,
        first_level_edge=first_edge,
        levels_used=used,
        binding_constraint=binding,
    )
    if gross == 0:
        priced_out = binding in ("min_edge", "max_entry_price")
        reason = "EDGE_BELOW_COST" if priced_out else "DEPTH_INSUFFICIENT"
        return SideCandidate(feasible=False, reason=reason, target_feasible=False, **common)
    if gross < min_order_shares:
        return SideCandidate(
            feasible=False, reason="BELOW_MINIMUM_SIZE", target_feasible=False, **common
        )
    if require_target and not reached_target:
        return SideCandidate(
            feasible=False, reason="TARGET_INFEASIBLE", target_feasible=False, **common
        )
    return SideCandidate(
        feasible=True,
        reason="EDGE_CLEARS_COST",
        target_feasible=ev_cons >= entry.target_profit_usdc,
        **common,
    )


def buffered_limit(
    worst_price: Decimal,
    *,
    p_conservative: float,
    buffer_ticks: int,
    tick: Decimal | None,
    max_entry_price: Decimal,
    fee_curve: FeeCurveParams,
) -> tuple[Decimal, str]:
    """Order limit: worst walked level plus up to ``buffer_ticks`` ticks.

    Stops at the last tick whose marginal conservative value is still >= 0 and never
    exceeds ``max_entry_price``. Returns (limit, how it was bounded).
    """
    if buffer_ticks <= 0:
        return worst_price, "no_buffer"
    if tick is None or tick <= 0:
        return worst_price, "buffer_skipped_tick_unknown"
    p_cons = _prob(p_conservative)
    limit, bound = worst_price, "buffer_full"
    for _ in range(buffer_ticks):
        candidate = limit + tick
        if candidate > max_entry_price or candidate >= 1:
            bound = "max_entry_price"
            break
        value = p_cons * (_ONE - phi_taker_fee_per_share(candidate, fee_curve) / candidate)
        if value - candidate < 0:
            bound = "nonnegative_value"
            break
        limit = candidate
    return limit, bound


@dataclass(frozen=True, kw_only=True)
class ExitCandidate:
    """Economic sale of held inventory, walked over bids (managed exits only)."""

    feasible: bool
    reason: str
    hold_value_per_share: Decimal | None
    best_bid: Decimal | None
    sell_shares: Decimal = _ZERO
    proceeds_net: Decimal = _ZERO
    worst_price: Decimal | None = None
    first_level_gain: Decimal | None = None
    levels_used: int = 0


def exit_walk(
    bids: Iterable[BookLevel],
    *,
    held_shares: Decimal,
    hold_value: float | None,
    margin: Decimal,
    fee_curve: FeeCurveParams,
    min_order_shares: Decimal,
    min_price: Decimal,
    share_increment: Decimal,
    max_levels: int,
) -> ExitCandidate:
    """Walk bids while the fee-net sale value of one share beats holding it by ``margin``.

    A sold share at price p yields ``p - phi(p)`` (sell fees in collateral). Holding it
    is worth ``hold_value`` (the model probability of the held side). Entry cost is sunk
    and plays no part in this choice. Never sells below ``min_price``.
    """
    levels = sorted(bids, key=lambda lv: lv.price, reverse=True)[:max_levels]
    best = levels[0].price if levels else None
    if hold_value is None:
        return ExitCandidate(
            feasible=False, reason="MODEL_NOT_READY", hold_value_per_share=None, best_bid=best
        )
    value = _prob(hold_value)
    remaining = _floor(held_shares, share_increment)
    sold = proceeds = _ZERO
    worst: Decimal | None = None
    first_gain: Decimal | None = None
    used = 0
    for level in levels:
        price = level.price
        if remaining <= 0 or price < min_price or price <= 0 or price >= 1:
            break
        gain = price - phi_taker_fee_per_share(price, fee_curve) - value
        if first_gain is None:
            first_gain = gain
        if gain < margin:
            break
        take = _floor(min(level.quantity, remaining), share_increment)
        if take <= 0:
            break
        sold += take
        proceeds += take * (price - phi_taker_fee_per_share(price, fee_curve))
        remaining -= take
        worst = price
        used += 1
    common = dict(
        hold_value_per_share=value,
        best_bid=best,
        sell_shares=sold,
        proceeds_net=proceeds,
        worst_price=worst,
        first_level_gain=first_gain,
        levels_used=used,
    )
    if not levels:
        return ExitCandidate(feasible=False, reason="NO_EXIT_BID", **common)
    if sold <= 0:
        return ExitCandidate(feasible=False, reason="HOLD_VALUE_ABOVE_BID", **common)
    if sold < min_order_shares:
        return ExitCandidate(feasible=False, reason="EXIT_BELOW_MINIMUM_SIZE", **common)
    return ExitCandidate(feasible=True, reason="BID_ABOVE_HOLD_VALUE", **common)


def choose_side(up: SideCandidate, down: SideCandidate) -> SideCandidate | None:
    """Deterministic best feasible side: higher conservative EV, then lower debit, then Up."""
    feasible = [c for c in (up, down) if c.feasible]
    if not feasible:
        return None
    return sorted(feasible, key=lambda c: (-c.ev_conservative, c.debit, 0 if c.up else 1))[0]


def book_implied_up(up_bid: Decimal | None, up_ask: Decimal | None) -> float | None:
    """Midpoint of the Up book as the market's probability baseline (diagnostic only)."""
    if up_bid is None or up_ask is None:
        return None
    mid = (up_bid + up_ask) / 2
    value = float(mid)
    return value if math.isfinite(value) else None
