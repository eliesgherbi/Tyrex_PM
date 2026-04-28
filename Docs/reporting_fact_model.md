# Reporting fact model

**Hub:** [README.md](README.md) · **Architecture:** [Architecture.md](Architecture.md) · **Operations:** [OPERATIONS.md](OPERATIONS.md)

`facts.jsonl` is **the** operator surface. Every meaningful decision, lifecycle transition, and reconcile outcome is one JSON line. Logs are debug detail; facts are the audit trail.

---

## 1. Envelope

Every line is the dict produced by `reporting/facts.py::make_fact`:

```json
{
  "schema_version": 2,
  "fact_type":      "<one of FACT_TYPE_* in reporting/schema_v2.py>",
  "ts":             "2026-04-17T14:23:11.412+00:00",
  "run_id":         "<uuid or --run-name>",
  "correlation_id": "<dedup_key | client_order_id | venue_order_id | null>",
  "payload":        { ... fact-specific ... }
}
```

- `schema_version=2` is the current contract; bump it when payload semantics change incompatibly.
- `ts` is UTC ISO from `core.time.utc_now()`.
- `correlation_id` is what you grep on to follow a single chain (guru_signal → strategy_skip / intent → risk_decision → oms_submit → oms_result).
- All `Decimal` values are stringified; USD totals in `risk_decision` evidence are quantized to 6 decimals via `risk/evidence_format.py::s_usd`.

---

## 2. Fact catalog

Constants live in `src/tyrex_pm/reporting/schema_v2.py`. Adding a fact starts there.

| `fact_type` | Producer | Correlation id | Purpose |
|-------------|----------|----------------|---------|
| `health` | `runtime/app.py`, `runtime/live_supervisor.py` | none | Process / heartbeat / WS state transitions (`started`, `stopped`, `heartbeat_unhealthy`, `user_ws_stale`, ...) |
| `guru_poll` | `runtime/app.py` | none | Each Data API tick: page count, fetched, kept-after-watermark, errors |
| `guru_signal` | `pipeline.process_new_guru_signals` | `dedup_key` | Normalized guru trade |
| `strategy_skip` | `pipeline.process_new_guru_signals` | `dedup_key` | Strategy filtered out the signal (token allowlist, min notional, dust, no inventory, ...) |
| `intent_created` | `pipeline.process_new_guru_signals` | `dedup_key` | Strategy emitted an `EnterIntent` / `ExitIntent` / `ReduceIntent` / `CancelIntent` |
| `risk_decision` | `risk/engine.evaluate_intent` | `dedup_key` | Approve / deny + per-policy evidence (the dense fact) |
| `oms_submit` | `pipeline` | `dedup_key` | Successful submit ack with raw `oms_result` |
| `oms_reject` | `pipeline` | `dedup_key` | Submit failed (HTTP error, duplicate fingerprint) |
| `oms_cancel` | `pipeline` | `dedup_key` | Cancel attempt + result |
| `oms_result` | reserved | — | Reserved for richer post-submit lifecycle (currently `oms_submit/cancel` carry the result inline) |
| `reconcile` | `pipeline.reconcile_coordinator` | none | Drift flags, severity, repair / adoption decisions, suppressed REST ids |
| `wallet_sync` | `pipeline.emit_wallet_sync` | none | Snapshot of balance, allowance, position count, open-order count after a REST refresh |
| `live_attest` | `runtime/live_attest.py` | none | Attestation milestones (`auth_ok`, `submit_ok`, `cancel_ok`, ...) plus V2 evidence phases: `v2_environment` (SDK module + version, host, chain, signature_type, builder code presence), `collateral_check` (post-bootstrap pUSD balance + per-exchange allowances), `market_info` (resolved tick/min-size/neg-risk/fee/outcomes), and `outcome_validation` on `complete` (post-cancel order id resolution + outcomes map). |

---

## 3. Key payloads

### `risk_decision`

```json
{
  "approved": false,
  "reason_codes": ["below_venue_min_size"],
  "detail": "...",
  "venue_min_size_final_size":      "4.54",
  "venue_min_size_default":         "5",
  "venue_min_size_policy":          "deny",
  "venue_min_size_final_notional_usd": "1.812456",
  ...                              // additional per-policy evidence keys
}
```

Reason codes are stable strings from `core/reason_codes.py`. Always extend that file rather than inventing new strings inline.

### `reconcile`

