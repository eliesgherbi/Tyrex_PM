# Milestone 0 — Market-aware strategy lifecycle runtime

**Program:** [phase1.md](phase1.md) §5 (Phase 1.0 foundation)  
**Status:** specification — not implemented  
**Blocks:** Milestones 1–7

---

## 1. Purpose

Decouple **process runtime** from **strategy exit** so paired-binary (and future strategies) run until the market's tradable window ends, pre-close flatten, strategy/survival exits, operator shutdown, or kill switch — **not** because an arbitrary `max_runtime_s` tick budget elapses while `event_end_ts` is known.

---

## 2. Why this milestone exists

Live runs live3/live5 force-flattened open survivors at `max_runtime_s` while the BTC 5-minute tradable window was still open. Phase 2 infrastructure is sufficient; the loop still uses:

```text
max_ticks = int(cfg.max_runtime_s / poll_interval)
tick_budget_exhausted → open_exposure force-flatten
```

Until this milestone lands, all later survival logic can still be cut by process runtime.

---

## 3. Scope

- New module `src/tyrex_pm/runtime/strategy_lifecycle.py`
- Config parsing for `runtime.strategy_lifecycle`
- Integration in `paired_binary_run.py` loop continuation and entry gating
- Reuse `strategies/paired_binary/market_timing.py` for clock and phase
- Reuse `paired_binary_shutdown.py` for pre-close flatten and fallback-runtime flatten
- Facts and tests listed below
- Deprecation warning for strategy-level `max_runtime_s` as normal exit (keep field for back-compat)

**Global constraints:** Do not change Phase 2 behavior when `survival.enabled: false`. Do not remove Phase 2 safety gates.

---

## 4. Non-goals

- Survivor target policy, stall, trailing stop, economics (Milestones 1–5)
- Hard cancel-all kill switch (Phase 6 architecture_enhance)
- Waiting for Polymarket settlement/resolution
- External signals, ML, positive-EV tuning, capital scaling
- Giant `monitor.py` changes

---

## 5. Current code areas to review

| File | Relevant symbols / behavior |
|------|----------------------------|
| `runtime/paired_binary_run.py` | `run_paired_binary_loop`, `max_ticks`, `tick_budget_exhausted`, open-exposure shutdown branch (~L1406–1570) |
| `runtime/paired_binary_shutdown.py` | `handle_open_exposure_at_shutdown`, `phase_has_open_exposure`, `effective_open_exposure_policy` |
| `strategies/paired_binary/market_timing.py` | `build_market_timing_snapshot`, `resolve_market_timing_metadata`, phase constants |
| `runtime/config.py` | `RuntimeConfig`, `PairedBinaryRuntimeConfig`, `_build_risk_runtime` |
| `strategies/paired_binary/entry_eval.py` | `evaluate_entry` — entry path to gate |
| `reporting/schema_v2.py` | Add new fact type constants |

Inspect these before implementing; preserve existing public interfaces unless this milestone explicitly replaces call sites.

---

## 6. Target architecture

```text
paired_binary_run loop tick
  → build_market_timing_snapshot(event_end_ts, ...)
  → MarketLifecycleGuard.evaluate(...)
       ├─ should_continue_loop → LoopDecision
       ├─ should_block_new_entry → EntryBlockDecision
       └─ should_pre_close_flatten → PreCloseFlattenDecision
  → if pre_close_flatten: handle_open_exposure_at_shutdown(reason=market_close_flatten)
  → if !continue_loop and fallback_runtime only: existing shutdown path
  → entry path: if EntryBlockDecision.blocked: skip entry + emit fact
```

**Clock semantics (locked):**

```text
Tradable window end = event_end_ts (exit_clock_source)
NOT final Polymarket settlement
```

---

## 7. Detailed implementation plan

### 7.1 Files to create

| File | Contents |
|------|----------|
| `src/tyrex_pm/runtime/strategy_lifecycle.py` | All classes below |
| `tests/test_strategy_lifecycle.py` | Unit tests for pure policy logic |
| `tests/test_paired_binary_market_lifecycle_runtime.py` | Integration with loop |
| `tests/test_paired_binary_phase2_regression.py` | Phase 2 unchanged with survival off + lifecycle default/back-compat |

### 7.2 Files to modify

| File | Changes |
|------|---------|
| `src/tyrex_pm/runtime/config.py` | Add `StrategyLifecycleConfig` dataclass; parse `runtime.strategy_lifecycle`; attach to `RuntimeConfig` |
| `src/tyrex_pm/runtime/paired_binary_run.py` | Replace tick-budget normal exit with lifecycle evaluation; wire entry block; pre-close flatten |
| `src/tyrex_pm/reporting/schema_v2.py` | Add fact type constants (§10) |
| `src/tyrex_pm/strategies/paired_binary/facts.py` | Emitters for lifecycle facts (or thin wrapper calling shared helper) |
| `config/scenarios/live_paired_binary_tiny_ws_primary.yaml` | Optional: add `strategy_lifecycle` block for validation (can wait for M7 scenario) |

