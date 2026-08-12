from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from tyrex_pm.execution.coordinator import AccountExecutionCoordinator, DispatchBlocked
from tyrex_pm.execution.evidence import ExecutionRole, SessionOpened, new_envelope
from tyrex_pm.execution.orders import MarketBuyOrderSpec, MarketSellOrderSpec
from tyrex_pm.execution.polymarket.gateway import PolymarketAsyncGateway
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal


class _AsyncPaginator:
    def __init__(self, items):  # noqa: ANN001
        self.items = tuple(items)

    async def iter_items(self):
        for item in self.items:
            yield item


@dataclass
class _Accepted:
    ok: bool
    order_id: str
    status: str
    making_amount: Decimal
    taking_amount: Decimal
    trade_ids: tuple[str, ...]


class _AsyncClient:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.posted: list[object] = []
        self.closed = False

    async def create_market_order(self, **kwargs):  # noqa: ANN003
        self.created.append(kwargs)
        return {"signed": True, "side": kwargs["side"]}

    async def post_order(self, signed):  # noqa: ANN001
        self.posted.append(signed)
        if signed["side"] == "BUY":
            return _Accepted(
                True,
                "venue-buy",
                "matched",
                Decimal("4.83"),
                Decimal("9.857141"),
                ("trade-1",),
            )
        return _Accepted(
            True,
            "venue-sell",
            "matched",
            Decimal("9.85"),
            Decimal("4.72"),
            ("trade-2",),
        )

    def list_open_orders(self, **kwargs):  # noqa: ANN003
        return _AsyncPaginator(({"id": "o1", **kwargs},))

    def list_account_trades(self, **kwargs):  # noqa: ANN003
        return _AsyncPaginator(({"id": "t1", **kwargs},))

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_buy_uses_spend_and_reports_venue_acquired_shares() -> None:
    client = _AsyncClient()
    gateway = PolymarketAsyncGateway(
        client=client,
        tick_size_resolver=lambda _token_id: Decimal("0.01"),
    )
    spec = MarketBuyOrderSpec(
        order_id="entry",
        market_id="m",
        instrument_id="m:YES",
        token_id="token",
        spend_amount=Decimal("4.83"),
        maximum_total_debit=Decimal("5"),
        worst_price=Decimal("0.51"),
        estimated_shares=Decimal("9.470588"),
    )
    prepared = await gateway.prepare_order(spec)
    result = await gateway.post_order(prepared)
    assert client.created == [
        {
            "token_id": "token",
            "side": "BUY",
            "amount": Decimal("4.83"),
            "max_spend": Decimal("5"),
            "max_price": Decimal("0.51"),
            "order_type": "FAK",
        }
    ]
    assert result.cumulative_matched_shares == Decimal("9.857141")


@pytest.mark.asyncio
async def test_sell_uses_shares_and_async_paginators_are_drained_async() -> None:
    client = _AsyncClient()
    gateway = PolymarketAsyncGateway(
        client=client,
        tick_size_resolver=lambda _token_id: Decimal("0.01"),
    )
    spec = MarketSellOrderSpec(
        order_id="exit",
        market_id="m",
        instrument_id="m:YES",
        token_id="token",
        shares=Decimal("9.85"),
        minimum_price=Decimal("0.47"),
    )
    prepared = await gateway.prepare_order(spec)
    result = await gateway.post_order(prepared)
    assert client.created[0]["shares"] == Decimal("9.85")
    assert "amount" not in client.created[0]
    assert result.cumulative_matched_shares == Decimal("9.85")
    assert await gateway.list_open_orders(token_id="token", market_id="m")
    assert await gateway.list_account_trades(token_id="token", market_id="m")


