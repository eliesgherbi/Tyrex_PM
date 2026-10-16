from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from tyrex_pm.research.replay.config import FeePolicy, Intent, Scenario
from tyrex_pm.research.replay.engine import Engine

IID = "polymarket:137:outcome:1"


def book(at=0, ask="0.5", quantity="10", bid="0.4"):
    return "book_snapshots", {
        "instrument_id": IID,
        "record_id": f"b{at}",
        "received_at": at,
        "source_id": "clob",
        "session_id": "s",
        "connection_id": "c",
        "snapshot_origin": "websocket",
        "bids": [{"price": Decimal(bid), "quantity": Decimal("5")}],
        "asks": [{"price": Decimal(ask), "quantity": Decimal(quantity)}],
        "provider_update_id": None,
    }


def trade(at, side="SELL", quantity="20", price="0.4"):
    return "trade_prints", {
        "instrument_id": IID,
        "received_at": at,
        "quantity": Decimal(quantity),
        "price": Decimal(price),
        "reported_side": side,
    }


def engine(**changes):
    scenario = replace(
        Scenario(),
        order_delay_ms=0,
        cancel_delay_ms=0,
        participation="1",
        fee_policy=FeePolicy(rate="0"),
        **changes,
    )
    result = Engine(scenario)
    result.constraint[IID] = {"tick": Decimal("0.01"), "minimum_shares": Decimal("1")}
    return result


def intent(oid="a", at=1, action="BUY", q="2", price="0.5", **kwargs):
    return Intent(oid, at, IID, action, q, price, **kwargs)


def fills(result):
    return [row for row in result["ledger"] if row["event"] == "fill"]


def test_delayed_taker_cannot_buy_old_ask():
    runner = engine()
    runner.scenario = replace(runner.scenario, order_delay_ms=1)
    result = runner.run([book(), book(500_000, ask="0.6"), trade(2_000_000)], [intent(price="0.5")])
    assert fills(result) == []
    assert result["cash"] == 100


def test_feed_lag_delays_decisions_instead_of_freezing_the_market():
    runner = engine(feed_delay_ms=1)
    result = runner.run([book(), book(500_000, ask="0.6"), trade(2_000_000)], [intent(price="0.5")])
    assert fills(result) == []


def test_recorded_fee_change_applies_only_after_observation():
    runner = engine()
    runner.scenario = replace(runner.scenario, fee_policy=FeePolicy(mode="recorded"))
    mid = "polymarket:condition:fixture"
    events = [("instruments", {"instrument_id": IID, "market_id": mid, "received_at": 0}),
        ("metadata_versions", {"entity_type": "market", "entity_id": mid, "received_at": 0,
            "record_id": "fee-0", "fee_schedule": '{"r":"0","e":"1"}',
            "tick_size": Decimal("0.01"), "minimum_order_size_unit": "shares",
            "minimum_order_size_value": Decimal("1")}), book(quantity="100"),
        ("metadata_versions", {"entity_type": "market", "entity_id": mid, "received_at": 10,
            "record_id": "fee-1", "fee_schedule": '{"r":"0.07","e":"1"}'}), trade(100)]
    result = runner.run(events, [intent("old", at=1), intent("new", at=20)])
    assert [fill["fee"] for fill in fills(result)] == [Decimal("0"), Decimal("0.035")]


def test_partial_fill_fok_and_no_reused_liquidity():
    result = engine().run(
        [book(quantity="3"), trade(100)],
        [intent("fok", q="4", lifetime="FOK"), intent("a", at=2, q="2"), intent("b", at=3, q="2")],
    )
    assert [row["quantity"] for row in fills(result)] == [Decimal("2"), Decimal("1")]
    assert result["inventory"][IID] == 3
    assert result["cash"] == Decimal("98.5")


def test_maker_touch_does_not_fill_default():
    result = engine().run(
        [book(), trade(10)], [intent(price="0.4", lifetime="GTC", post_only=True)]
    )
    assert fills(result) == []


def test_cancel_latency_allows_fill_before_cancel():
    runner = engine(maker_model="queue_trade_assumption_v1", maker_queue_multiplier="1")
    runner.scenario = replace(runner.scenario, cancel_delay_ms=1)
    result = runner.run(
        [book(), trade(500_000), trade(2_000_000)],
        [intent(price="0.4", lifetime="GTC", post_only=True), intent(at=2, action="CANCEL")],
    )
    assert len(fills(result)) == 1
    assert fills(result)[0]["maker"]
    assert result["inventory"][IID] == 2


def test_buy_fee_in_shares_and_sell_accounting():
    runner = engine()
    runner.scenario = replace(runner.scenario, fee_policy=FeePolicy(rate="0.07"))
    runner.curve = replace(runner.curve, fee_rate=Decimal("0.07"))
    result = runner.run([book(quantity="100"), trade(100)], [intent(q="10")])
    assert result["cash"] == 95
    assert result["inventory"][IID] == Decimal("9.65")
    assert result["fees_paid"] == Decimal("0.175")
    assert fills(result)[0]["net_quantity"] == Decimal("9.65")


def test_overselling_and_cash_reservations_reject():
    result = engine(initial_cash="1").run(
        [book(), trade(100)],
        [
            intent("sell", action="SELL"),
            intent("rest", q="2", price="0.4", lifetime="GTC"),
            intent("other", at=2, q="1"),
        ],
    )
    reasons = [r["reason"] for r in result["ledger"] if r["event"] == "rejected"]
    assert reasons == ["insufficient_unreserved_inventory", "insufficient_available_cash"]


def test_payout_not_reused_during_historical_actions():
    result = engine(initial_cash="1").run(
        [book(), trade(100)],
        [intent(q="2"), intent("later", at=50, q="1")],
        labels=[
            {"instrument_id": IID, "available_at": 20, "status": "PROVIDER_TERMINAL", "payout": "1"}
        ],
    )
    assert result["cash"] == 0
    assert result["payout_receivables"][IID] == 2
    assert result["realized_pnl"] == 1
    assert len(fills(result)) == 1


def test_invalid_scenarios_and_gap_with_inventory():
    with pytest.raises(ValueError):
        replace(Scenario(), participation="2").validate()
    from tyrex_pm.data_warehouse.contracts import canonical

    gap = (
        "operational_events",
        {
            "received_at": 20,
            "details": canonical({"type": "disconnected", "source": "clob", "connection_id": "c"}),
        },
    )
    result = engine().run([book(), gap, trade(100)], [intent()])
    assert not result["qualified"]
    assert "gap_with_exposure" in result["limitations"]
    assert result["net_equity_change"] is None
