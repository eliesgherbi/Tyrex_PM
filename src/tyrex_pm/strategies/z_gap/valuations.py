"""Pure Z-Gap entry and active-position valuations (Correction A/D).

Planned/estimated economics only — never labels fees as confirmed actual.
Does not invent quantity, query Portfolio, or emit venue orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from tyrex_pm.core.fees_phi import (
    FeeCurveParams,
    FeeEstimateKind,
    phi_taker_fee_per_share,
)
from tyrex_pm.core.numerics import as_decimal, require_polymarket_price
from tyrex_pm.strategies.z_gap.config import ZGapConfig
from tyrex_pm.strategies.z_gap.reasons import ZGapReason
from tyrex_pm.strategies.z_gap.snapshots import (
    DecisionEpoch,
    ZGapModelSnapshot,
    require_compatible_epoch,
)


class ZGapLeg(str, Enum):
    UP = "UP"
    DOWN = "DOWN"


@dataclass(frozen=True, kw_only=True)
class LegBookView:
    """Immutable executable book inputs for one outcome leg."""

    ask: Decimal | None
    ask_depth: Decimal | None
    bid: Decimal | None
    bid_depth: Decimal | None
    ready: bool = True
    reason_code: str | None = None


@dataclass(frozen=True, kw_only=True)
class EntryLegValuation:
    epoch: DecisionEpoch
    leg: ZGapLeg
    fair_probability: Decimal | None
    executable_ask: Decimal | None
    ask_depth: Decimal | None
    fee_buy_estimated: Decimal | None
    slippage_buy: Decimal | None
    c_entry_unit: Decimal | None
    e_settlement: Decimal | None
    e_repricing: Decimal | None
    f_exit_estimated: Decimal | None
    max_economic_price: Decimal | None
    ready: bool
    reason_codes: tuple[str, ...] = ()
    fee_kind: FeeEstimateKind = FeeEstimateKind.ESTIMATED
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.fee_kind is FeeEstimateKind.CONFIRMED_ACTUAL:
            raise ValueError("entry valuation must not claim confirmed actual fees")
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "evidence", dict(self.evidence))


@dataclass(frozen=True, kw_only=True)
class PositionView:
    """Immutable supplied position inputs — caller owns truth of qty/cost."""

    epoch: DecisionEpoch
    held_leg: ZGapLeg
    confirmed_quantity: Decimal
    entry_cost_total: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "confirmed_quantity",
            as_decimal(self.confirmed_quantity, field_name="confirmed_quantity"),
        )
        object.__setattr__(
            self,
            "entry_cost_total",
            as_decimal(self.entry_cost_total, field_name="entry_cost_total"),
        )
        if self.confirmed_quantity < 0:
            raise ValueError("confirmed_quantity must be >= 0")


@dataclass(frozen=True, kw_only=True)
class PositionValuation:
    epoch: DecisionEpoch
    held_leg: ZGapLeg
    confirmed_quantity: Decimal
    entry_cost_total: Decimal
    executable_bid: Decimal | None
    bid_depth: Decimal | None
    fee_sell_estimated: Decimal | None
    slippage_sell_applied: Decimal | None
    slippage_included_in_executable: bool
    v_sell_unit: Decimal | None
    v_sell: Decimal | None
    pnl_liquidation: Decimal | None
    p_held: Decimal | None
    v_resolve_gross: Decimal | None
    v_resolve_adj: Decimal | None
    remaining_hold_edge: Decimal | None
    market_richness: Decimal | None
    ready: bool
    reason_codes: tuple[str, ...] = ()
    fee_kind: FeeEstimateKind = FeeEstimateKind.ESTIMATED
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.fee_kind is FeeEstimateKind.CONFIRMED_ACTUAL:
            raise ValueError("position valuation must not claim confirmed actual fees")
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "evidence", dict(self.evidence))


def _p_for_leg(model: ZGapModelSnapshot, leg: ZGapLeg) -> Decimal | None:
    raw = model.p_up if leg is ZGapLeg.UP else model.p_down
    if raw is None:
        return None
    return Decimal(str(raw))


def value_entry_leg(
    *,
    model: ZGapModelSnapshot,
    leg: ZGapLeg,
    book: LegBookView,
    config: ZGapConfig,
    fee_curve: FeeCurveParams | None = None,
) -> EntryLegValuation:
    """
    C_entry,unit = ask + fee_buy + slip_buy
    E_settlement = p − C_entry,unit
    E_repricing = p − C_entry,unit − F_exit
    """
    epoch = model.epoch
    curve = fee_curve or config.friction.fee_curve
    slip_buy = as_decimal(config.entry.expected_slippage_buy, field_name="slip_buy")
    reasons: list[str] = []

    if not model.ready:
        reasons.append(ZGapReason.MODEL_NOT_READY.value)
        return EntryLegValuation(
            epoch=epoch,
            leg=leg,
            fair_probability=_p_for_leg(model, leg),
            executable_ask=book.ask,
            ask_depth=book.ask_depth,
            fee_buy_estimated=None,
            slippage_buy=slip_buy,
            c_entry_unit=None,
            e_settlement=None,
            e_repricing=None,
            f_exit_estimated=None,
            max_economic_price=None,
            ready=False,
            reason_codes=tuple(reasons),
        )

    p = _p_for_leg(model, leg)
    if p is None:
        reasons.append(ZGapReason.MODEL_NOT_READY.value)
        return EntryLegValuation(
            epoch=epoch,
            leg=leg,
            fair_probability=None,
            executable_ask=book.ask,
            ask_depth=book.ask_depth,
            fee_buy_estimated=None,
            slippage_buy=slip_buy,
            c_entry_unit=None,
            e_settlement=None,
            e_repricing=None,
            f_exit_estimated=None,
            max_economic_price=None,
            ready=False,
            reason_codes=tuple(reasons),
        )

    if not book.ready or book.ask is None:
        reasons.append(ZGapReason.BOOK_NOT_READY.value)
        return EntryLegValuation(
            epoch=epoch,
            leg=leg,
            fair_probability=p,
            executable_ask=book.ask,
            ask_depth=book.ask_depth,
            fee_buy_estimated=None,
            slippage_buy=slip_buy,
            c_entry_unit=None,
            e_settlement=None,
            e_repricing=None,
            f_exit_estimated=None,
            max_economic_price=None,
            ready=False,
            reason_codes=tuple(reasons or (book.reason_code or ZGapReason.BOOK_NOT_READY.value,)),
        )

    ask = require_polymarket_price(as_decimal(book.ask, field_name="ask"))
    fee_buy = phi_taker_fee_per_share(ask, curve)
    # Estimated exit friction at model probability (proxy near fair value).
    fee_sell_proxy = phi_taker_fee_per_share(p, curve)
    slip_sell = as_decimal(
        config.realization.expected_slippage_sell, field_name="slip_sell"
    )
    # If executable bid already includes depth-walk slip, still reserve slip_sell
    # for F_exit proxy unless configured included — use sell slip from config.
    if config.realization.slippage_included_in_executable_bid:
        # F_exit uses fee at p plus optional reserve; do not double-count book slip.
        f_exit = fee_sell_proxy + as_decimal(
            config.entry.exit_friction_reserve, field_name="exit_friction_reserve"
        )
    else:
        f_exit = (
            fee_sell_proxy
            + slip_sell
            + as_decimal(config.entry.exit_friction_reserve, field_name="exit_friction_reserve")
        )

    c_entry = ask + fee_buy + slip_buy
    e_settlement = p - c_entry
    e_repricing = p - c_entry - f_exit
    max_econ = p - fee_buy - slip_buy - (
        as_decimal(config.entry.theta_take, field_name="theta_take")
        if config.entry.require_repricing_edge
        else Decimal("0")
    )

    return EntryLegValuation(
        epoch=epoch,
        leg=leg,
        fair_probability=p,
        executable_ask=ask,
        ask_depth=book.ask_depth,
        fee_buy_estimated=fee_buy,
        slippage_buy=slip_buy,
        c_entry_unit=c_entry,
        e_settlement=e_settlement,
        e_repricing=e_repricing,
        f_exit_estimated=f_exit,
        max_economic_price=max_econ,
        ready=True,
        reason_codes=(),
        fee_kind=FeeEstimateKind.ESTIMATED,
        evidence={
            "fee_buy_kind": FeeEstimateKind.ESTIMATED.value,
            "f_exit_kind": FeeEstimateKind.ESTIMATED.value,
        },
    )


def value_position(
    *,
    model: ZGapModelSnapshot,
    position: PositionView,
    book: LegBookView,
    config: ZGapConfig,
    fee_curve: FeeCurveParams | None = None,
) -> PositionValuation:
    """
    V_sell,unit = bid_executable − fee_sell − slip_sell  (no double slip)
    V_sell = q × V_sell,unit
    PnL_liquidation = V_sell − C_entry
    market_richness = V_sell,unit − p_held
    remaining_hold_edge = p_held − V_sell,unit
    """
    mismatch = require_compatible_epoch(model.epoch, other=position.epoch)
    curve = fee_curve or config.friction.fee_curve
    q = position.confirmed_quantity
    reasons: list[str] = []

    if mismatch is not None:
        return PositionValuation(
            epoch=model.epoch,
            held_leg=position.held_leg,
            confirmed_quantity=q,
            entry_cost_total=position.entry_cost_total,
            executable_bid=book.bid,
            bid_depth=book.bid_depth,
            fee_sell_estimated=None,
            slippage_sell_applied=None,
            slippage_included_in_executable=config.realization.slippage_included_in_executable_bid,
            v_sell_unit=None,
            v_sell=None,
            pnl_liquidation=None,
            p_held=None,
            v_resolve_gross=None,
            v_resolve_adj=None,
            remaining_hold_edge=None,
            market_richness=None,
            ready=False,
            reason_codes=(mismatch.value,),
        )

    if not model.ready:
        reasons.append(ZGapReason.MODEL_NOT_READY.value)

    p_held = _p_for_leg(model, position.held_leg)
    if p_held is None:
        reasons.append(ZGapReason.MODEL_NOT_READY.value)

    if not book.ready or book.bid is None:
        reasons.append(ZGapReason.BOOK_NOT_READY.value)

    if reasons:
        return PositionValuation(
            epoch=model.epoch,
            held_leg=position.held_leg,
            confirmed_quantity=q,
            entry_cost_total=position.entry_cost_total,
            executable_bid=book.bid,
            bid_depth=book.bid_depth,
            fee_sell_estimated=None,
            slippage_sell_applied=None,
            slippage_included_in_executable=config.realization.slippage_included_in_executable_bid,
            v_sell_unit=None,
            v_sell=None,
            pnl_liquidation=None,
            p_held=p_held,
            v_resolve_gross=None if p_held is None else q * p_held,
            v_resolve_adj=None,
            remaining_hold_edge=None,
            market_richness=None,
            ready=False,
            reason_codes=tuple(reasons),
        )

    assert p_held is not None and book.bid is not None
    bid = require_polymarket_price(as_decimal(book.bid, field_name="bid"))
    fee_sell = phi_taker_fee_per_share(bid, curve)
    included = config.realization.slippage_included_in_executable_bid
    slip_cfg = as_decimal(
        config.realization.expected_slippage_sell, field_name="slip_sell"
    )
    slip_applied = Decimal("0") if included else slip_cfg
    v_unit = bid - fee_sell - slip_applied
    v_sell = q * v_unit
    pnl = v_sell - position.entry_cost_total
    v_resolve_gross = q * p_held
    penalty = as_decimal(
        config.friction.provisional_resolve_penalty_per_share,
        field_name="resolve_penalty",
    )
    v_resolve_adj = v_resolve_gross - q * penalty
    richness = v_unit - p_held
    remaining = p_held - v_unit

    return PositionValuation(
        epoch=model.epoch,
        held_leg=position.held_leg,
        confirmed_quantity=q,
        entry_cost_total=position.entry_cost_total,
        executable_bid=bid,
        bid_depth=book.bid_depth,
        fee_sell_estimated=fee_sell,
        slippage_sell_applied=slip_applied,
        slippage_included_in_executable=included,
        v_sell_unit=v_unit,
        v_sell=v_sell,
        pnl_liquidation=pnl,
        p_held=p_held,
        v_resolve_gross=v_resolve_gross,
        v_resolve_adj=v_resolve_adj,
        remaining_hold_edge=remaining,
        market_richness=richness,
        ready=True,
        reason_codes=(),
        fee_kind=FeeEstimateKind.ESTIMATED,
        evidence={
            "fee_sell_kind": FeeEstimateKind.ESTIMATED.value,
            "no_double_slippage": included,
        },
    )
