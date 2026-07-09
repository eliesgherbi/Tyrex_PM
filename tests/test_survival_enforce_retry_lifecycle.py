"""Survival enforce FAK reject → retry lifecycle tests."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from tyrex_pm.survival.enforcement_dispatch import (
    handle_survival_exit_oms_reject,
    select_enforce_order_decision,
    should_skip_survival_enforce,
    survival_fak_reject_count,
)
from tyrex_pm.runtime.config import parse_app_config


def _app_cfg(*, managed: bool = False):
    return parse_app_config(
        risk={"notional": {"min_usd": "1", "max_usd": "100", "max_policy": "cap"}},
        strategy={
            "kind": "paired_binary",
            "paired_binary": {
                "owner_id": "pb",
                "market_id": "m1",
                "yes_token_id": "y",
                "no_token_id": "n",
                "position_size": "5",
                "exit_order_style": "FAK",
            },
        },
        runtime={
            "market_data": {"enabled": True},
            "execution": {"planner": {"enabled": True}},
            "survival": {
                "enabled": True,
                "enforcement": {
                    "order_policy": {
                        "mode": "fak_then_managed_rest" if managed else "fak_retry",
                        "managed_rest_enabled": managed,
                    }
                },
            }
        },
    )


def test_oms_reject_records_retry_state() -> None:
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.TP_PENDING_YES,
        effective_qty=Decimal("5"),
        survivor_leg_state={"enforce_exit_in_flight": True},
    )
    ok = handle_survival_exit_oms_reject(
        state,
        leg="yes",
        trigger_type="survival_trailing_stop",
        error_msg="no orders found to match with FAK order",
    )
    assert ok is True
    assert survival_fak_reject_count(state) == 1
    assert state.survivor_leg_state["enforce_exit_in_flight"] is False


def test_pending_phase_blocks_unless_fak_reject() -> None:
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.TP_PENDING_YES,
        survivor_leg_state={},
    )
    skip, reason = should_skip_survival_enforce(
        state,
        survivor_leg="yes",
        seconds_to_close=600.0,
        flatten_before_event_end_s=20.0,
    )
    assert skip is True
    assert reason == "exit_already_pending"

    state.survivor_leg_state["survival_fak_reject_count"] = 1
    skip2, _ = should_skip_survival_enforce(
        state,
        survivor_leg="yes",
        seconds_to_close=600.0,
        flatten_before_event_end_s=20.0,
    )
    assert skip2 is False


def test_retry_selects_repriced_fak() -> None:
    app = _app_cfg()
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.TP_PENDING_YES,
        effective_qty=Decimal("5"),
        survivor_leg_state={"survival_fak_reject_count": 1, "survival_exit_attempt_count": 2},
    )
    dec = select_enforce_order_decision(
        app,
        state,
        survivor_leg="yes",
        qty=Decimal("5"),
        touch_price=Decimal("0.65"),
        executable_price=Decimal("0.65"),
        seconds_to_close=600.0,
        allow_partial=True,
        trigger_type="survival_trailing_stop",
        module="trailing_stop",
        is_retry=True,
    )
    assert dec is not None
    assert dec.order_type.value == "FAK"
    assert dec.price == Decimal("0.64")
