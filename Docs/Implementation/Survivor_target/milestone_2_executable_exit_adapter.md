# Milestone 2 — Depth-aware executable survival exit adapter

**Program:** [phase1.md](phase1.md) §6 Phase 1.4, §7  
**Status:** specification — not implemented  
**Depends on:** [Milestone 1](milestone_1_survival_models_and_target_policy.md) (survival package skeleton)

---

## 1. Purpose

Provide `SurvivalExitPlanner` — a single adapter that evaluates **executable** liquidity for survival decisions using Phase 2 `ExecutableBookView`, `ExecutionPlanner`, and reduce-only paths. Enforce: **touch bid alone is never authoritative** when `survival.enabled: true`.

---

## 2. Why this milestone exists

`PairedBinaryMonitor` compares touch bid to target (`yes_book.bid >= state.yes_target`). Phase 1 survival modules need shared executable evidence before M3/M4 enforcement. Landing this milestone before reachability/stall/trailing **enforce** modes prevents false positives.

---

## 3. Scope

- `src/tyrex_pm/survival/exit_planning.py`
- Integration hooks (callable from monitor; no full orchestration yet)
- Fact `survivor_executable_exit_evaluated`
- Unit tests with fixture snapshots

When `survival.enabled: false`, monitor behavior unchanged.

---

## 4. Non-goals

- Rewriting entire monitor TP path in M2 (provide API; M3/M7 wire orchestration)
- Hard economics gate (M5)
- Partial fill OMS submission changes beyond existing `clamp_exit_size` + planner

---

## 5. Current code areas to review

| File | Symbols |
|------|---------|
| `market_data/executable_book.py` | `ExecutableBookView.from_snapshot`, `PlannerEvidence` |
| `execution/planner.py` | `ExecutionPlanner.plan_exit` / FAK planning |
| `runtime/pipeline.py` | How planner evidence is built for exits |
| `strategies/paired_binary/sizing.py` | `clamp_exit_size`, `build_exit_work_unit` |
| `strategies/paired_binary/monitor.py` | TP trigger on touch bid |
| `risk/engine.py` | reduce-only evaluation |
| `risk/reduce_only_notional.py` | emergency bypass |

---

## 6. Target architecture

```text
SurvivalExitPlanner.evaluate_exit(
    coord, token_id, side=SELL, qty, context=URGENT_EXIT
) → SurvivalExitEvaluation
  ├─ capture fresh MarketStateSnapshot
  ├─ ExecutableBookView.from_snapshot(size=qty)
  ├─ optional ExecutionPlanner.plan (dry/evidence mode if available)
  ├─ compute ExecutableExitEvidence
  └─ recommend: proceed_full | proceed_partial | defer | blocked
```

---

## 7. Detailed implementation plan

### 7.1 Files to create

| File | Purpose |
|------|---------|
| `src/tyrex_pm/survival/exit_planning.py` | Planner adapter |
| `tests/test_survival_exit_planning.py` | Unit + fixture tests |

### 7.2 Files to modify

| File | Changes |
|------|---------|
| `survival/models.py` | Add `ExecutableExitEvidence`, `SurvivalExitEvaluation` |
| `survival/__init__.py` | Export `SurvivalExitPlanner` |
| `reporting/schema_v2.py` | `survivor_executable_exit_evaluated` |
| `strategies/paired_binary/facts.py` or `survival/facts.py` | Emitter |

### 7.3 Types to add

```python
@dataclass(frozen=True)
class ExecutableExitEvidence:
    touch_bid: Decimal | None
    executable_bid: Decimal | None      # best executable for size (VWAP or worst)
    sweep_vwap: Decimal | None
    worst_price_to_fill: Decimal | None
    available_depth: Decimal
    available_depth_fraction: Decimal   # available_depth / requested_qty
    expected_slippage: Decimal | None
    book_age_ms: int | None
    snapshot_id: str
    quality_verdict: str
    spread: Decimal | None
    planner_evidence_ref: dict[str, Any] | None

@dataclass(frozen=True)
class SurvivalExitEvaluation:
    verdict: Literal["proceed_full", "proceed_partial", "defer", "blocked"]
    recommended_qty: Decimal
    evidence: ExecutableExitEvidence
    reason: str | None

class SurvivalExitPlanner:
    def __init__(self, app: AppConfig): ...

    def evaluate_exit(
        self,
        *,
        coord: RuntimeCoordinator,
        token_id: TokenId,
        qty: Decimal,
        decision_context: DecisionContext,
        max_book_age_s: float,
    ) -> SurvivalExitEvaluation: ...

    def executable_bid_for_progress(self, evaluation: SurvivalExitEvaluation) -> Decimal | None:
        """Authoritative bid for stall/reachability/trailing — NOT touch alone."""
```

### 7.4 Locked rule

```text
No Phase 1 survival exit should be considered reachable or triggered only because
touch bid crossed a threshold.
```

When `survival.enabled: true`, any survival module must call `SurvivalExitPlanner` (or receive its evaluation) before enforce-mode action.

### 7.5 Partial exit

If `available_depth_fraction < 1` and `survival.exit_planning.allow_partial_survival_exit: true`:

- `verdict=proceed_partial`, `recommended_qty=available_depth` (clamp to sellable inventory)
- Emit fact with partial flag

Else: `defer` or `blocked` with reason `insufficient_depth`.

### 7.6 Config keys

```yaml
survival:
  exit_planning:
    require_executable_evidence: true
    allow_partial_survival_exit: true
    min_depth_fraction: "0.8"
    max_book_age_s: null   # inherit strategy/runtime
```

---

## 8. Config changes

Add `SurvivalExitPlanningConfig` under `SurvivalConfig`; defaults above apply only when `survival.enabled: true`.

---

## 9. State / data model changes

None persisted. Evaluation is ephemeral per tick.

---

## 10. Facts / observability

**`survivor_executable_exit_evaluated`** payload:

```text
touch_bid, executable_bid, sweep_vwap, available_depth_fraction,
expected_slippage, book_age_ms, snapshot_id, quality_verdict,
requested_qty, recommended_qty, verdict, spread
```

---

## 11. Tests

### `tests/test_survival_exit_planning.py`

- Fixture snapshot: touch bid high, depth low → defer/blocked without enforce
- Partial path when `allow_partial_survival_exit: true`
- Stale book → defer
- `executable_bid_for_progress` ≠ touch when depth thin

---

## 12. Acceptance criteria

- [ ] Survival modules can request executable evidence via single API.
- [ ] Touch bid alone never marked `proceed_full` when depth insufficient.
- [ ] Partial reduce path tested.
- [ ] With `survival.enabled: false`, no calls from monitor (unchanged touch TP).

---

## 13. Risks and rollback plan

| Risk | Mitigation |
|------|------------|
| Planner requires OMS context | Evidence-only path using ExecutableBookView without submit |
| Performance per tick | Dedupe evaluations within same monitor tick |

**Rollback:** `survival.enabled: false`.

---

## 14. Dependencies and next milestone

**Depends on:** M1 skeleton; Phase 2 ExecutableBookView.

**Blocks:** M3/M4 **enforce** modes (advisory can stub without M2).

**Next:** [Milestone 3](milestone_3_reachability_and_stall.md)
