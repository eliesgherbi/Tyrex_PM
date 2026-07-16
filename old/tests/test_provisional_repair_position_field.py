"""Regression: ``_resolve_provisional_repair`` reads the correct attribute on ``WalletPosition``.

The dataclass exposes ``qty`` (see ``core/models.py``); the repair path used to read
``.size`` which silently never crashed because ``wallet.positions`` was usually empty
in LIVE. Once the REST positions safety net (Fix 2) reliably populates the map, every
provisional row whose token had a held position blew up with::

    AttributeError: 'WalletPosition' object has no attribute 'size'

These tests pin the contract: ``reconcile_open_orders`` must complete cleanly when
``wallet.positions`` is populated, and the emitted ``provisional_repair_decisions``
record must carry ``position_size`` equal to ``str(qty)`` for the row's token.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import ClientOrderId, TokenId, VenueOrderId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.core.time import utc_now
from tyrex_pm.state.order_store import LocalOrder, OrderStore
from tyrex_pm.state.reconcile import reconcile_open_orders
from tyrex_pm.state.wallet_store import WalletStore


def _make_provisional_local(
    *,
    token: str = "tok-A",
    cid: str = "cid-1",
    vid: str | None = "0xVID1",
    ack_age_s: float = 1.0,
) -> LocalOrder:
    now = utc_now()
    ack = now - timedelta(seconds=ack_age_s)
    return LocalOrder(
        client_order_id=ClientOrderId(cid),
        venue_order_id=VenueOrderId(vid) if vid is not None else None,
        token_id=TokenId(token),
        side=Side.BUY,
        remaining=Decimal("10"),
        original_size=Decimal("10"),
        size_matched=Decimal("0"),
        confirmation="provisional",
        submit_ack_utc=ack,
        last_local_source="local",
        submit_fingerprint="fp-1",
        ack_status="live",
        limit_price=Decimal("0.5"),
        register_utc=ack,
    )


def test_repair_does_not_crash_when_position_exists_for_token() -> None:
    """The exact LIVE failure mode: provisional row + held position → AttributeError before fix."""
    wallet = WalletStore()
    wallet.positions[TokenId("tok-A")] = WalletPosition(
        token_id=TokenId("tok-A"),
        qty=Decimal("42.5"),
        avg_price_usd=Decimal("0.55"),
    )
    store = OrderStore()
    lo = _make_provisional_local(token="tok-A", ack_age_s=1.0)  # within submit_grace_s
    store.orders[lo.client_order_id] = lo

    res = reconcile_open_orders(
        wallet, store, submit_grace_s=15.0, unknown_terminal_timeout_s=60.0
    )

    assert len(res.provisional_repair_decisions) == 1
    rec = res.provisional_repair_decisions[0]
    assert rec["client_order_id"] == "cid-1"
    assert rec["decision"] == "pending_within_grace"
    assert rec["position_size_after"] == "42.5"


def test_repair_emits_null_position_size_when_position_absent() -> None:
    """The historic happy-path that hid the typo: empty positions ⇒ ``position_size = None``."""
    wallet = WalletStore()
    store = OrderStore()
    lo = _make_provisional_local(token="tok-B", ack_age_s=1.0)
    store.orders[lo.client_order_id] = lo

    res = reconcile_open_orders(
        wallet, store, submit_grace_s=15.0, unknown_terminal_timeout_s=60.0
    )

    assert len(res.provisional_repair_decisions) == 1
    assert res.provisional_repair_decisions[0]["position_size_after"] is None


def test_repair_position_size_uses_decimal_qty_for_negative_holdings() -> None:
    """Negative qty (legitimate short via merge) must serialize as the qty string, not crash."""
    wallet = WalletStore()
    wallet.positions[TokenId("tok-C")] = WalletPosition(
        token_id=TokenId("tok-C"),
        qty=Decimal("-7"),
        avg_price_usd=None,
    )
    store = OrderStore()
    lo = _make_provisional_local(token="tok-C", ack_age_s=1.0)
    store.orders[lo.client_order_id] = lo

    res = reconcile_open_orders(
        wallet, store, submit_grace_s=15.0, unknown_terminal_timeout_s=60.0
    )

    assert len(res.provisional_repair_decisions) == 1
    assert res.provisional_repair_decisions[0]["position_size_after"] == "-7"
