"""In-flight BUY reservation derivation (closes the submit→wallet.open_orders mirror gap).

Why this module exists
----------------------
``register_submit`` (see :mod:`tyrex_pm.execution.order_lifecycle`) creates a provisional
``LocalOrder`` *before* the venue HTTP call returns; on a successful ack the row gets a
``venue_order_id`` and the in-flight count counter is released. However, the merged
``WalletStore.open_orders`` view — the **only** input to deployment cap and USDC capital
checks — only gains the new order via:

* user-WS ``ORDER`` event (typical latency: 100 ms — a few seconds)
* REST ``/data/orders`` poll (bounded by the configured cadence)

Between the venue ack and that mirror update, the venue has already locked collateral but
local risk thinks it is free.  Live evidence (see ``var/reporting/runs/live_test_inverse_race``)
shows ~40 wasted ``oms_reject`` ("not enough balance / allowance") calls in a single 10 s burst
because of this race.

The fix is **not** a separate ledger: every in-flight reservation is exactly one provisional
``LocalOrder`` row in :class:`tyrex_pm.state.order_store.OrderStore` already.  This module
derives the synthetic resting BUYs from that authoritative source, dedup'd against the
wallet view by ``venue_order_id``.

Lifecycle (entirely implicit — driven by ``OrderStore`` mutations elsewhere)
---------------------------------------------------------------------------
* **ADD**: any provisional ``LocalOrder`` with ``side=BUY``, ``remaining > 0``,
  ``limit_price`` not None, and ``venue_order_id`` not present in
  ``wallet.open_orders`` is treated as a reservation by deployment + capital checks.
* **RELEASE**:
  * venue truth absorbs it → vid appears in ``wallet.open_orders`` → dedup-by-vid skips it.
  * venue rejects (HTTP 4xx / 425 / network) → :func:`release_after_ack` removes the row.
  * full fill / matched-out via WS UPDATE → :func:`apply_venue_open_order_to_local_orders` removes it.
  * cancel via UI / venue-truth poll → :func:`remove_local_resting_by_venue_order_id` removes it.
  * provisional repair drops a stale row (``terminal_dropped``) → row gone.
  * shadow instant fill (offline / parity mode) → :func:`ack_submit` drops the row.

Scope
-----
* Only ``side=BUY`` produces a reservation: SELLs free outcome inventory but do not consume
  USDC collateral on Polymarket (the inventory gate handles SELL safety).
* Rows without a ``limit_price`` are **skipped** (cannot price → cannot reserve a USD figure).
  The deployment math falls back to its existing mark-unknown handling for those.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import OpenOrderView
from tyrex_pm.risk.evidence_format import s_usd

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from tyrex_pm.state.order_store import OrderStore
    from tyrex_pm.state.wallet_store import WalletStore


@dataclass(frozen=True)
class InFlightReservationSummary:
    """Operator-facing audit of which provisional BUYs are reserving USD right now."""

    #: Synthetic resting BUY views consumed by deployment / capital math.
    reservations: tuple[OpenOrderView, ...]
    #: Total reserved USD across all in-flight rows.
    total_usd: Decimal
    #: Per-token reserved USD breakdown (empty when ``total_usd == 0``).
    by_token_usd: dict[TokenId, Decimal]
    #: Compact per-row evidence (capped) for ``risk_decision`` facts.
    examples: tuple[dict, ...]


def _wallet_known_vids(wallet: WalletStore) -> set[str]:
    return {
        str(o.venue_order_id) for o in wallet.open_orders if o.venue_order_id is not None
    }


def derive_in_flight_buy_reservations(
    order_store: OrderStore,
    wallet: WalletStore,
    *,
    examples_cap: int = 16,
) -> InFlightReservationSummary:
    """Build the synthetic in-flight BUY reservation view from ``OrderStore`` + ``WalletStore``.

    Dedup rule (avoids double-counting against ``wallet.open_orders``):

    * ``LocalOrder`` row contributes a reservation **iff** its ``venue_order_id`` is *not*
      present in ``wallet.open_orders``.  No-vid provisional rows always contribute (they
      cannot possibly be in the wallet view yet).
    * Rows must be ``side == BUY`` with ``remaining > 0`` and a non-null ``limit_price`` to
      be priced; rows missing a ``limit_price`` are skipped (no safe USD figure).
    """
    known_vids = _wallet_known_vids(wallet)
    reservations: list[OpenOrderView] = []
    by_token: dict[TokenId, Decimal] = {}
    total = Decimal("0")
    examples: list[dict] = []
    for cid, lo in order_store.orders.items():
        if lo.side != Side.BUY:
            continue
        if lo.remaining is None or lo.remaining <= 0:
            continue
        if lo.limit_price is None:
            continue
        # If this row is already in the wallet view (vid known), it's already accounted for
        # by ``open_buy_reserved_usd``; skip to avoid double counting.
        if lo.venue_order_id is not None and str(lo.venue_order_id) in known_vids:
            continue
        view = OpenOrderView(
            token_id=lo.token_id,
            side=Side.BUY,
            remaining_size=lo.remaining,
            limit_price=lo.limit_price,
            client_order_id=lo.client_order_id,
            venue_order_id=lo.venue_order_id,
            original_size=lo.original_size,
            size_matched=lo.size_matched,
            venue_state_source="local_in_flight",
        )
        reservations.append(view)
        usd = lo.remaining * lo.limit_price
        total += usd
        by_token[lo.token_id] = by_token.get(lo.token_id, Decimal("0")) + usd
        if len(examples) < examples_cap:
            examples.append(
                {
                    "client_order_id": str(cid),
                    "token_id": str(lo.token_id),
                    "remaining": str(lo.remaining),
                    "limit_price": str(lo.limit_price),
                    "reserved_usd": str(usd),
                    "has_venue_order_id": lo.venue_order_id is not None,
                    "confirmation": lo.confirmation,
                    "ack_status": lo.ack_status,
                }
            )
    return InFlightReservationSummary(
        reservations=tuple(reservations),
        total_usd=total,
        by_token_usd=by_token,
        examples=tuple(examples),
    )


def in_flight_evidence_payload(
    summary: InFlightReservationSummary,
) -> dict:
    """Produce the canonical payload merged into ``risk_decision.extensions``.

    Always includes the totals (even when ``0``) so a downstream report can confidently
    answer "was the capital gate aware of in-flight reservations?" without ambiguity.
    """
    return {
        "in_flight_reserved_usd_total": s_usd(summary.total_usd),
        "in_flight_reserved_usd_by_token": {
            str(k): s_usd(v) for k, v in summary.by_token_usd.items()
        },
        "in_flight_reservation_count": len(summary.reservations),
        "in_flight_reservation_examples": list(summary.examples),
    }
