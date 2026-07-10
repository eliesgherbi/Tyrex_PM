"""Fee-aware edge calculator for Z-Gap (A0.4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.quant.binary_fair_value import FairValueSnapshot, MODEL_STATUS_READY
from tyrex_pm.quant.fees import FeeModel, phi_taker_fee, validate_fee_price

EDGE_STATUS_READY = "ready"
EDGE_STATUS_NOT_READY = "not_ready"

LEG_UP = "UP"
LEG_DOWN = "DOWN"


@dataclass(frozen=True)
class EdgeSnapshot:
    edge_up: Decimal | None
    edge_down: Decimal | None
    selected_leg: str | None
    selected_edge: Decimal | None
    ask_up: Decimal | None
    ask_down: Decimal | None
    fee_up: Decimal | None
    fee_down: Decimal | None
    expected_slippage_up: Decimal
    expected_slippage_down: Decimal
    fee_model_id: str | None
    edge_status: str
    reject_reason: str | None
    snapshot_ts: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _not_ready(
    *,
    fair: FairValueSnapshot,
    fee_model: FeeModel | None,
    ask_up: Decimal | None,
    ask_down: Decimal | None,
    slippage_up: Decimal,
    slippage_down: Decimal,
    reason: str,
    ts: datetime | None = None,
) -> EdgeSnapshot:
    ts = ts or fair.snapshot_ts or _utc_now()
    return EdgeSnapshot(
        edge_up=None,
        edge_down=None,
        selected_leg=None,
        selected_edge=None,
        ask_up=ask_up,
        ask_down=ask_down,
        fee_up=None,
        fee_down=None,
        expected_slippage_up=slippage_up,
        expected_slippage_down=slippage_down,
        fee_model_id=fee_model.fee_model_id if fee_model is not None else None,
        edge_status=EDGE_STATUS_NOT_READY,
        reject_reason=reason,
        snapshot_ts=ts,
    )


def compute_edge(
    fair: FairValueSnapshot,
    *,
    ask_up: Decimal | None,
    ask_down: Decimal | None,
    fee_model: FeeModel | None,
    expected_slippage_up: Decimal = Decimal("0"),
    expected_slippage_down: Decimal = Decimal("0"),
) -> EdgeSnapshot:
    """Compute fee-aware edges without ``theta_take`` threshold gating (A0.5)."""
    if fair.model_status != MODEL_STATUS_READY:
        return _not_ready(
            fair=fair,
            fee_model=fee_model,
            ask_up=ask_up,
            ask_down=ask_down,
            slippage_up=expected_slippage_up,
            slippage_down=expected_slippage_down,
            reason=fair.reject_reason or "fair_value_not_ready",
        )
    if fee_model is None or not fee_model.is_resolved:
        return _not_ready(
            fair=fair,
            fee_model=fee_model,
            ask_up=ask_up,
            ask_down=ask_down,
            slippage_up=expected_slippage_up,
            slippage_down=expected_slippage_down,
            reason="z_gap_fee_model_unknown",
        )
    if ask_up is None or ask_down is None:
        return _not_ready(
            fair=fair,
            fee_model=fee_model,
            ask_up=ask_up,
            ask_down=ask_down,
            slippage_up=expected_slippage_up,
            slippage_down=expected_slippage_down,
            reason="missing_ask",
        )

    try:
        validate_fee_price(ask_up)
        validate_fee_price(ask_down)
        fee_up = phi_taker_fee(ask_up, fee_model)
        fee_down = phi_taker_fee(ask_down, fee_model)
    except ValueError as exc:
        return _not_ready(
            fair=fair,
            fee_model=fee_model,
            ask_up=ask_up,
            ask_down=ask_down,
            slippage_up=expected_slippage_up,
            slippage_down=expected_slippage_down,
            reason=str(exc),
        )

    assert fair.p_up is not None and fair.p_down is not None
    p_up = Decimal(str(fair.p_up))
    p_down = Decimal(str(fair.p_down))

    edge_up = p_up - ask_up - fee_up - expected_slippage_up
    edge_down = p_down - ask_down - fee_down - expected_slippage_down

    if edge_up >= edge_down:
        selected_leg = LEG_UP
        selected_edge = edge_up
    else:
        selected_leg = LEG_DOWN
        selected_edge = edge_down

    return EdgeSnapshot(
        edge_up=edge_up,
        edge_down=edge_down,
        selected_leg=selected_leg,
        selected_edge=selected_edge,
        ask_up=ask_up,
        ask_down=ask_down,
        fee_up=fee_up,
        fee_down=fee_down,
        expected_slippage_up=expected_slippage_up,
        expected_slippage_down=expected_slippage_down,
        fee_model_id=fee_model.fee_model_id,
        edge_status=EDGE_STATUS_READY,
        reject_reason=None,
        snapshot_ts=fair.snapshot_ts,
    )
