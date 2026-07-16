from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import (
    ClientOrderId,
    IntentId,
    RunId,
    TokenId,
    VenueOrderId,
)
from tyrex_pm.core.models import (
    ApprovedIntent,
    EnterIntent,
    OpenOrderView,
    TradeFillRecord,
)
from tyrex_pm.execution.order_lifecycle import (
    register_submit,
    submit_fingerprint_for_intent,
)
from tyrex_pm.ingestion.user_stream import apply_user_ws_message
from tyrex_pm.state.order_store import LocalOrder, OrderStore
from tyrex_pm.state.reconcile import reconcile_open_orders
from tyrex_pm.state.wallet_store import WalletStore


def _provisional(
    *,
    cid: str,
    vid: str | None,
    token: str,
    side: Side,
    size: Decimal,
    ack: datetime | None,
    fp: str | None = None,
) -> LocalOrder:
    return LocalOrder(
        client_order_id=ClientOrderId(cid),
        venue_order_id=VenueOrderId(vid) if vid is not None else None,
        token_id=TokenId(token),
        side=side,
        remaining=size,
        original_size=size,
        size_matched=Decimal("0"),
        confirmation="provisional",
        submit_ack_utc=ack,
        last_local_source="local",
        submit_fingerprint=fp,
    )


def _intent(
    *,
    token: str,
    side: Side,
    size: Decimal,
    price: Decimal | None,
    cid: str = "c-test",
) -> ApprovedIntent:
    return ApprovedIntent(
        intent=EnterIntent(
            token_id=TokenId(token),
            side=side,
            size=size,
            limit_price=price,
            order_style=OrderStyle.GTC,
            intent_id=IntentId("i1"),
        ),
        client_order_id=ClientOrderId(cid),
        run_id=RunId("r1"),
    )


def test_provisional_to_venue_confirmed_via_ws_placement() -> None:
    """1) provisional → venue_confirmed via WS PLACEMENT, no drift, no terminal record."""
    w = WalletStore()
    o = OrderStore()
    cid = ClientOrderId("c1")
    o.orders[cid] = _provisional(
        cid="c1",
        vid="0xorder1",
        token="123",
        side=Side.BUY,
        size=Decimal("10"),
        ack=datetime.now(timezone.utc),
    )
    apply_user_ws_message(
        w,
        {
            "type": "PLACEMENT",
            "id": "0xorder1",
            "asset_id": "123",
            "side": "BUY",
            "original_size": "10",
            "size_matched": "0",
            "price": "0.55",
            "status": "LIVE",
        },
        o,
    )
    assert o.orders[cid].confirmation == "venue_confirmed"
    res = reconcile_open_orders(w, o)
    assert res.blocking_drift_flags == ()
    assert res.provisional_repair_decisions == ()


def test_provisional_to_venue_confirmed_via_rest_repair() -> None:
    """2) provisional row + REST snapshot has the id → repair decision is ``confirmed_open_order``."""
    w = WalletStore()
    o = OrderStore()
    cid = ClientOrderId("c-rest")
    ack = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    o.orders[cid] = _provisional(
        cid="c-rest",
        vid="vid-rest",
        token="tok",
        side=Side.BUY,
        size=Decimal("4"),
        ack=ack,
    )
    w.open_orders = (
        OpenOrderView(
            token_id=TokenId("tok"),
            side=Side.BUY,
            remaining_size=Decimal("4"),
            limit_price=Decimal("0.5"),
            client_order_id=None,
            venue_order_id=VenueOrderId("vid-rest"),
            original_size=Decimal("4"),
            size_matched=Decimal("0"),
            venue_state_source="rest",
        ),
    )
    res = reconcile_open_orders(w, o, now=ack + timedelta(seconds=5))
    decisions = list(res.provisional_repair_decisions)
    assert decisions and decisions[0]["decision"] == "confirmed_open_order"
    assert decisions[0]["rest_open_order_found"] is True
    assert res.blocking_drift_flags == ()


def test_provisional_resolved_by_ws_trade_evidence() -> None:
    """3) provisional + WS trade ledger covers original size → ``filled_resolved``, drop with audit."""
    w = WalletStore()
    o = OrderStore()
    ack = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    cid = ClientOrderId("c-trade")
    o.orders[cid] = _provisional(
        cid="c-trade",
        vid="vid-trade",
        token="tok",
        side=Side.BUY,
        size=Decimal("3"),
        ack=ack,
    )
    w.trade_fill_records.append(
        TradeFillRecord(
            token_id=TokenId("tok"),
            side=Side.BUY,
            size=Decimal("3"),
            price=Decimal("0.5"),
            status="MATCHED",
            ts_utc=ack + timedelta(seconds=2),
        )
    )
    res = reconcile_open_orders(w, o, now=ack + timedelta(seconds=10))
    assert cid not in o.orders
    assert "provisional_filled_resolved" in res.drift_flags
    assert res.blocking_drift_flags == ()
    audits = [a for a in o.terminal_audit if a["decision"] == "filled_resolved"]
    assert audits and audits[0]["ws_trade_seen"] is True


