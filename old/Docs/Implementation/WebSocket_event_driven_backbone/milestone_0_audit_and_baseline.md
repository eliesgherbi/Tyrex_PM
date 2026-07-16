# Milestone 0 — Audit & Baseline

**Status:** Complete (documentation) — dual readiness below  
**Audit date:** 2026-06-29  
**Git at audit:** `ac5191d76aa26eea0c93cf8f004c0ba3e6cdf44e`  
**Updated after Group A:** 2026-06-29 — WS spike + M1/M2/M5 foundation implemented

## Objective

Produce a **checked, file-by-file TODO list** and a **quantitative baseline** from existing live paired-binary runs so Phase 2 improvements can be measured objectively. **Freeze canonical baseline run IDs** before implementation starts.

## Deliverables

| Deliverable | Location | Status |
|-------------|----------|--------|
| Frozen baseline run IDs + config hashes | [`baseline_runs.yaml`](baseline_runs.yaml) | Done — 1/3 facts paths verified on disk |
| File-by-file audit table | [`audit_checklist.md`](audit_checklist.md) | Done — all grep-hit paths |
| Baseline metrics (3 control runs) | [`baseline_metrics.md`](baseline_metrics.md) | Done — 1 verified, 2 reconstructed |
| Sign-off recommendation | This doc | See dual readiness below |

## Readiness status (dual track)

| Status | Meaning |
|--------|---------|
| **`NOT_READY_FOR_M9_BASELINE_COMPARISON`** | Two of three control `facts.jsonl` paths missing locally. **Hard blocker for M9 before/after report.** |
| **`READY_FOR_M1_FOUNDATION_WORK`** | WS fixture spike complete; Group A (M1 shadow + M2 store v2 + M5 FeatureBuilder v0) implemented. Does **not** imply WS-primary or strategy changes. |

### Tracked task (M9 blocker — not M1/M2/M5)

```text
Restore or re-run paired_binary_live_1782741234 and paired_binary_live_1782737121 before M9.
Update baseline_runs.yaml if run IDs change; re-parse baseline_metrics.md from disk.
```

## Documentation sanity check (pre-audit)

| Check | Result |
|-------|--------|
| `baseline_runs.yaml` exists | Yes |
| Three run IDs filled | Yes — unchanged canonical IDs |
| Facts paths under `var/reporting/runs/` | **1/3 present** (`1782742788` only) |
| `strategy_config_sha256` | `cf2b1b7c2dbe15d99beb494c9bb7f1866214862d0987fad24891b794f170d311` |
| `scenario_config_sha256` | `628d30b601a95627f794ea4017449c23bdeb73ac272c686a95ee2567ef6bf41b` |
| `git_commit_at_audit` | `ac5191d76aa26eea0c93cf8f004c0ba3e6cdf44e` |

**Substitution note:** Run IDs were **not** changed. Two artifact directories are missing from this workspace (`var/` is gitignored; likely deleted after prior forensics). Metrics for missing runs are **RECONSTRUCTED** from verified live reviews in the same project session — see [`baseline_metrics.md`](baseline_metrics.md). Re-verify from disk before M1 sign-off.

## Why this milestone exists

Phase 2 targets WS-authoritative market data while keeping paired-binary strategy logic unchanged. M0 establishes:

1. An authoritative inventory of every file touching market data, freshness, and paired-binary decisions.
2. Frozen control run IDs and config hashes for the M9 before/after report.
3. Quantitative REST-era baseline (book age, FAK rejects, PnL, latency gaps).

## Current codebase state (confirmed)

| Area | Fact |
|------|------|
| Live books | REST poll authoritative; WS shadow optional (`TYREX_MARKET_WS_SHADOW=1`) |
| WS parsers + transport | `ingestion/market_stream.py` + `ingestion/market_ws_ingest.py` (shadow only) |
| User WS | `ingestion/user_stream.py` — **live** |
| Store | `state/market_store.py` — v2 `capture()` / source tags; REST authoritative |
| FeatureBuilder | `market_data/features.py` — v0 only; **not wired to strategy decisions** |
| Paired-binary | Poll loop `tick_interval_s=0.2` in `paired_binary_run.py`; touch bid triggers |
| Planner | FAK uses `is_stale` + `estimate_fill_price` in `execution/planner.py` |
| Quality gate | **Does not exist** — binary `is_stale` only |
| Latency | Activation `LatencyTracker` only; stop/exit chain null in baseline runs |

