"""Tests for the in-flight BUY reservation gap fix.

Race we are guarding against (live evidence in ``var/reporting/runs/live_test_inverse_race``):

    1. Bot submits BUY for $4 → venue accepts → locks $4 of collateral.
    2. ``WalletStore.usdc_balance`` and ``WalletStore.open_orders`` have not yet been
       updated by user-WS / REST poll (typically 100–500 ms later).
    3. Next guru signal arrives. Risk reads stale wallet view → approves another $4 BUY.
    4. Venue rejects with HTTP 400 ``not enough balance / allowance``.

The fix derives in-flight reservations from ``OrderStore`` provisional rows and feeds
them into both the deployment cap check and the USDC capital check, dedup'd against the
wallet view by ``venue_order_id``.
"""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, TokenId, VenueOrderId
from tyrex_pm.core.models import EnterIntent, OpenOrderView, RiskContext, WalletPosition
from tyrex_pm.core.time import utc_now
from tyrex_pm.execution.order_lifecycle import (
    ack_submit,
    register_submit,
    release_after_ack,
    apply_venue_open_order_to_local_orders,
)
from tyrex_pm.risk.capital import evaluate_capital_buy
from tyrex_pm.risk.deployment import RiskConfigCaps, evaluate_deployment_caps
from tyrex_pm.risk.in_flight import derive_in_flight_buy_reservations
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore


def _wallet(*, balance: Decimal | None = None, allowance: Decimal | None = None) -> WalletStore:
    w = WalletStore()
    if balance is not None:
        w.usdc_balance = balance
    if allowance is not None:
        w.usdc_allowance = allowance
    return w


def _approved(token: str, *, size: Decimal, price: Decimal, cid: str = "cid-1") -> object:
    """Minimal ApprovedIntent stand-in compatible with order_lifecycle.register_submit."""
    from tyrex_pm.core.models import ApprovedIntent

    intent = EnterIntent(
        token_id=TokenId(token),
        side=Side.BUY,
        size=size,
        limit_price=price,
        order_style=OrderStyle.GTC,
    )
    return ApprovedIntent(
        intent=intent,
        client_order_id=ClientOrderId(cid),
        run_id="run-x",
    )


def _ctx(
    wallet: WalletStore,
    *,
    in_flight: tuple[OpenOrderView, ...] = (),
    open_orders: tuple[OpenOrderView, ...] | None = None,
    marks: dict[TokenId, Decimal] | None = None,
) -> RiskContext:
    return RiskContext(
        execution_mode=ExecutionMode.LIVE,
        wallet_positions=tuple(wallet.positions.values()),
        open_orders=open_orders if open_orders is not None else wallet.open_orders,
        usdc_balance=wallet.usdc_balance,
        usdc_allowance=wallet.usdc_allowance,
        last_wallet_sync_ts=None,
        mark_prices=dict(marks or {}),
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
        in_flight_buy_reservations=in_flight,
    )


# ---------------------------------------------------------------------------
# 1. First BUY submit creates an in-flight reservation visible to the derivation.
# ---------------------------------------------------------------------------
def test_register_submit_creates_visible_in_flight_reservation() -> None:
    store = OrderStore()
    wallet = _wallet(balance=Decimal("10"), allowance=Decimal("10"))
    ap = _approved("tok-A", size=Decimal("10"), price=Decimal("0.4"), cid="cid-1")
    register_submit(store, ap)

    summary = derive_in_flight_buy_reservations(store, wallet)
    assert len(summary.reservations) == 1
    assert summary.total_usd == Decimal("4.0")
    assert summary.by_token_usd == {TokenId("tok-A"): Decimal("4.0")}
    assert summary.examples[0]["client_order_id"] == "cid-1"
    assert summary.examples[0]["has_venue_order_id"] is False


