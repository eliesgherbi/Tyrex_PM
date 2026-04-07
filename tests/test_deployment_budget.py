"""Unit tests for :mod:`tyrex_pm.runtime.deployment_budget`."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from tyrex_pm.core.types import OrderIntent
from tyrex_pm.runtime.deployment_budget import (
    NautilusDeploymentBudget,
    position_entry_deployment_usd,
)
from tyrex_pm.runtime.state_readers import OrderSnapshot, POLYMARKET_VENUE_ID


def _iid(token_tail: str) -> InstrumentId:
    return InstrumentId.from_str(f"0xcond-{token_tail}.POLYMARKET")


def test_pending_sums_leaves_times_price() -> None:
    poly = MagicMock()
    cache = MagicMock()
    exec_r = MagicMock()
    tid = "11111"
    iid = _iid(tid)
    exec_r.list_open_orders.return_value = (
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
    )
    db = NautilusDeploymentBudget(poly, cache, exec_r, {})
    p, ok, err = db.pending_usd_for_token(tid)
    assert ok and err is None
    assert p == pytest.approx(1.2)
    exec_r.list_open_orders.assert_called_with(venue=POLYMARKET_VENUE_ID)


def test_filled_uses_abs_qty_times_avg_open() -> None:
    poly = MagicMock()
    cache = MagicMock()
    exec_r = MagicMock()
    exec_r.list_open_orders.return_value = ()
    pos = MagicMock()
    pos.instrument_id = _iid("222")
    sq = MagicMock()
    sq.as_double.return_value = -5.0
    pos.signed_qty = sq
    apx = MagicMock()
    apx.as_double.return_value = 0.2
    pos.avg_px_open = apx
    poly.is_flat.return_value = False
    cache.positions_open.return_value = (pos,)
    db = NautilusDeploymentBudget(poly, cache, exec_r, {})
    f, ok = db.filled_polymarket_usd()
    assert ok
    assert f == pytest.approx(1.0)


def test_position_entry_deployment_usd_none_on_bad_qty() -> None:
    pos = MagicMock()
    pos.signed_qty = None
    pos.avg_px_open = MagicMock()
    assert position_entry_deployment_usd(pos) is None


def test_token_cap_instrument_unknown_filled_zero() -> None:
    """No cache instrument for token → no matching positions → filled 0."""
    poly = MagicMock()
    cache = MagicMock()
    cache.positions_open.return_value = ()
    exec_r = MagicMock()
    exec_r.list_open_orders.return_value = ()
    db = NautilusDeploymentBudget(poly, cache, exec_r, {})
    tot, ok, err = db.token_deployment_usd("unknown_token")
    assert ok and err is None
    assert tot == 0.0


def test_order_deploy_matches_intent() -> None:
    poly = MagicMock()
    cache = MagicMock()
    exec_r = MagicMock()
    db = NautilusDeploymentBudget(poly, cache, exec_r, {})
    intent = OrderIntent(
        correlation_id="c",
        token_id="1",
        side="BUY",
        quantity=2.0,
        signal_kind="entry",
        reason_code="ok",
        price_ref=0.25,
    )
    assert db.order_deploy_usd(intent.price_ref, intent.quantity) == pytest.approx(0.5)
