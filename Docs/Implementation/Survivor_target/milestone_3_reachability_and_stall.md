# Milestone 3 — Reachability and stall / no-progress detection

**Program:** [phase1.md](phase1.md) §6 Phase 1.3, §6 Phase 1.3b  
**Status:** specification — not implemented  
**Depends on:** [M1](milestone_1_survival_models_and_target_policy.md), [M2](milestone_2_executable_exit_adapter.md)

---

## 1. Purpose

Implement **TargetReachabilityScorer** and **SurvivorStallDetector** to judge whether survivor targets are achievable and whether price is **progressing** after loser exit. Default: **advisory** (`enforcement_mode: advisory`) — emit facts without changing behavior until validation.

---

## 2. Why this milestone exists

Live runs live3/live5: survivor held open with ambitious target, insufficient progress, then max_runtime force-flatten. Trailing stop (M4) only helps after favorable movement. Reachability alone does not detect **stall**. Stall logic directly addresses: *When the survivor is not moving, exit intelligently.*

---

## 3. Scope

- `survival/reachability.py`, `survival/stall_exit.py`
- Monitor orchestration hook (thin) when `survival.enabled: true`
- Facts; unit tests; advisory default

---

## 4. Non-goals

- ML scoring
- Enforce mode enabled in default config
- Touch-bid-based progress (must use M2 executable bid)

---

## 5. Current code areas to review

| File | Notes |
|------|-------|
| `survival/target_policy.py` | Selected target/mode |
| `survival/exit_planning.py` | Executable bid |
| `strategies/paired_binary/monitor.py` | Survivor-only phases `ONLY_YES_ACTIVE`, `ONLY_NO_ACTIVE` |
| `market_data/features.py` | Optional velocity |
| `strategies/paired_binary/market_timing.py` | `seconds_to_close` |

---

## 6. Target architecture

```text
monitor tick (survivor phase, survival.enabled)
  → exit_eval = SurvivalExitPlanner.evaluate_exit(...)
  → reach = TargetReachabilityScorer.score(ctx, exit_eval)
  → emit survivor_reachability_scored
  → stall = SurvivorStallDetector.evaluate(ctx, exit_eval)
  → emit survivor_progress_evaluated
  → if stall detected: emit survivor_stall_detected
  → if enforcement enforce: apply StallAction / downgrade via SurvivorTargetPolicy
  → else: facts only
```

Monitor evaluation order (survival branch): existing TP check (upgrade to executable when enabled) → trailing (M4) → **stall** → reachability → economics (M5).

Pre-close flatten / shutdown **preempt** all survival modules.

---

## 7. Detailed implementation plan

### 7.1 Files to create

| File | Purpose |
|------|---------|
| `src/tyrex_pm/survival/reachability.py` | Scorer |
| `src/tyrex_pm/survival/stall_exit.py` | Stall detector |
| `tests/test_survival_reachability.py` | |
| `tests/test_survival_stall_exit.py` | |

### 7.2 Files to modify

| File | Changes |
|------|---------|
| `survival/models.py` | `ReachabilityResult`, `StallEvaluation`, enums |
| `strategies/paired_binary/monitor.py` | Thin `_evaluate_survival_exits(...)` hook |
| `runtime/config.py` | `reachability`, `stall_exit` config |
| `reporting/schema_v2.py` | Fact constants |
| `strategies/paired_binary/facts.py` | Emitters |

### 7.3 Reachability types

```python
class ReachabilityVerdict(str, Enum):
    REACHABLE = "reachable"
    WEAK = "weak"
    UNLIKELY = "unlikely"

@dataclass(frozen=True)
class ReachabilityResult:
    verdict: ReachabilityVerdict
    score: Decimal
    distance_to_target: Decimal
    evidence: dict[str, Any]

class TargetReachabilityScorer:
    def score(
        self,
        *,
        ctx: SurvivorLegState,
        target_price: Decimal,
        exit_eval: SurvivalExitEvaluation,
        timing: MarketTimingSnapshot,
        quality_meta: dict[str, Any] | None,
    ) -> ReachabilityResult: ...
```

**Inputs (executable-first):**

- Distance: `target_price - exit_eval.evidence.executable_bid` (or sweep_vwap)
- `seconds_to_close`, spread, depth fraction, slippage, velocity, phase, quality instability

**Weighted heuristic** — configurable weights from phase1.md; no ML.