@pytest.mark.asyncio
async def test_protection_prices_are_adapted_to_tick_without_weakening_limits() -> None:
    client = _AsyncClient()
    gateway = PolymarketAsyncGateway(
        client=client,
        tick_size_resolver=lambda _token_id: Decimal("0.01"),
    )
    buy = MarketBuyOrderSpec(
        order_id="entry",
        market_id="m",
        instrument_id="m:YES",
        token_id="token",
        spend_amount=Decimal("4.90"),
        maximum_total_debit=Decimal("5"),
        worst_price=Decimal("0.7146489189623736"),
    )
    sell = MarketSellOrderSpec(
        order_id="exit",
        market_id="m",
        instrument_id="m:YES",
        token_id="token",
        shares=Decimal("6.8"),
        minimum_price=Decimal("0.4712"),
    )

    prepared_buy = await gateway.prepare_order(buy)
    prepared_sell = await gateway.prepare_order(sell)

    assert client.created[0]["max_price"] == Decimal("0.71")
    assert prepared_buy.requested_protection_price == Decimal("0.7146489189623736")
    assert prepared_buy.effective_protection_price == Decimal("0.71")
    assert client.created[1]["min_price"] == Decimal("0.48")
    assert prepared_sell.effective_protection_price == Decimal("0.48")


@pytest.mark.asyncio
async def test_missing_authoritative_tick_fails_before_sdk_preparation() -> None:
    client = _AsyncClient()
    gateway = PolymarketAsyncGateway(client=client)
    spec = MarketBuyOrderSpec(
        order_id="entry",
        market_id="m",
        instrument_id="m:YES",
        token_id="token",
        spend_amount=Decimal("4.90"),
        maximum_total_debit=Decimal("5"),
        worst_price=Decimal("0.71"),
    )
    with pytest.raises(RuntimeError, match="authoritative tick size is unavailable"):
        await gateway.prepare_order(spec)
    assert client.created == []


@pytest.mark.asyncio
async def test_production_composition_adapts_signs_gates_and_posts() -> None:
    client = _AsyncClient()
    gateway = PolymarketAsyncGateway(
        client=client,
        tick_size_resolver=lambda _token_id: Decimal("0.01"),
    )
    gated_prices: list[Decimal] = []

    async def final_gate(spec):  # noqa: ANN001
        gated_prices.append(spec.worst_price)
        return True, None

    journal = MemoryExecutionJournal()
    coordinator = AccountExecutionCoordinator(
        journal=journal,
        gateway=gateway,
        final_gate=final_gate,
    )
    await coordinator.apply(
        SessionOpened(
            **new_envelope(session_id="session", dedupe_key="opened"),
            strategy_id="z_gap",
            market_id="m",
            window_id="window",
            token_id="token",
        )
    )
    spec = MarketBuyOrderSpec(
        order_id="entry",
        market_id="m",
        instrument_id="m:YES",
        token_id="token",
        spend_amount=Decimal("4.90"),
        maximum_total_debit=Decimal("5"),
        worst_price=Decimal("0.7146489189623736"),
    )
    result = await coordinator.submit(
        session_id="session",
        role=ExecutionRole.ENTRY,
        spec=spec,
    )

    assert result.accepted
    assert gated_prices == [Decimal("0.71")]
    assert client.created[0]["max_price"] == Decimal("0.71")
    assert len(client.posted) == 1
    state = coordinator.state("session")
    assert state.entry is not None
    assert state.entry.requested_protection_price == Decimal("0.7146489189623736")
    assert state.entry.effective_protection_price == Decimal("0.71")
    assert coordinator.mutation_attempt_count("session") == 1
    await coordinator.close()


@pytest.mark.asyncio
async def test_final_gate_rejection_discards_signed_order_without_post() -> None:
    client = _AsyncClient()
    gateway = PolymarketAsyncGateway(
        client=client,
        tick_size_resolver=lambda _token_id: Decimal("0.01"),
    )
    coordinator = AccountExecutionCoordinator(
        journal=MemoryExecutionJournal(),
        gateway=gateway,
        final_gate=lambda _spec: _resolved((False, "entry_price_moved")),
    )
    await coordinator.apply(
        SessionOpened(
            **new_envelope(session_id="session", dedupe_key="opened"),
            strategy_id="z_gap",
            market_id="m",
            window_id="window",
            token_id="token",
        )
    )
    spec = MarketBuyOrderSpec(
        order_id="entry",
        market_id="m",
        instrument_id="m:YES",
        token_id="token",
        spend_amount=Decimal("4.90"),
        maximum_total_debit=Decimal("5"),
        worst_price=Decimal("0.7146"),
    )
    with pytest.raises(DispatchBlocked, match="entry_price_moved"):
        await coordinator.submit(
            session_id="session",
            role=ExecutionRole.ENTRY,
            spec=spec,
        )
    assert client.posted == []
    assert gateway._prepared_specs == {}
    assert coordinator.state("session").phase.value == "COMPLETED_NO_DISPATCH"
    await coordinator.close()


