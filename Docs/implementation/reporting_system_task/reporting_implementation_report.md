# Reporting system — implementation report

**Date:** 2026-07-27  
**Branch:** `rest_project`  
**Initial / final HEAD:** `18235c84c99bb0066bcc5271bd761fb191b0548c` (unchanged; no commit/push)  
**Plan:** `reporting_implementation_plan.md` (owner-authorized one-shot M1–M8)

## Pre-existing dirty files preserved

Unrelated docs already modified before this task were left intact (not reset), including prior edits under `Docs/latest/**` and N7 readiness notes. Historical `var/` evidence was not rewritten or cleaned.

## Milestone completion

| Milestone | Status | Notes |
|-----------|--------|-------|
| M1 | COMPLETE | Compact `tyrex_pm.reporting` package (8 modules) |
| M2 | COMPLETE | Lanes, accumulators-before-sampling, checkpoints, health, ack |
| M3 | COMPLETE | `strategies/z_gap/reporting.py`; fake second-strategy test; docs |
| M4 | COMPLETE | OBSERVE/SHADOW → `var/runs/...`; host `JsonlFactSink` removed |
| M5 | COMPLETE | LIVE/N7 reporter; `_capture_intents` full diagnostics; pre-mutation barrier |
| M6 | COMPLETE | `config/reporting/{full,minimal}.yaml`; CLI `--reporting` |
| M7 | COMPLETE | `var/runtime_state` migration; N1/N2/N3 paths; N4–N6 retired; R7b reporter; test isolation |
| M8 | COMPLETE | Residual cleanup; docs; this report; full suite |

**Verdict:** `M1–M8 COMPLETE`

## Core architecture delivered

- One `ReportingPort` / `RunReporter` for OBSERVE / SHADOW / LIVE
- Two lanes: critical/audit vs optional analytics
- `SummaryAccumulators` only in `summary.py`; updated before sampling/drop
- Strategy diagnostics owned by strategy (`z_gap/reporting.py`); reporting core has no Z-Gap imports
- Atomic JSON replace for manifest/summary checkpoints
- Health: `HEALTHY` | `DEGRADED_ANALYTICS` | `CRITICAL_AUDIT_FAILURE`
- No dual-write, feature flag, or legacy fallback

## Integrations

| Mode | Path | Label |
|------|------|-------|
| OBSERVE | `var/runs/<strategy>/<run_id>/` | `observed_only` |
| SHADOW | same | `simulated` (+ assumptions) |
| LIVE / N7 / YAML live | `var/runs/z_gap/<run_id>/` | `real` (fake transport flagged in manifest) |

Primary artifacts: `manifest.json`, `run_summary.json`, `audit_events.jsonl`, `analytics_events.jsonl`, optional `attachments/`.

## LIVE pre-mutation barrier

For every exposure-increasing (`BUY`) submit in `LiveOMS` when a reporter is attached:

1. `allows_new_exposure` check  
2. construct pre-mutation critical event  
3. durable `persist_critical` ack  
4. only then call transport  
5. persist venue result; on post-mutation report failure → `CRITICAL_AUDIT_FAILURE`

Under `CRITICAL_AUDIT_FAILURE`: new BUY blocked; cancel and SELL/reduction remain permitted; reconciliation remains callable. Proven in `tests/test_reporting_live_barrier.py` with `FakeTransport` only.

## Checkpoint / stale-run rules (selected)

- Auto-checkpoint every **25** events (`checkpoint_every_n_events`)
- Stale `RUNNING` helper: `classify_stale_running(..., stale_after_s=300)` in `summary.py`
- Crash-safe path: temp write → flush/fsync as implemented in `atomic_write_json` → replace

## Reporting profiles / CLI

- `config/reporting/full.yaml` (default) — per-evaluation analytics on; debug/raw off  
- `config/reporting/minimal.yaml` — optional analytics reduced; aggregate counters retained  
- `--reporting` on `observe`, `shadow`, `run`, `n7-live`  
- Mandatory audit/manifest/summary cannot be disabled (validated in config loader)