Full audit rows → [`audit_checklist.md`](audit_checklist.md).

## Frozen baseline control runs

```yaml
control_success_run_id: paired_binary_live_1782741234      # NO SL + YES TP; +$1.30 PnL
control_bad_stop_run_id: paired_binary_live_1782742788     # YES SL; 2 FAK rejects; PnL unavailable
control_activation_abort_run_id: paired_binary_live_1782737121  # YES fill; NO reject; saga abort
```

Frozen config: `config/strategies/paired_binary.yaml` + `config/scenarios/live_paired_binary_tiny.yaml` (hashes in `baseline_runs.yaml`).

Key strategy parameters for M9 freeze:

```text
position_size: 5
pair_stop_loss_pct: 0.09
pair_take_profit_pct: 0.30
entry_order_style / exit_order_style: FAK
max_runtime_s: 300
max_holding_time_s: 299
tick_interval_s: 0.2
max_book_age_s: 5 (scenario)
```

## Baseline highlights (REST era)

| Run | Key Phase 2 signal |
|-----|-------------------|
| Success `1782741234` | Full cycle; clean FAK exits; +$1.30 PnL; latency chain incomplete |
| Bad stop `1782742788` | **4796ms** book age at activation; YES SL 6s after arm; **2 FAK rejects**; fill 0.33 vs trigger bid 0.37 |
| Abort `1782737121` | Capital gap on leg 2 (not book freshness); ~17s FAILED |

Details → [`baseline_metrics.md`](baseline_metrics.md).

## Confirmed current behavior vs planned changes

See [`audit_checklist.md`](audit_checklist.md) § "Confirmed current behavior vs planned changes".

**Phase 2 principle (unchanged):** Do not change paired-binary strategy logic until after M9. Infrastructure only M0–M8.

## Open questions

| # | Question | Milestone |
|---|----------|-----------|
| 1 | Market WS URL, subscribe payload, field names | **Resolved** — see [`ws_fixture_spike.md`](ws_fixture_spike.md) |
| 2 | Sequence/hash in WS payloads? | **Resolved** — `hash` present; numeric sequence absent |
| 3 | `crypto_5m` profile binding: strategy YAML vs market metadata | M3 |
| 4 | `allow_rest_recovery_for_exit: false` for first prod run? | Before M8 |
| 5 | Store full book or cap at top N on apply? | **Resolved in M2** — default top 5 via `store_top_n_levels` |
| 6 | `exchange_ts` field name | **Resolved** — `timestamp` (ms) |
| 7 | Restore 2 missing baseline artifact dirs | **Before M9** (hard blocker for comparison) |

## Acceptance criteria

- [x] Every grep-hit file has an audit table row
- [x] `baseline_runs.yaml` filled with three control run IDs, paths, hashes, git commit
- [x] Baseline metrics documented for all three control runs (with provenance)
- [x] Open questions assigned to future milestones
- [ ] **All three facts paths verified on disk** — **FAIL** (blocker)
- [x] Clear READY / NOT_READY recommendation

## M0 sign-off recommendation

### `NOT_READY_FOR_M9_BASELINE_COMPARISON`

Missing control run artifacts block the M9 before/after gate only.

### `READY_FOR_M1_FOUNDATION_WORK`

WS fixture spike documented; Group A foundation (M1/M2/M5) implemented with tests. Group B (M3–M8) not started.

**Group B blockers:**

1. Restore/re-run missing baseline runs before M9.
2. M3 DataQualityGate before any WS-primary entry gating.
3. M8 cutover explicitly not started.

## Not in scope

- Code changes
- WS implementation
- Strategy parameter changes

## Definition of done

Audit table complete; `baseline_runs.yaml` committed; baseline metrics written; dual readiness recorded. **Group B (M3+) not started.**
