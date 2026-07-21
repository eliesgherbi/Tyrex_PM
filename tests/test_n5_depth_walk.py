"""N5A: shadow_depth_walk_v1 fill model — causality, partials, determinism."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tyrex_pm.core.commands import (
    ExecutionPolicy,
    SubmitOrderCommand,
    new_client_order_id,
    new_command_id,
)
from tyrex_pm.core.ids import InstrumentId, MarketId, StrategyId, new_correlation_id
from tyrex_pm.core.intents import OrderSide, new_intent_id
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.execution.fill_ledger import FillLedger
from tyrex_pm.execution.order_store import OrderStore, OrderStatus
from tyrex_pm.execution.shadow_fill_model import FILL_MODEL_DEPTH_WALK_V1
from tyrex_pm.execution.shadow_oms import ShadowFeeConfig, ShadowFillConfig, ShadowOMS
from tyrex_pm.planning.plan import new_plan_id
from tyrex_pm.portfolio.portfolio import Portfolio

TS0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
INST = InstrumentId("tok-up")
MID = MarketId("m-n5")


def _oms(*, latency_ms: float = 100.0, cancel_residual: bool = False) -> tuple[ShadowOMS, OrderStore, Portfolio]:
    d = EventDispatcher()
    store = OrderStore()
    ledger = FillLedger()
    port = Portfolio(fill_ledger=ledger)
    port.set_market_id(MID)
    store.attach(d)
    ledger.attach(d)
    port.attach(d)
    oms = ShadowOMS(
        dispatcher=d,
        order_store=store,
        portfolio=port,
        config=ShadowFillConfig(
            cancel_unfilled_residual=cancel_residual,
            fee=ShadowFeeConfig(fee_rate=Decimal("0"), model_id="shadow_zero_fee_v1"),
            fill_model_id=FILL_MODEL_DEPTH_WALK_V1,
            latency_ms=latency_ms,
            extra_slip_ticks=Decimal("0"),
            tick_size=Decimal("0.01"),
        ),
    )
    return oms, store, port


def _book(asks: list[tuple[str, str]], *, at: datetime, bids: list[tuple[str, str]] | None = None) -> BookSnapshot:
    return BookSnapshot.from_levels(
        instrument_id=INST,
        ts_event=at,
        bids=[(Decimal(p), Decimal(q)) for p, q in (bids or [("0.40", "100")])],
        asks=[(Decimal(p), Decimal(q)) for p, q in asks],
    )


def _submit(oms: ShadowOMS, *, qty: str, limit: str, when: datetime, side: OrderSide = OrderSide.BUY):
    cmd = SubmitOrderCommand(
        command_id=new_command_id(),
        plan_id=new_plan_id(),
        intent_id=new_intent_id(),
        strategy_id=StrategyId("zgap"),
        instrument_id=INST,
        market_id=MID,
        side=side,
        quantity=Decimal(qty),
        limit_price=Decimal(limit),
        client_order_id=new_client_order_id(),
        created_at=when,
        correlation_id=new_correlation_id(),
        causation_id=None,
        execution_policy=ExecutionPolicy.NORMAL,
    )
    return oms.submit(cmd)


def test_depth_walk_no_lookahead_waits_for_arrival_book() -> None:
    oms, store, _ = _oms(latency_ms=200.0)
    t0 = TS0
    # Book before arrival must not fill while latency has not elapsed
    oms.on_book_updated(_book([("0.50", "100")], at=t0), available_at=t0)
    oid = _submit(oms, qty="10", limit="0.55", when=t0)
    rec = store.get(oid)
    assert rec is not None
    assert rec.filled_quantity == 0
    assert oms.last_match_trace is not None
    assert oms.last_match_trace.outcome == "no_fill"
    # A future book must not be used before arrival either
    future = t0 + timedelta(milliseconds=500)
    oms2, store2, _ = _oms(latency_ms=200.0)
    oms2.on_book_updated(_book([("0.50", "100")], at=t0), available_at=t0)
    oms2.on_book_updated(_book([("0.40", "100")], at=future), available_at=future)
    # Advance "now" via an intermediate book at arrival — fill uses book at/before arrival
    arrival = t0 + timedelta(milliseconds=200)
    oms.on_book_updated(_book([("0.50", "100")], at=arrival), available_at=arrival)
    rec = store.get(oid)
    assert rec is not None
    assert rec.status is OrderStatus.FILLED
    assert rec.filled_quantity == Decimal("10")
    assert oms.last_match_trace.outcome == "full"
    # Prove look-ahead: with only t0 + future books, fill at arrival uses t0 price not 0.40
    oid2 = _submit(oms2, qty="10", limit="0.55", when=t0)
    # now_proxy=future >= arrival → select latest <= arrival → t0 @ 0.50
    rec2 = store2.get(oid2)
    assert rec2 is not None and rec2.status is OrderStatus.FILLED
    assert rec2.average_fill_price == Decimal("0.50")


def test_depth_walk_full_partial_no_fill() -> None:
    # Full
    oms, store, _ = _oms(latency_ms=0.0)
    oms.on_book_updated(_book([("0.50", "100")], at=TS0), available_at=TS0)
    oid = _submit(oms, qty="10", limit="0.55", when=TS0)
    assert store.get(oid).status is OrderStatus.FILLED

    # Partial
    oms2, store2, _ = _oms(latency_ms=0.0, cancel_residual=True)
    oms2.on_book_updated(_book([("0.50", "4")], at=TS0), available_at=TS0)
    oid2 = _submit(oms2, qty="10", limit="0.55", when=TS0)
    rec2 = store2.get(oid2)
    assert rec2.filled_quantity == Decimal("4")
    assert rec2.status in {OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCELED}

    # No fill — empty executable depth at limit
    oms3, store3, _ = _oms(latency_ms=0.0)
    oms3.on_book_updated(_book([("0.90", "100")], at=TS0), available_at=TS0)
    oid3 = _submit(oms3, qty="10", limit="0.55", when=TS0)
    assert store3.get(oid3).filled_quantity == 0
    assert oms3.last_match_trace.outcome == "no_fill"


def test_depth_walk_deterministic_replay() -> None:
    def once() -> list[str]:
        oms, store, _ = _oms(latency_ms=50.0)
        t0 = TS0
        oms.on_book_updated(_book([("0.50", "3"), ("0.51", "10")], at=t0), available_at=t0)
        oid = _submit(oms, qty="10", limit="0.55", when=t0)
        # still waiting
        assert store.get(oid).filled_quantity == 0
        t1 = t0 + timedelta(milliseconds=50)
        oms.on_book_updated(_book([("0.50", "3"), ("0.51", "10")], at=t1), available_at=t1)
        rec = store.get(oid)
        return [
            str(rec.filled_quantity),
            str(rec.average_fill_price or 0),
            oms.last_match_trace.outcome,
        ]

    assert once() == once()


def test_sell_cannot_exceed_confirmed_inventory() -> None:
    oms, store, port = _oms(latency_ms=0.0)
    # Seed long inventory via buy
    oms.on_book_updated(
        _book([("0.50", "100")], at=TS0, bids=[("0.45", "100")]),
        available_at=TS0,
    )
    buy = _submit(oms, qty="5", limit="0.55", when=TS0)
    assert store.get(buy).status is OrderStatus.FILLED
    assert port.net_quantity(INST) == Decimal("5")
    # Attempt sell 10 → only confirmed 5 fills (book must be available at/after arrival)
    t1 = TS0 + timedelta(seconds=1)
    oms.on_book_updated(
        _book([("0.50", "100")], at=t1, bids=[("0.45", "100")]),
        available_at=t1,
    )
    sell = _submit(oms, qty="10", limit="0.40", when=t1, side=OrderSide.SELL)
    rec = store.get(sell)
    assert rec.filled_quantity == Decimal("5")
    assert port.net_quantity(INST) == Decimal("0")
