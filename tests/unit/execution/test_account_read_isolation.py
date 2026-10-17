"""Account and reconciliation reads never delay order signing (isolated SDK clients).

Live evidence (missed 20:43:13 DOWN entry): one account refresh took 8.55 s (cash 1.77 s,
DOWN 2.31 s, UP 2.05 s, open orders 2.42 s) and signing waited behind it. Here every
read on the account and reconciliation clients takes 8 s; signing must not notice.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tyrex_pm.execution.account_state import AccountStateAuthority, AccountStatePolicy
from tyrex_pm.execution.orders import MarketBuyOrderSpec
from tyrex_pm.execution.polymarket.gateway import PolymarketAsyncGateway, VenueReadView

SLOW_S = 8.0


def _buy(order_id: str = "entry-1") -> MarketBuyOrderSpec:
    return MarketBuyOrderSpec(
        order_id=order_id,
        market_id="market-1",
        instrument_id="yes",
        token_id="yes-token",
        spend_amount=Decimal("4.83"),
        maximum_total_debit=Decimal("5"),
        worst_price=Decimal("0.51"),
        estimated_shares=Decimal("9.47"),
    )


class _Paginator:
    def __init__(self, delay_s: float, calls: list, name: str) -> None:
        self.delay_s, self.calls, self.name = delay_s, calls, name

    async def iter_items(self):
        self.calls.append(self.name)
        await asyncio.sleep(self.delay_s)
        for item in ():
            yield item


class _Client:
    """SDK double: every read takes ``read_s``; signing takes ``create_s``."""

    def __init__(self, *, read_s: float = 0.0, create_s: float = 0.0) -> None:
        self.read_s, self.create_s = read_s, create_s
        self.calls: list[str] = []
        self.closed = False

    async def get_balance_allowance(self, *, asset_type: str, token_id: str | None = None):
        self.calls.append(f"balance:{asset_type}")
        await asyncio.sleep(self.read_s)
        return SimpleNamespace(balance="20000000", allowance="20000000")

    def list_open_orders(self, **_kwargs):
        return _Paginator(self.read_s, self.calls, "open_orders")

    def list_account_trades(self, **_kwargs):
        return _Paginator(self.read_s, self.calls, "trades")

    async def create_market_order(self, **kwargs):
        self.calls.append(f"create:{kwargs.get('token_id')}")
        await asyncio.sleep(self.create_s)
        return {"signed": True, "token": kwargs.get("token_id"), "at": time.monotonic()}

    async def post_order(self, signed):
        self.calls.append("post")
        return SimpleNamespace(ok=True, order_id="venue-1", status="matched",
                               making_amount=Decimal("0"), taking_amount=Decimal("0"),
                               trade_ids=())

    async def close(self) -> None:
        self.closed = True


def _gateway(**clients) -> PolymarketAsyncGateway:
    return PolymarketAsyncGateway(
        client=clients.get("trading", _Client()),
        inspection_client=clients.get("inspection"),
        account_client=clients.get("account"),
        reconciliation_client=clients.get("reconciliation"),
        tick_size_resolver=lambda _token: Decimal("0.01"),
        prepare_budget_ms=1500,
    )


def _policy() -> AccountStatePolicy:
    return AccountStatePolicy(refresh_interval_s=30.0, snapshot_max_age_s=30.0,
                              read_timeout_s=10.0, retry_attempts=1, retry_base_delay_s=0.001)


@pytest.mark.asyncio
async def test_signing_never_waits_for_a_slow_account_refresh() -> None:
    trading, account = _Client(), _Client(read_s=SLOW_S)
    gateway = _gateway(trading=trading, account=account, reconciliation=_Client())
    assert gateway.reads_isolated and gateway.client_guard is None
    reads = gateway.account_reads()
    assert isinstance(reads, VenueReadView) and reads.client is account
    authority = AccountStateAuthority(reads, policy=_policy())
    authority.prepare(market_id="market-1", token_ids=("yes-token", "no-token"),
                      required_collateral=Decimal("5"), active=True)
    for _ in range(100):
        if authority.refresh_status() is not None:
            break
        await asyncio.sleep(0.005)
    assert authority.refresh_status() is not None  # an 8 s read is in flight
    started = time.monotonic()
    prepared = await gateway.prepare_order(_buy())
    elapsed_ms = (time.monotonic() - started) * 1000
    assert elapsed_ms < 500, elapsed_ms
    assert prepared.refresh_wait_ms is None and prepared.prepare_queue_wait_ms < 50
    assert authority.refresh_status() is not None  # the refresh simply continues
    assert all(not call.startswith(("balance", "open_orders")) for call in trading.calls)
    assert account.calls and all(c.startswith(("balance", "open_orders")) for c in account.calls)
    await authority.close()


@pytest.mark.asyncio
async def test_reconciliation_reads_run_beside_signing_on_their_own_client() -> None:
    trading, reconciliation = _Client(), _Client(read_s=SLOW_S)
    gateway = _gateway(trading=trading, account=_Client(), reconciliation=reconciliation)
    recon = gateway.reconciliation_reads()
    assert recon.client is reconciliation and recon.role == "reconciliation"
    pending = asyncio.create_task(recon.list_account_trades(token_id="yes-token"))
    await asyncio.sleep(0.01)
    started = time.monotonic()
    await gateway.prepare_order(_buy())
    assert (time.monotonic() - started) * 1000 < 500
    assert not pending.done()  # still reading, in parallel
    assert reconciliation.calls == ["trades"]
    assert "trades" not in trading.calls
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)


def test_without_isolation_reads_share_the_trading_client_as_before() -> None:
    gateway = _gateway()
    assert not gateway.reads_isolated
    assert gateway.account_reads() is gateway and gateway.reconciliation_reads() is gateway


@pytest.mark.asyncio
async def test_metadata_warm_up_yields_to_an_order_in_progress() -> None:
    trading = _Client(create_s=0.2)
    gateway = _gateway(trading=trading)
    order = asyncio.create_task(gateway.prepare_order(_buy()))
    await asyncio.sleep(0.02)  # the entry is signing
    warm = asyncio.create_task(gateway.warm_order_metadata(
        ["next-token"], max_price=Decimal("0.5"), max_spend=Decimal("5")))
    await asyncio.sleep(0.05)
    assert trading.calls == ["create:yes-token"]  # the warm-up has not started
    await order
    await warm
    assert trading.calls == ["create:yes-token", "create:next-token"]


@pytest.mark.asyncio
async def test_close_closes_every_client() -> None:
    clients = {name: _Client() for name in ("trading", "inspection", "account",
                                            "reconciliation")}
    gateway = _gateway(**clients)
    await gateway.close()
    assert all(c.closed for c in clients.values())
