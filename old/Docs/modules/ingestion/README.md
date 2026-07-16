# `ingestion/`

Pulls outside data into Tyrex's canonical models. Three independent loops, each owned by a single supervisor task in `runtime/`.

## Files

| File | Loop | Source | Output |
|------|------|--------|--------|
| `guru_stream.py` | `poll_guru_incremental` | Polymarket Data API (`/activity`) | `GuruTradeSignal` rows |
| `user_stream.py` | `run_user_ws_ingest` | Polymarket user WS | Position deltas, fills, order lifecycle (mutates `WalletStore` + `OrderStore`) |
| `market_stream.py` | (helpers) | Polymarket market WS | Price/book updates for `MarketStateStore` (kept thin — not all live runs subscribe) |
| `historical_backfill.py` | one-shot | Data API fixture file | Replay of `GuruTradeSignal`s for `tyrex-pm run --fixture` |

## Guru watermark + dedup

`guru_stream` is the canonical example of "boundary correctness":

1. Sort candidates by `(ts_ms, dedup_key)`.
2. Skip rows already in `StrategyStore.guru_seen_dedup` (process lifetime + persisted across restarts).
3. Skip rows ≤ `StrategyStore.guru_watermark`.
4. Advance the watermark to the last accepted row.

Result: at-most-once delivery to strategies even across pagination retries, restarts, or duplicate API rows.

## User WS

`user_stream` is the **single writer** for venue-truth open orders during live trading. Order updates go through:

- `WalletStore.user_ws_upsert_order(view)` — `remaining_size > 0` keeps it live; `remaining_size <= 0` removes the row **and** stamps a tombstone (the inverse-race fix; see [LIVE_ARCHITECTURE §3.3](../../LIVE_ARCHITECTURE.md#33-ws-terminal-tombstones)).
- `WalletStore.user_ws_remove_order(vid)` — explicit cancel; same tombstone path.

Trade fills go to `WalletStore.record_user_ws_trade` and feed reconcile's `_trade_fill_evidence`.

## Where these loops live

`ingestion/*` exposes pure async functions; the supervisor wiring is in `runtime/app.py::cmd_run` and `runtime/live_supervisor.py`.

## Adding a new ingest source

1. Add a module that exposes one async function taking the relevant store + a `stop: asyncio.Event`.
2. Wire it into `runtime/app.py` next to the existing supervisors so it inherits the same shutdown plumbing.
3. Mutate **only one** store from each loop (single-writer per state slice).
