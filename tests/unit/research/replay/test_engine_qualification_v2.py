"""Qualification v2: reconstruction blips, gaps held without action, constraints."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from tyrex_pm.data_warehouse.contracts import canonical
from tyrex_pm.research.replay.config import FeePolicy, Intent, Scenario
from tyrex_pm.research.replay.engine import Engine

IID = "polymarket:137:outcome:1"
MS = 1_000_000
MID = "polymarket:condition:fixture"


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


def trade(at):
    return "trade_prints", {
        "instrument_id": IID,
        "received_at": at,
        "quantity": Decimal("20"),
        "price": Decimal("0.4"),
        "reported_side": "SELL",
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


def intent(oid="a", at=1, action="BUY", q="2", price="0.5"):
    return Intent(oid, at, IID, action, q, price)


def disconnect(at):
    return (
        "operational_events",
        {
            "received_at": at,
            "details": canonical({"type": "disconnected", "source": "clob", "connection_id": "c"}),
        },
    )


def test_a_blip_shorter_than_the_tolerance_is_not_a_gap():
    runner = engine(gap_tolerance_ms=250)
    result = runner.run([book(), disconnect(1 * MS), book(101 * MS), trade(2000 * MS)], [intent()])
    assert result["qualified"] is True
    assert result["qualification_level"] == "qualified"
    assert result["gap_blips_ignored"] == 1


def test_a_long_gap_held_without_any_order_is_a_note_not_a_disqualification():
    runner = engine(gap_tolerance_ms=250)
    result = runner.run([book(), disconnect(1 * MS), book(2001 * MS), trade(3000 * MS)], [intent()])
    assert result["qualified"] is True
    assert result["qualification_level"] == "qualified_with_notes"
    held = result["qualification_notes"]["held_through_gap"]
    assert held[0]["duration_ms"] == 2000


def test_an_order_arriving_inside_a_long_gap_disqualifies():
    runner = engine(gap_tolerance_ms=250)
    sell = intent("s", at=500 * MS, action="SELL", q="1", price="0.3")
    result = runner.run(
        [book(), disconnect(1 * MS), book(2001 * MS), trade(3000 * MS)], [intent(), sell]
    )
    assert result["qualified"] is False
    assert "gap_with_exposure" in result["limitations"]
    assert result["qualification_level"] == "unqualified"


def test_a_gap_open_at_the_end_is_measured_to_the_end():
    runner = engine(gap_tolerance_ms=250)
    result = runner.run([book(), disconnect(1 * MS), trade(3000 * MS)], [intent()])
    assert result["qualification_level"] == "qualified_with_notes"


def test_zero_tolerance_keeps_the_original_rule():
    result = engine().run([book(), disconnect(1 * MS), book(2 * MS), trade(3000 * MS)], [intent()])
    assert "gap_with_exposure" in result["limitations"]


def _metadata(tick="0.01", unit="UNKNOWN", minimum="5"):
    return {
        "entity_type": "market",
        "entity_id": MID,
        "received_at": 0,
        "tick_size": Decimal(tick),
        "minimum_order_size_value": Decimal(minimum),
        "minimum_order_size_unit": unit,
        "fee_schedule": canonical({"r": "0.07", "e": 1, "to": True}),
    }


def _fresh() -> Engine:
    runner = Engine(replace(Scenario(), fee_policy=FeePolicy(rate="0")))
    runner.observation("instruments", {"instrument_id": IID, "market_id": MID, "received_at": 0})
    return runner


def test_older_metadata_cannot_undo_a_venue_tick_change():
    runner = _fresh()
    runner.observation("metadata_versions", _metadata(tick="0.01"))
    change = {
        "change_type": "tick_size_change",
        "entity_id": IID,
        "received_at": 1,
        "new_value": canonical({"old_tick_size": "0.01", "new_tick_size": "0.001"}),
    }
    runner.observation("market_changes", change)
    runner.observation("metadata_versions", {**_metadata(tick="0.01"), "received_at": 2})
    assert runner.constraint[IID]["tick"] == Decimal("0.001")


def test_polymarket_minimum_size_with_unknown_unit_is_read_as_shares_and_logged():
    runner = _fresh()
    runner.observation("metadata_versions", _metadata(unit="UNKNOWN", minimum="5"))
    assert runner.constraint[IID]["minimum_shares"] == Decimal("5")
    logged = [row for row in runner.ledger if row["event"] == "minimum_size_unit_interpreted"]
    assert logged and logged[0]["unit"] == "shares"


def test_minimum_size_recorded_in_shares_needs_no_interpretation():
    runner = _fresh()
    runner.observation("metadata_versions", _metadata(unit="shares", minimum="5"))
    assert runner.constraint[IID]["minimum_shares"] == Decimal("5")
    assert not [row for row in runner.ledger if row["event"] == "minimum_size_unit_interpreted"]
