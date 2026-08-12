from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tyrex_pm.execution.final_gate import FinalExecutionGate, FinalGatePolicy
from tyrex_pm.execution.orders import MarketBuyOrderSpec
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
