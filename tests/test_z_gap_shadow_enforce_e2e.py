"""End-to-end shadow enforce scenarios for Z-Gap (A0.8)."""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from tyrex_pm.strategies.z_gap.scenario_oms import OmsFillSpec, ScenarioOMS
from tyrex_pm.strategies.z_gap.shadow_harness import ShadowHarness, TickSpec
from tyrex_pm.strategies.z_gap.state import ZGapPhase

EVENT_START = time.time() + 120
EVENT_END = EVENT_START + 300


@pytest.mark.asyncio
async def test_scenario1_full_buy_thesis_stop_exit(tmp_path) -> None:
    oms = ScenarioOMS(
        buy_fills=[OmsFillSpec(status="matched", taking_amount="8")],
        sell_fills=[OmsFillSpec(status="matched", making_amount="8")],
    )
    harness = ShadowHarness.create(
        tmp_path=tmp_path,
        event_start_ts=EVENT_START,
        event_end_ts=EVENT_END,
        oms=oms,
        exit_cfg={"stop_confirm_s": 0, "retry_interval_ms": 0, "z_stop": "0.05"},
    )
    result = await harness.run_ticks(
        [
            TickSpec(now_ts=EVENT_START + 90, binance_price=Decimal("100150")),
            TickSpec(now_ts=EVENT_START + 91, binance_price=Decimal("100150")),
            TickSpec(now_ts=EVENT_START + 92, binance_price=Decimal("99800")),
            TickSpec(now_ts=EVENT_START + 93, binance_price=Decimal("99800")),
        ]
    )
    lc = result.enforce_state.lifecycle
    assert lc is not None
    assert oms.buy_submit_count == 1
    assert oms.sell_submit_count == 1
    assert lc.phase == ZGapPhase.DONE
    assert lc.exit_reason == "thesis_stop"
    assert lc.active_quantity == 0
    summary = harness.terminal_summary()
    assert summary is not None
    assert summary.get("operational_pass") is True
    assert summary.get("allocation_zero") is True
    assert Decimal(str(summary.get("remaining_quantity", "1"))) == 0


@pytest.mark.asyncio
async def test_scenario2_partial_buy_lifecycle_flatten(tmp_path) -> None:
    oms = ScenarioOMS(
        buy_fills=[OmsFillSpec(status="matched", taking_amount="3")],
        sell_fills=[OmsFillSpec(status="matched", making_amount="3")],
    )
    near_end = EVENT_END - 10
    harness = ShadowHarness.create(
        tmp_path=tmp_path,
        event_start_ts=EVENT_START,
        event_end_ts=EVENT_END,
        oms=oms,
        exit_cfg={"stop_confirm_s": 0, "retry_interval_ms": 0, "z_stop": "0.05"},
    )
    result = await harness.run_ticks(
        [
            TickSpec(now_ts=EVENT_START + 90, binance_price=Decimal("100150")),
            TickSpec(now_ts=near_end, binance_price=Decimal("100150"), event_end_ts=EVENT_END),
            TickSpec(now_ts=near_end + 1, binance_price=Decimal("100150"), event_end_ts=EVENT_END),
        ]
    )
    lc = result.enforce_state.lifecycle
    assert lc is not None
    assert lc.entry_filled_shares == Decimal("3")
    assert lc.active_quantity == 0
    assert lc.exit_reason == "lifecycle_flatten"
    assert lc.entry_requested_shares > lc.entry_filled_shares


@pytest.mark.asyncio
async def test_scenario3_zero_buy_fill(tmp_path) -> None:
    oms = ScenarioOMS(buy_fills=[OmsFillSpec(status="expired", taking_amount="")])
    harness = ShadowHarness.create(
        tmp_path=tmp_path,
        event_start_ts=EVENT_START,
        event_end_ts=EVENT_END,
        oms=oms,
    )
    result = await harness.run_ticks(
        [TickSpec(now_ts=EVENT_START + 90, binance_price=Decimal("100150"))]
    )
    lc = result.enforce_state.lifecycle
    assert lc is not None
    assert lc.phase == ZGapPhase.DONE
    assert not lc.position_activated
    assert oms.sell_submit_count == 0


@pytest.mark.asyncio
async def test_scenario4_partial_sell_retry(tmp_path) -> None:
    oms = ScenarioOMS(
        buy_fills=[OmsFillSpec(status="matched", taking_amount="8")],
        sell_fills=[
            OmsFillSpec(status="matched", making_amount="3"),
            OmsFillSpec(status="matched", making_amount="5"),
        ],
    )
    harness = ShadowHarness.create(
        tmp_path=tmp_path,
        event_start_ts=EVENT_START,
        event_end_ts=EVENT_END,
        oms=oms,
        exit_cfg={"stop_confirm_s": 0, "retry_interval_ms": 0, "max_exit_attempts": 5, "z_stop": "0.05"},
    )
    result = await harness.run_ticks(
        [
            TickSpec(now_ts=EVENT_START + 90, binance_price=Decimal("100150")),
            TickSpec(now_ts=EVENT_START + 91, binance_price=Decimal("100150")),
            TickSpec(now_ts=EVENT_START + 92, binance_price=Decimal("99800")),
            TickSpec(now_ts=EVENT_START + 93, binance_price=Decimal("99800")),
            TickSpec(now_ts=EVENT_START + 94, binance_price=Decimal("99800")),
        ]
    )
    lc = result.enforce_state.lifecycle
    assert lc is not None
    assert oms.sell_submit_count == 2
    assert lc.phase == ZGapPhase.DONE
    assert lc.active_quantity == 0


@pytest.mark.asyncio
async def test_scenario5_exit_failure_max_attempts(tmp_path) -> None:
    near_end = EVENT_END - 10
    oms = ScenarioOMS(
        buy_fills=[OmsFillSpec(status="matched", taking_amount="5")],
        sell_fills=[OmsFillSpec(status="expired", making_amount="") for _ in range(5)],
    )
    harness = ShadowHarness.create(
        tmp_path=tmp_path,
        event_start_ts=EVENT_START,
        event_end_ts=EVENT_END,
        oms=oms,
        exit_cfg={"stop_confirm_s": 0, "retry_interval_ms": 0, "max_exit_attempts": 3},
    )
    result = await harness.run_ticks(
        [
            TickSpec(now_ts=EVENT_START + 90, binance_price=Decimal("100150")),
            TickSpec(now_ts=near_end, binance_price=Decimal("100150"), event_end_ts=EVENT_END),
            TickSpec(now_ts=near_end + 1, binance_price=Decimal("100150"), event_end_ts=EVENT_END),
            TickSpec(now_ts=near_end + 2, binance_price=Decimal("100150"), event_end_ts=EVENT_END),
            TickSpec(now_ts=near_end + 3, binance_price=Decimal("100150"), event_end_ts=EVENT_END),
            TickSpec(now_ts=near_end + 4, binance_price=Decimal("100150"), event_end_ts=EVENT_END),
        ]
    )
    lc = result.enforce_state.lifecycle
    assert lc is not None
    assert lc.phase == ZGapPhase.FAILED
    assert lc.manual_intervention_required
    assert lc.active_quantity > 0
    summary = harness.terminal_summary()
    assert summary is not None
    assert summary.get("manual_intervention_required") is True
    assert Decimal(str(summary.get("remaining_quantity"))) > 0
