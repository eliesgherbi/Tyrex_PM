from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId, VenueOrderId
from tyrex_pm.core.models import OpenOrderView, TradeFillRecord, WalletPosition
from tyrex_pm.core.time import utc_now

# Drop WS-terminal tombstones after this window so REST can re-hydrate if WS never had the id.
# A tombstone is stamped for both explicit CANCELLATION events and "remaining<=0" UPDATE events
# (full fill / matched-out terminal). It suppresses brief REST resurrection of an id that user-WS
# has authoritatively reported as no longer open.
_WS_CANCEL_TOMBSTONE_TTL_S = 600
_MAX_TRADE_LEDGER = 1000
#: Cap on the position-drift audit ring buffer (out-of-band SELL CONFIRMED events, etc.).
_MAX_POSITION_DRIFT_AUDIT = 200


@dataclass
class WalletStore:
    #: Outcome inventory from **confirmed** trades only (not derived from open-order rows).
    positions: dict[TokenId, WalletPosition] = field(default_factory=dict)
    open_orders: tuple[OpenOrderView, ...] = ()
    """Merged venue resting view: user WS primary; REST fills ids WS has not seen yet."""
    _rest_open_orders: tuple[OpenOrderView, ...] = field(default_factory=tuple)
    """User-channel order state keyed by venue order id string (authoritative when present)."""
    _user_ws_order_map: dict[str, OpenOrderView] = field(default_factory=dict)
    #: Venue ids reported terminal by user WS (CANCELLATION or UPDATE with remaining<=0) — used to
    #: suppress stale REST reintroduction briefly. Cleared by any later live WS upsert (rem>0) or
    #: by TTL expiry in :meth:`_prune_tombstones`.
    _ws_cancel_tombstones: dict[str, datetime] = field(default_factory=dict)
    #: User-channel trade events (MATCHED onward); collateral/positions use separate paths.
    trade_fill_records: list[TradeFillRecord] = field(default_factory=list)
    usdc_balance: Decimal | None = None
    usdc_allowance: Decimal | None = None
    last_sync_ts: datetime | None = None
    #: Last successful positions REST refresh (data-api/positions); separate from open-orders sync.
    last_positions_sync_ts: datetime | None = None
    #: Audit ring buffer of out-of-band position events (e.g., SELL CONFIRMED with no prior long).
    #: Reconcile may surface these into facts; consumers can drain by clearing the list.
    position_drift_audit: list[dict] = field(default_factory=list)

    def record_position_drift_audit(self, entry: dict) -> None:
        """Append a capped audit entry; oldest entries are dropped past the ring-buffer limit."""
        self.position_drift_audit.append(entry)
        if len(self.position_drift_audit) > _MAX_POSITION_DRIFT_AUDIT:
            self.position_drift_audit = self.position_drift_audit[-_MAX_POSITION_DRIFT_AUDIT:]

    def _prune_tombstones(self) -> None:
        cutoff = utc_now() - timedelta(seconds=_WS_CANCEL_TOMBSTONE_TTL_S)
        self._ws_cancel_tombstones = {
            k: v for k, v in self._ws_cancel_tombstones.items() if v > cutoff
        }

    def rebuild_open_orders_merged(self) -> None:
        """REST backstop for ids not in user WS; user WS wins on conflict; tombstones hide stale REST rows."""
        self._prune_tombstones()
        by_id: dict[str, OpenOrderView] = {}
        for o in self._rest_open_orders:
            if o.venue_order_id is None:
                continue
            vid = str(o.venue_order_id)
            if vid in self._ws_cancel_tombstones:
                continue
            if vid not in self._user_ws_order_map:
                by_id[vid] = o
        for vid, o in self._user_ws_order_map.items():
            by_id[vid] = o
        self.open_orders = tuple(by_id.values())

    def user_ws_remove_order(self, venue_order_id: VenueOrderId | str) -> None:
        key = str(venue_order_id)
        self._user_ws_order_map.pop(key, None)
        self._ws_cancel_tombstones[key] = utc_now()
        self.rebuild_open_orders_merged()

    def user_ws_upsert_order(self, view: OpenOrderView) -> None:
        """Apply a user-WS order event.

        ``remaining_size > 0``: live order; replace the WS map entry and clear any prior
        terminal tombstone (WS now considers the id alive).

        ``remaining_size <= 0``: WS-terminal (full fill, matched-out, etc.). This is the
        symmetric peer of :meth:`user_ws_remove_order`: drop the WS map entry **and** stamp
        a tombstone. Without the tombstone, a stale REST snapshot returned moments later
        can resurrect the same id in the merged book and trigger a false
        ``venue_open_not_tracked_locally`` because the local OMS row has already been
        cleaned up by the same WS event.
        """
        if view.venue_order_id is None:
            return
        key = str(view.venue_order_id)
        if view.remaining_size <= 0:
            self._user_ws_order_map.pop(key, None)
            self._ws_cancel_tombstones[key] = utc_now()
        else:
            self._ws_cancel_tombstones.pop(key, None)
            self._user_ws_order_map[key] = view
        self.rebuild_open_orders_merged()

    def get_tombstoned_rest_vids(self) -> tuple[str, ...]:
        """Venue ids currently in the REST snapshot but suppressed by an active tombstone.

        Used by reconcile facts to make the inverse-race suppression observable: if a vid
        appears here, it means REST briefly shows an order that WS has already declared
        terminal, and the merged view is correctly hiding it. Empty tuple in steady state.
        """
        self._prune_tombstones()
        if not self._ws_cancel_tombstones or not self._rest_open_orders:
            return ()
        rest_vids = {
            str(o.venue_order_id) for o in self._rest_open_orders if o.venue_order_id is not None
        }
        return tuple(sorted(rest_vids & self._ws_cancel_tombstones.keys()))

    def record_user_ws_trade(self, rec: TradeFillRecord) -> None:
        self.trade_fill_records.append(rec)
        if len(self.trade_fill_records) > _MAX_TRADE_LEDGER:
            self.trade_fill_records = self.trade_fill_records[-_MAX_TRADE_LEDGER:]
