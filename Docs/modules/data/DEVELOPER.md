# Developer guide — `tyrex_pm.data`

[README](README.md) · [OPERATIONS](../../OPERATIONS.md) § Guru ingestion · [LIVE_ARCHITECTURE](../../LIVE_ARCHITECTURE.md)

## Responsibility

**External I/O and normalization** for guru trades: **Data API** incremental poll, **RTDS** WebSocket stream, dedup + watermark, **`GuruTradeSignal`** publication on the Nautilus bus via **`GuruSignalPipeline`**.

## Ingest modes (`GuruIngestRuntimeState`)

| Mode | Publisher behavior |
|------|---------------------|
| `poll_only` | `GuruMonitorActor` publishes after watermark advance. |
| `rtds_shadow` | Poll publishes; stream logs `guru_stream_would_emit` for comparison. |
| `rtds_primary` | Stream publishes when healthy; poll takes over during fallback; gap-fill via REST. |

## Core files

- **`guru_monitor.py`** — poll loop, backoff, pipeline publish when ingest state allows.
- **`guru_stream_actor.py`** — async RTDS worker, queue drain timer, reconnect / liveness.
- **`guru_ingest_pipeline.py`** — dedup LRU, bus publish, **`guru_signal_emitted`** log (`source=`).
- **`guru_parse.py`**, **`guru_rtds_parse.py`** — row → `GuruTradeSignal`; stable **`ingest_source_trade_id`** (e.g. `transactionHash:asset`).

## Extension patterns

- **New source:** produce the **same** `GuruTradeSignal` + topic string as `guru_monitor.py`; do not fork `CopyStrategy` subscription.
- **Heavier parsing:** keep in `data/` pure functions; actor stays I/O + orchestration.

## Pitfalls

- **Wallet filter:** RTDS is global; client-side **`proxyWallet`** must match strategy `guru_wallet_address` (case-insensitive).
- **Dedup vs watermark:** dedup prevents bus replays; watermark prevents poll backfill storms.

## Tests

Unit tests around parse, dedup id, pipeline (no `TradingNode` required for pure functions).
