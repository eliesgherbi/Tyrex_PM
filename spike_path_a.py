"""
Spike: Validate that pre-seeding a synthetic order with the ORIGINAL strategy's
strategy_id and sending the fill through ExecEngine.process correctly closes
the existing position in netting mode.

This is throwaway code — not production quality.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from nautilus_trader.accounting.factory import AccountFactory
from nautilus_trader.adapters.polymarket.common.parsing import parse_polymarket_instrument
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.engine import ExecutionEngine
from nautilus_trader.model.enums import (
    AccountType,
    ContingencyType,
    LiquiditySide,
    OmsType,
    OrderSide,
    OrderType,
    PositionSide,
    TimeInForce,
    TriggerType,
)
from nautilus_trader.model.events import AccountState, OrderAccepted, OrderFilled, OrderInitialized
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    PositionId,
    StrategyId,
    TradeId,
    TraderId,
    Venue,
    VenueOrderId,
)
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import AccountBalance, Currency, Money, Price, Quantity
from nautilus_trader.model.orders.unpacker import OrderUnpacker
from nautilus_trader.model.position import Position
from nautilus_trader.portfolio.portfolio import Portfolio

POLYMARKET_VENUE = Venue("POLYMARKET")
USDC = Currency.from_str("USDC")
TRADER_ID = TraderId("SPIKE-001")
STRATEGY_ID = StrategyId("CopyBotSellValidate-000")  # the "real" strategy
ACCOUNT_ID = AccountId("POLYMARKET-001")


def make_instrument(condition_id: str, token_id: str) -> BinaryOption:
    market_info: dict[str, Any] = {
        "condition_id": condition_id,
        "question_id": f"q_{condition_id}",
        "question": "Test market?",
        "tokens": [{"token_id": token_id, "outcome": "Yes"}],
        "active": True,
        "closed": False,
        "market_slug": f"test-{condition_id}",
        "end_date_iso": "2030-01-01",
        "description": "test",
        "minimum_tick_size": "0.01",
        "minimum_order_size": "1",
        "maker_base_fee": "0",
        "taker_base_fee": "0",
    }
    return parse_polymarket_instrument(market_info, token_id, "Yes", ts_init=time.time_ns())


def add_account(cache: Cache) -> None:
    acct_state = AccountState(
        ACCOUNT_ID,
        AccountType.CASH,
        USDC,
        True,
        [AccountBalance(Money(1000, USDC), Money(0, USDC), Money(1000, USDC))],
        [],
        {},
        UUID4(),
        time.time_ns(),
        time.time_ns(),
    )
    acct = AccountFactory.create(acct_state)
    cache.add_account(acct)


def open_position(
    cache: Cache,
    instrument: BinaryOption,
    qty: float,
    strategy_id: StrategyId,
) -> Position:
    """Create a position using the netting position_id convention."""
    ts = time.time_ns()
    position_id = PositionId(f"{instrument.id}-{strategy_id}")
    fill = OrderFilled(
        TRADER_ID,
        strategy_id,
        instrument.id,
        ClientOrderId(UUID4().value),
        VenueOrderId(UUID4().value),
        ACCOUNT_ID,
        TradeId(UUID4().value),
        position_id,
        OrderSide.BUY,
        OrderType.MARKET,
        Quantity(qty, instrument.size_precision),
        Price(0.50, instrument.price_precision),
        instrument.quote_currency,
        Money(0, instrument.quote_currency),
        LiquiditySide.TAKER,
        UUID4(),
        ts,
        ts,
    )
    pos = Position(instrument=instrument, fill=fill)
    cache.add_position(pos, OmsType.NETTING)
    return pos


def create_synthetic_close_order(
    instrument: BinaryOption,
    strategy_id: StrategyId,
    qty: float,
) -> Any:
    """Create a synthetic SELL order with the original strategy's ID."""
    ts = time.time_ns()
    initialized = OrderInitialized(
        trader_id=TRADER_ID,
        strategy_id=strategy_id,
        instrument_id=instrument.id,
        client_order_id=ClientOrderId(UUID4().value),
        order_side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=Quantity(qty, instrument.size_precision),
        time_in_force=TimeInForce.GTC,
        post_only=False,
        reduce_only=True,
        quote_quantity=False,
        options={},
        emulation_trigger=TriggerType.NO_TRIGGER,
        trigger_instrument_id=None,
        contingency_type=ContingencyType.NO_CONTINGENCY,
        order_list_id=None,
        linked_order_ids=None,
        parent_order_id=None,
        exec_algorithm_id=None,
        exec_algorithm_params=None,
        exec_spawn_id=None,
        tags=["RECONCILIATION"],
        event_id=UUID4(),
        ts_init=ts,
        reconciliation=True,
    )
    return OrderUnpacker.from_init(initialized)


