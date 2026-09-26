from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tyrex_pm.execution.final_gate import FinalExecutionGate, FinalGatePolicy
from tyrex_pm.execution.orders import LimitOrderSpec, MarketBuyOrderSpec, OrderSide, TimeInForce
from tyrex_pm.market_data.book_health import SyncHealth
from tyrex_pm.runtime.capabilities import CapabilityController


def _spec(*, candidate_monotonic_ns: int, tick_size: str = "0.01") -> MarketBuyOrderSpec:
    return MarketBuyOrderSpec(
        order_id="entry",
        market_id="market",
        instrument_id="market:YES",
        token_id="yes",
        spend_amount=Decimal("4.90"),
        maximum_total_debit=Decimal("5"),
        worst_price=Decimal("0.71"),
        metadata={
            # Deliberately unrelated wall time: freshness must not use it.
            "candidate_at": "2020-01-01T00:00:00+00:00",
            "candidate_monotonic_ns": candidate_monotonic_ns,
            "venue_tick_size": tick_size,
        },
    )


def _view(*, tick_size: str = "0.01") -> SimpleNamespace:
    leg = SimpleNamespace(
        token_id="yes",
        sync_health=SyncHealth.READY,
        data_age_ms=10,
        tick_size=Decimal(tick_size),
        quote=SimpleNamespace(
            best_ask=Decimal("0.50"),
            best_bid=Decimal("0.49"),
            ask_size_at_touch=Decimal("100"),
        ),
    )
    return SimpleNamespace(binding_id="binding", up=leg, down=SimpleNamespace(token_id="no"))


def _capabilities() -> CapabilityController:
    return CapabilityController(
        live_requested=True,
        public_feeds_ready=True,
        active_market_ready=True,
        books_ready=True,
        model_ready=True,
        account_reads_ready=True,
        user_stream_ready=True,
        collateral_ready=True,
        entry_allowance_ready=True,
        order_metadata_ready=True,
        prior_scope_clear=True,
        entry_window_open=True,
        selected_token_sellable=True,
    )


@pytest.mark.asyncio
async def test_candidate_freshness_uses_monotonic_time_not_wall_clock() -> None:
    gate = FinalExecutionGate(
        capture_book=_view,
        capabilities=_capabilities(),
        active_binding_id=lambda: "binding",
        policy=FinalGatePolicy(max_candidate_age_ms=5_000),
    )
    allowed, reason = await gate(_spec(candidate_monotonic_ns=time.monotonic_ns()))
    assert allowed
    assert reason is None


@pytest.mark.asyncio
async def test_post_sign_gate_rejects_stale_candidate_and_changed_tick() -> None:
    stale_gate = FinalExecutionGate(
        capture_book=_view,
        capabilities=_capabilities(),
        active_binding_id=lambda: "binding",
        policy=FinalGatePolicy(max_candidate_age_ms=5),
    )
    allowed, reason = await stale_gate(
        _spec(candidate_monotonic_ns=time.monotonic_ns() - 10_000_000)
    )
    assert not allowed
    assert reason == "candidate_stale_after_signing"

    changed_tick_gate = FinalExecutionGate(
        capture_book=lambda: _view(tick_size="0.001"),
        capabilities=_capabilities(),
        active_binding_id=lambda: "binding",
    )
    allowed, reason = await changed_tick_gate(
        _spec(candidate_monotonic_ns=time.monotonic_ns(), tick_size="0.01")
    )
    assert not allowed
    assert reason == "book_tick_size_changed_after_signing"


def _limit(*, side: OrderSide, price: Decimal, post_only: bool = True) -> LimitOrderSpec:
    return LimitOrderSpec(
        order_id="limit",
        market_id="market",
        instrument_id="market:YES",
        token_id="yes",
        side=side,
        shares=Decimal("5"),
        limit_price=price,
        time_in_force=TimeInForce.GTC,
        metadata={
            "post_only": post_only,
            "candidate_monotonic_ns": time.monotonic_ns(),
            "venue_tick_size": "0.01",
        },
    )


@pytest.mark.asyncio
async def test_post_only_limit_refuses_a_crossing_price() -> None:
    gate = FinalExecutionGate(
        capture_book=_view,
        capabilities=_capabilities(),
        active_binding_id=lambda: "binding",
    )
    allowed, reason = await gate(_limit(side=OrderSide.BUY, price=Decimal("0.50")))
    assert not allowed
    assert reason == "post_only_would_cross"
    allowed, reason = await gate(_limit(side=OrderSide.SELL, price=Decimal("0.49")))
    assert not allowed
    assert reason == "post_only_would_cross"
    allowed, reason = await gate(_limit(side=OrderSide.BUY, price=Decimal("0.49")))
    assert allowed
    assert reason is None