### 7.3 Classes / functions to add (`strategy_lifecycle.py`)

```python
@dataclass(frozen=True)
class StrategyRuntimePolicy:
    mode: str                          # market_aware | fixed_duration | run_once
    exit_clock_source: str             # event_end_ts (v1 only)
    max_runtime_s: float | None
    fallback_max_runtime_s: float
    flatten_before_event_end_s: float
    block_new_entry_phases: frozenset[str]
    emit_unknown_market_end_warning: bool
    min_survival_window_s: float       # optional; default 45 for entry block

@dataclass(frozen=True)
class LoopDecision:
    continue_loop: bool
    reason: str
    clock_known: bool
    seconds_to_close: float | None
    used_fallback_runtime: bool

@dataclass(frozen=True)
class EntryBlockDecision:
    blocked: bool
    reason: str | None
    phase: str | None
    evidence: dict[str, Any]

@dataclass(frozen=True)
class PreCloseFlattenDecision:
    required: bool
    reason: str
    seconds_to_close: float | None

class MarketLifecycleGuard:
    def __init__(self, policy: StrategyRuntimePolicy, *, loop_started_mono: float): ...

    def evaluate_timing(self, snapshot: MarketTimingSnapshot) -> ...: ...

    def should_continue_loop(
        self,
        *,
        snapshot: MarketTimingSnapshot,
        has_open_exposure: bool,
        strategy_terminal: bool,
        operator_stop: bool,
    ) -> LoopDecision: ...

    def should_block_new_entry(self, snapshot: MarketTimingSnapshot) -> EntryBlockDecision: ...

    def should_pre_close_flatten(
        self,
        *,
        snapshot: MarketTimingSnapshot,
        has_open_exposure: bool,
    ) -> PreCloseFlattenDecision: ...

    def is_fallback_runtime_exhausted(self, *, now_mono: float) -> bool: ...

def build_strategy_runtime_policy(app: AppConfig) -> StrategyRuntimePolicy: ...
def parse_strategy_lifecycle_config(raw: dict[str, Any]) -> StrategyRuntimePolicy: ...
```

### 7.4 Behavior specification

#### When `event_end_ts` is known (`clock_known=True`)

- **Do not** stop loop because `ticks >= max_ticks` derived from strategy `max_runtime_s`.
- Continue until:
  - `strategy_terminal` (DONE/FAILED/etc.), or
  - pre-close flatten executed and flat, or
  - operator stop / asyncio cancel, or
  - kill switch (M6; stub hook OK in M0), or
  - survival/strategy exit completes (unchanged Phase 4.6 paths)
- Emit `strategy_runtime_decision` with `continue_loop=true` and reason e.g. `market_clock_active` on material transitions (dedupe).

#### When `event_end_ts` is unknown

- Use `fallback_max_runtime_s` from loop start mono time.
- On exhaustion with open exposure: existing `open_exposure_on_max_runtime` policy (force-flatten / continue / manual).
- Emit `strategy_runtime_fallback_max_runtime` once per run when clock unknown.

#### Pre-close window

- Condition: `0 < seconds_to_close <= flatten_before_event_end_s`
- Block new entry → `strategy_lifecycle_entry_blocked`
- If open exposure: `strategy_lifecycle_pre_close_flatten_required` then call `handle_open_exposure_at_shutdown` with reason `market_close_flatten` (extend shutdown module to accept optional reason string if not present).

#### Entry gating (lifecycle-only portion)

Block entry when timing phase in `block_new_entry_phases` (default `near_close`, `closed`) OR `seconds_to_close < min_survival_window_s` OR clock unknown without fallback configured.

**Note:** If `survival.enabled` is false, skip survival-specific gates only; lifecycle gates still apply.

#### Shutdown

- Process signal / operator stop: unchanged Phase 2 `handle_open_exposure_at_shutdown`.
- `open_exposure_on_max_runtime` applies only to **fallback runtime** and **shutdown**, not normal market-clock operation.

### 7.5 Functions to call or replace in `paired_binary_run.py`

| Current | Replace with |
|---------|--------------|
| `max_ticks = int(cfg.max_runtime_s / poll_interval)` | Only when `!clock_known` or `mode=fixed_duration`; else no tick cap |
| `tick_budget_exhausted = ticks >= max_ticks` | `lifecycle.is_fallback_runtime_exhausted()` OR fixed_duration mode |
| Normal break on tick budget with flat/idle | Unchanged |
| Normal break on tick budget with open exposure + known clock | **Remove** — continue loop |
| Entry evaluation before `run_pair_entry_from_idle` | Check `should_block_new_entry` |