## Runtime-state and legacy-path migration

- Active state root: `var/runtime_state/` (`r7_paths.STATE_ROOT`)
- `migrate_state_to_runtime_state`: copy without overwrite; conflict → preserve both + `refused_live`
- N1 → `var/recordings/n1/`; N2 → `var/runs/_ops/n2_smoke/`; N3 validation → `_ops/n3_validation`
- N4–N6 tool scripts deleted; `RETIRED.md` left; `N4ObserveRuntime` / `N6LiveHost` kept for composition
- R7b facts → common reporter under `var/runs/_ops/r7b/<run_id>/`
- Dead `live-once` CLI surface removed (only `r7b-live-once` remains)
- Deleted unused `reporting/jsonl.py`, `facts.py`, `serialize.py`

## Performance comparison (F3 fixture OBSERVE)

| Metric | Pre-M4 JsonlFactSink baseline | Post cutover (warmed) |
|--------|-------------------------------|------------------------|
| Wall run | ≈ 0.007169 s | ≈ 0.045397 s |
| Event/fact count | 76 | 70 |
| Avg / event | ≈ 9.4e-5 s | ≈ 6.5e-4 s |

New path is slower (batched writer + dual lanes + atomic checkpoints). Functional acceptance only; no invented hard latency SLA. Critical-lane ack behavior covered by barrier tests.

## Controlled E2E evidence (temporary)

Under `var/tmp_e2e_evidence/` (not for commit):

| Run | Location | Shape OK | Label |
|-----|----------|----------|-------|
| OBSERVE | `.../observe/` | yes | `observed_only` |
| SHADOW | `.../shadow/` | yes | `simulated` |
| Fake LIVE | `.../fake_live/` | yes | `real` + `fake_transport: true` |

## Residual searches (active `src/`)

- No host `JsonlFactSink` construction; raise-only residual mention in observe_host
- No primary `n7_oneshot_report.json` writer
- No Z-Gap imports inside `tyrex_pm.reporting`
- `var/state` remains only as `LEGACY_STATE_ROOT` for migration/conflict detection
- Historical docs under `Docs/implementation/z_gap_production_readiness/**` may still cite old paths (evidence); active `Docs/latest` updated for runs/runtime_state

## Test results

Targeted suites during implementation (examples): reporting core/barrier/second-strategy; F3–F5; N7; R7b; yaml CLI; migration — all green after fixes.

Full suite (final, post M6/M7 gap-close):

```text
python -m pytest -q --tb=line
→ 722 passed in 33.19s
```

Earlier mid-cutover count was 718; four additional reporting-profile/mandatory-evidence tests were added afterward.

## Limitations / follow-up

- Some historical readiness docs still mention retired tool commands (intentional archive).
- Optional: further reduce OBSERVE overhead (writer batching / sync policy) without changing contracts.
- Owner must still run the real tiny-LIVE validation separately.

## Safety confirmations

- `.env` unchanged  
- Z-Gap trading mathematics / entry-exit policy / risk thresholds / $5 tiny-LIVE budget unchanged  
- Unrelated dirty docs preserved; historical `var/` evidence preserved  
- **No real LIVE run, order, cancel, flatten, or other venue mutation was performed** during this task (fakes/mocks only)

## Owner tiny-LIVE command (do not run by agent)

```bash
python -m tyrex_pm.application.cli run --mode live --runtime config/runtime/live_btc_5m.yaml --live --reporting config/reporting/full.yaml
```

(Equivalent: `tyrex-pm n7-live --live` with the same sealed config.)

### Post-run owner checklist

1. `var/runs/z_gap/<run_id>/manifest.json` — mode `live`, no accidental fake flag  
2. `run_summary.json` — performance `real`, terminal status, counters  
3. `audit_events.jsonl` — pre-mutation + venue result + decision lineage  
4. `attachments/operator_outcome.json` — outcome / residual / PTB trust fields  
5. Confirm no new writes under abandoned `var/reporting/**` or `var/state/**` as active roots  
6. Confirm `var/runtime_state/` ack/residuals consistent with the run
