# Module: `tyrex_pm.data`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md)

## A. Role

**External reads** and **normalization** for Polymarket: HTTP Data API (**`/activity` TRADE** feed for the live follower, `/trades` where still useful for tools), guru row parsing, **incremental watermark** (`guru_watermark.py`), optional dedup LRU, and **`GuruMonitorActor`**. Market tooling (allowlist, resolution, book check) lives here for scripts and validation — **not** used for unbounded historical crawls on the copy path.

## B. Boundaries

**Belongs here:** Anything that talks to Polymarket **data** endpoints or prepares `GuruTradeSignal` for publication. Polling timers and backoff logging for the actor.

**Does not belong here:** Order placement (`execution/`), portfolio risk (`risk/`), or orchestration (`strategy/`). Do not embed “copy logic” beyond dedup + parse.

## C. Internal structure (implemented)

| File | Role |
|------|------|
| `data_api_client.py` | HTTP client: `get_trades`, **`get_user_trade_activity`** (`/activity`), 429 backoff. |
| `guru_parse.py` | Map trade/activity rows → `GuruTradeSignal`; `api_timestamp_to_ms`. |
| `guru_watermark.py` | `GuruWatermarkStore` — persisted `last_seen_ts_ms`. |
| `guru_dedup.py` | Secondary dedup store (file-backed LRU). |
| `guru_monitor.py` | `GuruMonitorActor`: incremental poll, resilient errors, `GURU_TRADE_TOPIC`. |
| `allowlist.py` | Allowlist helpers (validation tooling). |
| `resolution.py` | Market/token resolution (used by scripts / validation). |
| `book_check.py` | Order book checks as needed for tooling. |

## D. Main interactions

- **core:** emits `GuruTradeSignal`.
- **strategy:** `CopyStrategy` subscribes to `GURU_TRADE_TOPIC` (see `guru_monitor.py`).
- **runtime:** `guru_compose` constructs `GuruMonitorActor` from strategy + runtime settings.

## E. Status

**Production-shaped:** incremental guru poll (watermark + bounded pages + dedup safety net).

**Tooling:** resolution / allowlist / book check; `get_trades` remains for non-follower tools.

## F. Extension guidance

- New data sources should still **publish the same `GuruTradeSignal` type** on the same topic (or a documented new topic + new strategy subscriber).
- Keep long-running I/O and sleeps **inside the actor** or client, not in `strategy`.
- For guru discovery / ranking, prefer a **separate** pipeline that eventually configures which wallet this actor polls — avoid bloating `GuruMonitorActor` with ranking logic.
