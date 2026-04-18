# `state/`

In-memory truth. Each store has **one writer** (per slice) and many readers. Reconciliation is the only place where the local view is compared against the venue.

## Files

| File | Owner | Purpose |
|------|-------|---------|
| `wallet_store.py` | `ingestion/user_stream` (WS), `runtime/live_supervisor` (REST refresh) | Positions, merged open orders (REST + user-WS, with WS-terminal tombstones), USDC balance + allowance, last-sync timestamps, position-drift audit |
| `order_store.py` | `execution/order_lifecycle` | Local OMS rows (`LocalOrder`), provisional repair lifecycle, in-flight counts, submit-fingerprint guard, terminal audit ring buffer |
| `market_store.py` | `ingestion/market_stream` | Last-known mid/last for tokens (used by sizing / liquidity guard) |
| `strategy_store.py` | `ingestion/guru_stream` | Guru watermark (`(ts_ms, dedup_id)`) and dedup set; persisted across restarts |
| `shadow_wallet.py` | `runtime` (shadow only) | `apply_shadow_bootstrap` (seed USDC) + `apply_shadow_fill` (synthetic fill into `WalletStore` so shadow runs accumulate positions without a live venue) |
| `reconcile.py` | `runtime/pipeline` (called per signal + per supervisor tick) | Drift detection + repair / adoption / tombstone state machine |

## Wallet store details

- `open_orders` is a *merged* view: REST snapshot + user-WS upserts, with WS-terminal **tombstones** suppressing stale REST resurrection (`_WS_CANCEL_TOMBSTONE_TTL_S = 600`).
- `user_ws_upsert_order(view)` stamps a tombstone when `remaining_size <= 0`; `user_ws_remove_order(vid)` does the same on explicit cancel.
- `get_tombstoned_rest_vids()` exposes the currently-suppressed ids — `pipeline.reconcile_coordinator` writes them as `tombstoned_rest_vids` on the `reconcile` fact for observability.
- `record_position_drift_audit` keeps the last 200 out-of-band position events (e.g. SELL CONFIRMED with no prior long).

## Order store details

`LocalOrder.confirmation` is one of:

- `provisional` — submitted but not yet confirmed by venue truth.
- `venue_confirmed` — visible in the merged wallet view.

Terminal states (`filled_resolved`, `unknown_terminal`) are not persisted on the row; they go into `terminal_audit` (1024-entry ring) and the row is removed.

## Reconcile state machine (`reconcile.reconcile_open_orders`)

Six axes run every reconcile tick:

| Axis | What it does |
|------|--------------|
| **Provisional repair** | For each `provisional` local row: confirm if visible, drop if WS trade evidence covers original size (`filled_resolved`), drop after timeout if WS fresh & no restart (`unknown_terminal`), block otherwise (`local_open_not_on_venue`) |
| **Venue order match** | For each merged `OpenOrderView`: ensure remaining/original sizes match the corresponding `LocalOrder` |
| **Venue adoption** | For each venue `vid` with no local row: try to adopt onto a recent no-vid provisional row (token+side+size+price within `adoption_grace_s`); otherwise non-blocking briefly; otherwise block (`venue_open_not_tracked_locally`) |
| **Pruning** | Drop `venue_confirmed` rows that vanished from a fresh merged book (UI cancel / full fill); audited |
| **Tombstone surfacing** | Bubble `WalletStore.get_tombstoned_rest_vids()` into reconcile facts |
| **Severity classification** | `_severity_for_blocking` maps blocking flags → `none / transient_venue_lag_candidate / size_mismatch / structural` |

The full policy summary is in `reconcile.RECONCILE_POLICY_SUMMARY` and emitted on every `reconcile` fact's `reconcile_policy_summary`.

Detail: [LIVE_ARCHITECTURE §3](../../LIVE_ARCHITECTURE.md#3-reconcile-state-machine).

## Boundaries

- These dataclasses are **not** frozen — they are stores. The frozen DTOs in `core/models.py` flow through them.
- A store must never call into `risk/` or `strategies/`.
- Single-writer rule: only the owning loop mutates a slice. Cross-loop reads are fine; cross-loop writes are not.