### 7.4 Stall types

```python
class StallAction(str, Enum):
    DOWNGRADE = "downgrade"
    EXIT_SMALL_LOSS = "exit_small_loss"
    EXIT_MARKET = "exit_market"

@dataclass(frozen=True)
class StallEvaluation:
    stalled: bool
    progress_to_target: Decimal
    elapsed_fraction: Decimal
    action: StallAction | None
    evidence: dict[str, Any]

class SurvivorStallDetector:
    def record_baseline(
        self,
        *,
        exit_eval: SurvivalExitEvaluation,
        selected_target: Decimal,
        loser_exit_ts: float,
        seconds_to_close: float | None,
        flatten_before_event_end_s: float,
    ) -> SurvivorLegState: ...

    def evaluate(
        self,
        *,
        state: SurvivorLegState,
        exit_eval: SurvivalExitEvaluation,
        now_ts: float,
    ) -> StallEvaluation: ...
```

**Formulas (locked):**

```text
progress_to_target =
  (current_executable_bid - survivor_bid_0)
  / max(selected_target - survivor_bid_0, epsilon)

elapsed_fraction =
  elapsed_since_loser_exit
  / max(available_survival_time, epsilon)

available_survival_time = max(seconds_to_close_0 - flatten_before_event_end_s, epsilon)
```

**Stall condition:**

```text
elapsed_fraction >= stall_threshold_fraction
AND progress_to_target < min_progress_ratio
AND elapsed_since_loser_exit >= check_after_s
OR elapsed_since_loser_exit >= max_stall_s with progress below floor
```

### 7.5 Enforcement

```yaml
survival:
  reachability:
    enforcement_mode: advisory   # advisory | enforce
  stall_exit:
    enabled: true
    enforcement_mode: advisory
    min_progress_ratio: "0.35"
    stall_threshold_fraction: "0.40"
    check_after_s: 20
    max_stall_s: 60
    action_when_stalled: downgrade
```

- **Advisory:** emit facts only; no target change, no exit intent.
- **Enforce:** call `SurvivorTargetPolicy` downgrade or emit exit via `exit_engine.try_build_exit`.

### 7.6 Functions to call

- `SurvivalExitPlanner.evaluate_exit` — every evaluation
- `SurvivorTargetPolicy.select_plan` — on downgrade action
- `try_build_exit` / monitor `_dispatch_exit` — on exit actions

---

## 8. Config changes

See §7.5. Defaults: both modules `advisory`.

---

## 9. State / data model changes

- `SurvivorLegState` populated at loser exit (M1/M3 boundary): `survivor_bid_0` from executable bid, timestamps, targets.
- Persist in `PairedBinaryRuntimeState` optional JSON field.

---

## 10. Facts / observability

| Fact | When |
|------|------|
| `survivor_reachability_scored` | Deduped periodic |
| `survivor_progress_evaluated` | Each stall evaluation tick |
| `survivor_stall_detected` | Stall condition true |
| `survivor_target_downgraded` | Enforce downgrade |

Include: executable bid, touch bid, progress ratio, elapsed, verdict, enforcement_mode, action.

---

## 11. Tests

### `tests/test_survival_reachability.py`

- reachable vs unlikely boundaries
- uses executable bid not touch

### `tests/test_survival_stall_exit.py`

- high elapsed, low progress → stalled
- advisory does not return exit action
- enforce + downgrade calls policy

### Integration

- `tests/test_paired_binary_survival_monitor.py` (stub until M7) — advisory emits facts, no extra OMS submits

---

## 12. Acceptance criteria

- [ ] Advisory mode emits facts; **no behavior change** vs Phase 2 baseline.
- [ ] Enforce mode (test-only config) can downgrade or trigger early exit.
- [ ] Stall detects no-progress scenario matching live3 pattern (replay fixture).
- [ ] All progress math uses executable bid from M2.

---

## 13. Risks and rollback plan

| Risk | Mitigation |
|------|------------|
| False stall on wide spread | Require min book quality in detector |
| Fact spam | Dedupe `survivor_progress_evaluated` |

**Rollback:** `survival.enabled: false` or `enforcement_mode: advisory`.

---

## 14. Dependencies and next milestone

**Depends on:** M1, M2.

**Next:** [Milestone 4](milestone_4_trailing_stop.md) (requires M2).

**Parallel:** M5 economics can start after M2 in advisory mode.
