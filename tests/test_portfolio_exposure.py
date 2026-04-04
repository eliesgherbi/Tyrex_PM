"""Phase B B1 — :class:`~tyrex_pm.runtime.portfolio_exposure.NautilusPortfolioExposureAggregator`."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from nautilus_trader.adapters.polymarket import POLYMARKET_VENUE
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Money

from tyrex_pm.core.types import OrderIntent
from tyrex_pm.runtime.portfolio_exposure import (
    NautilusPortfolioExposureAggregator,
    cache_best_mark_float,
)
from tyrex_pm.runtime.state_readers import (
    POLYMARKET_VENUE_ID,
    OrderSnapshot,
)


def _iid(token_tail: str) -> InstrumentId:
    return InstrumentId.from_str(f"0xcond-{token_tail}.POLYMARKET")


def _make_agg(
    *,
    portfolio: MagicMock,
    cache: MagicMock,
    orders: tuple[OrderSnapshot, ...] = (),
    static: dict[str, str] | None = None,
) -> NautilusPortfolioExposureAggregator:
    exec_r = MagicMock()
    exec_r.list_open_orders.return_value = orders
    return NautilusPortfolioExposureAggregator(
        portfolio,
        cache,
        exec_r,
        static or {},
    )


def test_pending_multiply_leaves_by_price() -> None:
    poly = MagicMock()
    cache = MagicMock()
    tid = "99999"
    iid = _iid(tid)
    snaps = (
        OrderSnapshot(
            client_order_id="a",
            venue_order_id="1",
            status="OPEN",
            side="BUY",
            quantity="10",
            leaves_quantity="3",
            price="0.4",
            instrument_id=str(iid),
        ),
        OrderSnapshot(
            client_order_id="b",
            venue_order_id="2",
            status="OPEN",
            side="BUY",
            quantity="5",
            leaves_quantity="2",
            price="0.5",
            instrument_id=str(iid),
        ),
    )
    agg = _make_agg(portfolio=poly, cache=cache, orders=snaps)
    r = agg.aggregate(intent=None, fail_on_unresolved=True)
    assert r.pending_complete
    assert r.pending_notional_usd == pytest.approx(3 * 0.4 + 2 * 0.5)


def test_pending_opposing_orders_do_not_net() -> None:
    """§4.2 gross resting — BUY and SELL rests both add positive notionals."""
    poly = MagicMock()
    cache = MagicMock()
    iid = _iid("77777")
    snaps = (
        OrderSnapshot(
            client_order_id="a",
            venue_order_id="1",
            status="OPEN",
            side="BUY",
            quantity="10",
            leaves_quantity="2",
            price="0.5",
            instrument_id=str(iid),
        ),
        OrderSnapshot(
            client_order_id="b",
            venue_order_id="2",
            status="OPEN",
            side="SELL",
            quantity="10",
            leaves_quantity="4",
            price="0.25",
            instrument_id=str(iid),
        ),
    )
    agg = _make_agg(portfolio=poly, cache=cache, orders=snaps)
    r = agg.aggregate(intent=None, fail_on_unresolved=True)
    assert r.pending_notional_usd == pytest.approx(2 * 0.5 + 4 * 0.25)


def test_pending_skips_non_polymarket_venue_on_snapshot() -> None:
    poly = MagicMock()
    cache = MagicMock()
    snaps = (
        OrderSnapshot(
            client_order_id="x",
            venue_order_id="1",
            status="OPEN",
            side="BUY",
            quantity="100",
            leaves_quantity="100",
            price="99",
            instrument_id="BTCUSDT.BINANCE",
        ),
    )
    agg = _make_agg(portfolio=poly, cache=cache, orders=snaps)
    r = agg.aggregate(intent=None, fail_on_unresolved=True)
    assert r.pending_notional_usd == 0.0
    assert r.complete


def test_filled_net_sums_signed_net_exposure() -> None:
    poly = MagicMock()
    cache = MagicMock()
    i1 = _iid("111")
    i2 = _iid("222")

    inst1 = MagicMock()
    inst1.id = i1
    inst2 = MagicMock()
    inst2.id = i2
    cache.instruments.return_value = (inst1, inst2)

    def is_flat(iid: InstrumentId) -> bool:
        return False

    poly.is_flat.side_effect = is_flat

    cinst1 = MagicMock()
    cinst1.make_price.return_value = MagicMock()
    cinst2 = MagicMock()
    cinst2.make_price.return_value = MagicMock()

    def cinstrument(iid: InstrumentId) -> MagicMock | None:
        if iid == i1:
            return cinst1
        if iid == i2:
            return cinst2
        return None

    cache.instrument.side_effect = cinstrument

    def nexp(iid: InstrumentId, price: object) -> Money:
        if iid == i1:
            return Money(100, USDC)
        if iid == i2:
            return Money(-30, USDC)
        raise AssertionError

    poly.net_exposure.side_effect = nexp

    orders: tuple[OrderSnapshot, ...] = ()
    agg = _make_agg(portfolio=poly, cache=cache, orders=orders)
    r = agg.aggregate(intent=None, fail_on_unresolved=True)
    assert r.filled_complete
    assert r.filled_net_exposure_usd == pytest.approx(70.0)


def test_e_portfolio_is_pending_plus_abs_filled() -> None:
    poly = MagicMock()
    cache = MagicMock()
    i1 = _iid("333")
    inst1 = MagicMock()
    inst1.id = i1
    cache.instruments.return_value = (inst1,)
    poly.is_flat.return_value = False
    cinst = MagicMock()
    cinst.make_price.return_value = MagicMock()
    cache.instrument.return_value = cinst
    poly.net_exposure.return_value = Money(-40, USDC)

    iid = _iid("333")
    snaps = (
        OrderSnapshot(
            client_order_id="a",
            venue_order_id="1",
            status="OPEN",
            side="BUY",
            quantity="5",
            leaves_quantity="2",
            price="0.5",
            instrument_id=str(iid),
        ),
    )
    agg = _make_agg(portfolio=poly, cache=cache, orders=snaps)
    r = agg.aggregate(intent=None, fail_on_unresolved=True)
    assert r.complete
    assert r.pending_notional_usd == pytest.approx(1.0)
    assert r.filled_net_exposure_usd == pytest.approx(-40.0)
    assert r.e_portfolio == pytest.approx(1.0 + abs(-40.0))


def test_unresolved_mark_fail_closed_strict() -> None:
    poly = MagicMock()
    cache = MagicMock()
    i1 = _iid("444")
    inst1 = MagicMock()
    inst1.id = i1
    cache.instruments.return_value = (inst1,)
    poly.is_flat.return_value = False
    cache.instrument.return_value = inst1
    cache.price.return_value = None
    cache.mark_price.return_value = None
    inst1.make_price.return_value = MagicMock(name="px")
    poly.net_exposure.return_value = Money(1, USDC)

    agg = _make_agg(portfolio=poly, cache=cache)
    r = agg.aggregate(intent=None, fail_on_unresolved=True)
    assert not r.complete
    assert r.e_portfolio is None
    assert r.error and "unresolved mark" in r.error


def test_intent_price_ref_used_for_intent_instrument_not_cache() -> None:
    poly = MagicMock()
    cache = MagicMock()
    tid = "55555"
    iid = _iid(tid)
    inst1 = MagicMock()
    inst1.id = iid
    cache.instruments.return_value = (inst1,)
    poly.is_flat.return_value = False

    # Cache has no quote; intent supplies mark.
    cache.price.return_value = None
    cache.mark_price.return_value = None

    static = {tid: str(iid)}

    cinst = MagicMock()

    def mp(dec: Decimal) -> MagicMock:
        assert dec == Decimal("0.99")
        return MagicMock()

    cinst.make_price.side_effect = mp
    cache.instrument.return_value = cinst
    poly.net_exposure.return_value = Money(12, USDC)

    intent = OrderIntent(
        correlation_id="g",
        token_id=tid,
        side="BUY",
        quantity=1.0,
        signal_kind="entry",
        reason_code="ok",
        price_ref=0.99,
    )
    agg = _make_agg(portfolio=poly, cache=cache, static=static)
    r = agg.aggregate(intent=intent, fail_on_unresolved=True)
    assert r.filled_complete
    poly.net_exposure.assert_called_once()


def test_fail_open_omits_unresolved_non_flat_without_silent_default_complete() -> None:
    """Explicit opt-in: unresolved marks skipped; aggregate may still 'complete' with underestimate."""
    poly = MagicMock()
    cache = MagicMock()
    i_res = _iid("601")
    i_unres = _iid("602")
    a = MagicMock()
    a.id = i_res
    b = MagicMock()
    b.id = i_unres
    cache.instruments.return_value = (a, b)
    poly.is_flat.return_value = False

    def cinstrument(iid: InstrumentId) -> MagicMock:
        m = MagicMock()
        m.make_price.return_value = MagicMock()
        return m

    cache.instrument.side_effect = cinstrument

    def price(iid_arg: InstrumentId, pt: object) -> object | None:
        if iid_arg == i_res:
            p = MagicMock()
            p.as_double.return_value = 0.5
            pt_bucket = getattr(pt, "name", str(pt))
            if "LAST" in pt_bucket or pt == 4:
                return p
        return None

    cache.price.side_effect = price
    cache.mark_price.return_value = None

    def nexp(iid: InstrumentId, price: object = None, **_: object) -> Money:
        if iid == i_res:
            return Money(10, USDC)
        raise AssertionError

    poly.net_exposure.side_effect = nexp

    agg = _make_agg(portfolio=poly, cache=cache)
    r = agg.aggregate(intent=None, fail_on_unresolved=False)
    assert r.complete
    assert r.filled_net_exposure_usd == pytest.approx(10.0)
    assert str(i_unres) in r.omitted_instruments_unresolved_mark


def test_pending_invalid_price_is_not_silent() -> None:
    poly = MagicMock()
    cache = MagicMock()
    iid = _iid("888")
    snaps = (
        OrderSnapshot(
            client_order_id="a",
            venue_order_id="1",
            status="OPEN",
            side="BUY",
            quantity="1",
            leaves_quantity="1",
            price="not-a-float",
            instrument_id=str(iid),
        ),
    )
    agg = _make_agg(portfolio=poly, cache=cache, orders=snaps)
    r = agg.aggregate(intent=None, fail_on_unresolved=True)
    assert not r.pending_complete
    assert not r.complete


def test_list_open_orders_uses_polymarket_venue() -> None:
    poly = MagicMock()
    cache = MagicMock()
    exec_r = MagicMock()
    exec_r.list_open_orders.return_value = ()
    agg = NautilusPortfolioExposureAggregator(poly, cache, exec_r, {})
    agg.aggregate()
    exec_r.list_open_orders.assert_called_with(venue=POLYMARKET_VENUE_ID)


def test_cache_best_mark_float_reads_price_types() -> None:
    cache = MagicMock()
    iid = _iid("700")
    cache.price.return_value = None
    last_px = MagicMock()
    last_px.as_double.return_value = 0.42

    def pr(_iid: InstrumentId, pt: object) -> object | None:
        name = getattr(pt, "name", "")
        if name == "LAST":
            return last_px
        return None

    cache.price.side_effect = pr
    assert cache_best_mark_float(cache, iid) == pytest.approx(0.42)


def test_polymarket_venue_constant_matches_reader() -> None:
    assert POLYMARKET_VENUE_ID == POLYMARKET_VENUE


def test_flat_instruments_contribute_zero_to_filled_leg() -> None:
    poly = MagicMock()
    cache = MagicMock()
    i_flat = _iid("501")
    inst = MagicMock()
    inst.id = i_flat
    cache.instruments.return_value = (inst,)
    poly.is_flat.return_value = True
    agg = _make_agg(portfolio=poly, cache=cache)
    r = agg.aggregate(intent=None, fail_on_unresolved=True)
    assert r.filled_net_exposure_usd == 0.0
    poly.net_exposure.assert_not_called()
