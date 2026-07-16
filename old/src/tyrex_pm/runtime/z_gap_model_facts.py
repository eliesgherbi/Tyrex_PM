"""Fact-ready payload builders for Z-Gap model state (A0.3+) and fee/edge (A0.4)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tyrex_pm.quant.binary_fair_value import FairValueSnapshot
from tyrex_pm.quant.edge import EdgeSnapshot
from tyrex_pm.quant.fees import FeeModel, curve_parameters_hash, phi_taker_fee
from tyrex_pm.quant.volatility import VolatilitySnapshot

FACT_TYPE_MODEL_STATE_SNAPSHOT = "model_state_snapshot"
FACT_TYPE_FEE_MODEL_RESOLVED = "fee_model_resolved"
FACT_TYPE_EDGE_EVALUATED = "edge_evaluated"


def build_model_state_snapshot_payload(
    fair: FairValueSnapshot,
    *,
    vol: VolatilitySnapshot | None = None,
) -> dict[str, Any]:
    """Payload contract for ``model_state_snapshot`` facts (emission deferred to A0.5)."""
    sigma_ready = vol.ready if vol is not None else fair.model_status == "ready"
    sample_count = vol.sample_count if vol is not None else None
    jump_guard_tripped = vol.jump_guard_tripped if vol is not None else False
    return {
        "S": str(fair.S) if fair.S is not None else None,
        "K": str(fair.K) if fair.K is not None else None,
        "tau_s": fair.tau_s,
        "sigma": fair.sigma,
        "sigma_units": fair.sigma_units,
        "z": fair.z,
        "p_up": fair.p_up,
        "p_down": fair.p_down,
        "model_status": fair.model_status,
        "sigma_ready": sigma_ready,
        "sample_count": sample_count,
        "jump_guard_tripped": jump_guard_tripped,
        "reject_reason": fair.reject_reason,
        "snapshot_ts": fair.snapshot_ts.isoformat(),
    }


def build_fee_model_resolved_payload(
    fee_model: FeeModel,
    *,
    price: Decimal | None = None,
    condition_id: str | None = None,
    market_id: str | None = None,
) -> dict[str, Any]:
    """Payload contract for ``fee_model_resolved`` facts (emission deferred to A0.5)."""
    phi_price: str | None = None
    if price is not None and fee_model.is_resolved:
        try:
            phi_price = str(phi_taker_fee(price, fee_model))
        except ValueError:
            phi_price = None
    cid = condition_id or fee_model.condition_id
    mid = market_id or fee_model.market_id
    return {
        "fee_model_id": fee_model.fee_model_id,
        "fee_model_status": fee_model.fee_model_status,
        "fd_r": str(fee_model.fd_r) if fee_model.fd_r is not None else None,
        "fd_e": str(fee_model.fd_e) if fee_model.fd_e is not None else None,
        "fd_to": fee_model.fd_to,
        "curve_parameters_hash": curve_parameters_hash(
            fd_r=fee_model.fd_r,
            fd_e=fee_model.fd_e,
            fd_to=fee_model.fd_to,
        ),
        "price": str(price) if price is not None else None,
        "phi_price": phi_price,
        "source": fee_model.source,
        "condition_id": cid,
        "market_id": mid,
    }


def build_edge_evaluated_payload(
    fair: FairValueSnapshot,
    edge: EdgeSnapshot,
) -> dict[str, Any]:
    """Payload contract for ``edge_evaluated`` facts (emission deferred to A0.5)."""
    return {
        "p_up": fair.p_up,
        "p_down": fair.p_down,
        "ask_up": str(edge.ask_up) if edge.ask_up is not None else None,
        "ask_down": str(edge.ask_down) if edge.ask_down is not None else None,
        "fee_up": str(edge.fee_up) if edge.fee_up is not None else None,
        "fee_down": str(edge.fee_down) if edge.fee_down is not None else None,
        "expected_slippage_up": str(edge.expected_slippage_up),
        "expected_slippage_down": str(edge.expected_slippage_down),
        "edge_up": str(edge.edge_up) if edge.edge_up is not None else None,
        "edge_down": str(edge.edge_down) if edge.edge_down is not None else None,
        "selected_leg": edge.selected_leg,
        "selected_edge": str(edge.selected_edge) if edge.selected_edge is not None else None,
        "fee_model_id": edge.fee_model_id,
        "edge_status": edge.edge_status,
        "reject_reason": edge.reject_reason,
        "snapshot_ts": edge.snapshot_ts.isoformat(),
    }
