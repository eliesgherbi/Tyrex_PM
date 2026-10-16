"""Amount-denominated (spend) FAK BUY: the live market-order contract in the engine."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from tyrex_pm.research.replay.config import Intent, Scenario
from tyrex_pm.research.replay.engine import Engine


class _Book:
    valid = True
    epoch_id = 1

    def __init__(self, asks):
        self.asks = {Decimal(p): Decimal(q) for p, q in asks}
        self.bids = {}


def engine(asks, **scenario):
    base = Scenario(adapter="strategy_closed_loop_v1", participation="1", initial_cash="100")
    e = Engine(replace(base, **scenario))
    e.books.books["up"] = _Book(asks)
    e.constraint["up"] = {"tick": Decimal("0.01"), "minimum_shares": Decimal("5")}
    return e


def buy(spend, limit):
    return Intent(order_id="o1", at_ns=0, instrument_id="up", action="BUY",
                  spend_amount=spend, limit_price=limit)


def test_spend_walks_levels_and_never_exceeds_the_limit_or_spend() -> None:
    e = engine([("0.40", "5"), ("0.45", "100"), ("0.60", "100")])
    e.submit(buy("4.90", "0.50"))
    fills = [x for x in e.ledger if x["event"] == "fill"]
    assert [(f["quantity"], f["price"]) for f in fills] == [
        (Decimal("5"), Decimal("0.40")),
        (Decimal("6.44"), Decimal("0.45")),
    ]
    spent = sum(f["quantity"] * f["price"] for f in fills)
    assert spent <= Decimal("4.90") and max(f["price"] for f in fills) <= Decimal("0.50")
    assert e.cash == Decimal("100") - spent


def test_unspent_amount_is_cancelled_and_reported() -> None:
    e = engine([("0.40", "2")])
    e.submit(buy("2.00", "0.40"))
    cancelled = next(x for x in e.ledger if x["event"] == "unfilled_cancelled")
    assert cancelled["unspent_amount"] == Decimal("2.00") - Decimal("0.80")
    assert not e.orders["o1"].active


def test_participation_and_adverse_ticks_apply_to_amount_orders() -> None:
    e = engine([("0.40", "100")], participation="0.1", adverse_price_ticks=1)
    e.submit(buy("10", "0.41"))
    (fill,) = [x for x in e.ledger if x["event"] == "fill"]
    assert fill["price"] == Decimal("0.41") and fill["quantity"] == Decimal("10")


def test_minimum_size_uses_the_shares_the_spend_buys_at_the_limit() -> None:
    e = engine([("0.40", "100")])
    e.submit(buy("1.00", "0.40"))  # 2.5 shares at the limit < minimum 5
    assert e.ledger[-1]["event"] == "rejected" and e.ledger[-1]["reason"] == "below_minimum"


def test_spend_validation() -> None:
    with pytest.raises(ValueError):
        Intent(order_id="o", at_ns=0, instrument_id="up", action="SELL", spend_amount="1",
               limit_price="0.5").validate()
    with pytest.raises(ValueError):
        Scenario(adapter="strategy_closed_loop_v1", order_delay_ms=100,
                 final_gate_delay_ms=200).validate()
