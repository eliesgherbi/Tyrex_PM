# Milestone 4 — Survivor trailing stop

**Program:** [phase1.md](phase1.md) §6 Phase 1.2  
**Status:** specification — not implemented  
**Depends on:** [Milestone 2](milestone_2_executable_exit_adapter.md)

---

## 1. Purpose

Implement **SurvivorTrailingStop** — a state machine that arms after **executable** favorable movement and triggers on executable bid/VWAP reversal, protecting survivor gains without relying on touch bid alone.

---

## 2. Why this milestone exists

After loser exit, fixed TP only exits at ambitious target. Trailing stop locks profit when survivor trends favorably then reverses — complementary to stall detection (M3) which handles **no** progress.

---

## 3. Scope

- `survival/trailing_stop.py`
- Monitor hook in survivor phases
- Facts; tests; advisory default

---

## 4. Non-goals

- Protection overlay registration (defer; inline monitor hook for M4)
- Arming on touch bid without depth/freshness gates
- Blocking pre-close flatten or shutdown

---

## 5. Current code areas to review

| File | Notes |
|------|-------|
| `protection/trigger_eval.py` | Reference for pure trigger math (do not merge packages) |
| `survival/exit_planning.py` | Executable evidence |
| `strategies/paired_binary/monitor.py` | Insert after TP check |
| `strategies/paired_binary/exit_engine.py` | `try_build_exit`, trigger types |

---

## 6. Target architecture

```text
SurvivorTrailingStop.evaluate(
    state: TrailingStopState,
    exit_eval: SurvivalExitEvaluation,
    timing: MarketTimingSnapshot,
    cfg: TrailingStopConfig,
) → TrailingStopEvaluation
  states: DISARMED → ARMED → TRIGGERED → EXIT_PENDING
```

---

## 7. Detailed implementation plan

### 7.1 Files to create

| File | Purpose |
|------|---------|
| `src/tyrex_pm/survival/trailing_stop.py` | FSM |
| `tests/test_survival_trailing_stop.py` | |

### 7.2 Files to modify

| File | Changes |
|------|---------|
| `survival/models.py` | `TrailingStopState`, `TrailingStopEvaluation` |
| `strategies/paired_binary/monitor.py` | Call trailing after TP, before stall |
| `runtime/config.py` | `survival.trailing_stop` |
| `reporting/schema_v2.py`, facts emitters | |

### 7.3 Types

```python
class TrailingStopState(str, Enum):
    DISARMED = "disarmed"
    ARMED = "armed"
    TRIGGERED = "triggered"
    EXIT_PENDING = "exit_pending"

@dataclass
class TrailingStopRuntime:
    state: TrailingStopState
    peak_executable_bid: Decimal | None
    trail_floor: Decimal | None
    armed_at_ts: float | None

@dataclass(frozen=True)
class TrailingStopEvaluation:
    new_runtime: TrailingStopRuntime
    should_exit: bool
    reason: str | None
    evidence: dict[str, Any]

class SurvivorTrailingStop:
    def evaluate(
        self,
        *,
        runtime: TrailingStopRuntime,
        exit_eval: SurvivalExitEvaluation,
        survivor_entry: Decimal,
        loser_exit_ts: float,
        timing: MarketTimingSnapshot,
        cfg: TrailingStopConfig,
    ) -> TrailingStopEvaluation: ...
```

### 7.4 Arming requirements (all must pass)

```text
elapsed since loser_exit >= arm_delay_s
executable_gain >= arm_after_executable_gain   # vs survivor_bid_0 or entry reference
book fresh (book_age_ms <= max)
spread <= max_spread
available_depth_fraction >= min_depth_fraction
seconds_to_close > disable_near_close_s (or not in near-close phase)
```

Use `exit_eval.evidence.executable_bid` for gain and peak tracking.

### 7.5 Trigger rule (locked)

```text
Fire when executable_bid (or sweep_vwap) <= trail_floor
trail_floor = peak_executable_bid - trail_distance
Also enforce min_profit_lock floor vs survivor_entry once armed
NOT touch bid alone
```

### 7.6 Integration

- Phases: `ONLY_YES_ACTIVE`, `ONLY_NO_ACTIVE`
- Order: after existing TP check (TP may use executable when survival enabled)
- Before stall/reachability in enforce path
- On trigger + enforce: `_dispatch_exit` with trigger_type `trailing_stop` (extend `TriggerType` if needed)
- Pre-close / shutdown: skip trailing evaluation if flatten already required (M0 guard)

### 7.7 Config

```yaml
survival:
  trailing_stop:
    enabled: true
    enforcement_mode: advisory
    arm_after_executable_gain: "0.02"
    trail_distance: "0.03"
    min_profit_lock: "0.01"
    arm_delay_s: 5
    require_fresh_book: true
    max_spread: "0.04"
    min_depth_fraction: "0.8"
    disable_near_close_s: 30
    max_book_age_s: null
```

---

## 8. Config changes

Add `TrailingStopConfig`; default `enforcement_mode: advisory`.

---

## 9. State / data model changes

- `TrailingStopRuntime` nested in `SurvivorLegState` or parallel on `PairedBinaryRuntimeState`
- Persist optional for recovery mid-survivor

---

## 10. Facts / observability

| Fact | When |
|------|------|
| `survivor_trailing_stop_armed` | Transition DISARMED→ARMED |
| `survivor_trailing_stop_triggered` | TRIGGERED |

Payload: peak_executable_bid, trail_floor, executable_bid, touch_bid, spread, depth_fraction, enforcement_mode.

---

## 11. Tests

### `tests/test_survival_trailing_stop.py`

- Does not arm on touch-only gain with thin depth
- Does not arm on stale/wide book
- Arms then triggers on executable reversal
- Does not arm inside `disable_near_close_s`
- Advisory: no exit intent

---

## 12. Acceptance criteria

- [ ] Trailing stop does not arm on touch bid alone.
- [ ] Does not arm on stale/wide/thin book.
- [ ] Enforce mode (test config) triggers reduce-only survivor exit.
- [ ] Does not block pre-close flatten (M0 integration test).

---

## 13. Risks and rollback plan

| Risk | Mitigation |
|------|------------|
| Whipsaw | arm_delay_s, min_depth |
| Duplicate exit with TP | TP checked first; trailing only if TP not hit |

**Rollback:** `trailing_stop.enabled: false` or advisory.

---

## 14. Dependencies and next milestone

**Depends on:** M2 (required); M0 for near-close disable.

**Next:** [Milestone 5](milestone_5_economics_gate.md) or M7 observability.
