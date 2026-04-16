"""Unit tests for :mod:`tyrex_pm.runtime.venue_state`."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from nautilus_trader.cache.cache import Cache

from tyrex_pm.runtime.state_readers import NautilusExecutionStateReader
from tyrex_pm.runtime.venue_state import VenueState, VenueStateConfig


def _minimal_cache() -> Cache:
    return Cache(database=None)


def test_venue_state_cash_ready_after_balance() -> None:
    c = _minimal_cache()
    vs = VenueState(config=VenueStateConfig(), cache=c, fact_emit=None)
    assert vs.venue_state_cash_ready is False
    vs.apply_clob_balance({"balance": "1000000"}, datetime.now(tz=UTC))
    assert vs.venue_state_cash_ready is True
    assert vs.cash_free() == Decimal("1")


def test_execution_reader_flag_off_uses_cache() -> None:
    c = _minimal_cache()
    r = NautilusExecutionStateReader(c, venue_state=None)
    assert r.list_open_orders(venue=None) == ()
