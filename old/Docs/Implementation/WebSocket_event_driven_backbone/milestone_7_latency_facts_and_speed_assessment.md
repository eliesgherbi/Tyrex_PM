# Milestone 7 — Latency Facts & Speed Assessment

## Objective

Emit **decision-level observability**: decision snapshots (with FeatureBuilder v0), quality reports, planner evidence, and full **latency chain** from trigger through sellability.

## Why this milestone exists

`LatencyTracker` only marks activation today. M9 before/after comparison requires consistent fields on every entry, stop, TP, and exit — plus user/market WS sync timing.

## Current codebase state

**Implemented (Group B + Group C):** `decision_snapshot.py`, pipeline + planner path; **`observability.py`** wires monitor (stop/TP/FAK retry), activation, and entry eval in `paired_binary_run.py`. Legacy paired-binary facts retained alongside new facts.

## Target behavior

On every material decision (entry eval, TP trigger, stop trigger, exit submit, activation):

```text
decision_snapshot:
  decision_id, decision_type
  snapshot_id(s), pair_snapshot_id
  DataQualityReport summary
  PlannerEvidence (if plan produced)
  BasicFeatureSnapshot (FeatureBuilder v0)

latency_chain:
  market_book_age_ms
  trigger_monotonic_ns → submit → ack → fill
  submit_to_ack_ms
  ack_to_user_fill_ms
  fill_to_sellable_ms
  trigger_to_submit_ms
  trigger_to_fill_ms
  user_ws_age_ms
  wallet_position_age_ms
```

Wire `LatencyTracker.mark_*` in pipeline, paired_binary_run, activation, and retry paths (each retry = new decision_id).

## Files likely touched

- `src/tyrex_pm/strategies/paired_binary/latency.py`
- `src/tyrex_pm/strategies/paired_binary/facts.py`
- `src/tyrex_pm/runtime/pipeline.py`
- `src/tyrex_pm/runtime/paired_binary_run.py`
- `src/tyrex_pm/runtime/allocation_runtime.py`
- `src/tyrex_pm/reporting/schema_v2.py`
- `src/tyrex_pm/ingestion/user_stream.py`
- `src/tyrex_pm/market_data/decision_snapshot.py` — integrates M5 FeatureBuilder

## New files likely created

- `src/tyrex_pm/market_data/decision_snapshot.py`
- `tests/test_latency_facts.py`
- `tests/test_decision_snapshot_facts.py`
- `scripts/compare_run_latency.py` (for M9)

## Contracts / interfaces

```python
@dataclass(frozen=True)
class DecisionSnapshot:
    decision_id: str
    decision_type: str
    snapshot_ids: dict[str, str]  # leg -> snapshot_id
    pair_snapshot_id: str | None
    quality_report: DataQualityReport
    planner_evidence: PlannerEvidence | None
    features: BasicFeatureSnapshot

@dataclass(frozen=True)
class LatencyChain:
    decision_id: str
    trigger_to_submit_ms: int | None
    submit_to_ack_ms: int | None
    trigger_to_fill_ms: int | None
    ack_to_user_fill_ms: int | None
    fill_to_sellable_ms: int | None
    market_book_age_ms: int | None
    user_ws_age_ms: int | None
    wallet_position_age_ms: int | None
    source: BookSource | None
```

## Config changes

```yaml
runtime:
  observability:
    emit_decision_snapshot: true
    sample_raw_events: 0
```

## Facts / observability changes

- `decision_snapshot`
- `latency_chain`
- Extend `paired_binary_book_capture_quality` where redundant — prefer decision_snapshot

## Tests to add or update

- Stop exit: all latency fields populated
- Missing ack: null fields + reason
- `decision_snapshot.features.snapshot_id` == planner `snapshot_id`
- Each FAK retry: distinct `decision_id` in facts
- Schema validation for new types

## Acceptance criteria

- [ ] ≥1 live run has complete `latency_chain` on stop exit (requires live validation post-merge)
- [x] Pipeline emits `decision_snapshot` + `latency_chain` when planner runs with observability enabled
- [x] Monitor/activation/entry material paths emit `decision_snapshot` (Group C0)
- [x] FAK retry emits distinct `decision_id` / snapshot (`test_fak_retry_fresh_snapshot.py`, C0 wiring tests)
- [x] Missing ack: null latency fields with reason, no crash (`test_latency_facts.py`)
- [x] Schema validation for new fact types (`validate_fact_payload`)
- [ ] Doc/script for p50/p95 `book_age_ms` from facts.jsonl (deferred to M9)

## Implementation status

**Group B:** Planner path + schema.  
**Group C0:** Monitor stop/TP/FAK retry, activation, entry eval — closes monitor/activation observability gap.  
**Still partial on live paths:** ack/fill/user_ws timing requires live OMS + user WS during stop exit.

## Risks

- Use `monotonic_ns` for deltas; wall clock for age only
- Fact size — no full book levels in facts

## Open questions

None — reference snapshot_id only, not full book.

## Definition of done

All decision types emit snapshot + latency facts; M5 features included; schema updated; tests green.

## Not in scope

- Full raw WS persistence
- M9 report writing
- M8 cutover
