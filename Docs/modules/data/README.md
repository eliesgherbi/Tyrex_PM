# Module: `tyrex_pm.data`

[← Back to module index](../README.md) · [Architecture](../../Architecture.md)

## A. Role

**External reads** and **normalization** for Polymarket: HTTP Data API, guru trade parsing, deduplication, and the **`GuruMonitorActor`** that feeds the Nautilus message bus. Market tooling (allowlist, resolution, book check) lives here for reuse by scripts and validation.

## B. Boundaries

**Belongs here:** Anything that talks to Polymarket **data** endpoints or prepares `GuruTradeSignal` for publication. Polling timers and backoff logging for the actor.

**Does not belong here:** Order placement (`execution/`), portfolio risk (`risk/`), or orchestration (`strategy/`). Do not embed “copy logic” beyond dedup + parse.

## C. Internal structure (implemented)

| File | Role |
|------|------|
| `data_api_client.py` | HTTP client for trades (rate-limit aware / backoff hooks). |
| `guru_parse.py` | Map API row → `GuruTradeSignal`. |
| `guru_dedup.py` | Persistent dedup store (file-backed). |
| `guru_monitor.py` | `GuruMonitorActor`, `GuruMonitorActorConfig`, topic constant `GURU_TRADE_TOPIC`. |
| `allowlist.py` | Allowlist helpers (validation tooling). |
| `resolution.py` | Market/token resolution (used by scripts / validation). |
| `book_check.py` | Order book checks as needed for tooling. |

## D. Main interactions

- **core:** emits `GuruTradeSignal`.
- **strategy:** `CopyStrategy` subscribes to `GURU_TRADE_TOPIC` (see `guru_monitor.py`).
- **runtime:** `guru_compose` constructs `GuruMonitorActor` from strategy + runtime settings.

## E. Status

**Production-shaped:** guru poll actor + client + dedup.

**Tooling:** resolution / allowlist / book check support ops and tests, not the hot copy loop.

## F. Extension guidance

- New data sources should still **publish the same `GuruTradeSignal` type** on the same topic (or a documented new topic + new strategy subscriber).
- Keep long-running I/O and sleeps **inside the actor** or client, not in `strategy`.
- For guru discovery / ranking, prefer a **separate** pipeline that eventually configures which wallet this actor polls — avoid bloating `GuruMonitorActor` with ranking logic.
