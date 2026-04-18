# `reporting/`

Operator surface. Every meaningful decision is one JSON line in `facts.jsonl`. Logs are debug detail; facts are the audit trail.

## Files

| File | Purpose |
|------|---------|
| `schema_v2.py` | `FACT_SCHEMA_VERSION = 2` and `FACT_TYPE_*` constants. **Adding a fact starts here** |
| `facts.py` | `make_fact(fact_type, run_id, payload, *, correlation_id=None)` — the one envelope builder |
| `sinks/jsonl.py` | `JsonlSink` — context-managed append-only writer, fsync on close. Tests must use it as a context manager |
| `summarize.py` | `summarize_run(facts_path) -> dict` — counts per fact type, top reason codes, last reconcile severity. Called at end of `cmd_run` to write `run_summary.json` |
| `oms_payload.py` | Helpers for normalizing OMS response strings into facts |

## Envelope

```json
{
  "schema_version": 2,
  "fact_type":      "<one of FACT_TYPE_*>",
  "ts":             "<UTC ISO from core.time.utc_now>",
  "run_id":         "<RunId>",
  "correlation_id": "<dedup_key | client_order_id | venue_order_id | null>",
  "payload":        { ... }
}
```

## Fact catalog

See [../../reporting_fact_model.md §2](../../reporting_fact_model.md#2-fact-catalog) for the full table.

| Fact | Producer (module) |
|------|--------------------|
| `health` | `runtime/app.py`, `runtime/live_supervisor.py` |
| `guru_poll` | `runtime/app.py` |
| `guru_signal` / `strategy_skip` / `intent_created` / `risk_decision` | `runtime/pipeline.py` |
| `oms_submit` / `oms_reject` / `oms_cancel` | `runtime/pipeline.py` |
| `reconcile` | `runtime/pipeline.reconcile_coordinator` |
| `wallet_sync` | `runtime/pipeline.emit_wallet_sync` |
| `live_attest` | `runtime/live_attest.py` |

## Dedup signatures

Two facts have producer-side dedup so tight REST/WS bursts don't flood the file:

| Fact | Signature lives in | Notes |
|------|--------------------|-------|
| `reconcile` | `pipeline._reconcile_signature` | Drift flags + severity + suppressed-rest-ids + decision counts |
| `wallet_sync` | `pipeline._wallet_sync_signature` | Balance + allowance + counts; **excludes** `last_sync_ts` and `last_positions_sync_ts` (regression: see `live_tes_700`) |

Both signatures persist on `RuntimeCoordinator` (`last_reconcile_signature`, `last_wallet_sync_signature`).

## Adding a fact

1. Add `FACT_TYPE_<NAME>` in `schema_v2.py`.
2. Build the payload in the producer; quantize Decimal USD values via `risk.evidence_format.s_usd` (or `s_usd_map`).
3. Decide whether to dedup (most operator-action-relevant facts should). If yes: write a `_<name>_signature` helper next to the emitter and persist `last_<name>_signature` on the coordinator.
4. `sink.write(make_fact(FACT_TYPE_<NAME>, run_id, payload, correlation_id=...))`.
5. Update [../../reporting_fact_model.md](../../reporting_fact_model.md) and add at least one golden test.

## Conventions

- **Never** invent free-form fact types in producers — declare in `schema_v2.py` first.
- **Never** log information that you also emit as a fact; pick one operator surface per signal.
- All money values stringified at emission via `s_usd()`; never re-quantize in business code.
- `correlation_id` is your cheap join key — set it whenever a fact belongs to a chain (`dedup_key` for guru-driven flows, `client_order_id` for OMS, `venue_order_id` for cancel results).
