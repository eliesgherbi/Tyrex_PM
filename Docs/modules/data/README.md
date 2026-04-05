# Module: `tyrex_pm.data`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md)

## A. Role

**External reads** and **normalization** for Polymarket: HTTP Data API (**`/activity` TRADE** for incremental poll / gap-fill), **Polymarket RTDS** WebSocket (`activity` / `trades`) via **`GuruStreamActor`**, guru row parsing, **incremental watermark** (`guru_watermark.py`), shared dedup LRU, and **`GuruSignalPipeline`** (`guru_ingest_pipeline.py`). Market tooling (allowlist, resolution, book check) lives here for scripts and validation — **not** used for unbounded historical crawls on the copy path.

**Operational default (see [OPERATIONS.md](../../OPERATIONS.md)):** `guru_ingest_mode: rtds_primary` on the runtime YAML so **`GuruStreamActor`** publishes `GuruTradeSignal` when healthy; **`GuruMonitorActor`** remains for poll-based shadow, fallback, recovery, and gap-fill REST reads.

## B. Boundaries

**Belongs here:** Anything that talks to Polymarket **data** endpoints or prepares `GuruTradeSignal` for publication. Polling timers and backoff logging for the actor.

**Does not belong here:** Order placement (`execution/`), portfolio risk (`risk/`), or orchestration (`strategy/`). Do not embed “copy logic” beyond dedup + parse.

## C. Internal structure (implemented)

| File | Role |
|------|------|
| `data_api_client.py` | HTTP client: `get_trades`, **`get_user_trade_activity`** (`/activity`), 429 backoff. |
| `guru_parse.py` | Map trade/activity rows → `GuruTradeSignal`; **`ingest_source_trade_id`** (`transactionHash:asset` when tx present); `api_timestamp_to_ms`. |
| `guru_watermark.py` | `GuruWatermarkStore` — persisted `last_seen_ts_ms`. |
| `guru_dedup.py` | Secondary dedup store (file-backed LRU); shared by poll + stream. |
| `guru_monitor.py` | **`GuruMonitorActor`**: incremental poll, resilient errors; publishes via **`GuruSignalPipeline`** when ingest state allows. |
| `guru_stream_actor.py` | **`GuruStreamActor`**: RTDS worker + queue drain; publish or shadow `would_emit` per **`GuruIngestRuntimeState`**; reconnect/stall → optional fallback. |
| `guru_rtds_ws.py` / `guru_rtds_parse.py` | WebSocket client + payload → `GuruTradeSignal` / `proxyWallet` match. |
| `guru_ingest_pipeline.py` | **`GuruSignalPipeline`**: dedup, bus publish, **`guru_signal_emitted`** log (`source=rtds` / `poll` / `gap_fill`). |
| `guru_ingest_state.py` | **`GuruIngestRuntimeState`**: `poll_only` / `rtds_shadow` / `rtds_primary` behavior. |
| `guru_gap_fill.py` | REST gap-fill after reconnect (pipeline publish). |
| `allowlist.py` | Allowlist helpers (validation tooling). |
| `resolution.py` | Market/token resolution (used by scripts / validation). |
| `book_check.py` | Order book checks as needed for tooling. |

## D. Main interactions

- **core:** emits `GuruTradeSignal`.
- **strategy:** `CopyStrategy` subscribes to `GURU_TRADE_TOPIC` (see `guru_monitor.py`).
- **runtime:** `guru_compose` constructs **`GuruMonitorActor`** and optionally **`GuruStreamActor`** from strategy + runtime settings.

## E. Status

**Production-shaped:** **C1** — RTDS primary ingestion + shared dedup/watermark + poll fallback/shadow + gap-fill; incremental poll unchanged for those paths.

**Tooling:** resolution / allowlist / book check; `get_trades` remains for non-follower tools; **`scripts/spike_rtds_activity.py`** for wallet validation.

**Docs:** [OPERATIONS.md](../../OPERATIONS.md) § Guru ingestion (C1), [Implementation/plan_C1_Time-to-Follow.md](../../Implementation/plan_C1_Time-to-Follow.md).

## F. Extension guidance

- New data sources should still **publish the same `GuruTradeSignal` type** on the same topic (or a documented new topic + new strategy subscriber).
- Keep long-running I/O and sleeps **inside the actor** or client, not in `strategy`.
- For guru discovery / ranking, prefer a **separate** pipeline that eventually configures which wallet this actor polls — avoid bloating `GuruMonitorActor` with ranking logic.
