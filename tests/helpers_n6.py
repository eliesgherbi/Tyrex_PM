"""Shared builders for N6 Scope A live-host acceptance tests.

Wires an :class:`N6LiveHost` against ``FakeTransport`` + ``FakeClock`` with
mutations armed via a fake-only :class:`MutationAuthorization`. Patterns follow
``tests/helpers_r5.py`` and ``tests/test_r4_risk.py``.

Never imports ``runtime.r7*``. Never touches ``.env``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import (
    InstrumentId,
    MarketId,
    OrderId,
    StrategyId,
    TokenId,
    new_correlation_id,
)
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
from tyrex_pm.risk.dedup import IntentDedupRegistry
from tyrex_pm.runtime.live_config import LiveConfig, LiveScope
from tyrex_pm.runtime.n6_authorization import MutationAuthorization
from tyrex_pm.runtime.n6_live_host import N6LiveHost
from tyrex_pm.runtime.scope_a_ladder import ScopeATimingLadder

# Deterministic epoch. Event ends at T0 + 5 minutes.
T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
EVENT_END = T0 + timedelta(minutes=5)

DEFAULT_MARKET_ID = "cond-n6-1"
DEFAULT_YES_TOKEN = "tok-yes-n6"
DEFAULT_NO_TOKEN = "tok-no-n6"


def make_market(
    *,
    market_id: str = DEFAULT_MARKET_ID,
    yes_token: str = DEFAULT_YES_TOKEN,
    no_token: str = DEFAULT_NO_TOKEN,
    event_end: datetime | None = None,
    tick: Decimal = Decimal("0.01"),
    min_order_size: Decimal | None = None,
) -> BinaryMarket:
    mid = MarketId(market_id)
    yes, no = make_binary_instruments(
        market_id=mid, yes_token=TokenId(yes_token), no_token=TokenId(no_token)
    )
    return BinaryMarket(
        market_id=mid,
        condition_id=market_id,
        question="N6 Scope A acceptance market?",
        yes=yes,
        no=no,
        status=MarketStatus.ACTIVE,
        event_end=event_end or EVENT_END,
        tick_size=tick,
        min_order_size=min_order_size,
    )


def make_ladder(*, event_end: datetime | None = None) -> ScopeATimingLadder:
    return ScopeATimingLadder(
        event_end=event_end or EVENT_END,
        last_allowed_entry_before_end=timedelta(minutes=4),
        discretionary_exit_cutoff_before_end=timedelta(minutes=3),
        mandatory_flatten_start_before_end=timedelta(minutes=2),
        residual_operator_deadline_before_end=timedelta(minutes=1),
        event_end_safety_buffer=timedelta(seconds=30),
        acknowledgment_timeout=timedelta(seconds=5),
        cancel_recon_budget=timedelta(seconds=10),
    )


def make_config(
    *,
    enabled: bool = True,
    mutations_enabled: bool = True,
    max_order_notional: Decimal = Decimal("10"),
    hard_collateral_cap: Decimal = Decimal("10"),
) -> LiveConfig:
    return LiveConfig(
        enabled=enabled,
        mutations_enabled=mutations_enabled,
        scope=LiveScope.A,
        ack_timeout_ms=5000,
        max_order_notional=max_order_notional,
        hard_collateral_cap=hard_collateral_cap,
    )


def make_clock(*, at: datetime | None = None) -> FakeClock:
    # No wall_step: now_utc() is stable until advance()/set_utc() is called.
    return FakeClock(at or T0)


_DEFAULT = object()


def make_host(
    *,
    clock: FakeClock | None = None,
    market: BinaryMarket | None = None,
    ladder: ScopeATimingLadder | None = None,
    config: LiveConfig | None = None,
    transport: object | None = None,
    authorization: object = _DEFAULT,
    persistence_path=None,
    min_valid_order_notional: Decimal = Decimal("1"),
    enabled: bool = True,
    mutations_enabled: bool = True,
    max_order_notional: Decimal = Decimal("10"),
    hard_collateral_cap: Decimal = Decimal("10"),
    strategy_id: StrategyId | None = None,
) -> N6LiveHost:
    """Build an N6LiveHost with mutations armed against a FakeTransport.

    ``authorization`` defaults to ``MutationAuthorization.for_fake_transport()``.
    Pass ``authorization=None`` to test unarmed/mutations-off behavior.
    """
    clock = clock or make_clock()
    market = market or make_market()
    ladder = ladder or make_ladder(event_end=market.event_end)
    config = config or make_config(
        enabled=enabled,
        mutations_enabled=mutations_enabled,
        max_order_notional=max_order_notional,
        hard_collateral_cap=hard_collateral_cap,
    )
    transport = transport if transport is not None else FakeTransport()
    if authorization is _DEFAULT:
        authorization = MutationAuthorization.for_fake_transport()

    kwargs = dict(
        live=config,
        clock=clock,
        transport=transport,
        market=market,
        ladder=ladder,
        authorization=authorization,
        persistence_path=persistence_path,
        min_valid_order_notional=min_valid_order_notional,
        # N6LiveHost's default dedup factory omits the required lifetime; supply one.
        dedup=IntentDedupRegistry(lifetime=timedelta(hours=1)),
    )
    if strategy_id is not None:
        kwargs["strategy_id"] = strategy_id
    return N6LiveHost(**kwargs)


def yes_book(
    host: N6LiveHost,
    *,
    ask: str = "0.50",
    bid: str = "0.48",
    depth: str = "100",
    ts: datetime | None = None,
) -> BookSnapshot:
    return BookSnapshot.from_levels(
        instrument_id=host.market.yes.instrument_id,
        ts_event=ts or host.clock.now_utc(),
        bids=[(bid, depth)],
        asks=[(ask, depth)],
    )


def make_enter_intent(
    host: N6LiveHost,
    *,
    target_notional: Decimal = Decimal("5"),
    max_price: Decimal = Decimal("0.99"),
    decision_epoch: int = 1,
) -> EnterIntent:
    return EnterIntent(
        intent_id=new_intent_id(),
        strategy_id=host.strategy_id,
        instrument_id=host.market.yes.instrument_id,
        market_id=host.market.market_id,
        created_at=host.clock.now_utc(),
        correlation_id=new_correlation_id(),
        causation_id=None,
        reason_code="N6_ENTER",
        target_notional=target_notional,
        outcome=OutcomeSide.YES,
        decision_epoch=decision_epoch,
        max_price=max_price,
    )


def make_exit_intent(host: N6LiveHost) -> ExitIntent:
    return ExitIntent(
        intent_id=new_intent_id(),
        strategy_id=host.strategy_id,
        instrument_id=host.market.yes.instrument_id,
        market_id=host.market.market_id,
        created_at=host.clock.now_utc(),
        correlation_id=new_correlation_id(),
        causation_id=None,
        reason_code="N6_EXIT",
    )


def fill_order(
    host: N6LiveHost,
    order_id,
    *,
    qty,
    price,
    side: str = "BUY",
    trade_id: str | None = None,
    token_id: str | None = None,
) -> VenueTradeSnapshot:
    """Confirm a venue fill for a submitted order.

    Finds the ``venue_order_id`` from OMS tracking, scripts the fill on the fake
    transport, then applies it as fill truth via ``ingest_confirmed_trade``.
    Fill price is the execution truth — never the submitted limit price.
    """
    oid = order_id if isinstance(order_id, str) else order_id.value
    track = host.oms._tracking.get(oid)
    assert track is not None, f"no OMS tracking for order {oid}"
    venue_order_id = track.venue_order_id
    assert venue_order_id is not None, f"order {oid} has no venue_order_id"

    token = token_id or host.market.yes.instrument_id.value
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
    host.ingest_confirmed_trade(order_id=OrderId(oid), trade=trade)
    return trade
