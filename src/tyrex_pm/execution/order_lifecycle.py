from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.ids import ClientOrderId, VenueOrderId
from tyrex_pm.core.models import ApprovedIntent, OpenOrderView
from tyrex_pm.core.time import utc_now
from tyrex_pm.state.order_store import (
    LocalOrder,
    OrderStore,
    compute_submit_fingerprint,
)
from tyrex_pm.state.wallet_store import WalletStore


def submit_fingerprint_for_intent(ap: ApprovedIntent) -> str:
    """Stable fingerprint for the duplicate-submit guard while a provisional row repairs."""
    intent = ap.intent
    return compute_submit_fingerprint(
        token_id=intent.token_id,
        side=intent.side,
        size=intent.size,
        limit_price=getattr(intent, "limit_price", None),
    )


def register_submit(order_store: OrderStore, ap: ApprovedIntent) -> None:
    intent = ap.intent
    fp = submit_fingerprint_for_intent(ap)
    order_store.orders[ap.client_order_id] = LocalOrder(
        client_order_id=ap.client_order_id,
        venue_order_id=None,
        token_id=intent.token_id,
        side=intent.side,
        remaining=intent.size,
        original_size=intent.size,
        size_matched=Decimal("0"),
        confirmation="provisional",
        submit_ack_utc=None,
        last_local_source="local",
        submit_fingerprint=fp,
        limit_price=getattr(intent, "limit_price", None),
        register_utc=utc_now(),
    )
    order_store.pending_repair_fingerprints.add(fp)
    order_store.in_flight_order_count += 1
    q = order_store.in_flight_by_token.get(intent.token_id, Decimal("0"))
    order_store.in_flight_by_token[intent.token_id] = q + intent.size


def _release_in_flight_for(order_store: OrderStore, token_id, size: Decimal) -> None:
    order_store.in_flight_order_count = max(0, order_store.in_flight_order_count - 1)
    q = order_store.in_flight_by_token.get(token_id, Decimal("0")) - size
    if q <= 0:
        order_store.in_flight_by_token.pop(token_id, None)
    else:
        order_store.in_flight_by_token[token_id] = q


def ack_submit(
    order_store: OrderStore,
    ap: ApprovedIntent,
    venue_order_id: VenueOrderId | None,
    *,
    shadow_instant_fill: bool,
    ack_status: str | None = None,
) -> None:
    """After venue ack: clear submit in-flight; live keeps resting row with ack metadata."""
    o = order_store.orders.get(ap.client_order_id)
    if o is None:
        return
    intent = ap.intent
    _release_in_flight_for(order_store, intent.token_id, intent.size)
    if shadow_instant_fill:
        if o.submit_fingerprint:
            order_store.pending_repair_fingerprints.discard(o.submit_fingerprint)
        order_store.orders.pop(ap.client_order_id, None)
        return
    if venue_order_id is not None:
        order_store.orders[ap.client_order_id] = LocalOrder(
            client_order_id=ap.client_order_id,
            venue_order_id=venue_order_id,
            token_id=o.token_id,
            side=o.side,
            remaining=o.remaining,
            original_size=intent.size,
            size_matched=Decimal("0"),
            confirmation="provisional",
            submit_ack_utc=utc_now(),
            last_local_source="local",
            submit_fingerprint=o.submit_fingerprint,
            ack_status=ack_status,
            limit_price=o.limit_price if o.limit_price is not None else getattr(intent, "limit_price", None),
            register_utc=o.register_utc,
        )


def attach_venue_order_id_to_local(
    order_store: OrderStore,
    client_order_id: ClientOrderId,
    venue_order_id: VenueOrderId | str,
    *,
    ack_status: str | None = None,
) -> bool:
    """Adopt a venue order id onto an existing no-vid provisional row.

    Used by the venue-adoption matcher in :mod:`tyrex_pm.state.reconcile` to cure
    REST-ahead-of-local-registration races: the venue / REST view shows a fresh
    order id, but ``ack_submit`` never linked it (e.g. response parse missed the id).
    """
    lo = order_store.orders.get(client_order_id)
    if lo is None or lo.venue_order_id is not None:
        return False
    vid = VenueOrderId(str(venue_order_id))
    order_store.orders[client_order_id] = LocalOrder(
        client_order_id=lo.client_order_id,
        venue_order_id=vid,
        token_id=lo.token_id,
        side=lo.side,
        remaining=lo.remaining,
        original_size=lo.original_size,
        size_matched=lo.size_matched,
        confirmation=lo.confirmation,
        submit_ack_utc=lo.submit_ack_utc if lo.submit_ack_utc is not None else utc_now(),
        last_local_source="venue_adoption",
        submit_fingerprint=lo.submit_fingerprint,
        ack_status=ack_status if ack_status is not None else lo.ack_status,
        repair_attempts=lo.repair_attempts,
        limit_price=lo.limit_price,
        register_utc=lo.register_utc,
    )
    return True