Inspect loop for all `break` paths; ensure `paired_binary_loop_stopped` health fact still emitted from `finally`.

### 7.6 Config keys

**Add:**

```yaml
runtime:
  strategy_lifecycle:
    mode: market_aware
    exit_clock_source: event_end_ts
    max_runtime_s: null
    fallback_max_runtime_s: 900
    flatten_before_event_end_s: 20
    block_new_entry_phases: [near_close, closed]
    emit_unknown_market_end_warning: true
    min_survival_window_s: 45
```

**Deprecate (warn in logs/facts, do not remove):**

- Strategy YAML `paired_binary.max_runtime_s` as loop driver — document migration to `fallback_max_runtime_s` when clock unknown.

---

## 8. Config changes

- Parse in `_build_risk_runtime` or dedicated helper near `RuntimeConfig` construction.
- Defaults: `mode=market_aware`, `max_runtime_s=null`, `flatten_before_event_end_s=20`, `fallback_max_runtime_s=900`.
- When `strategy_lifecycle` block absent: **back-compat mode** — behave as today (strategy `max_runtime_s` drives tick budget) until scenario opts in. Document in config README.

---

## 9. State / data model changes

- No persisted state file required for M0.
- Optional: store `lifecycle_clock_source` on loop health payload.
- `PairedBinaryRuntimeState` unchanged unless entry-block reason needed in state (prefer facts only).

---

## 10. Facts / observability

Add to `schema_v2.py`:

| Constant | Payload (minimum) |
|----------|---------------------|
| `strategy_runtime_decision` | `continue_loop`, `reason`, `clock_known`, `seconds_to_close`, `event_end_ts`, `phase` |
| `strategy_runtime_fallback_max_runtime` | `fallback_max_runtime_s`, `elapsed_s`, `warning` |
| `strategy_lifecycle_entry_blocked` | `reason`, `phase`, `seconds_to_close`, `min_survival_window_s` |
| `strategy_lifecycle_pre_close_flatten_required` | `seconds_to_close`, `flatten_before_event_end_s`, `exposure_snapshot` |

Emit via existing `pb_facts._write_fact` pattern or shared `emit_strategy_lifecycle_*` helpers.

---

## 11. Tests

### `tests/test_strategy_lifecycle.py`

- Known `event_end_ts`, before end → `continue_loop=True` regardless of tick count
- Unknown end → fallback exhausts at `fallback_max_runtime_s`
- Pre-close: entry blocked; flatten required with exposure
- Closed market: entry blocked
- `block_new_entry_phases` respected

### `tests/test_paired_binary_market_lifecycle_runtime.py`

- Fixture loop: survivor phase, known `event_end_ts`, ticks exceed old `max_runtime_s` → no force-flatten
- Pre-close triggers shutdown flatten path
- Unknown clock → fallback flatten behavior preserved

### `tests/test_paired_binary_phase2_regression.py`

- Run existing Phase 2 force-flatten / lifecycle tests with **no** `strategy_lifecycle` config block (back-compat)
- With `strategy_lifecycle` disabled/back-compat and `survival.enabled: false` → identical classifications

---

## 12. Acceptance criteria

- [ ] With `event_end_ts` known, open survivor is **not** force-flattened only because old strategy `max_runtime_s` elapsed.
- [ ] With `survival.enabled: false`, Phase 2 lifecycle validation still passes (back-compat or explicit regression test).
- [ ] Unknown `event_end_ts` emits `strategy_runtime_fallback_max_runtime` and uses fallback runtime.
- [ ] Pre-close window blocks entry and flattens open exposure via reduce-only path.
- [ ] Operator shutdown still uses Phase 2 open-exposure policy.
- [ ] No changes to monitor TP/SL when survival disabled.

---

## 13. Risks and rollback plan

| Risk | Mitigation |
|------|------------|
| Missing `event_end_ts` in production | Fallback + warning; preflight script |
| Pre-close 20s too tight | Config tunable; start at 20 |
| Break runs without lifecycle config | Back-compat default = current behavior |

**Rollback:** Feature-flag via absent `strategy_lifecycle` YAML block → old tick budget path.

---

## 14. Dependencies and next milestone

**Depends on:** Phase 2 complete only.

**Blocks:** Milestones 1–7 (all assume correct loop clock).

**Next:** [milestone_1_survival_models_and_target_policy.md](milestone_1_survival_models_and_target_policy.md) — survival package skeleton can start in parallel only after M0 interface for loop is merged or stubbed.
