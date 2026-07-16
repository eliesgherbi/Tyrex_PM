# Group B Implementation Summary (M3 / M4 / M7)

**Completed:** 2026-06-29  
**Scope:** DataQualityGate, ExecutableBookView + PlannerEvidence, decision snapshots + latency facts.

## Delivered

| Milestone | Modules | Tests |
|-----------|---------|-------|
| M3 | `market_data/quality.py`, `market_data/readiness.py` | `test_data_quality_gate.py`, `test_quality_entry_vs_exit.py`, `test_emergency_only_semantics.py`, `test_market_readiness_states.py` |
| M4 | `market_data/executable_book.py`, `execution/planner.py` (depth + evidence) | `test_executable_book_view.py`, `test_planner_evidence.py`, `test_fak_retry_fresh_snapshot.py` |
| M7 | `market_data/decision_snapshot.py`, `strategies/paired_binary/latency.py`, `strategies/paired_binary/facts.py`, `runtime/pipeline.py`, `reporting/schema_v2.py` | `test_latency_facts.py`, `test_decision_snapshot_facts.py`, `test_planner_evidence_facts.py` |

## Runtime posture (pre-M8)

- **DataQualityGate:** `runtime.market_data.quality.enforcement_mode: observe_only` (default). Verdicts are evaluated and emitted as facts; live decisions are **not** blocked by quality until M8 sets `enforce`.
- **REST authoritative:** Authoritative `coord.market_state` unchanged; WS shadow store remains separate (M1).
- **Strategy unchanged:** No paired-binary threshold / TP / SL / survivor / market-selection changes.
- **FAK retry policy:** `plan_fak_with_fresh_evidence` / `plan_fak_retry_for_remaining` always capture a new snapshot, `decision_id`, quality report, executable view, and planner evidence.

## Config additions

```yaml
runtime:
  market_data:
    quality:
      enforcement_mode: observe_only   # observe_only | enforce
      market_profile: crypto_5m
      require_ws_primary_for_entry: true
      allow_rest_recovery_for_exit: true
    market_profiles:
      crypto_5m:
        pass_max_age_ms: 750
        reject_max_age_ms: 1500
        emergency_max_age_ms: 3000
  execution:
    planner:
      use_executable_depth: true
  observability:
    emit_decision_snapshot: true
```

## New fact types

- `data_quality_verdict`
- `execution_planner_evidence`
- `decision_snapshot` (includes FeatureBuilder v0 fields)
- `latency_chain`
- `market_readiness_transition` (schema constant; emitted by readiness tracker when wired)

## Test results

55 Group B + planner tests passed (see CI / local run).

## Not implemented (by design)

- M6 hybrid event scheduler
- M8 WS-primary cutover
- M9 baseline rerun/report
- M10 future signals

## Remaining blockers before M6

1. **M8 review gate** — confirm `enforce` cutover plan before wiring gate into entry_eval/monitor decision paths beyond observation.
2. **M6 design review** — hybrid wake/debounce must not run until Group B observability is validated on at least one live/shadow run.
3. **M9 baseline artifacts** — still missing 2/3 control `facts.jsonl` (does not block M6, blocks M9).
