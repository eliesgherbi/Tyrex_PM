# Milestone 4 — ExecutableBookView & ExecutionPlanner Evidence

## Objective

Plan FAK orders from **executable depth at size** and attach **PlannerEvidence** to every plan. Enforce **fresh snapshot on every retry** — never reuse stale evidence.

## Why this milestone exists

Stop slippage and FAK rejects (e.g. run `1782742788`) stem from planning at stale touch without depth. Retries that reuse old limits repeat the same mistake.

## Current codebase state

**Confirmed:** `execution/planner.py` — `is_stale`, `estimate_fill_price`, touch-heavy FAK. Partial book capture facts.

## Target behavior

```python
capture = store.capture(token_id)  # fresh each decision/retry
report = gate.evaluate_snapshot(capture, context=..., size=qty)
view = ExecutableBookView.from_snapshot(capture, side=SELL, size=qty)
plan = planner.plan_fak_sell(..., executable_view=view, quality_report=report)
# plan.evidence: snapshot_id, book_age_ms, source, touch, worst_price,
#                sweep_vwap, expected_slippage, available_depth, quality_verdict
```

Urgent exit: limit from `worst_price_to_fill` / sweep VWAP, not raw touch when depth thin.

### FAK / urgent exit retry policy (fixed)

```text
Every retry MUST capture a fresh MarketStateSnapshot.
Never reuse old PlannerEvidence.

Each retry gets:
  new decision_id
  new snapshot_id
  new DataQualityReport
  new ExecutableBookView
  new PlannerEvidence

Partial fill:
  remaining_qty = original_qty - filled_qty
  capture fresh snapshot
  build ExecutableBookView for remaining_qty
  run DataQualityGate again (context STOP or URGENT_EXIT)
  plan retry from fresh depth
  emit new planner evidence

FAK reject:
  capture fresh snapshot
  do NOT reuse previous touch / worst_price / sweep_vwap
  re-evaluate quality
  re-plan from current executable depth
  emit new evidence
```

Planner must propagate `quality_verdict=DEGRADED` or `EMERGENCY_ONLY` from report into evidence.

## Files likely touched

- `src/tyrex_pm/execution/planner.py`
- `src/tyrex_pm/execution/models.py`
- `src/tyrex_pm/strategies/paired_binary/monitor.py` — retry loop uses fresh capture
- `src/tyrex_pm/runtime/pipeline.py`
- `src/tyrex_pm/strategies/paired_binary/facts.py`
- `src/tyrex_pm/runtime/paired_binary_run.py` — entry FAK retry path

## New files likely created

- `src/tyrex_pm/market_data/executable_book.py`
- `tests/test_executable_book_view.py`
- `tests/test_planner_evidence.py`
- `tests/test_fak_retry_fresh_snapshot.py`

## Contracts / interfaces

```python
@dataclass(frozen=True)
class ExecutableBookView:
    side: Side
    size: Decimal
    touch_price: Decimal | None
    worst_price_to_fill: Decimal | None
    sweep_vwap: Decimal | None
    available_depth: Decimal
    levels_consumed: int
    snapshot_id: str

@dataclass(frozen=True)
class PlannerEvidence:
    decision_id: str
    snapshot_id: str
    book_age_ms: int
    source: BookSource
    touch_price: Decimal | None
    worst_price_to_fill: Decimal | None
    sweep_vwap: Decimal | None
    expected_slippage: Decimal | None
    available_depth: Decimal
    quality_verdict: QualityVerdict
    emergency_reason: str | None = None
```

## Config changes

```yaml
runtime:
  execution:
    planner:
      use_executable_depth: true
      max_slippage_vs_touch: 0.05
```

## Facts / observability changes

- `execution_planner_evidence` — full bundle per plan attempt (including retries)
- `fak_retry_attempt` — attempt_number, prior_snapshot_id, new_snapshot_id, reject_reason
- Extend stop/exit facts with evidence fields

## Tests to add or update

- Thin book: worst_price worse than touch
- Deep book: worst_price == touch
- DEGRADED: plan produced with `quality_verdict=DEGRADED` in evidence
- EMERGENCY_ONLY: evidence includes `emergency_reason`
- **Two retries use two different `snapshot_id` values**
- **Stale planner evidence never reused after reject**
- **Partial-fill retry sizes against `remaining_qty` and fresh depth**
- Evidence `snapshot_id` matches `store.capture().snapshot_id`

## Acceptance criteria

- [x] All FAK plans (entry + exit) emit evidence fact (via pipeline when planner enabled + observability)
- [x] Retry tests pass (distinct snapshot_ids)
- [x] Stop plans use executable depth when `use_executable_depth: true`
- [x] No code path copies prior `PlannerEvidence` into retry

## Implementation status (Group B)

**Shipped:** `src/tyrex_pm/market_data/executable_book.py`; planner integration with fresh capture; `plan_fak_with_fresh_evidence` / `plan_fak_retry_for_remaining` helpers.

## Risks

- More aggressive limits → higher FAK reject count before fill — measure in M9

## Open questions

- Optional cap: `min(worst_price, touch * (1 - max_slippage))` — implement if rejects too high in M9

## Definition of done

ExecutableBookView wired; retry policy enforced in monitor + entry paths; all retry tests green.

## Not in scope

- Strategy stop/TP threshold changes
- WS-primary cutover ([M8](milestone_8_ws_primary_cutover.md))
- FeatureBuilder v0 ([M5](milestone_5_feature_builder_v0.md)) — consumes same snapshots separately
