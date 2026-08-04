"""BS-5..BS-9: BookView strategy path, WS sync, rollover, reasons, revalidation."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tyrex_pm.core.book_events import (
    BookDeltaReceived,
    BookLevelDelta,
    BookSide,
    BookSnapshotReceived,
)
from tyrex_pm.core.events import EventSource
from tyrex_pm.core.ids import InstrumentId, MarketId, TokenId, new_correlation_id, new_event_id
from tyrex_pm.core.intents import IntentId, OrderSide
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.market_data.binding_record import (
    BindingLifecycleRole,
    MarketBindingRecord,
    make_binding_id,
)
from tyrex_pm.market_data.book_feed import BindingFeed, BookFeedSupervisor, COLD_GAP_MAX_S
from tyrex_pm.market_data.book_health import SideLiquidity, SyncHealth
from tyrex_pm.market_data.book_store import MarketStateStore
from tyrex_pm.planning.book_revalidation import revalidate_plan_against_book_view
from tyrex_pm.planning.plan import ExecutionPlan, new_plan_id
from tyrex_pm.strategies.z_gap.reasons import ZGapReason

TS = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
UP = "tok-up"
DOWN = "tok-down"


def _binding(role=BindingLifecycleRole.ACTIVE, role_epoch=0) -> MarketBindingRecord:
    return MarketBindingRecord(
        binding_id=make_binding_id(window_slug="btc-updown-5m-1", condition_id="0xabc"),
        window_slug="btc-updown-5m-1",
        condition_id="0xabc",
        market_id="0xabc",
        up_token_id=UP,
        down_token_id=DOWN,
        event_start=TS,
        event_end=TS,
        role=role,
        role_epoch=role_epoch,
        outcome_semantics="UP_DOWN",
        resolved_at=TS,
    )


def _seed_ready(store: MarketStateStore, disp: EventDispatcher, token: str, ask: str, bid: str) -> None:
    disp.publish(
        BookSnapshotReceived(
            event_id=new_event_id(),
            correlation_id=new_correlation_id(),
            ts_event=TS,
            ts_received=TS,
            source=EventSource.POLYMARKET_CLOB,
            book=BookSnapshot(
                instrument_id=InstrumentId(token),
                ts_event=TS,
                bids=(BookLevel(price=Decimal(bid), quantity=Decimal("20")),),
                asks=(BookLevel(price=Decimal(ask), quantity=Decimal("20")),),
            ),
            connection_epoch=1,
        )
    )


def test_book_view_reaches_assemble_and_blocks_syncing() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
    b = _binding()
    store.apply_rest_snapshot(
        BookSnapshot(
            instrument_id=InstrumentId(UP),
            ts_event=TS,
            bids=(BookLevel(price=Decimal("0.4"), quantity=Decimal("5")),),
            asks=(BookLevel(price=Decimal("0.6"), quantity=Decimal("5")),),
        ),
        ts_received=TS,
        mark_ready=False,
        binding_id=b.binding_id,
    )
    store.apply_rest_snapshot(
        BookSnapshot(
            instrument_id=InstrumentId(DOWN),
            ts_event=TS,
            bids=(BookLevel(price=Decimal("0.3"), quantity=Decimal("5")),),
            asks=(BookLevel(price=Decimal("0.7"), quantity=Decimal("5")),),
        ),
        ts_received=TS,
        mark_ready=False,
        binding_id=b.binding_id,
    )
    view = store.capture_pair(b)
    assert view.up.sync_health is SyncHealth.SYNCING
    leg = view.leg_book_view(up=True)
    assert leg.ready is False
    assert leg.reason_code == ZGapReason.BOOK_SYNCING.value


def test_ready_book_view_exposes_executable_ask() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
    b = _binding()
    _seed_ready(store, disp, UP, "0.55", "0.45")
    _seed_ready(store, disp, DOWN, "0.62", "0.38")
    view = store.capture_pair(b, connection_epoch=1)
    assert view.up.ready_for_entry_ask
    assert view.up.quote.best_ask == Decimal("0.55")
    buy = view.executable_buy(up=True, requested_shares=Decimal("10"))
    assert buy.shortfall == 0
    assert buy.vwap == Decimal("0.55")
    assert buy.available_depth == Decimal("20")


def test_ws_delta_size_zero_and_metrics_reconcile() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
    _seed_ready(store, disp, UP, "0.6", "0.4")
    disp.publish(
        BookDeltaReceived(
            event_id=new_event_id(),
            correlation_id=new_correlation_id(),
            ts_event=TS,
            ts_received=TS,
            source=EventSource.POLYMARKET_CLOB,
            connection_epoch=1,
            changes=(
                BookLevelDelta(
                    instrument_id=InstrumentId(UP),
                    side=BookSide.ASK,
                    price=Decimal("0.6"),
                    size=Decimal("0"),
                ),
                BookLevelDelta(
                    instrument_id=InstrumentId(UP),
                    side=BookSide.ASK,
                    price=Decimal("0.61"),
                    size=Decimal("3"),
                ),
            ),
        )
    )
    st = store.get(InstrumentId(UP))
    assert st.book is not None
    assert st.book.best_ask is not None
    assert st.book.best_ask.price == Decimal("0.61")
    m = store.metrics.for_token(UP)
    assert m.reconcile_ok()
    assert m.levels_deleted >= 1
    assert m.levels_inserted >= 1


@pytest.mark.asyncio
async def test_rollover_promote_preserves_warm_books_and_cold_gap_budget(tmp_path) -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
    payloads = {
        UP: {
            "asset_id": UP,
            "timestamp": "1750000000000",
            "bids": [{"price": "0.4", "size": "2"}],
            "asks": [{"price": "0.6", "size": "2"}],
        },
        DOWN: {
            "asset_id": DOWN,
            "timestamp": "1750000000000",
            "bids": [{"price": "0.3", "size": "2"}],
            "asks": [{"price": "0.7", "size": "2"}],
        },
    }

    def fetcher(token_id: str):
        from tyrex_pm.adapters.polymarket.rest_book import normalize_rest_book_payload

        return normalize_rest_book_payload(payloads[token_id], token_id=token_id)

    supervisor = BookFeedSupervisor(
        store=store, dispatcher=disp, out_dir=tmp_path, rest_fetcher=fetcher
    )
    prepared = _binding(role=BindingLifecycleRole.PREPARED_NEXT)
    feed = BindingFeed(
        binding=prepared, store=store, dispatcher=disp, rest_fetcher=fetcher
    )
    for tok in (UP, DOWN):
        store.apply_rest_snapshot(
            fetcher(tok).book,
            ts_received=TS,
            mark_ready=True,
            binding_id=prepared.binding_id,
            source="REST_RECONCILED",
        )
    supervisor.prepared = feed
    v_up = store.get(InstrumentId(UP)).book_version
    binding_id = prepared.binding_id
    active = await supervisor.promote_prepared()
    assert active.binding.binding_id == binding_id
    assert active.binding.role is BindingLifecycleRole.ACTIVE
    assert store.get(InstrumentId(UP)).book_version == v_up
    assert COLD_GAP_MAX_S == 2.0
    # Correct identity promoted even if later marked syncing (R8).
    store.get  # noqa: B018 — identity already asserted
    st = store.get(InstrumentId(UP))
    assert st.book is not None


def test_explicit_empty_ask_reason_distinct_from_unavailable() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
    b = _binding()
    disp.publish(
        BookSnapshotReceived(
            event_id=new_event_id(),
            correlation_id=new_correlation_id(),
            ts_event=TS,
            ts_received=TS,
            source=EventSource.POLYMARKET_CLOB,
            book=BookSnapshot(
                instrument_id=InstrumentId(UP),
                ts_event=TS,
                bids=(BookLevel(price=Decimal("0.4"), quantity=Decimal("1")),),
                asks=(),
            ),
        )
    )
    _seed_ready(store, disp, DOWN, "0.7", "0.3")
    view = store.capture_pair(b)
    assert view.up.ask_liquidity is SideLiquidity.EXPLICITLY_EMPTY
    leg = view.leg_book_view(up=True)
    assert leg.reason_code == ZGapReason.VENUE_ASK_EXPLICITLY_EMPTY.value
    uninit = store.capture_pair(
        MarketBindingRecord(
            binding_id="other|0x",
            window_slug="other",
            condition_id="0x",
            market_id="0x",
            up_token_id="zzz",
            down_token_id="yyy",
            event_start=TS,
            event_end=TS,
            role=BindingLifecycleRole.ACTIVE,
            role_epoch=0,
            outcome_semantics="UP_DOWN",
            resolved_at=TS,
        )
    )
    assert uninit.up.sync_health is SyncHealth.UNINITIALIZED
    assert uninit.leg_book_view(up=True).reason_code == ZGapReason.BOOK_UNAVAILABLE.value


def test_revalidation_version_bump_soft_and_hard_invalidators() -> None:
    store = MarketStateStore()
    disp = EventDispatcher()
    store.attach(disp)
    b = _binding()
    _seed_ready(store, disp, UP, "0.50", "0.40")
    _seed_ready(store, disp, DOWN, "0.60", "0.35")
    view1 = store.capture_pair(b)
    plan = ExecutionPlan(
        plan_id=new_plan_id(),
        intent_id=IntentId("i1"),
        instrument_id=InstrumentId(UP),
        token_id=TokenId(UP),
        market_id=MarketId("0xabc"),
        side=OrderSide.BUY,
        quantity=Decimal("5"),
        limit_price=Decimal("0.50"),
        expected_notional=Decimal("2.5"),
        book_ts_event=TS,
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        planned_at=TS,
        correlation_id=new_correlation_id(),
        causation_id=None,
        evidence={
            "binding_id": b.binding_id,
            "book_version": view1.up.book_version,
            "pair_version": list(view1.pair_version),
            "side": "BUY",
            "edge": "0.05",
        },
    )
    # Unchanged book → ok
    ok = revalidate_plan_against_book_view(plan, view1, active_binding_id=b.binding_id)
    assert ok.ok is True
    assert ok.audited_version_change is False

    # Distant insert bumps version but same executable ask → audit + still ok
    disp.publish(
        BookDeltaReceived(
            event_id=new_event_id(),
            correlation_id=new_correlation_id(),
            ts_event=TS,
            ts_received=TS,
            source=EventSource.POLYMARKET_CLOB,
            changes=(
                BookLevelDelta(
                    instrument_id=InstrumentId(UP),
                    side=BookSide.ASK,
                    price=Decimal("0.90"),
                    size=Decimal("100"),
                ),
            ),
        )
    )
    view2 = store.capture_pair(b)
    soft = revalidate_plan_against_book_view(plan, view2, active_binding_id=b.binding_id)
    assert soft.ok is True
    assert soft.audited_version_change is True

    # Price move outside tolerance → block
    disp.publish(
        BookSnapshotReceived(
            event_id=new_event_id(),
            correlation_id=new_correlation_id(),
            ts_event=TS,
            ts_received=TS,
            source=EventSource.POLYMARKET_CLOB,
            book=BookSnapshot(
                instrument_id=InstrumentId(UP),
                ts_event=TS,
                bids=(BookLevel(price=Decimal("0.40"), quantity=Decimal("20")),),
                asks=(BookLevel(price=Decimal("0.80"), quantity=Decimal("20")),),
            ),
        )
    )
    view3 = store.capture_pair(b)
    hard = revalidate_plan_against_book_view(plan, view3, active_binding_id=b.binding_id)
    assert hard.ok is False
    assert hard.reason == "price_outside_tolerance"

    # Binding rollover → block
    rolled = revalidate_plan_against_book_view(
        plan, view3, active_binding_id="other-binding"
    )
    assert rolled.ok is False
    assert rolled.reason == "active_binding_changed"
