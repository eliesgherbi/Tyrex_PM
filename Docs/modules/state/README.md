# `state/`

In-memory truth. Each store has **one writer** (per slice) and many readers. Reconciliation is the only place where the local view is compared against the venue.

## Files

| File | Owner | Purpose |
|------|-------|---------|
| `wallet_store.py` | `ingestion/user_stream` (WS), `runtime/live_supervisor` (REST refresh) | **Venue/wallet truth:** positions, merged open orders, USDC balance + allowance |
| `allocation_ledger.py` | `runtime/pipeline` (buy/sell/reserve hooks) | **Strategy ownership truth:** per-`owner_id` token qty, exit reservations; persisted to `var/state/allocation_ledger.json`. **Clamp grace:** recent BUY credits skip clamp-to-zero when REST `venue_qty=0` within `clamp_grace_s_after_buy` (default 90s). **Repair:** `repair_allocation_to_target` from CONFIRMED WS or venue position |
| `order_store.py` | `execution/order_lifecycle` | Local OMS rows, provisional repair, in-flight counts |
| `market_store.py` | `ingestion/market_stream` + `runtime/market_data_runtime` (REST bootstrap/refresh when `market_data.enabled`) | **`MarketStateStore`** (P2 architecture_enhance): per-token book — best bid/ask, spread, mid, `last_update_ts`, `is_stale`, `estimate_fill_price` (VWAP), `estimate_slippage`. Wired in `app.py` when `market_data.enabled=true` (P4.5). **Phase 4.6** `paired_binary` reads YES/NO books from here for entry eval and monitor ticks |
| `fill_state.py` | — (pure helper) | **Fill/trade finality classification** (P3.5 architecture_enhance): `classify(status)` + `is_execution_evidence` / `is_position_final` / `is_allocation_final` / `releases_reservation` / `counts_for_realized_pnl`. Unknown statuses fail closed |
| `entry_fill_lifecycle.py` | `runtime/entry_qty_reconcile`, strategies | **Order vs fill vs owned qty** (Phase 4.6 fix): `EntryFillStatus`, `OrderFillSnapshot`, `resolve_leg_fill_snapshot`. Separates resting submit from sellable inventory |
| `strategy_store.py` | `ingestion/guru_stream` | Guru watermark + dedup set |
| `shadow_wallet.py` | `runtime` (shadow only) | Synthetic bootstrap + instant fills |
| `reconcile.py` | `runtime/pipeline` | Drift detection + repair / adoption / tombstone |

## Two inventory layers

| Layer | Store | Used for |
|-------|-------|----------|
| **Venue inventory** | `WalletStore.positions` | `RiskEngine` final SELL gate (`check_inventory_sell`) |
| **Strategy allocation** | `AllocationLedger` | Strategy SELL sizing (all strategies clamp planned size to `get_available_allocated`) |

Strategies read allocation; runtime mutates it through pipeline hooks (`maybe_apply_allocation_buy`, `maybe_reserve_exit_allocation`, `maybe_apply_allocation_sell`, `maybe_clamp_allocations_to_venue`, `maybe_repair_allocation_from_evidence`, etc.).

### Live truth sources and monitoring reliability (Phase 4.6 fix)

| Question | Answer |
|----------|--------|
| Fastest fill detection | User-WS trade events |
| Allocation-final | User-WS `CONFIRMED` (`fill_state.is_allocation_final`) |
| Order book for monitor | `MarketStateStore` (REST/market_data runtime) |
| SELL sizing | `min(planned, owner allocation, venue qty)` — allocation repaired from finality before exit |
| Clamp skipped when | Recent BUY + `venue_qty=0` within grace window |
| Repair when | CONFIRMED qty > ledger after erroneous clamp |

See [phase_4_6 §18](../../Implementation/architecture_enhance/phase_4_6_paired_binary_strategy_production_protection.md#18-live-truth-sources-and-monitoring-reliability).

### Order lifecycle vs allocation lifecycle (Phase 4.6 fix)

| Event | OrderStore | AllocationLedger |
|-------|------------|------------------|
| BUY submit ack `live` | Record resting row | **No credit** (`allocation_buy_skipped_unfilled_order`) |
| BUY OMS `matched` + `taking_amount` | `size_matched` updated | Credit filled qty only (`allocation_buy_applied_from_fill`) |
| WS `CONFIRMED` | — | Repair/confirm via `maybe_repair_allocation_from_evidence` |
| Shadow instant fill | Row removed | Credit + wallet position (aligned pair for entry reconcile) |

Strategies must use `resolve_leg_fill_snapshot` / `entry_qty_reconcile` for entry completion — not `get_available_allocated` alone.

## Wallet store details

- `open_orders` is a merged view: REST snapshot + user-WS upserts, with WS-terminal tombstones.
- `get_tombstoned_rest_vids()` — surfaced on `reconcile` facts.

## Order store details

`LocalOrder.confirmation`: `provisional` | `venue_confirmed`. Terminal states go to `terminal_audit` ring buffer.

## Fill finality (P3.5 architecture_enhance)

`fill_state.classify(status)` centralizes Polymarket trade-status meaning so every consumer agrees:

| status | evidence | position final | allocation final | releases reservation | realized PnL |
|--------|----------|----------------|------------------|----------------------|--------------|
| MATCHED | yes | no | no | no | no |
| MINED | yes | no | no | no | no |
| CONFIRMED | yes | yes | yes | yes | yes |
| RETRYING | no | no | no | no | no |
| FAILED | no | no | no | yes | no |
| *(unknown)* | no | no | no | no | no |

`allocation_buy_applied` is emitted when allocation credits; protection registration requires `is_allocation_final == true` (CONFIRMED). MATCHED-only `allocation_buy_applied` does **not** satisfy the registration boundary. `FinalityWaiter` (P4.5) polls for CONFIRMED before live protection register in the validation harness.

## Reconcile

Six axes in `reconcile.reconcile_open_orders` — see [LIVE_ARCHITECTURE §3](../../LIVE_ARCHITECTURE.md#3-reconcile-state-machine).

## Boundaries

- Stores must never call into `risk/` or `strategies/`.
- Single-writer rule: only the owning loop mutates a slice.

## Strategy-local persistence (Phase 4.6)

`paired_binary` persists lifecycle state to `var/state/paired_binary/<owner_id>.json` (proposed). This is **not** a store mutation from the strategy package at runtime — the runner writes after state transitions. Recovery reads this file plus `AllocationLedger` + `WalletStore` on startup. See [phase_4_6 design](../../Implementation/architecture_enhance/phase_4_6_paired_binary_strategy_production_protection.md).