@pytest.mark.asyncio
async def test_prepare_uses_fetcher_when_local_tick_missing() -> None:
    client = _AsyncClient()
    calls = {"resolver": 0, "fetcher": 0}

    def resolver(_token_id: str) -> Decimal | None:
        calls["resolver"] += 1
        return None

    async def fetcher(_token_id: str) -> Decimal | None:
        calls["fetcher"] += 1
        return Decimal("0.01")

    gateway = PolymarketAsyncGateway(
        client=client,
        tick_size_resolver=resolver,
        tick_size_fetcher=fetcher,
    )
    spec = MarketBuyOrderSpec(
        order_id="entry",
        market_id="m",
        instrument_id="m:YES",
        token_id="token",
        spend_amount=Decimal("4.83"),
        maximum_total_debit=Decimal("5"),
        worst_price=Decimal("0.503"),
    )
    prepared = await gateway.prepare_order(spec)
    assert calls == {"resolver": 1, "fetcher": 1}
    assert prepared.tick_size == Decimal("0.01")
    assert prepared.effective_protection_price == Decimal("0.50")
    assert prepared.dispatch_spec.metadata["venue_tick_size"] == "0.01"


@pytest.mark.asyncio
async def test_prepare_fails_closed_when_tick_still_unavailable() -> None:
    from tyrex_pm.execution.polymarket.gateway import VenueOrderPreparationError

    async def fetcher(_token_id: str) -> Decimal | None:
        return None

    gateway = PolymarketAsyncGateway(
        client=_AsyncClient(),
        tick_size_resolver=lambda _token_id: None,
        tick_size_fetcher=fetcher,
    )
    spec = MarketBuyOrderSpec(
        order_id="entry",
        market_id="m",
        instrument_id="m:YES",
        token_id="token",
        spend_amount=Decimal("4.83"),
        maximum_total_debit=Decimal("5"),
        worst_price=Decimal("0.50"),
    )
    with pytest.raises(VenueOrderPreparationError, match="authoritative tick size is unavailable"):
        await gateway.prepare_order(spec)


@pytest.mark.asyncio
async def test_warm_order_metadata_signs_without_posting() -> None:
    client = _AsyncClient()
    gateway = PolymarketAsyncGateway(client=client)
    result = await gateway.warm_order_metadata(
        ("token-a", "token-b", "token-a"),
        max_price=Decimal("0.99"),
        max_spend=Decimal("5"),
    )
    assert result.ok
    assert result.warmed_token_ids == ("token-a", "token-b")
    assert len(client.created) == 2
    assert client.posted == []
    assert all(item["side"] == "BUY" for item in client.created)
    assert all(item["max_spend"] == Decimal("5") for item in client.created)
    assert all(item["max_price"] == Decimal("0.99") for item in client.created)
    assert gateway._prepared_specs == {}


@pytest.mark.asyncio
async def test_warm_order_metadata_reports_partial_failure() -> None:
    class _FailingClient(_AsyncClient):
        async def create_market_order(self, **kwargs):  # noqa: ANN003
            if kwargs["token_id"] == "bad":
                raise RuntimeError("metadata boom")
            return await super().create_market_order(**kwargs)

    client = _FailingClient()
    gateway = PolymarketAsyncGateway(client=client)
    result = await gateway.warm_order_metadata(
        ("good", "bad"),
        max_price=Decimal("0.99"),
        max_spend=Decimal("5"),
    )
    assert not result.ok
    assert result.warmed_token_ids == ("good",)
    by_token = {item.token_id: item for item in result.tokens}
    assert by_token["good"].ok
    assert not by_token["bad"].ok
    assert "RuntimeError" in str(by_token["bad"].error)
    assert client.posted == []


async def _resolved(value):  # noqa: ANN001
    return value