def run_spike() -> None:
    clock = LiveClock()
    cache = Cache(database=None)
    msgbus = MessageBus(trader_id=TRADER_ID, clock=clock)
    portfolio = Portfolio(msgbus=msgbus, clock=clock, cache=cache)

    engine = ExecutionEngine(
        msgbus=msgbus,
        cache=cache,
        clock=clock,
    )

    instrument = make_instrument("0xABC123", "0xTOKEN456")
    cache.add_currency(USDC)
    cache.add_instrument(instrument)
    add_account(cache)

    # --- Open a position owned by the real strategy ---
    pos = open_position(cache, instrument, qty=29.66, strategy_id=STRATEGY_ID)
    print(f"[1] Position opened: id={pos.id}, qty={pos.signed_decimal_qty()}, "
          f"strategy={pos.strategy_id}, side={pos.side}")

    positions_open = cache.positions_open(instrument_id=instrument.id)
    print(f"[2] Open positions in cache: {len(positions_open)}")
    for p in positions_open:
        print(f"    id={p.id}, qty={p.signed_decimal_qty()}, strategy={p.strategy_id}")

    # --- Create synthetic SELL order with SAME strategy_id ---
    order = create_synthetic_close_order(
        instrument=instrument,
        strategy_id=STRATEGY_ID,
        qty=29.66,
    )
    print(f"\n[3] Synthetic order created: client_order_id={order.client_order_id}, "
          f"strategy={order.strategy_id}, side={order.side}")

    cache.add_order(order)

    # Apply OrderAccepted to transition from INITIALIZED
    ts_now = clock.timestamp_ns()
    venue_order_id = VenueOrderId(UUID4().value)
    accepted = OrderAccepted(
        trader_id=TRADER_ID,
        strategy_id=STRATEGY_ID,
        instrument_id=instrument.id,
        client_order_id=order.client_order_id,
        venue_order_id=venue_order_id,
        account_id=ACCOUNT_ID,
        event_id=UUID4(),
        ts_event=ts_now,
        ts_init=ts_now,
        reconciliation=True,
    )
    order.apply(accepted)
    cache.update_order(order)
    print(f"[4] Order accepted: status={order.status}")

    # --- Create OrderFilled event ---
    netting_position_id = PositionId(f"{instrument.id}-{STRATEGY_ID}")
    fill = OrderFilled(
        TRADER_ID,
        STRATEGY_ID,
        instrument.id,
        order.client_order_id,
        venue_order_id,
        ACCOUNT_ID,
        TradeId(UUID4().value),
        netting_position_id,
        OrderSide.SELL,
        OrderType.MARKET,
        Quantity(29.66, instrument.size_precision),
        Price(0.50, instrument.price_precision),
        instrument.quote_currency,
        Money(0, instrument.quote_currency),
        LiquiditySide.TAKER,
        UUID4(),
        ts_now,
        ts_now,
        reconciliation=True,
    )
    print(f"[5] OrderFilled event created: position_id={fill.position_id}")

    # --- Send through ExecEngine.process ---
    print(f"\n[6] Sending fill via ExecEngine.process...")
    try:
        msgbus.send("ExecEngine.process", fill)
        print(f"    SUCCESS: fill processed without error")
    except Exception as e:
        print(f"    ERROR: {type(e).__name__}: {e}")

    # --- Check results ---
    positions_open_after = cache.positions_open(instrument_id=instrument.id)
    positions_closed = cache.positions_closed(instrument_id=instrument.id)

    print(f"\n[7] RESULTS:")
    print(f"    Open positions: {len(positions_open_after)}")
    for p in positions_open_after:
        print(f"      id={p.id}, qty={p.signed_decimal_qty()}, strategy={p.strategy_id}")
    print(f"    Closed positions: {len(positions_closed)}")
    for p in positions_closed:
        print(f"      id={p.id}, qty={p.signed_decimal_qty()}, strategy={p.strategy_id}, "
              f"realized_pnl={p.realized_pnl}")

    # --- Verdict ---
    if len(positions_open_after) == 0 and len(positions_closed) == 1:
        print(f"\n>>> SPIKE PASSED: Position was correctly closed by synthetic fill <<<")
        print(f"    Position closed: qty={positions_closed[0].signed_decimal_qty()}")
        return True
    else:
        print(f"\n>>> SPIKE FAILED: Position was NOT correctly closed <<<")
        if positions_open_after:
            print(f"    Still open: {[str(p.id) for p in positions_open_after]}")
        return False


