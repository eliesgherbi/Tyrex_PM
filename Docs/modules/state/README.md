# `state/`

In-memory truth. Each store has **one writer** (per slice) and many readers. Reconciliation is the only place where the local view is compared against the venue.

## Files

| File | Owner | Purpose |
|------|-------|---------|
| `wallet_store.py` | `ingestion/user_stream` (WS), `runtime/live_supervisor` (REST refresh) | **Venue/wallet truth:** positions, merged open orders, USDC balance + allowance |
| `allocation_ledger.py` | `runtime/pipeline` (buy/sell/reserve hooks) | **Strategy ownership truth:** per-`owner_id` token qty, exit reservations; persisted to `var/state/allocation_ledger.json` |
| `order_store.py` | `execution/order_lifecycle` | Local OMS rows, provisional repair, in-flight counts |
| `market_store.py` | `ingestion/market_stream` | Last-known mid/last for tokens |
| `strategy_store.py` | `ingestion/guru_stream` | Guru watermark + dedup set |
| `shadow_wallet.py` | `runtime` (shadow only) | Synthetic bootstrap + instant fills |
| `reconcile.py` | `runtime/pipeline` | Drift detection + repair / adoption / tombstone |

## Two inventory layers

| Layer | Store | Used for |
|-------|-------|----------|
| **Venue inventory** | `WalletStore.positions` | `RiskEngine` final SELL gate (`check_inventory_sell`) |
| **Strategy allocation** | `AllocationLedger` | Strategy SELL sizing (all strategies clamp planned size to `get_available_allocated`) |

Strategies read allocation; runtime mutates it through pipeline hooks (`maybe_apply_allocation_buy`, `maybe_reserve_exit_allocation`, `maybe_apply_allocation_sell`, etc.).

## Wallet store details

- `open_orders` is a merged view: REST snapshot + user-WS upserts, with WS-terminal tombstones.
- `get_tombstoned_rest_vids()` — surfaced on `reconcile` facts.

## Order store details

`LocalOrder.confirmation`: `provisional` | `venue_confirmed`. Terminal states go to `terminal_audit` ring buffer.

## Reconcile

Six axes in `reconcile.reconcile_open_orders` — see [LIVE_ARCHITECTURE §3](../../LIVE_ARCHITECTURE.md#3-reconcile-state-machine).

## Boundaries

- Stores must never call into `risk/` or `strategies/`.
- Single-writer rule: only the owning loop mutates a slice.