# ---------------------------------------------------------------------------
# 2. Second BUY denied by deployment cap because the first reservation is still active.
# ---------------------------------------------------------------------------
def test_second_buy_denied_when_first_reservation_pushes_over_cap() -> None:
    store = OrderStore()
    wallet = _wallet(balance=Decimal("100"), allowance=Decimal("100"))
    # First BUY takes $4, registered but not yet in wallet.open_orders.
    register_submit(
        store,
        _approved("tok-A", size=Decimal("10"), price=Decimal("0.4"), cid="cid-1"),
    )
    summary = derive_in_flight_buy_reservations(store, wallet)
    ctx = _ctx(wallet, in_flight=summary.reservations, marks={TokenId("tok-B"): Decimal("0.5")})

    # Attempt a second $4 BUY on a different token. portfolio_cap = $5 → first $4
    # reservation + $4 synthetic = $8 ⇒ deny.
    pending = EnterIntent(
        token_id=TokenId("tok-B"),
        side=Side.BUY,
        size=Decimal("8"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    caps = RiskConfigCaps(token_cap_usd=Decimal("100"), portfolio_cap_usd=Decimal("5"))
    ok, reason, evidence = evaluate_deployment_caps(caps, ctx, pending_intent=pending)
    assert not ok
    assert reason == rc.PORTFOLIO_DEPLOYMENT_CAP
    assert evidence["in_flight_reserved_usd_total"] == "4.000000"
    assert evidence["in_flight_reservation_count"] == 1
    assert evidence["in_flight_reserved_usd_by_token"] == {"tok-A": "4.000000"}


# ---------------------------------------------------------------------------
# 3. Venue reject path releases the reservation (release_after_ack drops the row).
# ---------------------------------------------------------------------------
def test_venue_reject_releases_reservation() -> None:
    store = OrderStore()
    wallet = _wallet(balance=Decimal("10"), allowance=Decimal("10"))
    ap = _approved("tok-A", size=Decimal("10"), price=Decimal("0.4"), cid="cid-1")
    register_submit(store, ap)
    assert len(derive_in_flight_buy_reservations(store, wallet).reservations) == 1

    release_after_ack(store, ap.client_order_id)

    summary = derive_in_flight_buy_reservations(store, wallet)
    assert summary.reservations == ()
    assert summary.total_usd == Decimal("0")


# ---------------------------------------------------------------------------
# 4. Venue truth absorbs the order without double-counting (dedup-by-vid).
# ---------------------------------------------------------------------------
def test_venue_open_orders_absorbs_reservation_no_double_count() -> None:
    store = OrderStore()
    wallet = _wallet(balance=Decimal("100"), allowance=Decimal("100"))
    ap = _approved("tok-A", size=Decimal("10"), price=Decimal("0.4"), cid="cid-1")
    register_submit(store, ap)
    # ack_submit links the venue order id onto the local provisional row.
    vid = VenueOrderId("0xVID-A")
    ack_submit(store, ap, vid, shadow_instant_fill=False, ack_status="live")

    # Stage 1: the wallet view has not seen the new order yet → reservation visible.
    s1 = derive_in_flight_buy_reservations(store, wallet)
    assert len(s1.reservations) == 1
    assert s1.total_usd == Decimal("4.0")

    # Stage 2: WS event mirrors the order into wallet.open_orders.
    view = OpenOrderView(
        token_id=TokenId("tok-A"),
        side=Side.BUY,
        remaining_size=Decimal("10"),
        limit_price=Decimal("0.4"),
        client_order_id=None,
        venue_order_id=vid,
        original_size=Decimal("10"),
        size_matched=Decimal("0"),
        venue_state_source="user_ws",
    )
    wallet.user_ws_upsert_order(view)
    apply_venue_open_order_to_local_orders(store, view)

    # Stage 3: dedup-by-vid drops the reservation; wallet now carries the row.
    s2 = derive_in_flight_buy_reservations(store, wallet)
    assert s2.reservations == (), (
        "reservation must be released once vid is in wallet.open_orders to avoid double-count"
    )

    # Sanity: deployment cap accounting has the order via wallet.open_orders, not the reservation leg.
    ctx = _ctx(wallet, in_flight=s2.reservations)
    caps = RiskConfigCaps(token_cap_usd=Decimal("100"), portfolio_cap_usd=Decimal("100"))
    ok, _, ev = evaluate_deployment_caps(caps, ctx)
    assert ok
    assert ev["per_token_deployed_usd"] == {"tok-A": "4.000000"}
    assert ev["in_flight_reserved_usd_total"] == "0.000000"


# ---------------------------------------------------------------------------
# 5. Capital gate: USDC balance net of in-flight reservation denies the second BUY.
#    This is the exact "balance: 6030366, sum of matched orders: 3996000" race.
# ---------------------------------------------------------------------------
def test_capital_gate_nets_in_flight_against_balance() -> None:
    store = OrderStore()
    # Wallet shows $6.03 free (REST not yet caught up); venue has already locked $4.
    wallet = _wallet(balance=Decimal("6.03"), allowance=Decimal("6.03"))
    register_submit(
        store,
        _approved("tok-A", size=Decimal("10"), price=Decimal("0.4"), cid="cid-1"),
    )
    summary = derive_in_flight_buy_reservations(store, wallet)
    ctx = _ctx(wallet, in_flight=summary.reservations)

    # Try a second $4 BUY. Wallet says $6.03 free, but reservation eats $4 → only $2.03 left.
    pending = EnterIntent(
        token_id=TokenId("tok-B"),
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.4"),
        order_style=OrderStyle.GTC,
    )
    res = evaluate_capital_buy(pending, ctx, enabled=True)
    assert not res.ok
    assert res.reason == rc.INSUFFICIENT_CAPITAL
    assert res.evidence["wallet_usdc_balance"] == "6.030000"
    assert res.evidence["in_flight_reserved_usd_total"] == "4.000000"
    assert res.evidence["effective_free_balance_usd"] == "2.030000"
    assert res.evidence["intent_need_usd"] == "4.000000"
    assert res.evidence["capital_deny_kind"] == "balance"


def test_capital_gate_passes_when_effective_balance_sufficient() -> None:
    store = OrderStore()
    wallet = _wallet(balance=Decimal("100"), allowance=Decimal("100"))
    register_submit(
        store,
        _approved("tok-A", size=Decimal("10"), price=Decimal("0.4"), cid="cid-1"),
    )
    summary = derive_in_flight_buy_reservations(store, wallet)
    ctx = _ctx(wallet, in_flight=summary.reservations)

    pending = EnterIntent(
        token_id=TokenId("tok-B"),
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.4"),
        order_style=OrderStyle.GTC,
    )
    res = evaluate_capital_buy(pending, ctx, enabled=True)
    assert res.ok
    assert res.evidence["effective_free_balance_usd"] == "96.000000"
    assert res.evidence["capital_gate_enabled"] is True
    assert "capital_deny_kind" not in res.evidence


# ---------------------------------------------------------------------------
# 6. Deployment evidence carries reservation breakdown unconditionally (approve + deny).
# ---------------------------------------------------------------------------
def test_deployment_evidence_always_includes_in_flight_breakdown() -> None:
    store = OrderStore()
    wallet = _wallet(balance=Decimal("100"), allowance=Decimal("100"))
    register_submit(
        store, _approved("tok-A", size=Decimal("5"), price=Decimal("0.4"), cid="cid-A")
    )
    register_submit(
        store, _approved("tok-B", size=Decimal("3"), price=Decimal("0.5"), cid="cid-B")
    )
    summary = derive_in_flight_buy_reservations(store, wallet)
    ctx = _ctx(
        wallet,
        in_flight=summary.reservations,
        marks={TokenId("tok-C"): Decimal("0.5")},
    )

    pending = EnterIntent(
        token_id=TokenId("tok-C"),
        side=Side.BUY,
        size=Decimal("4"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    caps = RiskConfigCaps(token_cap_usd=Decimal("100"), portfolio_cap_usd=Decimal("1000"))
    ok, _reason, evidence = evaluate_deployment_caps(caps, ctx, pending_intent=pending)
    assert ok
    assert evidence["in_flight_reserved_usd_total"] == "3.500000"  # 5*0.4 + 3*0.5 = 2 + 1.5
    assert evidence["in_flight_reservation_count"] == 2
    assert evidence["in_flight_reserved_usd_by_token"] == {
        "tok-A": "2.000000",
        "tok-B": "1.500000",
    }
    # Per-token deployed must include the in-flight legs (so cap accounting is correct).
    assert evidence["per_token_deployed_usd"]["tok-A"] == "2.000000"
    assert evidence["per_token_deployed_usd"]["tok-B"] == "1.500000"
    assert evidence["per_token_deployed_usd"]["tok-C"] == "2.000000"
    assert evidence["portfolio_deployed_usd"] == "5.500000"


# ---------------------------------------------------------------------------
# 7. SELL intents do not produce reservations (BUY-only collateral lock semantics).
# ---------------------------------------------------------------------------
def test_sell_local_orders_do_not_become_reservations() -> None:
    from tyrex_pm.state.order_store import LocalOrder

    store = OrderStore()
    wallet = _wallet(balance=Decimal("10"), allowance=Decimal("10"))
    store.orders[ClientOrderId("cid-sell")] = LocalOrder(
        client_order_id=ClientOrderId("cid-sell"),
        venue_order_id=None,
        token_id=TokenId("tok-A"),
        side=Side.SELL,
        remaining=Decimal("10"),
        original_size=Decimal("10"),
        size_matched=Decimal("0"),
        confirmation="provisional",
        submit_ack_utc=None,
        last_local_source="local",
        submit_fingerprint="fp-x",
        limit_price=Decimal("0.4"),
        register_utc=utc_now(),
    )
    summary = derive_in_flight_buy_reservations(store, wallet)
    assert summary.reservations == ()


# ---------------------------------------------------------------------------
# 8. No-limit-price (market) provisional rows are skipped from reservation pricing.
# ---------------------------------------------------------------------------
def test_market_buy_without_limit_price_is_skipped() -> None:
    from tyrex_pm.state.order_store import LocalOrder

    store = OrderStore()
    wallet = _wallet(balance=Decimal("10"), allowance=Decimal("10"))
    store.orders[ClientOrderId("cid-mkt")] = LocalOrder(
        client_order_id=ClientOrderId("cid-mkt"),
        venue_order_id=None,
        token_id=TokenId("tok-A"),
        side=Side.BUY,
        remaining=Decimal("10"),
        original_size=Decimal("10"),
        size_matched=Decimal("0"),
        confirmation="provisional",
        submit_ack_utc=None,
        last_local_source="local",
        submit_fingerprint="fp-mkt",
        limit_price=None,
        register_utc=utc_now(),
    )
    summary = derive_in_flight_buy_reservations(store, wallet)
    assert summary.reservations == ()
    assert summary.total_usd == Decimal("0")