def remove_local_resting_by_venue_order_id(order_store: OrderStore, venue_order_id: VenueOrderId | str) -> int:
    """Drop resting local rows for this venue id (UI cancel / fill-complete / WS terminal). Returns rows removed."""
    key = str(venue_order_id)
    n = 0
    for cid, lo in list(order_store.orders.items()):
        if lo.venue_order_id is not None and str(lo.venue_order_id) == key:
            if lo.submit_fingerprint:
                order_store.pending_repair_fingerprints.discard(lo.submit_fingerprint)
            order_store.orders.pop(cid, None)
            n += 1
    return n


def apply_venue_open_order_to_local_orders(order_store: OrderStore, view: OpenOrderView) -> None:
    """User WS (or explicit repair) order snapshot → align local OMS row by venue id."""
    if view.venue_order_id is None:
        return
    if view.remaining_size <= 0:
        remove_local_resting_by_venue_order_id(order_store, view.venue_order_id)
        return
    vid = str(view.venue_order_id)
    matched = view.size_matched
    if matched is None and view.original_size is not None:
        matched = view.original_size - view.remaining_size
    orig = view.original_size
    for cid, lo in list(order_store.orders.items()):
        if lo.venue_order_id is not None and str(lo.venue_order_id) == vid:
            order_store.orders[cid] = LocalOrder(
                client_order_id=lo.client_order_id,
                venue_order_id=lo.venue_order_id,
                token_id=lo.token_id,
                side=lo.side,
                remaining=view.remaining_size,
                original_size=orig if orig is not None else lo.original_size,
                size_matched=matched if matched is not None else lo.size_matched,
                confirmation="venue_confirmed",
                submit_ack_utc=lo.submit_ack_utc,
                last_local_source=view.venue_state_source or "user_ws",
                submit_fingerprint=lo.submit_fingerprint,
                ack_status=lo.ack_status,
                limit_price=lo.limit_price if lo.limit_price is not None else view.limit_price,
                register_utc=lo.register_utc,
            )
            if lo.submit_fingerprint:
                order_store.pending_repair_fingerprints.discard(lo.submit_fingerprint)
            return


def sync_local_open_orders_from_venue_wallet(order_store: OrderStore, wallet: WalletStore) -> None:
    """REST/merged repair: align local resting fields from ``wallet.open_orders`` (source tagged on view)."""
    by_vid: dict[str, OpenOrderView] = {}
    for o in wallet.open_orders:
        if o.venue_order_id is not None:
            by_vid[str(o.venue_order_id)] = o
    for cid, lo in list(order_store.orders.items()):
        if lo.venue_order_id is None:
            continue
        v = by_vid.get(str(lo.venue_order_id))
        if v is None:
            continue
        if v.remaining_size <= 0:
            remove_local_resting_by_venue_order_id(order_store, lo.venue_order_id)
            continue
        matched = v.size_matched
        if matched is None and v.original_size is not None:
            matched = v.original_size - v.remaining_size
        src = v.venue_state_source or "rest"
        order_store.orders[cid] = LocalOrder(
            client_order_id=lo.client_order_id,
            venue_order_id=lo.venue_order_id,
            token_id=lo.token_id,
            side=lo.side,
            remaining=v.remaining_size,
            original_size=v.original_size if v.original_size is not None else lo.original_size,
            size_matched=matched if matched is not None else lo.size_matched,
            confirmation="venue_confirmed",
            submit_ack_utc=lo.submit_ack_utc,
            last_local_source=src,
            submit_fingerprint=lo.submit_fingerprint,
            ack_status=lo.ack_status,
            limit_price=lo.limit_price if lo.limit_price is not None else v.limit_price,
            register_utc=lo.register_utc,
        )
        if lo.submit_fingerprint:
            order_store.pending_repair_fingerprints.discard(lo.submit_fingerprint)


def remove_resting_order(order_store: OrderStore, client_order_id: ClientOrderId) -> None:
    """Remove local resting order after venue cancel/fill (not submit in-flight)."""
    o = order_store.orders.pop(client_order_id, None)
    if o is not None and o.submit_fingerprint:
        order_store.pending_repair_fingerprints.discard(o.submit_fingerprint)


def release_after_ack(order_store: OrderStore, client_order_id: ClientOrderId) -> None:
    """Clear submit in-flight + drop local row (shadow instant-fill / failed submit cleanup)."""
    o = order_store.orders.pop(client_order_id, None)
    if o is None:
        return
    if o.submit_fingerprint:
        order_store.pending_repair_fingerprints.discard(o.submit_fingerprint)
    order_store.in_flight_order_count = max(0, order_store.in_flight_order_count - 1)
    q = order_store.in_flight_by_token.get(o.token_id, Decimal("0")) - o.remaining
    if q <= 0:
        order_store.in_flight_by_token.pop(o.token_id, None)
    else:
        order_store.in_flight_by_token[o.token_id] = q
