"""Paper venue: one coherent simulated account behind every runtime read and write."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tyrex_pm.core.snapshots import BookLevel
from tyrex_pm.domain.polymarket.fees import FeeCurveParams
from tyrex_pm.execution.coordinator import PreparedOrder
from tyrex_pm.execution.orders import LimitOrderSpec, MarketBuyOrderSpec, MarketSellOrderSpec
from tyrex_pm.execution.paper import PaperAccount, PaperVenue, PaperVenueError
from tyrex_pm.execution.settlement.ledger import EpisodeState, SettlementLedger
from tyrex_pm.execution.settlement.resolution import PayoutObservation, ResolutionManager
from tyrex_pm.runtime.settlement_worker import SettlementWorker

CURVE = FeeCurveParams(fee_rate=Decimal("0.07"), exponent=Decimal("1"))


def lv(p: str, q: str) -> BookLevel:
    return BookLevel(price=Decimal(p), quantity=Decimal(q))


def view(asks=(), bids=()):
    book = SimpleNamespace(asks=tuple(asks), bids=tuple(bids))
    leg = SimpleNamespace(token_id="up", book=book)
    other = SimpleNamespace(token_id="down", book=None)
    return SimpleNamespace(up=leg, down=other)


def venue(tmp_path: Path, **kw) -> PaperVenue:
    account = PaperAccount.open(tmp_path / "paper_account.json",
                                initial_collateral=Decimal("20"))
    return PaperVenue(account=account, fee_curve=lambda _t: CURVE, order_latency_ms=0, **kw)


def buy(spend="4", worst="0.50") -> MarketBuyOrderSpec:
    return MarketBuyOrderSpec(order_id="b1", market_id="0xc", instrument_id="up", token_id="up",
                              spend_amount=Decimal(spend), maximum_total_debit=Decimal("5"),
                              worst_price=Decimal(worst), estimated_shares=Decimal("8"))


def sell(shares: str, minimum="0.40") -> MarketSellOrderSpec:
    return MarketSellOrderSpec(order_id="s1", market_id="0xc", instrument_id="up",
                               token_id="up", shares=Decimal(shares),
                               minimum_price=Decimal(minimum))


async def post(v: PaperVenue, spec):
    return await v.post_order(await v.prepare_order(spec))


@pytest.mark.asyncio
async def test_buy_by_spend_charges_fees_in_shares_and_debits_collateral(tmp_path: Path) -> None:
    v = venue(tmp_path)
    v.book_provider = lambda: view(asks=[lv("0.40", "5"), lv("0.45", "100"), lv("0.60", "9")])
    result = await post(v, buy())
    assert result.accepted and result.status == "matched"
    assert result.cumulative_matched_shares == Decimal("5") + Decimal("4.44")
    spent = Decimal("5") * Decimal("0.40") + Decimal("4.44") * Decimal("0.45")
    assert v.account.collateral == Decimal("20") - spent
    balance, allowance = await v.conditional_balance("up")
    assert balance < result.cumulative_matched_shares  # fee charged in shares
    trades = await v.list_account_trades(token_id="up")
    assert [t.taker_order_id for t in trades] == ["paper-order-1", "paper-order-1"]


@pytest.mark.asyncio
async def test_sell_respects_minimum_price_and_inventory(tmp_path: Path) -> None:
    v = venue(tmp_path)
    v.book_provider = lambda: view(asks=[lv("0.40", "100")])
    await post(v, buy("4", "0.40"))
    held, _ = await v.conditional_balance("up")
    v.book_provider = lambda: view(bids=[lv("0.50", "3"), lv("0.39", "100")])
    result = await post(v, sell(str(held)))
    assert result.cumulative_matched_shares == Decimal("3")  # 0.39 is below the minimum
    remaining, _ = await v.conditional_balance("up")
    assert remaining == held - Decimal("3")
    v.book_provider = lambda: view(bids=[])
    none = await post(v, sell(str(remaining)))
    assert none.accepted and none.status == "unmatched" and none.trade_ids == ()


@pytest.mark.asyncio
async def test_insufficient_collateral_is_rejected_and_maker_orders_refused(tmp_path: Path) -> None:
    v = venue(tmp_path)
    v.book_provider = lambda: view(asks=[lv("0.40", "100")])
    v.account.collateral = Decimal("2")
    rejected = await post(v, buy("4"))
    assert not rejected.accepted
    with pytest.raises(PaperVenueError):
        await v.prepare_order(LimitOrderSpec(order_id="l", market_id="0xc", instrument_id="up",
                                             token_id="up", side=__import__(
                                                 "tyrex_pm.execution.orders",
                                                 fromlist=["OrderSide"]).OrderSide.BUY,
                                             shares=Decimal("5"), limit_price=Decimal("0.4")))


@pytest.mark.asyncio
async def test_account_survives_restart_and_startup_sees_holdings(tmp_path: Path) -> None:
    v = venue(tmp_path)
    v.book_provider = lambda: view(asks=[lv("0.40", "100")])
    await post(v, buy("4", "0.40"))
    reopened = PaperAccount.open(tmp_path / "paper_account.json",
                                 initial_collateral=Decimal("999"))
    assert reopened.collateral == v.account.collateral
    assert reopened.balances == v.account.balances
    facts = PaperVenue(account=reopened, fee_curve=lambda _t: CURVE).startup_inspection()
    assert facts.coverage.complete and [h.token_id for h in facts.holdings] == ["up"]
    assert facts.holdings[0].condition_id == "0xc"


@pytest.mark.asyncio
async def test_worker_proves_payout_and_paper_redemption_returns_cash(tmp_path: Path) -> None:
    v = venue(tmp_path)
    v.book_provider = lambda: view(asks=[lv("0.40", "100")])
    await post(v, buy("4", "0.40"))
    held, _ = await v.conditional_balance("up")
    ledger = SettlementLedger(tmp_path / "ledger.jsonl")
    ledger.record("EPISODE_RESERVED", "e", window_id="w", market_id="m", condition_id="0xc",
                  token_id="up", outcome_index=0, max_debit="4", max_fees="0")
    ledger.record("FILL_CONFIRMED", "e", net_shares=str(held), cost_basis="4")
    ledger.record("HOLD_REQUESTED", "e", quantity=str(held))
    ledger.record("HOLD_COMMITTED", "e", quantity=str(held))
    finalized = {"value": False}

    async def reader(_condition, _index):
        if not finalized["value"]:
            return PayoutObservation(False, None, "test", "unresolved")
        return PayoutObservation(True, Decimal("1"), "test", "ok")

    records = []
    manager = ResolutionManager(ledger, payout_reader=reader,
                                redemption_verifier=v.verify_redemption, wallet=v.wallet)
    worker = SettlementWorker(manager=manager, interval_s=0.01,
                              record=lambda kind, payload: records.append(kind),
                              paper_redeemer=v.redeem)
    before = v.account.collateral
    await worker.once()
    assert ledger.episodes["e"].state is EpisodeState.RESOLUTION_UNCERTAIN
    finalized["value"] = True
    report = await worker.once()
    assert ledger.episodes["e"].state is EpisodeState.COMPLETED
    assert report["paper_redeemed"] == ["e"]
    assert v.account.collateral == before + held
    worker.start()
    await asyncio.sleep(0.02)
    await worker.stop()
    assert "SETTLEMENT_WORKER_STOPPED" in records
    assert ledger.episodes["e"].realized_pnl == held - Decimal("4")


def test_prepared_order_shape() -> None:
    assert PreparedOrder(local_order_id="x", digest="x", payload=None).dispatch_spec is None
