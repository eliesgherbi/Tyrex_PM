"""Recorded fee schedules arrive in two observed shapes (S0 item 4).

CLOB market metadata: {"e": 1, "r": "0.07", "to": true}
Gamma market metadata: {"exponent": 1, "rate": "0.07", "rebateRate": "0.2", "takerOnly": true}

In ``recorded`` fee mode a later Gamma row must not drop the curve learned from the
same values in CLOB shape.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from tyrex_pm.research.replay.config import FeePolicy, Intent, Scenario
from tyrex_pm.research.replay.engine import Engine

IID = "polymarket:137:outcome:1"
MID = "polymarket:condition:0x1"


def metadata(at: int, schedule: str) -> tuple[str, dict]:
    return "metadata_versions", {
        "entity_type": "market",
        "entity_id": MID,
        "received_at": at,
        "tick_size": Decimal("0.01"),
        "fee_schedule": schedule,
    }


def rows() -> list[tuple[str, dict]]:
    instrument = ("instruments", {"instrument_id": IID, "market_id": MID, "received_at": 0})
    book = ("book_snapshots", {
        "instrument_id": IID, "record_id": "b", "received_at": 3, "source_id": "clob",
        "session_id": "s", "connection_id": "c", "snapshot_origin": "websocket",
        "bids": [{"price": Decimal("0.40"), "quantity": Decimal("5")}],
        "asks": [{"price": Decimal("0.50"), "quantity": Decimal("10")}],
        "provider_update_id": None,
    })
    return [
        instrument,
        metadata(1, '{"e":1,"r":"0.07","to":true}'),
        metadata(2, '{"exponent":1,"rate":"0.07","rebateRate":"0.2","takerOnly":true}'),
        book,
    ]


def test_gamma_fee_shape_keeps_recorded_curve() -> None:
    scenario = replace(
        Scenario(), order_delay_ms=0, participation="1", fee_policy=FeePolicy(mode="recorded")
    )
    engine = Engine(scenario)
    result = engine.run(rows(), [Intent("a", 3, IID, "BUY", "2", "0.50")])
    events = [e["event"] for e in result["ledger"]]
    assert "fill" in events, result["ledger"]
    assert "recorded_fee_curve_unavailable" not in result["limitations"]
    fill = next(e for e in result["ledger"] if e["event"] == "fill")
    assert fill["fee"] == Decimal("0.035")  # 2 * 0.07 * 0.5 * 0.5
