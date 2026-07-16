"""Tests for fee-aware edge calculator (A0.4)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tyrex_pm.quant.binary_fair_value import FairValueSnapshot, MODEL_STATUS_NOT_READY, MODEL_STATUS_READY
from tyrex_pm.quant.edge import (
    EDGE_STATUS_NOT_READY,
    EDGE_STATUS_READY,
    LEG_DOWN,
    LEG_UP,
    compute_edge,
)
from tyrex_pm.quant.fees import parse_fee_model_from_raw, phi_taker_fee
from tyrex_pm.runtime.z_gap_model_facts import (
    build_edge_evaluated_payload,
    build_fee_model_resolved_payload,
)

TS = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
FD_RAW = {"fd": {"r": 0.07, "e": 1, "to": True}}


def _fair(*, p_up: float = 0.6, p_down: float = 0.4, status: str = MODEL_STATUS_READY) -> FairValueSnapshot:
    return FairValueSnapshot(
        S=Decimal("100000"),
        K=Decimal("99900"),
        tau_s=120.0,
        sigma=0.01,
        sigma_units="per_sqrt_second",
        z=0.5,
        p_up=p_up,
        p_down=p_down,
        model_status=status,
        reject_reason=None if status == MODEL_STATUS_READY else "missing_sigma",
        snapshot_ts=TS,
    )


def _fee_model():
    return parse_fee_model_from_raw(FD_RAW, condition_id="0xabc", market_id="btc_5m_test")


def test_edge_with_dynamic_fees_hand_computed() -> None:
    fair = _fair(p_up=0.6, p_down=0.4)
    ask_up = Decimal("0.55")
    ask_down = Decimal("0.45")
    fee_model = _fee_model()
    fee_up = phi_taker_fee(ask_up, fee_model)
    slippage = Decimal("0.01")
    edge = compute_edge(
        fair,
        ask_up=ask_up,
        ask_down=ask_down,
        fee_model=fee_model,
        expected_slippage_up=slippage,
        expected_slippage_down=slippage,
    )
    expected_up = Decimal("0.6") - ask_up - fee_up - slippage
    expected_down = Decimal("0.4") - ask_down - phi_taker_fee(ask_down, fee_model) - slippage
    assert edge.edge_status == EDGE_STATUS_READY
    assert edge.edge_up == expected_up
    assert edge.edge_down == expected_down
    assert edge.fee_up == fee_up
    assert edge.selected_leg == LEG_UP
    assert edge.selected_edge == expected_up


def test_selects_up_when_edge_up_higher() -> None:
    fair = _fair(p_up=0.7, p_down=0.3)
    edge = compute_edge(
        fair,
        ask_up=Decimal("0.50"),
        ask_down=Decimal("0.50"),
        fee_model=_fee_model(),
    )
    assert edge.selected_leg == LEG_UP
    assert edge.edge_up is not None and edge.edge_down is not None
    assert edge.edge_up > edge.edge_down


def test_selects_down_when_edge_down_higher() -> None:
    fair = _fair(p_up=0.35, p_down=0.65)
    edge = compute_edge(
        fair,
        ask_up=Decimal("0.40"),
        ask_down=Decimal("0.35"),
        fee_model=_fee_model(),
    )
    assert edge.selected_leg == LEG_DOWN
    assert edge.edge_down is not None and edge.edge_up is not None
    assert edge.edge_down > edge.edge_up


def test_missing_fair_value_not_ready() -> None:
    fair = _fair(status=MODEL_STATUS_NOT_READY)
    edge = compute_edge(
        fair,
        ask_up=Decimal("0.5"),
        ask_down=Decimal("0.5"),
        fee_model=_fee_model(),
    )
    assert edge.edge_status == EDGE_STATUS_NOT_READY
    assert edge.reject_reason == "missing_sigma"


def test_missing_asks_not_ready() -> None:
    edge = compute_edge(_fair(), ask_up=None, ask_down=Decimal("0.5"), fee_model=_fee_model())
    assert edge.edge_status == EDGE_STATUS_NOT_READY
    assert edge.reject_reason == "missing_ask"


def test_unknown_fee_model_not_ready() -> None:
    unknown = parse_fee_model_from_raw({})
    edge = compute_edge(
        _fair(),
        ask_up=Decimal("0.5"),
        ask_down=Decimal("0.5"),
        fee_model=unknown,
    )
    assert edge.edge_status == EDGE_STATUS_NOT_READY
    assert edge.reject_reason == "z_gap_fee_model_unknown"


def test_slippage_reduces_edge() -> None:
    base = compute_edge(
        _fair(),
        ask_up=Decimal("0.50"),
        ask_down=Decimal("0.50"),
        fee_model=_fee_model(),
        expected_slippage_up=Decimal("0"),
        expected_slippage_down=Decimal("0"),
    )
    slipped = compute_edge(
        _fair(),
        ask_up=Decimal("0.50"),
        ask_down=Decimal("0.50"),
        fee_model=_fee_model(),
        expected_slippage_up=Decimal("0.02"),
        expected_slippage_down=Decimal("0.02"),
    )
    assert base.edge_up is not None and slipped.edge_up is not None
    assert slipped.edge_up == base.edge_up - Decimal("0.02")


def test_no_theta_take_gating() -> None:
    """Ready status returned even when edge < theta_take (threshold is A0.5)."""
    fair = _fair(p_up=0.51, p_down=0.49)
    edge = compute_edge(
        fair,
        ask_up=Decimal("0.50"),
        ask_down=Decimal("0.50"),
        fee_model=_fee_model(),
    )
    assert edge.edge_status == EDGE_STATUS_READY
    assert edge.edge_up is not None
    assert edge.selected_edge is not None
    # No theta_take filter — negative edge still reported as ready.
    assert edge.reject_reason is None


def test_fee_model_resolved_payload_contract() -> None:
    model = _fee_model()
    payload = build_fee_model_resolved_payload(
        model,
        price=Decimal("0.50"),
        market_id="btc_5m_test",
    )
    assert payload["fee_model_id"] == "polymarket_dynamic_fd_v1"
    assert payload["fee_model_status"] == "resolved"
    assert payload["fd_r"] == "0.07"
    assert payload["phi_price"] == "0.017500"
    assert payload["curve_parameters_hash"] is not None


def test_edge_evaluated_payload_contract() -> None:
    fair = _fair()
    edge = compute_edge(
        fair,
        ask_up=Decimal("0.50"),
        ask_down=Decimal("0.50"),
        fee_model=_fee_model(),
    )
    payload = build_edge_evaluated_payload(fair, edge)
    assert payload["p_up"] == 0.6
    assert payload["edge_status"] == EDGE_STATUS_READY
    assert payload["selected_leg"] in {LEG_UP, LEG_DOWN}
    assert payload["fee_model_id"] == "polymarket_dynamic_fd_v1"
