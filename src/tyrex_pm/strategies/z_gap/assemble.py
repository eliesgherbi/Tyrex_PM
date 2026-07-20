"""Assemble one atomic ZGapDecisionSnapshot from normalized store views.

Calls reusable F2 indicators; contains no OMS/risk/planning behavior.
Does not live inside the generic ObserveHost as formula code.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.core.ids import CorrelationId, EventId
from tyrex_pm.core.time_authority import TimeAuthorityView
from tyrex_pm.domain.polymarket.fees import FeeCurveParams
from tyrex_pm.domain.polymarket.ptb import PtbSnapshot
from tyrex_pm.indicators.binary_fair_value import FairValueInput, compute_fair_value
from tyrex_pm.indicators.ewma_volatility import (
    SIGMA_UNITS_PER_SQRT_SECOND,
    VolatilitySnapshot,
)
from tyrex_pm.indicators.reference_basis import compute_basis_bps
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import ExecutableQuote
from tyrex_pm.strategies.z_gap.config import ZGapConfig
from tyrex_pm.strategies.z_gap.decision_input import ZGapDecisionSnapshot
from tyrex_pm.strategies.z_gap.snapshots import DecisionEpoch, build_model_snapshot
from tyrex_pm.strategies.z_gap.valuations import LegBookView, PositionView


def _leg_book(quote: ExecutableQuote, *, side: str) -> LegBookView:
    ask = quote.best_ask
    bid = quote.best_bid
    ready = ask is not None or bid is not None
    return LegBookView(
        ask=ask,
        ask_depth=quote.ask_size_at_touch if ask is not None else None,
        bid=bid,
        bid_depth=quote.bid_size_at_touch if bid is not None else None,
        ready=ready,
        reason_code=None if ready else f"{side}_book_empty",
    )


def assemble_zgap_decision_snapshot(
    *,
    market_snapshot: DecisionSnapshot,
    vol: VolatilitySnapshot,
    ptb: PtbSnapshot | None,
    time_view: TimeAuthorityView,
    config: ZGapConfig,
    fee_curve: FeeCurveParams,
    fee_resolved: bool,
    target_notional: Decimal,
    trigger: str,
    correlation_id: CorrelationId,
    causation_id: EventId | None,
    window_id: str,
    settlement_ref: Decimal | None = None,
    settlement_ref_fresh: bool = False,
    position: PositionView | None = None,
    capabilities: Mapping[str, bool] | None = None,
) -> ZGapDecisionSnapshot:
    """Build one sealed Z-Gap decision snapshot for the current evaluation."""
    now = market_snapshot.observed_at
    market = market_snapshot.market

    epoch = DecisionEpoch.new(
        market_id=market.market_id,
        window_id=window_id,
        evaluated_at=now,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )

    S = None if market_snapshot.reference is None else market_snapshot.reference.price
    K = None if ptb is None else ptb.k

    tau_s: float | None = None
    if market.event_end is not None:
        tau_s = max(0.0, (market.event_end - now).total_seconds())

    fv = compute_fair_value(
        FairValueInput(
            S=S,
            K=K,
            sigma=vol.sigma,
            tau_s=tau_s,
            sigma_units=vol.sigma_units or SIGMA_UNITS_PER_SQRT_SECOND,
            tau_floor_s=config.volatility.tau_floor_s,
            snapshot_ts=now,
        ),
        vol=vol,
    )

    ref_fresh = (
        market_snapshot.reference is not None
        and market_snapshot.reference_freshness.is_fresh
    )
    # When no distinct settlement reference is supplied (F3 fixtures), basis is
    # identically zero from S=S and must not be gated by dual-feed freshness.
    if settlement_ref is None or (S is not None and settlement_ref == S):
        basis = compute_basis_bps(
            trading_ref=S,
            settlement_ref=S,
            trading_ref_fresh=S is not None,
            settlement_ref_fresh=S is not None,
        )
    else:
        basis = compute_basis_bps(
            trading_ref=S,
            settlement_ref=settlement_ref,
            trading_ref_fresh=ref_fresh,
            settlement_ref_fresh=settlement_ref_fresh,
        )

    extra: list[str] = []
    if not time_view.ready:
        extra.append(time_view.reason_code or "time_not_ready")
    if ptb is not None and not ptb.usable_for_entry:
        extra.extend(ptb.readiness_reasons or (ptb.quality.value,))

    model = build_model_snapshot(
        epoch=epoch,
        fair_value_status=fv.model_status,
        fair_value_reject=fv.reject_reason,
        S=fv.S,
        K=fv.K,
        sigma=fv.sigma,
        sigma_units=fv.sigma_units,
        tau_s=fv.tau_s,
        z=fv.z,
        p_up=fv.p_up,
        p_down=fv.p_down,
        basis_bps=basis.basis_bps,
        ptb=ptb,
        jump_guard_tripped=vol.jump_guard_tripped,
        extra_reasons=tuple(extra),
        source_timestamps={
            "evaluated_at": now,
            **({"ptb_receive": ptb.receive_ts} if ptb is not None else {}),
        },
        freshness={
            "reference": ref_fresh,
            "yes_book": market_snapshot.yes_freshness.is_fresh,
            "no_book": market_snapshot.no_freshness.is_fresh,
            "time": time_view.ready,
        },
    )

    evidence: dict[str, Any] = {
        "basis_validity": basis.validity.value,
        "basis_ready": basis.ready,
        "vol_ready": vol.ready,
        "vol_reject": vol.reject_reason,
        "fair_value_status": fv.model_status,
        "fair_value_reject": fv.reject_reason,
        "economics_label": "estimated",
        "valuation_label": "counterfactual",
    }

    return ZGapDecisionSnapshot(
        epoch=epoch,
        model=model,
        time=time_view,
        ptb=ptb,
        up_book=_leg_book(market_snapshot.yes_quote, side="up"),
        down_book=_leg_book(market_snapshot.no_quote, side="down"),
        fee_curve=fee_curve,
        fee_resolved=fee_resolved,
        target_notional=target_notional,
        trigger=trigger,
        observed_at=now,
        correlation_id=correlation_id,
        causation_id=causation_id,
        position=position,
        capabilities=dict(capabilities or {}),
        evidence=evidence,
    )
