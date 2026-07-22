"""Builders for N7 one-shot acceptance tests (FakeTransport only)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import MarketId, OrderId, TokenId, new_correlation_id
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.intents import EnterIntent, ExitIntent, new_intent_id
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.domain.polymarket.market import (
    BinaryMarket,
    MarketStatus,
    make_binary_instruments,
)
from tyrex_pm.execution.polymarket.fake_transport import FakeTransport
from tyrex_pm.execution.polymarket.transport import VenueTradeSnapshot
from tyrex_pm.runtime.live_config import LiveConfig, LiveScope
from tyrex_pm.runtime.n7_oneshot_host import N7OneShotHost
from tyrex_pm.runtime.n7_sealed import N7SealedConfig
from tyrex_pm.runtime.n7_timing import N7_TIMING

T0 = datetime(2026, 7, 22, 14, 0, 0, tzinfo=timezone.utc)
EVENT_END = T0 + timedelta(minutes=5)


def make_market(
    *,
    market_id: str = "cond-n7-1",
    event_end: datetime | None = None,
) -> BinaryMarket:
    mid = MarketId(market_id)
    yes, no = make_binary_instruments(
        market_id=mid, yes_token=TokenId("tok-yes-n7"), no_token=TokenId("tok-no-n7")
    )
    return BinaryMarket(
        market_id=mid,
        condition_id=market_id,
        question="N7 one-shot market?",
        yes=yes,
        no=no,
        status=MarketStatus.ACTIVE,
        event_end=event_end or EVENT_END,
        tick_size=Decimal("0.01"),
    )


def make_sealed(
    *,
    max_buy: Decimal = Decimal("5.00"),
    max_daily_notional: Decimal = Decimal("5.00"),
    max_daily_loss: Decimal = Decimal("5.00"),
) -> N7SealedConfig:
    live = LiveConfig(
        enabled=False,
        mutations_enabled=False,
        scope=LiveScope.A,
        ack_timeout_ms=N7_TIMING.ack_timeout_ms,
        max_order_notional=max_buy,
        order_style="marketable_limit",
        hard_collateral_cap=max_buy,
    )
    return N7SealedConfig(
        live=live,
        max_buy_collateral=max_buy,
        max_daily_notional=max_daily_notional,
        max_daily_loss=max_daily_loss,
    )


def make_n7_host(
    *,
    arm: bool = True,
    clock: FakeClock | None = None,
    market: BinaryMarket | None = None,
    sealed: N7SealedConfig | None = None,
    persistence_path: Path | None = None,
    min_valid_order_notional: Decimal = Decimal("1"),
) -> N7OneShotHost:
    clock = clock or FakeClock(T0)
    market = market or make_market()
    sealed = sealed or make_sealed()
    host = N7OneShotHost(
        sealed=sealed,
        clock=clock,
        transport=FakeTransport(),
        market=market,
        persistence_path=persistence_path,
        min_valid_order_notional=min_valid_order_notional,
    )
    if arm:
        err = host.arm_fake()
        assert err is None, err
    return host


def yes_book(host: N7OneShotHost, *, ask: str = "0.50", bid: str = "0.48") -> BookSnapshot:
    return BookSnapshot.from_levels(
        instrument_id=host.market.yes.instrument_id,
        ts_event=host.clock.now_utc(),
        bids=[(bid, "100")],
        asks=[(ask, "100")],
    )


def make_enter(host: N7OneShotHost, *, notional: Decimal = Decimal("5")) -> EnterIntent:
    assert host.inner is not None
    return EnterIntent(
        intent_id=new_intent_id(),
        strategy_id=host.inner.strategy_id,
        instrument_id=host.market.yes.instrument_id,
        market_id=host.market.market_id,
        created_at=host.clock.now_utc(),
        correlation_id=new_correlation_id(),
        causation_id=None,
        reason_code="N7_ENTER",
        target_notional=notional,
        outcome=OutcomeSide.YES,
        decision_epoch=1,
        max_price=Decimal("0.99"),
    )


def make_exit(host: N7OneShotHost) -> ExitIntent:
    assert host.inner is not None
    return ExitIntent(
        intent_id=new_intent_id(),
        strategy_id=host.inner.strategy_id,
        instrument_id=host.market.yes.instrument_id,
        market_id=host.market.market_id,
        created_at=host.clock.now_utc(),
        correlation_id=new_correlation_id(),
        causation_id=None,
        reason_code="N7_EXIT",
    )


def fill_order(
    host: N7OneShotHost,
    order_id,
    *,
    qty,
    price,
    side: str = "BUY",
    trade_id: str | None = None,
) -> VenueTradeSnapshot:
    assert host.inner is not None
    oid = order_id if isinstance(order_id, str) else order_id.value
    track = host.inner.oms._tracking.get(oid)
    assert track is not None
    venue_order_id = track.venue_order_id
    assert venue_order_id is not None
    token = host.market.yes.instrument_id.value
    tid = trade_id or f"trade-{oid[:8]}-{side}-{qty}@{price}"
    host.transport.add_fill(
        venue_order_id=venue_order_id,
        trade_id=tid,
        token_id=token,
        side=side,
        size=str(qty),
        price=str(price),
    )
    trade = VenueTradeSnapshot(
        venue_trade_id=tid,
        venue_order_id=venue_order_id,
        instrument_token_id=token,
        side=side,
        size=Decimal(str(qty)),
        price=Decimal(str(price)),
        status="CONFIRMED",
    )
    host.inner.ingest_confirmed_trade(order_id=OrderId(oid), trade=trade)
    return trade