@pytest.mark.asyncio
async def test_d6_buy_gate_refuses_below_effective_cutoff() -> None:
    gate = FinalExecutionGate(
        capture_book=_view,
        capabilities=_capabilities(),
        active_binding_id=lambda: "binding",
    )
    gate.entry_cutoff_s = 105.0
    gate.entry_tau_s = lambda: 100.0
    allowed, reason = await gate(_spec(candidate_monotonic_ns=time.monotonic_ns()))
    assert not allowed
    assert reason == "ENTRY_CUTOFF_PASSED"


@pytest.mark.asyncio
async def test_hard_cutoff_equality_allows_tau_equal_to_cutoff() -> None:
    gate = FinalExecutionGate(
        capture_book=_view,
        capabilities=_capabilities(),
        active_binding_id=lambda: "binding",
    )
    gate.entry_cutoff_s = 105.0
    gate.entry_tau_s = lambda: 105.0
    allowed, reason = await gate(_spec(candidate_monotonic_ns=time.monotonic_ns()))
    assert allowed
    assert reason is None


@pytest.mark.asyncio
async def test_hard_cutoff_rejects_observed_ask70_tau() -> None:
    gate = FinalExecutionGate(
        capture_book=_view,
        capabilities=_capabilities(),
        active_binding_id=lambda: "binding",
    )
    gate.entry_cutoff_s = 105.0
    gate.entry_tau_s = lambda: 104.930171
    allowed, reason = await gate(_spec(candidate_monotonic_ns=time.monotonic_ns()))
    assert not allowed
    assert reason == "ENTRY_CUTOFF_PASSED"


@pytest.mark.asyncio
async def test_d8_sell_ignores_entry_cutoff() -> None:
    gate = FinalExecutionGate(
        capture_book=_view,
        capabilities=_capabilities(),
        active_binding_id=lambda: "binding",
    )
    gate.entry_cutoff_s = 105.0
    gate.entry_tau_s = lambda: 40.0
    sell = _limit(side=OrderSide.SELL, price=Decimal("0.40"), post_only=False)
    allowed, reason = await gate(sell)
    assert allowed
    assert reason is None


@pytest.mark.asyncio
async def test_d10_stricter_strategy_cutoff_crossed_during_prepare() -> None:
    gate = FinalExecutionGate(
        capture_book=_view,
        capabilities=_capabilities(),
        active_binding_id=lambda: "binding",
    )
    gate.entry_cutoff_s = 120.0
    gate.entry_tau_s = lambda: 118.0
    allowed, reason = await gate(_spec(candidate_monotonic_ns=time.monotonic_ns()))
    assert not allowed
    assert reason == "ENTRY_CUTOFF_PASSED"


@pytest.mark.asyncio
async def test_v8_buy_gate_fails_when_features_invalidate() -> None:
    gate = FinalExecutionGate(
        capture_book=_view,
        capabilities=_capabilities(),
        active_binding_id=lambda: "binding",
    )
    gate.features_ready = lambda: "EWMA_NOT_READY"
    allowed, reason = await gate(_spec(candidate_monotonic_ns=time.monotonic_ns()))
    assert not allowed
    assert reason == "EWMA_NOT_READY"


@pytest.mark.asyncio
async def test_v9_sell_ignores_ewma_unreadiness() -> None:
    gate = FinalExecutionGate(
        capture_book=_view,
        capabilities=_capabilities(),
        active_binding_id=lambda: "binding",
    )
    gate.features_ready = lambda: "EWMA_NOT_READY"
    sell = _limit(side=OrderSide.SELL, price=Decimal("0.40"), post_only=False)
    allowed, reason = await gate(sell)
    assert allowed
    assert reason is None


@pytest.mark.asyncio
async def test_v10_empty_graph_features_callback_is_noop() -> None:
    gate = FinalExecutionGate(
        capture_book=_view,
        capabilities=_capabilities(),
        active_binding_id=lambda: "binding",
    )
    gate.features_ready = lambda: True
    allowed, reason = await gate(_spec(candidate_monotonic_ns=time.monotonic_ns()))
    assert allowed
    assert reason is None
