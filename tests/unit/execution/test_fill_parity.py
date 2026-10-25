"""V1-b: the paper venue and the replay engine fill the same order on the same book identically.

Both use execution.fill_model. Same book, same fee curve, no friction: same fills, same fees,
same shares received and the same cash spent.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tyrex_pm.core.snapshots import BookLevel
from tyrex_pm.domain.polymarket.fees import FeeCurveParams
from tyrex_pm.execution.orders import MarketBuyOrderSpec, MarketSellOrderSpec
from tyrex_pm.execution.paper import PaperAccount, PaperVenue
from tyrex_pm.research.replay.config import FeePolicy, Intent, Scenario
from tyrex_pm.research.replay.engine import Engine

CURVE = FeeCurveParams(fee_rate=Decimal("0.07"), exponent=Decimal("1"))
ASKS = [("0.40", "5"), ("0.45", "100"), ("0.60", "9")]
BIDS = [("0.38", "3"), ("0.35", "100")]
IID = "up"


def _engine_fills(intents: list[Intent]) -> tuple[list[tuple], dict]:
    scenario = replace(
        Scenario(), order_delay_ms=0, cancel_delay_ms=0, participation="1",
        fee_policy=FeePolicy(rate="0.07", buy_fee_unit="shares"),
    )
    engine = Engine(scenario)
    engine.curve = CURVE
    engine.constraint[IID] = {"tick": Decimal("0.01"), "minimum_shares": Decimal("1")}
    snapshot = ("book_snapshots", {
        "instrument_id": IID, "record_id": "b0", "received_at": 0, "source_id": "clob",
        "session_id": "s", "connection_id": "c", "snapshot_origin": "websocket",
        "bids": [{"price": Decimal(p), "quantity": Decimal(q)} for p, q in BIDS],
        "asks": [{"price": Decimal(p), "quantity": Decimal(q)} for p, q in ASKS],
        "provider_update_id": None,
    })
    trade = ("trade_prints", {"instrument_id": IID, "received_at": 100,
                              "quantity": Decimal(1), "price": Decimal("0.4"),
                              "reported_side": "SELL"})
    result = engine.run([snapshot, trade], intents)
    fills = [
        (row["side"], row["quantity"], row["price"], row["fee"])
        for row in result["ledger"] if row["event"] == "fill"
    ]
    return fills, result


def _paper(tmp_path: Path, initial: str = "20") -> PaperVenue:
    account = PaperAccount.open(tmp_path / "paper.json", initial_collateral=Decimal(initial))
    book = SimpleNamespace(
        asks=tuple(BookLevel(price=Decimal(p), quantity=Decimal(q)) for p, q in ASKS),
        bids=tuple(BookLevel(price=Decimal(p), quantity=Decimal(q)) for p, q in BIDS),
    )
    view = SimpleNamespace(up=SimpleNamespace(token_id=IID, book=book),
                           down=SimpleNamespace(token_id="down", book=None))
    return PaperVenue(account=account, fee_curve=lambda _t: CURVE, order_latency_ms=0,
                      book_provider=lambda: view)


def _paper_fills(venue: PaperVenue) -> list[tuple]:
    return [
        (t["side"], Decimal(t["size"]), Decimal(t["price"]), Decimal(t["fee"]))
        for t in venue.account.trades
    ]


async def _post(venue: PaperVenue, spec) -> None:
    await venue.post_order(await venue.prepare_order(spec))


def test_amount_buy_fills_identically(tmp_path: Path) -> None:
    engine_fills, result = _engine_fills(
        [Intent(order_id="o1", at_ns=1, instrument_id=IID, action="BUY",
                spend_amount="4", limit_price="0.50")]
    )
    venue = _paper(tmp_path)
    asyncio.run(_post(venue, MarketBuyOrderSpec(
        order_id="b1", market_id="0xc", instrument_id=IID, token_id=IID,
        spend_amount=Decimal("4"), maximum_total_debit=Decimal("5"),
        worst_price=Decimal("0.50"), estimated_shares=Decimal("8"))))
    assert engine_fills and engine_fills == _paper_fills(venue)
    # The engine computes at a higher decimal precision; holdings agree to 18 decimals.
    q = Decimal("1e-18")
    assert venue.account.balances[IID].quantize(q) == result["inventory"][IID].quantize(q)
    spent = Decimal(Scenario().initial_cash) - Decimal(result["cash"])
    assert Decimal("20") - venue.account.collateral == spent


def test_sell_fills_identically(tmp_path: Path) -> None:
    # Hold 4 shares first in both venues (bought on the same book), then sell them.
    buy = Intent(order_id="o1", at_ns=1, instrument_id=IID, action="BUY",
                 quantity="4", limit_price="0.40")
    sell = Intent(order_id="o2", at_ns=2, instrument_id=IID, action="SELL",
                  quantity="3.5", limit_price="0.30")
    engine_fills, _ = _engine_fills([buy, sell])
    venue = _paper(tmp_path)
    venue.account.balances[IID] = Decimal("3.5")
    asyncio.run(_post(venue, MarketSellOrderSpec(
        order_id="s1", market_id="0xc", instrument_id=IID, token_id=IID,
        shares=Decimal("3.5"), minimum_price=Decimal("0.30"))))
    engine_sells = [f for f in engine_fills if f[0] == "SELL"]
    assert engine_sells and engine_sells == _paper_fills(venue)


@pytest.mark.parametrize("participation", ["1", "0.5"])
def test_participation_is_one_rule(tmp_path: Path, participation: str) -> None:
    from tyrex_pm.execution.fill_model import walk_spend

    levels = {Decimal(p): Decimal(q) for p, q in ASKS}
    fills, unspent = walk_spend(levels, spend=Decimal("4"), limit=Decimal("0.50"),
                                participation=Decimal(participation))
    assert sum(f.shares * f.price for f in fills) + unspent == Decimal("4")
    assert all(f.shares <= levels[f.level_price] * Decimal(participation) for f in fills)