def test_provisional_absent_within_grace_non_blocking() -> None:
    """4) provisional absent within submit_grace_s → non-blocking ``provisional_pending_venue``."""
    w = WalletStore()
    o = OrderStore()
    ack = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    o.orders[ClientOrderId("c1")] = _provisional(
        cid="c1",
        vid="0xabc",
        token="t1",
        side=Side.BUY,
        size=Decimal("10"),
        ack=ack,
    )
    res = reconcile_open_orders(
        w,
        o,
        venue_user_ws_stale=False,
        submit_grace_s=15.0,
        unknown_terminal_timeout_s=60.0,
        now=ack + timedelta(seconds=5),
    )
    assert res.blocking_drift_flags == ()
    assert "provisional_pending_venue" in res.drift_flags


def test_provisional_absent_past_timeout_ws_fresh_unknown_terminal() -> None:
    """5) provisional past unknown_terminal_timeout_s, ws fresh, no restart → UNKNOWN_TERMINAL drop."""
    w = WalletStore()
    o = OrderStore()
    ack = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    cid = ClientOrderId("c-ghost")
    o.orders[cid] = _provisional(
        cid="c-ghost",
        vid="0xghost",
        token="t1",
        side=Side.BUY,
        size=Decimal("10"),
        ack=ack,
        fp="abcd1234",
    )
    o.pending_repair_fingerprints.add("abcd1234")
    res = reconcile_open_orders(
        w,
        o,
        venue_user_ws_stale=False,
        submit_grace_s=15.0,
        unknown_terminal_timeout_s=60.0,
        now=ack + timedelta(seconds=120),
    )
    assert cid not in o.orders
    assert "abcd1234" not in o.pending_repair_fingerprints
    assert res.blocking_drift_flags == ()
    assert "provisional_unknown_terminal" in res.drift_flags
    audits = [a for a in o.terminal_audit if a["decision"] == "unknown_terminal"]
    assert audits and audits[0]["venue_order_id"] == "0xghost"
    assert audits[0]["user_ws_fresh"] is True
    assert audits[0]["venue_restart_suspected"] is False
    assert audits[0]["ack_age_s"] >= 60.0
    # Back-compat tuple still present for older operator dashboards.
    assert len(res.provisional_timeout_resolutions) == 1


def test_provisional_absent_past_timeout_ws_stale_blocked() -> None:
    """6) ws stale ⇒ never auto-terminalize a provisional even past timeout; stay blocking."""
    w = WalletStore()
    o = OrderStore()
    ack = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    cid = ClientOrderId("c-stale")
    o.orders[cid] = _provisional(
        cid="c-stale",
        vid="0xghost",
        token="t1",
        side=Side.BUY,
        size=Decimal("10"),
        ack=ack,
    )
    res = reconcile_open_orders(
        w,
        o,
        venue_user_ws_stale=True,
        submit_grace_s=15.0,
        unknown_terminal_timeout_s=60.0,
        now=ack + timedelta(seconds=120),
    )
    assert cid in o.orders
    assert "local_open_not_on_venue" in res.blocking_drift_flags
    decisions = list(res.provisional_repair_decisions)
    assert decisions and decisions[0]["decision"] == "blocked_unsafe_to_resolve"


def test_provisional_during_venue_restart_suspected_blocked() -> None:
    """7) HTTP 425 / matching engine restart suspected ⇒ never auto-terminalize."""
    w = WalletStore()
    o = OrderStore()
    ack = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    cid = ClientOrderId("c-425")
    o.orders[cid] = _provisional(
        cid="c-425",
        vid="0xghost",
        token="t1",
        side=Side.BUY,
        size=Decimal("10"),
        ack=ack,
    )
    res = reconcile_open_orders(
        w,
        o,
        venue_user_ws_stale=False,
        venue_restart_suspected=True,
        submit_grace_s=15.0,
        unknown_terminal_timeout_s=60.0,
        now=ack + timedelta(seconds=120),
    )
    assert cid in o.orders
    assert "local_open_not_on_venue" in res.blocking_drift_flags


