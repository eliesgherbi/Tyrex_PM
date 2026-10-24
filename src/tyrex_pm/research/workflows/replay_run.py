"""Real closed-loop replay used by workflow jobs. Not a mock of the engine."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.research.replay.config import FeePolicy, Intent, Scenario
from tyrex_pm.research.replay.engine import DECISION, Engine

MS = 1_000_000
IID = "polymarket:137:outcome:1"


def _book(at: int, ask: str) -> tuple[str, dict]:
    return "book_snapshots", {
        "instrument_id": IID,
        "record_id": f"b{at}",
        "received_at": at,
        "source_id": "clob",
        "session_id": "s",
        "connection_id": "c",
        "snapshot_origin": "websocket",
        "bids": [{"price": Decimal("0.40"), "quantity": Decimal("5")}],
        "asks": [{"price": Decimal(ask), "quantity": Decimal("10")}],
        "provider_update_id": None,
    }


class _Agent:
    strategy_datasets = ("book_snapshots",)

    def __init__(self, order_at: int, price: str) -> None:
        self.order_at = order_at
        self.price = price
        self.engine = None

    def start(self, engine, horizon) -> None:
        self.engine = engine
        engine.schedule(self.order_at, DECISION, "decision", self.order_at)

    def handle(self, kind, payload) -> None:
        if kind == "decision" and payload == self.order_at:
            self.engine.schedule_order(
                Intent("workflow-order", 0, IID, "BUY", "2", self.price),
                decided_at_ns=self.engine.now,
            )


def run_closed_loop_fixture(unit: dict) -> dict:
    """One deterministic closed-loop fill through ``Engine``.

    The book path matches the established arrival-time case: a decision at 10 ms
    arrives 300 ms later, after the ask has moved from 0.50 to 0.55.
    """
    scenario = Scenario(
        adapter="strategy_closed_loop_v1",
        name=str(unit.get("scenario_name") or "workflow_fixture"),
        order_delay_ms=300,
        cancel_delay_ms=0,
        participation="1",
        fee_policy=FeePolicy(rate="0"),
        feedback_delay_ms=0,
    )
    scenario.validate()
    engine = Engine(scenario)
    engine.constraint[IID] = {"tick": Decimal("0.01"), "minimum_shares": Decimal("1")}
    agent = _Agent(10 * MS, "0.60")
    result = engine.run_closed_loop(
        [_book(0, "0.50"), _book(200 * MS, "0.55"), _book(400 * MS, "0.70")],
        agent,
    )
    fills = [event for event in result["ledger"] if event["event"] == "fill"]
    if len(fills) != 1:
        raise RuntimeError(f"workflow replay fixture expected one fill, saw {len(fills)}")
    fill = fills[0]
    return {
        "unit_id": unit["id"],
        "engine": "replay.engine.Engine",
        "adapter": scenario.adapter,
        "scenario_name": scenario.name,
        "fill_price": str(fill["price"]),
        "fill_at_ns": int(fill["at_ns"]),
        "fill_quantity": str(fill["quantity"]),
        "market_id": unit.get("market_id"),
        "realized_pnl": None,
    }
