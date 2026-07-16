"""Venue-truth mirror race tests: REST sees an order id we don't yet track locally.

Covers the adoption state machine in :func:`tyrex_pm.state.reconcile.reconcile_open_orders`:

  1) strong match (token+side+size+price within tolerance, ack age within ``adoption_grace_s``)
     → venue id is **adopted** onto the no-vid provisional row, no blocking flag.
  2) weak candidate (token+side present but size or price outside tolerance)
     → **deferred** non-blocking within ``adoption_grace_s``; reconcile does NOT block.
  3) no local candidate at all → keeps historical fail-closed
     ``venue_open_not_tracked_locally`` (blocking).
  4) candidate exists but ``ack_age_s > adoption_grace_s`` → falls back to blocking
     ``venue_open_not_tracked_locally``.
  5) stuck-provisional behavior unchanged (a ``no-vid`` row past ``submit_grace_s`` still
     surfaces ``local_open_not_on_venue``).
  6) facts/observability: ``venue_adoption_decisions`` carries candidate id, match basis,
     decision, and ``blocking`` flag.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import ClientOrderId, TokenId, VenueOrderId
from tyrex_pm.core.models import OpenOrderView
from tyrex_pm.state.order_store import LocalOrder, OrderStore
from tyrex_pm.state.reconcile import reconcile_open_orders
from tyrex_pm.state.wallet_store import WalletStore


def _no_vid_provisional(
    *,
    cid: str,
    token: str,
    side: Side,
    size: Decimal,
    price: Decimal | None,
    register_age_s: float = 1.0,
    now: datetime,
) -> LocalOrder:
    return LocalOrder(
        client_order_id=ClientOrderId(cid),
        venue_order_id=None,
        token_id=TokenId(token),
        side=side,
        remaining=size,
        original_size=size,
        size_matched=Decimal("0"),
        confirmation="provisional",
        submit_ack_utc=None,
        last_local_source="local",
        submit_fingerprint=f"fp-{cid}",
        ack_status=None,
        limit_price=price,
        register_utc=now - timedelta(seconds=register_age_s),
    )


def _venue_view(
    *,
    vid: str,
    token: str,
    side: Side,
    size: Decimal,
    price: Decimal | None,
    source: str = "rest",
) -> OpenOrderView:
    return OpenOrderView(
        token_id=TokenId(token),
        side=side,
        remaining_size=size,
        limit_price=price if price is not None else Decimal("0"),
        client_order_id=None,
        venue_order_id=VenueOrderId(vid),
        original_size=size,
        size_matched=Decimal("0"),
        venue_state_source=source,
        order_status="LIVE",
    )


def test_venue_id_adopted_onto_no_vid_provisional_when_attrs_match_within_grace() -> None:
    """1) Single no-vid provisional with matching token/side/size/price + fresh age → ADOPT."""
    now = datetime(2026, 4, 17, 21, 0, 0, tzinfo=timezone.utc)
    w = WalletStore()
    o = OrderStore()
    cid = "cid-fresh-1"
    o.orders[ClientOrderId(cid)] = _no_vid_provisional(
        cid=cid,
        token="tok-A",
        side=Side.BUY,
        size=Decimal("6.349"),
        price=Decimal("0.63"),
        register_age_s=1.5,
        now=now,
    )
    o.pending_repair_fingerprints.add(f"fp-{cid}")
    w.user_ws_upsert_order(
        _venue_view(
            vid="0xV1",
            token="tok-A",
            side=Side.BUY,
            size=Decimal("6.34"),
            price=Decimal("0.63"),
            source="rest",
        )
    )

    res = reconcile_open_orders(w, o, adoption_grace_s=5.0, now=now)

    # No blocking drift: row was adopted before the unmatched-venue loop fired.
    assert "venue_open_not_tracked_locally" not in res.blocking_drift_flags
    assert "venue_adopted_into_local_provisional" in res.drift_flags
    # Local row now carries the venue id.
    assert str(o.orders[ClientOrderId(cid)].venue_order_id) == "0xV1"
    # Decision is recorded with strong-match basis.
    decisions = list(res.venue_adoption_decisions)
    assert len(decisions) == 1
    d = decisions[0]
    assert d["decision"] == "adopted_no_vid_provisional"
    assert d["candidate_client_order_id"] == cid
    assert d["match_basis"]["size_match_within_tol"] is True
    assert d["match_basis"]["price_match_within_tol"] is True
    assert d["blocking"] is False


def test_venue_id_deferred_when_candidate_size_or_price_outside_tolerance() -> None:
    """2) Same token/side and within age, but size or price mismatches → DEFER (non-blocking)."""
    now = datetime(2026, 4, 17, 21, 0, 0, tzinfo=timezone.utc)
    w = WalletStore()
    o = OrderStore()
    cid = "cid-defer"
    o.orders[ClientOrderId(cid)] = _no_vid_provisional(
        cid=cid,
        token="tok-A",
        side=Side.BUY,
        size=Decimal("10"),
        price=Decimal("0.50"),
        register_age_s=2.0,
        now=now,
    )
    w.user_ws_upsert_order(
        _venue_view(
            vid="0xV2",
            token="tok-A",
            side=Side.BUY,
            size=Decimal("3"),
            price=Decimal("0.80"),
            source="rest",
        )
    )

    res = reconcile_open_orders(w, o, adoption_grace_s=5.0, now=now)

    assert "venue_open_not_tracked_locally" not in res.blocking_drift_flags
    assert "venue_open_not_tracked_locally_pending_adoption" in res.drift_flags
    assert o.orders[ClientOrderId(cid)].venue_order_id is None
    decisions = list(res.venue_adoption_decisions)
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "defer_within_adoption_grace"
    assert decisions[0]["candidate_client_order_id"] is None
    assert decisions[0]["blocking"] is False


def test_venue_id_blocks_when_no_local_candidate_present() -> None:
    """3) No no-vid provisional row at all → fail-closed ``venue_open_not_tracked_locally``."""
    now = datetime(2026, 4, 17, 21, 0, 0, tzinfo=timezone.utc)
    w = WalletStore()
    o = OrderStore()
    w.user_ws_upsert_order(
        _venue_view(
            vid="0xV3",
            token="tok-FOREIGN",
            side=Side.SELL,
            size=Decimal("4"),
            price=Decimal("0.20"),
            source="rest",
        )
    )

    res = reconcile_open_orders(w, o, adoption_grace_s=5.0, now=now)

    assert "venue_open_not_tracked_locally" in res.blocking_drift_flags
    decisions = list(res.venue_adoption_decisions)
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "blocked_unmatched"
    assert decisions[0]["blocking"] is True


def test_venue_id_blocks_when_candidate_exists_but_grace_expired() -> None:
    """4) Candidate exists, attrs match, but ``register_age_s > adoption_grace_s`` → BLOCK."""
    now = datetime(2026, 4, 17, 21, 0, 0, tzinfo=timezone.utc)
    w = WalletStore()
    o = OrderStore()
    cid = "cid-too-old"
    o.orders[ClientOrderId(cid)] = _no_vid_provisional(
        cid=cid,
        token="tok-A",
        side=Side.BUY,
        size=Decimal("5"),
        price=Decimal("0.50"),
        register_age_s=20.0,  # well beyond default adoption_grace_s
        now=now,
    )
    w.user_ws_upsert_order(
        _venue_view(
            vid="0xV4",
            token="tok-A",
            side=Side.BUY,
            size=Decimal("5"),
            price=Decimal("0.50"),
            source="rest",
        )
    )

    res = reconcile_open_orders(w, o, adoption_grace_s=5.0, now=now)

    assert "venue_open_not_tracked_locally" in res.blocking_drift_flags
    assert o.orders[ClientOrderId(cid)].venue_order_id is None
    decisions = list(res.venue_adoption_decisions)
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "blocked_unmatched"
    assert decisions[0]["blocking"] is True


def test_stuck_provisional_path_unchanged_by_adoption_logic() -> None:
    """5) Existing stuck-provisional behavior (no-vid row past submit_grace_s) still surfaces
    ``local_open_not_on_venue`` and is BLOCKING — adoption only fires when REST sees the id.
    """
    now = datetime(2026, 4, 17, 21, 0, 0, tzinfo=timezone.utc)
    w = WalletStore()
    o = OrderStore()
    cid = "cid-stuck"
    # Provisional row has a venue_order_id (so it's no longer "no-vid"); but venue truth is empty.
    # This is the classic stuck-provisional path the previous patch covers.
    o.orders[ClientOrderId(cid)] = LocalOrder(
        client_order_id=ClientOrderId(cid),
        venue_order_id=VenueOrderId("0xstuck"),
        token_id=TokenId("tok-A"),
        side=Side.BUY,
        remaining=Decimal("5"),
        original_size=Decimal("5"),
        size_matched=Decimal("0"),
        confirmation="provisional",
        submit_ack_utc=now - timedelta(seconds=30),  # past submit_grace_s
        last_local_source="local",
        submit_fingerprint="fp-stuck",
        limit_price=Decimal("0.50"),
        register_utc=now - timedelta(seconds=30),
    )

    res = reconcile_open_orders(
        w,
        o,
        submit_grace_s=15.0,
        unknown_terminal_timeout_s=60.0,
        adoption_grace_s=5.0,
        now=now,
    )

    # Stuck provisional path: blocked_absent → local_open_not_on_venue (blocking).
    assert "local_open_not_on_venue" in res.blocking_drift_flags
    # Adoption path did nothing here (no venue side).
    assert res.venue_adoption_decisions == ()


def test_observability_payload_carries_match_basis_and_decision() -> None:
    """6) Decision record exposes the fields operators need to debug a race."""
    now = datetime(2026, 4, 17, 21, 0, 0, tzinfo=timezone.utc)
    w = WalletStore()
    o = OrderStore()
    cid = "cid-obs"
    o.orders[ClientOrderId(cid)] = _no_vid_provisional(
        cid=cid,
        token="tok-A",
        side=Side.BUY,
        size=Decimal("5"),
        price=Decimal("0.50"),
        register_age_s=2.0,
        now=now,
    )
    w.user_ws_upsert_order(
        _venue_view(
            vid="0xVobs",
            token="tok-A",
            side=Side.BUY,
            size=Decimal("5"),
            price=Decimal("0.50"),
            source="rest",
        )
    )

    res = reconcile_open_orders(w, o, adoption_grace_s=5.0, now=now)

    d = res.venue_adoption_decisions[0]
    required_keys = {
        "venue_order_id",
        "venue_token_id",
        "venue_side",
        "venue_original_size",
        "venue_remaining",
        "venue_limit_price",
        "venue_state_source",
        "adoption_grace_s",
        "user_ws_fresh",
        "venue_restart_suspected",
        "candidate_client_order_id",
        "match_basis",
        "decision",
        "decision_reason",
        "blocking",
        "resolved_at_utc",
    }
    assert required_keys.issubset(d.keys())
    assert d["candidate_client_order_id"] == cid
    assert d["decision"] == "adopted_no_vid_provisional"
    assert d["blocking"] is False


def test_adoption_skipped_when_user_ws_stale() -> None:
    """Safety: never auto-adopt while user WS is stale; defer-or-block path applies instead."""
    now = datetime(2026, 4, 17, 21, 0, 0, tzinfo=timezone.utc)
    w = WalletStore()
    o = OrderStore()
    cid = "cid-stale"
    o.orders[ClientOrderId(cid)] = _no_vid_provisional(
        cid=cid,
        token="tok-A",
        side=Side.BUY,
        size=Decimal("5"),
        price=Decimal("0.50"),
        register_age_s=1.0,
        now=now,
    )
    w.user_ws_upsert_order(
        _venue_view(
            vid="0xVstale",
            token="tok-A",
            side=Side.BUY,
            size=Decimal("5"),
            price=Decimal("0.50"),
            source="rest",
        )
    )

    res = reconcile_open_orders(
        w,
        o,
        venue_user_ws_stale=True,
        adoption_grace_s=5.0,
        now=now,
    )

    assert "venue_open_not_tracked_locally" in res.blocking_drift_flags
    assert o.orders[ClientOrderId(cid)].venue_order_id is None
    assert res.venue_adoption_decisions[0]["decision"] == "blocked_unmatched"