```json
{
  "drift_flags":           ["venue_open_not_tracked_locally"],
  "blocking_drift_flags":  [],
  "reconcile_blocks_live": false,
  "reconcile_severity":    "info",
  "drift_flag_counts":     {"venue_open_not_tracked_locally": 1},
  "venue_user_ws_stale":   false,
  "venue_restart_suspected": false,
  "submit_grace_s":        15.0,
  "unknown_terminal_timeout_s": 60.0,
  "adoption_grace_s":      5.0,
  "venue_adoption_decisions": [
    {"venue_order_id": "0xabc...", "decision": "non_blocking_within_adoption_grace",
     "candidate_local_cid": "...", "match_basis": "token+side+size+price",
     "age_s": 1.2}
  ],
  "tombstoned_rest_vids": ["0xdef..."]
}
```

**Dedup**: consecutive reconciles with the same `_reconcile_signature` are dropped (see `pipeline._reconcile_signature`). The signature includes all drift flags + severity + suppressed-rest-ids + decision counts; per-row decision payloads are not inside the signature, so a *new* row still emits.

### `wallet_sync`

```json
{
  "wallet_usdc_balance":   "532.412304",
  "wallet_usdc_allowance": "1000000000000000000.000000",
  "last_sync_ts":          "2026-04-17T14:23:11.111+00:00",
  "last_positions_sync_ts":"2026-04-17T14:23:11.290+00:00",
  "position_count":        2,
  "open_order_count":      1,
  "marks_present_count":   2,
  "marks_missing_count":   0
}
```

**Dedup**: the signature deliberately **excludes** both `last_sync_ts` and `last_positions_sync_ts` (they advance on every refresh; including them defeated dedup as observed in `live_tes_700`). Two refreshes that change nothing actionable produce one fact.

### `oms_submit` / `oms_reject`

```json
{
  "client_order_id":  "...",
  "oms_result":       "<raw venue JSON or shadow ack string>",
  "status_code":      400,                       // reject only
  "error_msg":        "not enough balance / allowance",
  "venue_restart_suspected": false,              // true on HTTP 425

  // V2 / Phase 5 — tick quantization evidence (always present; only the
  // ``_applied=true`` branch carries the rest of the keys).
  "tick_quantize_applied":   true,
  "tick_size":               "0.01",
  "original_price":          "0.5523",
  "quantized_price":         "0.55",
  "price_was_quantized":     true
}
```

---

## 4. Other run artifacts

`var/reporting/runs/<run_id>/` also contains:

- `manifest.json` — parsed `AppConfig.raw`, git SHA, scenario name, args.
- `run_summary.json` — generated by `reporting/summarize.py` after the loop exits: counts per `fact_type`, top reason codes, last reconcile severity, runtime seconds.

---

## 5. How to add a fact

1. Add `FACT_TYPE_<NAME>` in `reporting/schema_v2.py`.
2. Decide whether it should dedup; if so, write the signature alongside its emitter and persist `last_<name>_signature` on `RuntimeCoordinator`.
3. Use `make_fact(FACT_TYPE_<NAME>, run_id, payload, correlation_id=...)` and `JsonlSink.write(...)`.
4. Quantize Decimal USD values via `s_usd()` (or `s_usd_map()` for dicts).
5. Update §2 above and add at least one golden test asserting the payload shape.
6. Never log the same information twice (logs vs facts) — pick the operator surface that matters and keep the other quiet.

---

## 6. Reading patterns

```bash
# Quick counts
jq -r '.fact_type' var/reporting/runs/<id>/facts.jsonl | sort | uniq -c

# Why was a guru signal dropped?
jq -c 'select(.fact_type=="strategy_skip") | {ts, dedup_key: .correlation_id, reason: .payload.reason}' \
  var/reporting/runs/<id>/facts.jsonl

# All denied risk decisions and their codes
jq -c 'select(.fact_type=="risk_decision" and .payload.approved==false)
       | {ts, codes: .payload.reason_codes, cid: .correlation_id}' \
  var/reporting/runs/<id>/facts.jsonl

# Reconciles that actually blocked
jq -c 'select(.fact_type=="reconcile" and .payload.reconcile_blocks_live==true) | .payload.blocking_drift_flags' \
  var/reporting/runs/<id>/facts.jsonl
```

The `correlation_id` is the cheap join key; a single guru trade typically produces one of each: `guru_signal`, `intent_created`, `risk_decision`, then `oms_submit` / `oms_reject` / `oms_cancel` (or one `strategy_skip`).