def test_duplicate_submit_guard_blocks_resubmit_while_repair_pending() -> None:
    """8) registering a second equivalent submit while one is provisional must be detectable."""
    o = OrderStore()
    ap = _intent(
        token="tok-dup",
        side=Side.BUY,
        size=Decimal("5"),
        price=Decimal("0.3"),
        cid="c-first",
    )
    fp = submit_fingerprint_for_intent(ap)
    register_submit(o, ap)
    assert fp in o.pending_repair_fingerprints
    # An equivalent intent (same token, side, size, price) → same fingerprint.
    ap2 = _intent(
        token="tok-dup",
        side=Side.BUY,
        size=Decimal("5"),
        price=Decimal("0.3"),
        cid="c-second",
    )
    fp2 = submit_fingerprint_for_intent(ap2)
    assert fp2 == fp
    assert o.has_pending_submit_fingerprint(fp2) is True


def test_venue_confirmed_drift_still_uses_strict_logic() -> None:
    """9) venue_confirmed local with size mismatch ⇒ still blocking (repair path doesn't apply)."""
    w = WalletStore()
    w.open_orders = (
        OpenOrderView(
            token_id=TokenId("tok"),
            side=Side.BUY,
            remaining_size=Decimal("9"),
            limit_price=Decimal("0.5"),
            client_order_id=None,
            venue_order_id=VenueOrderId("v1"),
            original_size=Decimal("10"),
            size_matched=Decimal("1"),
            venue_state_source="user_ws",
        ),
    )
    o = OrderStore()
    o.orders[ClientOrderId("x")] = LocalOrder(
        client_order_id=ClientOrderId("x"),
        venue_order_id=VenueOrderId("v1"),
        token_id=TokenId("tok"),
        side=Side.BUY,
        remaining=Decimal("5"),
        confirmation="venue_confirmed",
        last_local_source="user_ws",
    )
    res = reconcile_open_orders(w, o)
    assert "open_order_size_mismatch" in res.blocking_drift_flags
    # No repair decision emitted for venue_confirmed rows.
    assert res.provisional_repair_decisions == ()


def test_repair_decisions_carry_required_observability_fields() -> None:
    """10) repair decision facts include the operator-required fields for live forensics."""
    w = WalletStore()
    o = OrderStore()
    ack = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    cid = ClientOrderId("c-obs")
    o.orders[cid] = _provisional(
        cid="c-obs",
        vid="0xobs",
        token="tok",
        side=Side.BUY,
        size=Decimal("2"),
        ack=ack,
        fp="fp-abc",
    )
    res = reconcile_open_orders(
        w,
        o,
        venue_user_ws_stale=False,
        submit_grace_s=15.0,
        unknown_terminal_timeout_s=60.0,
        now=ack + timedelta(seconds=5),
    )
    d = dict(res.provisional_repair_decisions[0])
    required = {
        "venue_order_id",
        "submit_fingerprint",
        "ack_status",
        "ack_age_s",
        "user_ws_fresh",
        "venue_restart_suspected",
        "ws_order_seen",
        "ws_trade_seen",
        "rest_open_order_found",
        "rest_get_order_found",
        "rest_recent_trade_found",
        "position_size_after",
        "balance_after",
        "repair_attempt",
        "decision",
        "decision_reason",
        "blocking",
        "submit_grace_s",
        "unknown_terminal_timeout_s",
    }
    missing = required - set(d.keys())
    assert not missing, f"missing repair-decision fields: {missing}"


@pytest.mark.parametrize(
    "ack_age_s,expected_decision,expected_blocking",
    [
        (5, "pending_within_grace", False),
        (30, "blocked_absent", True),
        (90, "unknown_terminal", False),
    ],
)
def test_provisional_lifecycle_thresholds(
    ack_age_s: float, expected_decision: str, expected_blocking: bool
) -> None:
    """Threshold sanity: pending → blocking → unknown_terminal across submit_grace / timeout."""
    w = WalletStore()
    o = OrderStore()
    ack = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    cid = ClientOrderId("c-th")
    o.orders[cid] = _provisional(
        cid="c-th",
        vid="0xth",
        token="t",
        side=Side.BUY,
        size=Decimal("1"),
        ack=ack,
    )
    res = reconcile_open_orders(
        w,
        o,
        submit_grace_s=15.0,
        unknown_terminal_timeout_s=60.0,
        now=ack + timedelta(seconds=ack_age_s),
    )
    d = dict(res.provisional_repair_decisions[0])
    assert d["decision"] == expected_decision
    assert d["blocking"] is expected_blocking
