# Phase 2B Open Follow-ups

## M2B.0-B follow-up — REST recovery event path

Status: open  
Blocking: not blocking M2B.0-C or M2B.1-A; must be resolved before recorder maturity / M2B.1-B acceptance.

Details:

- `bootstrap_market_store_from_rest` supports event-backbone projection when kwargs are passed.
- Runtime call-site wiring is deferred.
- `REST_RECOVERY_USED` canonical event is not emitted yet.
- Current live REST bootstrap remains legacy unless kwargs are explicitly passed.

Required future resolution:

- Wire `event_backbone_*` into the appropriate market-data runtime / gap-recovery caller.
- Emit `REST_RECOVERY_USED` when event backbone is enabled and REST recovery is used.
- Preserve flag-off legacy behavior.

**M2B.1-B note:** Record mode remains WS-only. `REST_RECOVERY_USED` emission requires wiring in `runtime/market_data_runtime.py` and `runtime/app.py` live paths (forbidden in M2B.1-B). Remains open; blocking before M2B.3/M2B.5 replay-quality acceptance.

---

## M2B.0-C follow-up — E2E shadow parity

Status: open  
Blocking: not blocking M2B.1-A; should be resolved before M2B.5 replay validation.

Details:

- M2B.0-C unit/runtime tests prove optional field presence/absence.
- No full end-to-end shadow run comparing fact type sequence flag-off vs flag-on was run.
- Future resolution: add a small shadow parity fixture or replay-backed diff proving only optional fields differ.

---

## M2B.0-C follow-up — correlation context technical debt

Status: open  
Blocking: not blocking M2B.1-A.

Details:

- Survival correlation currently uses `state._event_correlation_context`.
- Accepted as minimal pass-through.
- Future resolution: replace with explicit typed context if/when monitor/survival interfaces are cleaned up.

---

## M2B.1-A follow-up — graceful manifest finalization

Status: resolved in M2B.1-B implementation  
Blocking: n/a

Details:

- `EventSink.stop()` sets `recording_ended_ts` on graceful shutdown.
- `cmd_record` catches `asyncio.CancelledError` and `KeyboardInterrupt` (via `app.py`) so cleanup runs.
- `tests/test_record_heartbeat.py::test_graceful_stop_sets_recording_ended_ts` proves manifest finalization.

---

## M2B.1-B follow-up — coverage_report.json from cleared session

Status: resolved in M2B.1-B recorder follow-up  
Blocking: was blocking 24h acceptance run

Details:

- `build_coverage_report()` now reads persisted `manifest.json` files from the day directory.
- Final heartbeat on shutdown uses manifest totals after `session.markets` is cleared.
- Discovery skips expired markets (`skip_expired_markets`, default true).
- Future markets deferred until `pre_open_recording_lead_s` before window start (default 60s).

---

## M2B.3-A follow-up — RTDS Chainlink subscription bug (`msg_type` vs `type`)

Status: **resolved** (M2B.3-A RTDS fix)  
Blocking: was blocking M2B.4 analysis on real price-to-beat data

Details:

- Rich recording on 2026-07-05 showed `external/polymarket_rtds_chainlink/manifest.json` with empty segments.
- Root cause: `reference_prices.py` sent `msg_type` instead of provider-required `type` in subscription payload.
- Fix: use `build_reference_price_subscription()` with `"type": "*"` and JSON-string filters.
- Normalizer and PriceToBeatTracker behaved correctly (`missing` status when no ticks).

---

## M2B.3-A follow-up — Gamma post-settlement priceToBeat backfill

Status: open / deferred  
Blocking: not blocking M2B.4 planning

Details:

- Gamma `eventMetadata.priceToBeat` appears post-settlement (~5–7 min after close).
- M2B.3-A derives live price-to-beat from RTDS Chainlink ticks at `event_start_ts`.
- Optional future: explicit post-settlement backfill job for forensic validation.

---

## M2B.2 follow-up — record-mode WS reconnect logging

Status: open / optional  
Blocking: not blocking M2B.3

Details:

- Polymarket WS reconnect traceback is expected and non-fatal in record mode.
- Consider downgrading `log.exception` to `log.warning` to reduce console noise.
- Behavior is already correct; cosmetic only.

---

## M2B.4 follow-up — latency prior from live facts

Status: open / spec-defined  
Blocking: Notebook 02 final protection-distance candidates (not M2B.4 implementation start)

Details:

- `research/lib/latency.py` (deferred) derives prior from live run facts.
- Prefer M2B.0-C event/fact correlation fields; older runs may yield low-confidence partial priors.
- Output artifact: `research/output/m2b4/latency_prior.json`.
- Notebook 02 debug mode allowed without prior; must set `insufficient_latency_prior: true` and skip final candidates.

---

## M2B.4 follow-up — stable parameter claims vs M2B.1-B

Status: open / policy  
Blocking: **stable** (non-provisional) M2B.4 parameter freeze only — does not block notebook implementation

Details:

- All M2B.4 outputs remain provisional until ≥1 week or ≥500 clean markets **and** M2B.1-B 24h/long-run recorder gate accepted.
- Per-bucket minimums additionally gate numeric candidates within notebooks.

---

## M2B.1-B follow-up — 24h recorder ops acceptance

Status: running / pending report  
Blocking: full M2B.1-B acceptance

Details:

- M2B.1-B implementation and short smoke are accepted.
- A separate 24h unattended recorder validation is being run by the user.
- Full acceptance requires ≥95% BTC 5m market coverage and healthy coverage/heartbeat/manifests.

Required report:

- `recording_started_ts` / `recording_ended_ts`
- `expected_market_count` / `recorded_market_count` / `coverage_pct`
- total events / `dropped_events`
- `stall_detected` status
- per-market manifest summary
- any gaps or failures

---

## M2B.1-A precondition — paired_binary_run.py must not be touched

Status: active guardrail  
Blocking: M2B.1-B and beyond.

Details:

- Record mode must not modify `runtime/paired_binary_run.py`.
- `tap_in_live` must default false; live behavior unchanged when false.
