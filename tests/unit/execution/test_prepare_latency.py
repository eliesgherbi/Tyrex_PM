"""G2 submit latency: refresh wait, prepare budget, and split timings."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import pytest

from tyrex_pm.execution.account_state import AccountStateAuthority, AccountStatePolicy
from tyrex_pm.execution.coordinator import AccountExecutionCoordinator
from tyrex_pm.execution.evidence import ExecutionRole, SessionOpened, new_envelope
from tyrex_pm.execution.orders import MarketBuyOrderSpec
from tyrex_pm.execution.polymarket.gateway import (
    PolymarketAsyncGateway,
    VenueOrderPreparationError,
)
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal


def _buy() -> MarketBuyOrderSpec:
    return MarketBuyOrderSpec(
        order_id="entry-1",
        market_id="market-1",
        instrument_id="yes",
        token_id="yes-token",
        spend_amount=Decimal("4.83"),
        maximum_total_debit=Decimal("5"),
        worst_price=Decimal("0.51"),
        estimated_shares=Decimal("9.47"),
    )


class _FastClient:
    def __init__(self, *, create_delay_s: float = 0.0) -> None:
        self.created: list[dict] = []
        self.create_delay_s = create_delay_s

    async def create_market_order(self, **kwargs):  # noqa: ANN003
        if self.create_delay_s:
            await asyncio.sleep(self.create_delay_s)
        self.created.append(kwargs)
        return {"signed": True, "side": kwargs["side"]}

    async def close(self) -> None:
        return None

    async def post_order(self, signed):  # noqa: ANN001
        return type(
            "_Accepted",
            (),
            {
                "ok": True,
                "order_id": "venue-1",
                "status": "matched",
                "making_amount": Decimal("0"),
                "taking_amount": Decimal("0"),
                "trade_ids": (),
            },
        )()


class _SlowRefreshGateway:
    def __init__(self, *, hold_s: float) -> None:
        self.hold_s = hold_s
        self.collateral_started = asyncio.Event()

    async def collateral_balance(self):
        self.collateral_started.set()
        await asyncio.sleep(self.hold_s)
        return Decimal("20"), Decimal("20")

    async def conditional_balance(self, _token_id):
        return Decimal("0"), Decimal("100")

    async def list_open_orders(self, **_kwargs):
        return ()


def _policy() -> AccountStatePolicy:
    return AccountStatePolicy(
        refresh_interval_s=30.0,
        snapshot_max_age_s=30.0,
        read_timeout_s=10.0,
        retry_attempts=1,
        retry_base_delay_s=0.001,
    )


@pytest.mark.asyncio
async def test_prepare_waits_briefly_for_in_flight_refresh() -> None:
    authority = AccountStateAuthority(_SlowRefreshGateway(hold_s=0.2), policy=_policy())
    authority.prepare(
        market_id="market-1",
        token_ids=("yes-token",),
        required_collateral=Decimal("5"),
        active=True,
    )
    for _ in range(100):
        if authority.refresh_status() is not None:
            break
        await asyncio.sleep(0.005)
    assert authority.refresh_status() is not None

    client = _FastClient()
    gateway = PolymarketAsyncGateway(
        client=client,
        tick_size_resolver=lambda _token: Decimal("0.01"),
        client_guard=authority,
        refresh_wait_budget_ms=500,
        prepare_budget_ms=1500,
    )
    started = time.monotonic()
    prepared = await gateway.prepare_order(_buy())
    elapsed_ms = (time.monotonic() - started) * 1000
    assert elapsed_ms < 800
    assert prepared.refresh_wait_ms is not None
    assert prepared.refresh_wait_ms >= 50
    assert prepared.create_market_order_ms is not None
    assert prepared.create_market_order_ms < 100
    assert client.created
    await authority.close()


@pytest.mark.asyncio
async def test_long_refresh_fails_closed_without_starting_sign() -> None:
    authority = AccountStateAuthority(_SlowRefreshGateway(hold_s=5.0), policy=_policy())
    authority.prepare(
        market_id="market-1",
        token_ids=("yes-token",),
        required_collateral=Decimal("5"),
        active=True,
    )
    for _ in range(200):
        status = authority.refresh_status()
        if status is not None and status.elapsed_ms >= 50:
            break
        await asyncio.sleep(0.01)
    assert authority.refresh_status() is not None

    client = _FastClient()
    gateway = PolymarketAsyncGateway(
        client=client,
        tick_size_resolver=lambda _token: Decimal("0.01"),
        client_guard=authority,
        refresh_wait_budget_ms=200,
        prepare_budget_ms=1500,
    )
    started = time.monotonic()
    with pytest.raises(VenueOrderPreparationError) as exc:
        await gateway.prepare_order(_buy())
    elapsed_ms = (time.monotonic() - started) * 1000
    assert exc.value.error_code == "ACCOUNT_REFRESH_IN_FLIGHT"
    assert elapsed_ms < 800
    assert not client.created
    await authority.close()


@pytest.mark.asyncio
async def test_prepare_budget_abandons_slow_create_without_waiting_it_out() -> None:
    client = _FastClient(create_delay_s=1.0)
    gateway = PolymarketAsyncGateway(
        client=client,
        tick_size_resolver=lambda _token: Decimal("0.01"),
        prepare_budget_ms=100,
    )
    started = time.monotonic()
    with pytest.raises(VenueOrderPreparationError) as exc:
        await gateway.prepare_order(_buy())
    elapsed_ms = (time.monotonic() - started) * 1000
    assert exc.value.error_code == "PREPARE_BUDGET_EXCEEDED"
    assert elapsed_ms < 400
    # The SDK call is not cancelled; it may still complete in the background.
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_warm_then_create_stays_under_100ms_on_fixture() -> None:
    client = _FastClient()
    gateway = PolymarketAsyncGateway(
        client=client,
        tick_size_resolver=lambda _token: Decimal("0.01"),
        prepare_budget_ms=1500,
    )
    warm = await gateway.warm_order_metadata(
        ("yes-token",),
        max_price=Decimal("0.99"),
        max_spend=Decimal("5"),
        amount=Decimal("1"),
    )
    assert warm.ok
    prepared = await gateway.prepare_order(_buy())
    assert prepared.create_market_order_ms is not None
    assert prepared.create_market_order_ms < 100
    assert prepared.order_metadata_cache_miss is False


@pytest.mark.asyncio
async def test_slow_create_after_warm_records_cache_miss() -> None:
    recorded: list[tuple[str, dict]] = []
    client = _FastClient(create_delay_s=0.05)
    gateway = PolymarketAsyncGateway(
        client=client,
        tick_size_resolver=lambda _token: Decimal("0.01"),
        prepare_budget_ms=1500,
        metadata_cache_miss_ms=10,
        evidence_sink=lambda name, payload: recorded.append((name, dict(payload))),
    )
    await gateway.warm_order_metadata(
        ("yes-token",),
        max_price=Decimal("0.99"),
        max_spend=Decimal("5"),
        amount=Decimal("1"),
    )
    prepared = await gateway.prepare_order(_buy())
    assert prepared.order_metadata_cache_miss is True
    assert any(name == "ORDER_METADATA_CACHE_MISS" for name, _ in recorded)


@pytest.mark.asyncio
async def test_coordinator_persists_prepare_splits_on_success() -> None:
    client = _FastClient()
    gateway = PolymarketAsyncGateway(
        client=client,
        tick_size_resolver=lambda _token: Decimal("0.01"),
        prepare_budget_ms=1500,
    )
    coordinator = AccountExecutionCoordinator(
        journal=MemoryExecutionJournal(),
        gateway=gateway,
        final_gate=lambda _spec: asyncio.sleep(0, result=(True, None)),
    )
    await coordinator.apply(
        SessionOpened(
            **new_envelope(session_id="s1", dedupe_key="opened"),
            strategy_id="ask70",
            market_id="market-1",
            window_id="w1",
            token_id="yes-token",
        )
    )
    await coordinator.submit(session_id="s1", role=ExecutionRole.ENTRY, spec=_buy())
    order = coordinator.state("s1").entry
    assert order is not None
    assert order.create_market_order_ms is not None
    assert order.prepare_queue_wait_ms is not None
    assert order.tick_resolve_ms is not None
    await coordinator.close()
