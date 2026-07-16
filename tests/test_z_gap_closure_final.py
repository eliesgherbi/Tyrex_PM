"""A0.8 final closure integration tests — pipeline parity, venue lag, reservations."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from unittest.mock import patch

import pytest

from tyrex_pm.core.ids import TokenId
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.z_gap.lifecycle import (
    mark_exit_triggered,
    reconcile_entry_fill,
    reconcile_exit_fill,
)
from tyrex_pm.strategies.z_gap.reconciliation import RECON_VENUE_LAG_EXPECTED
from tyrex_pm.strategies.z_gap.scenario_oms import OmsFillSpec, ScenarioOMS
from tyrex_pm.strategies.z_gap.shadow_harness import ShadowHarness, TickSpec
from tyrex_pm.strategies.z_gap.state import ZGapLifecycleState, ZGapPhase
from tyrex_pm.state.entry_fill_lifecycle import EntryFillStatus, OrderFillSnapshot

EVENT_START = time.time() + 120
EVENT_END = EVENT_START + 300
RECON_CFG = {"venue_sync_grace_ms": 100, "poll_interval_ms": 50, "max_attempts": 3}


def _coord() -> RuntimeCoordinator:
    return RuntimeCoordinator(
        wallet=WalletStore(),
        orders=OrderStore(),
        health=HealthRuntime(),
        allocation_ledger=AllocationLedger(),
    )


@pytest.mark.asyncio
async def test_shadow_pipeline_uses_pre_submit_and_process_intent(tmp_path) -> None:
    """ScenarioOMS replaces only the OMS boundary; orchestration stays on the live path."""
    oms = ScenarioOMS(
        buy_fills=[OmsFillSpec(status="matched", taking_amount="8")],
        sell_fills=[OmsFillSpec(status="matched", making_amount="8")],
    )
    pre_submit_calls: list[bool] = []
    pipeline_oms: list[object] = []

    import tyrex_pm.runtime.z_gap_enforce as z_gap_enforce
    import tyrex_pm.strategies.z_gap.entry_plan as entry_plan

    orig_pre = entry_plan.validate_z_gap_pre_submit
    orig_pi = z_gap_enforce.process_intent_work_unit

    def spy_pre(*args, **kwargs):
        pre_submit_calls.append(True)
        return orig_pre(*args, **kwargs)

    async def spy_pi(*args, **kwargs):
        pipeline_oms.append(kwargs.get("oms"))
        return await orig_pi(*args, **kwargs)

    harness = ShadowHarness.create(
        tmp_path=tmp_path,
        event_start_ts=EVENT_START,
        event_end_ts=EVENT_END,
        oms=oms,
        exit_cfg={"stop_confirm_s": 0, "retry_interval_ms": 0, "z_stop": "0.05"},
    )
    with (
        patch.object(entry_plan, "validate_z_gap_pre_submit", spy_pre),
        patch.object(z_gap_enforce, "validate_z_gap_pre_submit", spy_pre),
        patch.object(z_gap_enforce, "process_intent_work_unit", spy_pi),
    ):
        result = await harness.run_ticks(
            [
                TickSpec(now_ts=EVENT_START + 90, binance_price=Decimal("100150")),
                TickSpec(now_ts=EVENT_START + 91, binance_price=Decimal("100150")),
                TickSpec(now_ts=EVENT_START + 92, binance_price=Decimal("99800")),
                TickSpec(now_ts=EVENT_START + 93, binance_price=Decimal("99800")),
            ]
        )

    assert pre_submit_calls
    assert pipeline_oms
    assert all(isinstance(x, ScenarioOMS) for x in pipeline_oms)
    assert result.enforce_state.lifecycle is not None
    assert result.enforce_state.lifecycle.phase == ZGapPhase.DONE


@pytest.mark.asyncio
async def test_venue_lag_outcome_a_wallet_catches_up_exit_proceeds(tmp_path) -> None:
    near_end = EVENT_END - 10
    oms = ScenarioOMS(
        buy_fills=[OmsFillSpec(status="matched", taking_amount="8")],
        sell_fills=[OmsFillSpec(status="matched", making_amount="8")],
    )
    harness = ShadowHarness.create(
        tmp_path=tmp_path,
        event_start_ts=EVENT_START,
        event_end_ts=EVENT_END,
        oms=oms,
        live_like_risk=True,
        reconciliation=RECON_CFG,
        exit_cfg={"stop_confirm_s": 0, "retry_interval_ms": 0, "z_stop": "0.05"},
    )
    result = await harness.run_ticks(
        [
            TickSpec(now_ts=EVENT_START + 90, binance_price=Decimal("100150")),
            TickSpec(now_ts=EVENT_START + 91, binance_price=Decimal("100150")),
            TickSpec(
                now_ts=EVENT_START + 92,
                binance_price=Decimal("100150"),
                wallet_venue_qty=Decimal("8"),
            ),
            TickSpec(now_ts=near_end, binance_price=Decimal("100150"), event_end_ts=EVENT_END),
            TickSpec(now_ts=near_end + 1, binance_price=Decimal("100150"), event_end_ts=EVENT_END),
        ]
    )
    lc = result.enforce_state.lifecycle
    assert lc is not None
    assert lc.phase == ZGapPhase.DONE
    assert lc.active_quantity == 0
    assert oms.sell_submit_count == 1
    summary = harness.terminal_summary()
    assert summary is not None
    assert summary.get("operational_pass") is True
    assert summary.get("position_closed") is True
    assert Decimal(str(summary.get("allocated_quantity", "1"))) == 0
    recon_facts = [f for f in harness.sink.facts if f.get("fact_type") == "z_gap_position_reconciliation"]
    assert any(
        (f.get("payload") or {}).get("status") == RECON_VENUE_LAG_EXPECTED for f in recon_facts
    )


@pytest.mark.asyncio
async def test_venue_lag_outcome_b_never_catches_up_fails_closed(tmp_path) -> None:
    oms = ScenarioOMS(
        buy_fills=[OmsFillSpec(status="matched", taking_amount="8")],
        sell_fills=[OmsFillSpec(status="matched", making_amount="8")],
    )
    harness = ShadowHarness.create(
        tmp_path=tmp_path,
        event_start_ts=EVENT_START,
        event_end_ts=EVENT_END,
        oms=oms,
        live_like_risk=True,
        reconciliation=RECON_CFG,
        exit_cfg={"stop_confirm_s": 0, "retry_interval_ms": 0, "z_stop": "0.05"},
    )
    await harness.run_ticks(
        [
            TickSpec(now_ts=EVENT_START + 90, binance_price=Decimal("100150")),
            TickSpec(now_ts=EVENT_START + 91, binance_price=Decimal("100150")),
        ]
    )
    await asyncio.sleep(0.25)
    result = await harness.run_ticks(
        [
            TickSpec(now_ts=EVENT_START + 92, binance_price=Decimal("100150")),
            TickSpec(now_ts=EVENT_START + 93, binance_price=Decimal("100150")),
        ]
    )
    lc = result.enforce_state.lifecycle
    assert lc is not None
    assert lc.phase == ZGapPhase.FAILED
    assert lc.active_quantity == Decimal("8")
    assert lc.manual_intervention_required is True
    assert lc.position_closed is False
    assert oms.sell_submit_count == 0
    summary = harness.terminal_summary()
    assert summary is not None
    assert summary.get("operational_pass") is False
    assert summary.get("position_closed") is False
    assert Decimal(str(summary.get("remaining_quantity"))) == Decimal("8")
    assert Decimal(str(summary.get("allocated_quantity"))) == Decimal("8")
    assert Decimal(str(summary.get("venue_reported_quantity"))) == 0


def test_unfilled_sell_releases_reservation() -> None:
    coord = _coord()
    ledger = coord.allocation_ledger
    assert ledger is not None
    ledger.apply_buy("z_gap", TokenId("111"), Decimal("8"), correlation_id="b1")
    mut = ledger.reserve_exit("z_gap", TokenId("111"), Decimal("8"), "exit-order-1")
    assert mut is not None
    assert ledger.get_allocated("z_gap", TokenId("111")) == Decimal("8")
    assert ledger.get_reserved("z_gap", TokenId("111")) == Decimal("8")
    release = ledger.release_reservation("exit-order-1", reason="unfilled", source="test")
    assert release is not None
    assert ledger.get_reserved("z_gap", TokenId("111")) == Decimal("0")


def test_partial_sell_reserves_outstanding_only() -> None:
    coord = _coord()
    ledger = coord.allocation_ledger
    assert ledger is not None
    ledger.apply_buy("z_gap", TokenId("111"), Decimal("8"), correlation_id="b1")
    ledger.reserve_exit("z_gap", TokenId("111"), Decimal("8"), "exit-1")
    ledger.apply_exit_fill("exit-1", Decimal("3"), source="test")
    assert ledger.get_allocated("z_gap", TokenId("111")) == Decimal("5")
    assert ledger.get_reserved("z_gap", TokenId("111")) == Decimal("5")


def test_duplicate_buy_fill_idempotent() -> None:
    from tyrex_pm.strategies.z_gap.reconciliation import duplicate_fill_idempotent

    coord = _coord()
    ledger = coord.allocation_ledger
    assert ledger is not None
    before = ledger.get_allocated("z_gap", TokenId("111"))
    ledger.apply_buy("z_gap", TokenId("111"), Decimal("5"), correlation_id="b1")
    assert duplicate_fill_idempotent(
        coord,
        owner_id="z_gap",
        token_id=TokenId("111"),
        qty=Decimal("5"),
        before_qty=before,
    )


def test_duplicate_sell_fill_idempotent_via_dedup() -> None:
    coord = _coord()
    ledger = coord.allocation_ledger
    assert ledger is not None
    ledger.apply_buy("z_gap", TokenId("111"), Decimal("8"), correlation_id="b1")
    ledger.reserve_exit("z_gap", TokenId("111"), Decimal("3"), "exit-1")
    ledger.apply_exit_fill("exit-1", Decimal("3"), source="test", dedup_key="sell-1")
    ledger.apply_exit_fill("exit-1", Decimal("3"), source="test", dedup_key="sell-1")
    assert ledger.get_allocated("z_gap", TokenId("111")) == Decimal("5")


def test_delayed_fill_cannot_create_negative_allocation() -> None:
    coord = _coord()
    ledger = coord.allocation_ledger
    assert ledger is not None
    ledger.apply_buy("z_gap", TokenId("111"), Decimal("5"), correlation_id="b1")
    ledger.apply_sell("z_gap", TokenId("111"), Decimal("5"), correlation_id="s1")
    ledger.apply_sell("z_gap", TokenId("111"), Decimal("1"), correlation_id="s2")
    assert ledger.get_allocated("z_gap", TokenId("111")) == Decimal("0")


def test_exit_retry_cannot_exceed_residual_active_quantity() -> None:
    coord = _coord()
    lc = ZGapLifecycleState(market_id="m1", condition_id="0x", owner_id="z_gap")
    lc.phase = ZGapPhase.ACTIVE
    lc.token_id = "111"
    lc.active_quantity = Decimal("5")
    mark_exit_triggered(lc, exit_reason="lifecycle_flatten")
    out = reconcile_exit_fill(lc, filled_qty=Decimal("3"), coord=None)
    assert out.event == "exit_partial"
    assert lc.active_quantity == Decimal("2")
    mark_exit_triggered(lc, exit_reason="lifecycle_flatten")
    out2 = reconcile_exit_fill(lc, filled_qty=Decimal("3"), coord=None)
    assert out2.failure is True
    assert out2.failure_reason == "oversell"
    assert lc.active_quantity == Decimal("2")


def test_entry_duplicate_snapshot_does_not_double_activate() -> None:
    coord = _coord()
    lc = ZGapLifecycleState(market_id="m1", condition_id="0x", owner_id="z_gap")
    lc.phase = ZGapPhase.ENTRY_PENDING
    lc.token_id = "111"
    snap = OrderFillSnapshot(
        token_id=TokenId("111"),
        owner_id="z_gap",
        client_order_id="entry-1",
        submitted_qty=Decimal("5"),
        filled_qty=Decimal("5"),
        confirmed_qty=Decimal("5"),
        remaining_qty=Decimal("0"),
        status=EntryFillStatus.MATCHED,
        source="test",
        sellable_qty=Decimal("5"),
    )
    coord.allocation_ledger.apply_buy("z_gap", TokenId("111"), Decimal("5"), correlation_id="b1")
    first = reconcile_entry_fill(lc, snap, coord=coord)
    assert first.event == "entry_filled"
    qty_after = lc.active_quantity
    second = reconcile_entry_fill(lc, snap, coord=coord)
    assert second.event == "ignored"
    assert lc.active_quantity == qty_after
    ledger = coord.allocation_ledger
    assert ledger is not None
    assert ledger.get_allocated("z_gap", TokenId("111")) == Decimal("5")