def run_control_test_external_strategy() -> None:
    """Control test: verify that EXTERNAL strategy creates a NEW position (the bug)."""
    clock = LiveClock()
    cache = Cache(database=None)
    msgbus = MessageBus(trader_id=TRADER_ID, clock=clock)
    portfolio = Portfolio(msgbus=msgbus, clock=clock, cache=cache)

    engine = ExecutionEngine(
        msgbus=msgbus,
        cache=cache,
        clock=clock,
    )

    instrument = make_instrument("0xCONTROL", "0xTOKENCTRL")
    cache.add_currency(USDC)
    cache.add_instrument(instrument)
    add_account(cache)

    pos = open_position(cache, instrument, qty=10.0, strategy_id=STRATEGY_ID)
    print(f"\n[CTRL-1] Position opened: id={pos.id}, qty={pos.signed_decimal_qty()}")

    # Create order with EXTERNAL strategy (the current broken approach)
    order = create_synthetic_close_order(
        instrument=instrument,
        strategy_id=StrategyId("EXTERNAL"),
        qty=10.0,
    )
    cache.add_order(order)

    ts_now = clock.timestamp_ns()
    venue_order_id = VenueOrderId(UUID4().value)
    accepted = OrderAccepted(
        trader_id=TRADER_ID,
        strategy_id=StrategyId("EXTERNAL"),
        instrument_id=instrument.id,
        client_order_id=order.client_order_id,
        venue_order_id=venue_order_id,
        account_id=ACCOUNT_ID,
        event_id=UUID4(),
        ts_event=ts_now,
        ts_init=ts_now,
        reconciliation=True,
    )
    order.apply(accepted)
    cache.update_order(order)

    external_position_id = PositionId(f"{instrument.id}-EXTERNAL")
    fill = OrderFilled(
        TRADER_ID,
        StrategyId("EXTERNAL"),
        instrument.id,
        order.client_order_id,
        venue_order_id,
        ACCOUNT_ID,
        TradeId(UUID4().value),
        external_position_id,
        OrderSide.SELL,
        OrderType.MARKET,
        Quantity(10.0, instrument.size_precision),
        Price(0.50, instrument.price_precision),
        instrument.quote_currency,
        Money(0, instrument.quote_currency),
        LiquiditySide.TAKER,
        UUID4(),
        ts_now,
        ts_now,
        reconciliation=True,
    )

    try:
        msgbus.send("ExecEngine.process", fill)
    except Exception as e:
        print(f"    ERROR: {type(e).__name__}: {e}")

    positions_open_after = cache.positions_open(instrument_id=instrument.id)
    positions_closed = cache.positions_closed(instrument_id=instrument.id)

    print(f"[CTRL-2] Open positions after EXTERNAL fill: {len(positions_open_after)}")
    for p in positions_open_after:
        print(f"    id={p.id}, qty={p.signed_decimal_qty()}, strategy={p.strategy_id}")
    print(f"[CTRL-3] Closed positions: {len(positions_closed)}")

    if len(positions_open_after) == 2:
        print(f"\n>>> CONTROL CONFIRMED: EXTERNAL strategy creates a NEW position (the bug) <<<")
    elif len(positions_open_after) == 1 and len(positions_closed) == 0:
        print(f"\n>>> UNEXPECTED: Only 1 open position but it's the wrong one <<<")
    else:
        print(f"\n>>> UNEXPECTED result <<<")


if __name__ == "__main__":
    print("=" * 70)
    print("SPIKE: Path A — Synthetic order with correct strategy_id")
    print("=" * 70)

    print("\n--- Control test: EXTERNAL strategy (demonstrates the bug) ---")
    run_control_test_external_strategy()

    print("\n" + "=" * 70)
    print("\n--- Main test: Same strategy_id (the fix) ---")
    result = run_spike()

    print("\n" + "=" * 70)
    print(f"FINAL VERDICT: {'PASS' if result else 'FAIL'}")
    print("=" * 70)
