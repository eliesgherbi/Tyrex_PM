# Phase 1 — Survivor Target & Survival Layer

**Status:** design / implementation plan (no code yet)  
**Program:** Survival and damage-control for paired-binary and future strategies  
**Prerequisite:** [Phase 2 operational complete](../WebSocket_event_driven_backbone/phase2_final_operational_hardening.md)  
**Next (out of scope here):** Phase 3 alpha research (external signals, positive-EV tuning)

---

## 1. Executive summary

Phase 2 proved the infrastructure backbone: WS-primary books, quality gates, decision evidence, and shutdown safety are operationally sufficient to **diagnose strategy behavior**. Recent BTC 5-minute paired-binary live runs show the strategy mechanics work (entry, dual stop, survivor repricing, TP, force-flatten) but expose a **strategy-management weakness**: after one leg stops, the survivor is asked to recover the full loser loss plus desired profit — an ambitious target that only completes when the market strongly trends. When it does not, the survivor stalls and the process exits via artificial `max_runtime_s` force-flatten, often before the market's tradable window ends.

**Phase 1 objective:** When the strategy is wrong, lose less; when the survivor is not moving, exit intelligently; when the target is unrealistic, adapt; when market conditions become unsafe, flatten or reduce risk; when the market remains open, do not exit only because process runtime expired.

This document defines **Phase 1.0 (foundation)** plus six implementation subphases (1.1–1.6), a reusable module architecture (not a giant `monitor.py` patch), config keys, facts, tests, and implementation order. **No Phase 3 prediction signals, no capital scaling, no removal of Phase 2 safety gates.**

**Rollout safety:** Global survival behavior is **`enabled: false` by default**. Phase 2 lifecycle must pass unchanged with survival disabled. Enable survival only in Phase 1 tiny-live scenario overlays, advisory first, enforce after replay validation.

**Recommendation:** `READY_FOR_PHASE_1_DESIGN_REVIEW` — Phase 2 baseline is sufficient; remaining work is design execution, not infrastructure.

---

## 2. Phase 2 baseline and what is already solved

Phase 2 is **operationally complete**. Do not reopen Phase 2 unless a real safety regression is found.

### 2.1 Delivered capabilities

| Capability | Primary modules | Notes |
|------------|-----------------|-------|
| WS-primary authoritative market data | `market_data/*`, `ingestion/market_stream.py`, `runtime/market_data_runtime.py` | REST poll disabled for new risk in WS-primary scenarios |
| DataQualityGate | `market_data/quality.py`, `market_data/decision_gate.py` | Entry blocked when WS/quality fails |
| DecisionSnapshot facts | `market_data/decision_snapshot.py`, `strategies/paired_binary/observability.py` | Material decisions carry snapshot_id, source, age, verdict |
| LatencyChain facts | `strategies/paired_binary/latency.py` | trigger→submit→fill chain on activation/material exits |
| ExecutionPlanner evidence | `execution/planner.py`, `market_data/executable_book.py` | `ExecutableBookView`, `PlannerEvidence`, depth at size |
| Market-aware state recovery | `runtime/paired_binary_recovery.py` | Token mismatch → reset; terminal reset on new pair |
| Terminal state reset on new token pair | `runtime/paired_binary_recovery.py`, `runtime/config.py` | `allow_terminal_state_resume: false` default |
| Open-exposure shutdown force-flatten | `runtime/paired_binary_shutdown.py` | Reduce-only flatten at max_runtime / shutdown |
| Reduce-only min-notional bypass for emergency exits | `risk/reduce_only_notional.py`, `risk/engine.py` | Shutdown / urgent exits bypass min-notional when configured |
| Windows-safe persisted-state save | `runtime/paired_binary_run.py` (finally flush) | Persist + health facts on in-process exit |
| Market timing diagnostics | `strategies/paired_binary/market_timing.py` | Phase classification; **observability only today** |
| No-entry summary facts | `strategies/paired_binary/no_entry_summary.py` | Aggregated skip reasons at loop end |
| Validator classifications | `scripts/validate_paired_binary_phase2_live_run.py` | lifecycle / observation / force-flatten pass-fail |

### 2.2 Phase 2 conclusion

```text
Infrastructure is now good enough to diagnose strategy behavior.
Remaining problems are mostly survival / strategy-management problems, not data-backbone failures.
```

Phase 1 builds **on top of** this backbone. All new survival exits must still flow: `Intent → RiskEngine → ExecutionPlanner → SingleWriterOMS`, with Phase 2 quality and reduce-only guards intact.

---

## 3. Evidence from recent live runs

Source: `var/reporting/live_runs_review.json` — five WS-primary tiny live reruns (`paired_binary_ws_primary_live{1..5}_*`), scenario `live_paired_binary_tiny_ws_primary.yaml`, strategy params from `config/strategies/paired_binary.yaml`.

| Run | Classification | Entry | Outcome |
|-----|----------------|-------|---------|
| live1 | `PHASE2_WS_LIFECYCLE_PASS` | yes 0.54 / no 0.47 (~balanced) | NO stop → survivor YES repriced to 0.8671 → TP at 0.89 → **+$1.20** (+0.24/pair) |
| live2 | `PHASE2_WS_OBSERVATION_PASS` | none | No-buy / observation |
| live3 | `PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS` | yes 0.51 / no 0.50 (~balanced) | NO stop → survivor YES target 0.8271 → **max_runtime force-flatten** → +$0.35 (+0.07/pair) |
| live4 | `PHASE2_WS_OBSERVATION_PASS` | none | No-buy / observation |
| live5 | `PHASE2_WS_OPEN_EXPOSURE_FORCE_FLATTEN_PASS` | yes 0.51 / no 0.50 | YES stop → survivor NO target 0.8071 → **max_runtime force-flatten** → **-$0.40** (-0.08/pair) |

### 3.1 Observed patterns

1. **Successful entries** happen when the book is fresh and balanced around 0.50 / 0.50.
2. **No-buy runs** usually happen on skewed / resolving / one-sided books.
3. **Stop-loss logic** fires approximately where expected.
4. **Full lifecycle can succeed** when the survivor strongly trends to TP.
5. **When the survivor does not reach the high repriced TP**, force-flatten gives smaller profit or loss.
6. **The current strategy is too dependent** on the survivor reaching an ambitious recovery target.
7. **Artificial max_runtime** can cut the strategy before the market naturally resolves (tradable window still open).

### 3.2 Main strategic weakness

```text
After one leg stops, the survivor is asked to recover the loser loss plus desired profit.
This is only realistic when the market strongly trends.
If not, the strategy waits too long or exits by runtime force-flatten.
```

Current repricing (`strategies/paired_binary/pnl.py::reprice_survivor_target_after_loser_exit`) implements **full recovery target** only, using price-delta formulas rather than ledger cashflows. There is no stall detection, trailing stop, reachability downgrade, executable-liquidity gate, or market-lifecycle-aware runtime control on the survivor leg today.

---

## 4. Phase 1 objective

Phase 1 is the **survival and damage-control layer** for paired-binary and future strategies.

| In scope | Out of scope (Phase 3+) |
|----------|-------------------------|
| Lose less when wrong | External BTC / Chainlink / OBI / volatility signals |
| Intelligent survivor exit when price stalls | ML-based alpha |
| Adaptive targets when full recovery is unrealistic | Positive-EV optimization |
| Flatten / reduce risk in unsafe conditions | Capital scaling |
| Run until tradable window ends (not arbitrary runtime) | Removing Phase 2 safety gates |

**Operational goals:**

```text
When the strategy is wrong, lose less.
When the survivor is not moving, exit intelligently.
When the target is unrealistic, adapt.
When market conditions become unsafe, flatten or reduce risk.
When the market remains open, do not exit only because process runtime expired.
```

Phase 1 is **not** Phase 3 alpha research.

**Non-goals (explicit):**

```text
Do not add external BTC signals, Chainlink signals, OBI signals, or volatility prediction signals.
Do not add ML-based alpha or positive-EV tuning.
Do not scale capital.
Do not remove Phase 2 safety gates.
```

---

## 5. Phase 1.0 — Market-aware strategy lifecycle (foundation)

> **This is not a late optional subphase.** Phase 1.0 must land **first**. Until process runtime is decoupled from strategy exit, survivor logic can still be cut by arbitrary `max_runtime_s` (live3/live5 failure mode).

All other Phase 1 subphases (1.1–1.6) depend on a correct market lifecycle clock and loop continuation policy.

### 5.1 Current behavior (problem)

Today, **process runtime and strategy exit are conflated**:

| Layer | Config / code | Behavior |
|-------|---------------|----------|
| Strategy | `PairedBinaryStrategyConfig.max_runtime_s` (default 600; YAML 330) | Drives `max_ticks` in `run_paired_binary_loop` |
| Strategy | `max_holding_time_s` (299) | Monitor timeout exit while both legs active |
| Runtime | `PairedBinaryRuntimeConfig.open_exposure_on_max_runtime` | Force-flatten when tick budget exhausted with open exposure |
| Runtime | `open_exposure_timeout_s` | Extension window for continue policy |
| Diagnostics | `event_end_ts` / `market_timing.py` | Emitted in facts; **does not control loop** |

```1405:1407:src/tyrex_pm/runtime/paired_binary_run.py
    max_ticks = max(1, int(cfg.max_runtime_s / poll_interval)) if cfg.max_runtime_s else 10_000
```

When `tick_budget_exhausted` and exposure remains, Phase 2 force-flatten runs — this terminated live3/live5 survivors while the tradable window was still open.

### 5.2 Design principle

```text
Runtime should be a process-control concept.
Strategy exit should be a strategy/survival concept.
```

### 5.3 What “run until market resolution” means (explicit)

We do **not** mean waiting until final Polymarket settlement or on-chain resolution.

We mean:

```text
Run until the market's tradable window is effectively over,
or until pre-close flatten time,
or until strategy/survival/kill logic exits.
```

Use `event_end_ts` as the **market lifecycle clock** where available (`market_timing.py::resolve_market_timing_metadata`).

| Concept | Meaning |
|---------|---------|
| Tradable window | Time during which CLOB orders can reasonably be placed and filled |
| Window end | `event_end_ts` (scenario metadata or market API cache) |
| Pre-close flatten | Begin reduce-only flatten when `now >= event_end_ts - flatten_before_event_end_s` |
| Settlement | **Out of scope** — process may exit flat before settlement |

### 5.4 `max_runtime_s` semantics (locked)

| Condition | `max_runtime_s` role |
|-----------|---------------------|
| `event_end_ts` **known** | **Must not** be a normal strategy exit. Loop continues until lifecycle/survival exits or pre-close flatten. `max_runtime_s: null` recommended. |
| `event_end_ts` **unknown** | **Fallback process-control only.** Use `fallback_max_runtime_s`; emit warning fact. Not a strategy TP/SL substitute. |
| Operator shutdown / hard kill | Phase 2 open-exposure policy applies regardless of lifecycle clock |

Deprecate (do not remove immediately): `paired_binary.max_runtime_s` on **strategy** YAML — migrate to `runtime.strategy_lifecycle` with deprecation warning.

### 5.5 Config (canonical keys)

```yaml
runtime:
  strategy_lifecycle:
    mode: market_aware                    # market_aware | fixed_duration | run_once
    exit_clock_source: event_end_ts       # primary clock; future: condition_end_ts, etc.
    max_runtime_s: null                   # null when market end known — NOT a strategy exit
    fallback_max_runtime_s: 900           # process-control when exit clock unknown
    flatten_before_event_end_s: 20        # validation default; tune 20–30s using LatencyChain evidence
    block_new_entry_phases: [near_close, closed]
    emit_unknown_market_end_warning: true

  paired_binary:
    poll_interval_s: 1.0
    open_exposure_on_max_runtime: force_reduce_only_exit   # shutdown / fallback runtime only
    open_exposure_timeout_s: 300
    market_timing_diagnostics:
      enabled: true
      near_close_window_s: 45             # aligns with block_new_entry phase "near_close"
```

**Pre-close flatten default:** Start with **`flatten_before_event_end_s: 20`** (validation default). Five seconds is too tight for live 5-minute Polymarket books given FAK latency, reconnect gaps, and reduce-only retry paths observed in Phase 2. Tune toward **20–30 seconds** using Phase 2 `LatencyChain` and execution evidence before tightening.

### 5.6 Guardrails

| Condition | Behavior |
|-----------|----------|
| `event_end_ts` **known** | Loop until strategy/survival exit, pre-close flatten, operator stop, or kill switch. **No tick-budget stop from old `max_runtime_s`.** |
| `event_end_ts` **unknown** | Use `fallback_max_runtime_s`; emit `strategy_runtime_fallback_max_runtime`. |
| Market **closed** (`now >= event_end_ts`) | Block new entry; flatten open exposure; else `manual_intervention_required`. |
| **Pre-close window** | Block new entry; emit `strategy_lifecycle_pre_close_flatten_required`; flatten open exposure. |
| Process **shutdown signal** | Phase 2 open-exposure shutdown (`paired_binary_shutdown.py`). |

### 5.7 Implementation (Phase 1.0)

**Module:** `src/tyrex_pm/runtime/strategy_lifecycle.py`

| Type / function | Responsibility |
|-----------------|----------------|
| `StrategyRuntimePolicy` | Config bundle from `runtime.strategy_lifecycle` |
| `MarketLifecycleGuard` | Entry block, pre-close flatten, loop continuation |
| `should_continue_loop(...)` | Returns `LoopDecision(continue, reason)` |
| `should_block_new_entry(...)` | Entry gating (see §5.8A) |
| `should_pre_close_flatten(...)` | True when inside flatten window with open exposure |

**Integration in `paired_binary_run.py`:**

- Replace `max_ticks` / `tick_budget_exhausted` with lifecycle evaluation each tick.
- Keep `open_exposure_on_max_runtime` for **operator shutdown and fallback-only** runtime — not normal survivor monitoring.

**Facts:** `strategy_runtime_decision`, `strategy_runtime_fallback_max_runtime`, `strategy_lifecycle_entry_blocked`, `strategy_lifecycle_pre_close_flatten_required`.

---

## 5.8 Decision flows — entry gating vs open-exposure management

Phase 1 splits **new-entry gating** from **open-exposure management**. Do not merge these into a single precedence list.

### A. New-entry gating

Before opening new risk, **block entry** if any of these apply:

```text
Note: If survival.enabled is false, skip survival-specific gates only. Do not block entry for that
reason alone. Still apply Phase 2 gates and runtime lifecycle gates (items 1–9 below).

1. kill switch active (Phase 1.6 or existing soft kill)
2. market near_close or closed (market timing phase)
3. event_end_ts unknown AND no fallback_max_runtime_s policy configured
4. DataQualityGate fails (Phase 2)
5. not enough remaining time for minimum survival window (configurable min_survival_window_s)
6. book not fresh / not balanced / not executable (entry_eval + executable evidence)
7. spread too wide (strategy spread gates)
8. depth insufficient at intended entry size (ExecutableBookView)
9. strategy_lifecycle entry block from MarketLifecycleGuard
```

Emit `strategy_lifecycle_entry_blocked` with reason code and evidence.

### B. Open-exposure management

When there is open exposure, evaluate exits in this **priority order** (first actionable match wins):

```text
1. Operator hard stop / process shutdown / hard kill (Phase 2 shutdown + future Phase 6)
2. Market closed or pre-close flatten window (Phase 1.0)
3. Existing stop-loss / take-profit (Phase 4.6 monitor — must use executable evidence when survival enabled)
4. Survival exits (only when survival.enabled):
   a. trailing stop trigger
   b. stall / no-progress exit
   c. reachability downgrade or exit
   d. economics early exit
5. Fallback max_runtime ONLY when event_end_ts unknown (process-control)
6. Continue holding — emit strategy_runtime_decision with reason
```

**Nuance (locked):**

```text
Normal TP can still be taken before pre-close flatten if executable.
Emergency shutdown, hard kill, or market-close flatten must NOT be suppressed by normal TP/SL logic.
```

Pre-close flatten and shutdown paths **preempt** survival hold logic. Survival trailing/stall/reachability never block mandatory flatten.

---

## 6. Phase 1 subphases (1.1–1.6)

Subphases 1.1–1.6 implement survival mechanics **after** Phase 1.0 foundation lands. All are gated by `survival.enabled` (default `false`).

### Phase 1.1 — Ledger-based survivor target policy

**Current state:** Single mode — full recovery via price-delta formula in `pnl.py`. Not ledger/cash based.

**Principle (locked):**

```text
Do not treat full recovery as the default behavior once the loser exits.
Full recovery is only allowed when it is reachable and executable.
Otherwise downgrade to small_profit, breakeven, or small_loss.
```

**Module:** `src/tyrex_pm/survival/target_policy.py` — `SurvivorTargetPolicy`

#### Ledger-based target computation

Implementation must compute required survivor exit price from **actual runtime economics**, not conceptual price deltas alone.

**Inputs (from state, OMS evidence, ledger):**

```text
yes_entry_qty / no_entry_qty
yes_entry_price / no_entry_price  (or entry cash / qty)
loser_exit_qty
loser_exit_price                  (or exit cash / qty)
survivor_remaining_qty
total_entry_cost                  (matched entry cashflows)
loser_exit_proceeds               (matched loser exit cashflow)
estimated_fees                    (if available; else config bps)
slippage_buffer
planner executable exit estimate  (SurvivalExitPlanner, when available)
```

**Core formula:**

```text
required_survivor_exit_price =
  (
    target_total_net
    + total_entry_cost
    - loser_exit_proceeds
    + estimated_fees
    + slippage_buffer
  )
  / survivor_qty
```

Where `target_total_net` is derived from the selected mode:

| Mode | `target_total_net` (per lifecycle, not per-share) | When to apply |
|------|---------------------------------------------------|---------------|
| `full_recovery` | `desired_net_profit` (from pair budgets) | Reachability **reachable** + executable |
| `breakeven` | `0` | Reachability **weak** |
| `small_loss` | `-max_acceptable_loss_usd` | Reachability **unlikely** |
| `small_profit` | `+min_profit_floor_usd` | Moderate progress; take modest win |
| `dynamic` | Select among above via reachability + stall | Recommended when survival enabled |

**Binary market classification (after computing `required_survivor_exit_price`):**

```text
if required_price > 1.0:
    classify as IMPOSSIBLE → emit survivor_target_impossible; downgrade immediately

if required_price > max_reasonable_exit_price:    # config, e.g. 0.98 for binary
    classify as UNREALISTIC → emit survivor_target_unreachable; downgrade

if required_price exceeds current executable bid by too much given time remaining:
    classify as WEAK or UNLIKELY (reachability module)
```

**Trigger price:** Apply slippage buffer to produce `trigger_target` for monitor comparison — but comparison must use **executable bid/VWAP**, not touch alone (§7).

**Orchestration:**

- On loser fill → `SurvivorTargetPolicy.select_mode(...)` → compute ledger price → classify → emit `survivor_target_selected`.
- Store `survivor_target_mode`, baseline for stall detection (`survivor_bid_0`), in `SurvivorLegState` (`survival/models.py`).

---

### Phase 1.2 — Survivor trailing stop

**Gap:** Protects favorable movement only **after** price moves favorably. Does not address stall (see 1.3).

**Module:** `src/tyrex_pm/survival/trailing_stop.py`

| Parameter | Purpose |
|-----------|---------|
| `arm_after_executable_gain` | Minimum favorable move in **executable bid/VWAP**, not touch |
| `trail_distance` | Distance below peak executable bid |
| `min_profit_lock` | Floor once armed |
| `arm_delay_s` | Avoid immediate stop-out after loser exit |
| `require_fresh_book` | DataQualityGate / max book age |
| `max_spread_to_arm` | Spread gate |
| `min_depth_fraction` | `available_depth / exit_size` from ExecutableBookView |
| `disable_near_close_s` | Defer to pre-close flatten |

**Arming rule (locked):** Trailing stop **only arms** when book freshness, spread, and depth gates pass **and** executable evidence confirms favorable movement.

**States:** `DISARMED → ARMED → TRIGGERED → EXIT_PENDING`

**Trigger rule (locked):** Fire on **executable bid/VWAP** crossing trail floor, not touch bid alone.

**Rollout:** `enforcement_mode: advisory` first; `enforce` after replay.

**Facts:** `survivor_trailing_stop_armed`, `survivor_trailing_stop_triggered`.

---

### Phase 1.3 — Target reachability score

**Module:** `src/tyrex_pm/survival/reachability.py` — `TargetReachabilityScorer`

**Purpose:** Decide whether selected survivor TP is realistic; drive target mode selection and early exit.

**Inputs (executable-first):**

| Input | Source |
|-------|--------|
| Distance to target | executable bid/VWAP vs `required_survivor_exit_price` |
| Time remaining | `seconds_to_close` from `event_end_ts` |
| Spread | LegBook |
| Depth at size | `ExecutableBookView.available_depth`, `available_depth_fraction` |
| Expected slippage | `PlannerEvidence.expected_slippage` |
| Book velocity | `market_data/features.py` v0 or bid delta window |
| Market timing phase | `market_timing.classify_market_timing_phase` |
| Quality instability | recent quality/reconnect transitions |

**Outputs:**

| Verdict | Meaning | Suggested action |
|---------|---------|------------------|
| `reachable` | Executable path to target before close | Allow `full_recovery` or `small_profit` |
| `weak` | Uncertain | Downgrade to `breakeven` / `small_profit` |
| `unlikely` | Not achievable | Downgrade to `small_loss` or early exit |

**Default:** `enforcement_mode: advisory`. Behavior change only when `enforce`.

**Facts:** `survivor_reachability_scored`, `survivor_target_unreachable`, `survivor_target_impossible`.

---

### Phase 1.3b — Survivor stall / no-progress exit

**Objective addressed:**

```text
When the survivor is not moving, exit intelligently.
```

Trailing stop protects after favorable movement. Reachability judges whether a target **looks** realistic. **Stall detection** catches the live-run failure mode: survivor remains open, target is ambitious, but **price does not progress** toward it.

**Module:** `src/tyrex_pm/survival/stall_exit.py` — `SurvivorStallDetector`

(Do not bury stall logic inside reachability alone — separate module with shared inputs.)

#### State recorded at loser exit

```text
survivor_bid_0              # executable bid at loser exit (not touch-only)
selected_target             # required_survivor_exit_price / trigger_target
seconds_to_close_0
loser_exit_ts
available_survival_time     # seconds_to_close_0 - flatten_before_event_end_s (min floor)
```

#### Evaluation each survivor tick

```text
progress_to_target =
  (current_executable_bid - survivor_bid_0)
  / max(selected_target - survivor_bid_0, epsilon)

elapsed_fraction =
  elapsed_since_loser_exit
  / max(available_survival_time, epsilon)
```

**Stall condition (heuristic v1):**

```text
IF elapsed_fraction >= stall_threshold_fraction
   AND progress_to_target < min_progress_ratio
   AND elapsed_since_loser_exit >= check_after_s
THEN stall_detected
```

Also fire if `elapsed_since_loser_exit >= max_stall_s` with progress below floor.

**Actions (`action_when_stalled`):**

| Action | Behavior |
|--------|----------|
| `downgrade` | Downgrade target mode (breakeven → small_loss) |
| `exit_small_loss` | Trigger survivor early exit at small_loss target |
| `exit_market` | Executable market exit via SurvivalExitPlanner |

**Config:**

```yaml
survival:
  stall_exit:
    enabled: true
    enforcement_mode: advisory       # advisory | enforce
    min_progress_ratio: "0.35"
    stall_threshold_fraction: "0.40"   # elapsed_fraction threshold
    check_after_s: 20
    max_stall_s: 60
    action_when_stalled: downgrade     # downgrade | exit_small_loss | exit_market
```

**Facts:** `survivor_stall_detected`, `survivor_progress_evaluated` (each evaluation tick, deduped).

**Integration order in monitor:** After TP check, before reachability downgrade — stall can trigger even when reachability still says "weak" not "unlikely".

---

### Phase 1.4 — Depth-aware executable survival exit adapter

**Module:** `src/tyrex_pm/survival/exit_planning.py` — `SurvivalExitPlanner`

Wraps `ExecutionPlanner` + `ExecutableBookView` for all survival-triggered exits.

**Rule (locked):**

```text
No Phase 1 survival exit should be considered reachable or triggered only because
the touch bid crossed a threshold.
```

**Required evidence for survival decisions:**

```text
ExecutableBookView
depth at intended exit size
sweep VWAP
available_depth_fraction
expected slippage
planner evidence (snapshot_id, book_age_ms, quality_verdict)
book freshness
spread
```

**Applies to:** target reachability, trailing stop arming/trigger, economics gate, stall exit, early exit, pre-close flatten (when depth available).

**Partial exit:** If full-size depth insufficient, survival policy may emit **partial reduce** rather than blocked exit (config `allow_partial_survival_exit: true`).

**Fact:** `survivor_executable_exit_evaluated` on each material survival exit evaluation.

**Implementation order:** Land **before** enforcing reachability/stall/trailing (see §13).

---

### Phase 1.5 — Fee/slippage-aware PnL gate

**Module:** `src/tyrex_pm/survival/economics.py` — `ExitEconomics`

Ledger-based net estimate using entry cash, loser proceeds, executable survivor exit, fees, slippage.

**Outputs:** `proceed | defer | abort_entry | exit_survivor_early`

**Integration:** Pre-entry (optional when survival enabled), post-loser each tick, pre-close.

**Fact:** `survivor_economics_evaluated`, `survivor_early_exit_triggered`.

---

### Phase 1.6 — Kill switches

**Module:** `src/tyrex_pm/survival/kill_switches.py` + hooks in `risk/engine.py` and runtime loop.

| Switch | Action |
|--------|--------|
| `per_pair_max_loss_usd` | Deny entry; force-flatten pair exposure |
| `daily_max_loss_usd` | Deny all entry |
| `max_failed_lifecycle_count` | Pause strategy |
| `max_consecutive_no_entry` | Pause entry |
| `max_bad_market_quality_streak` | Pause entry |
| `max_manual_intervention_count` | Hard stop until operator reset |

**Fact:** `kill_switch_triggered`.

Hard cancel-all (Phase 6) is **out of scope**.

---

## 7. Executable liquidity requirement (cross-cutting)

Phase 1 survival must not replicate the current monitor pattern of triggering from **touch bid alone** (`monitor.py` compares `yes_book.bid >= state.yes_target`).

| Decision | Touch bid | Executable evidence |
|----------|-----------|---------------------|
| Reachability verdict | Diagnostic only | **Authoritative** |
| Trailing stop arm/trigger | Never alone | **Required** |
| Stall progress ratio | Never alone | **Required** (`current_executable_bid`) |
| TP trigger (when survival enabled) | Never alone | **Required** |
| Economics gate | Never alone | **Required** |
| Pre-close flatten | N/A | Use best available; partial if needed |

When survival is **disabled**, existing Phase 2 touch-based TP/SL behavior is unchanged.

---

## 8. Architecture review

### 8.1 Current spine (unchanged)

```text
MarketStateStore (WS-primary) → DataQualityGate → Strategy/Monitor → Intent
  → RiskEngine → ExecutionPlanner (ExecutableBookView) → SingleWriterOMS
```

### 8.2 Anti-pattern to avoid

```text
paired_binary should orchestrate strategy-specific decisions,
but reusable mechanics should live in reusable modules.
```

No giant `monitor.py` patch. Survival logic lives under `src/tyrex_pm/survival/` and `runtime/strategy_lifecycle.py`.

### 8.3 Package layout

```text
src/tyrex_pm/runtime/
  strategy_lifecycle.py     # Phase 1.0 — MarketLifecycleGuard, StrategyRuntimePolicy

src/tyrex_pm/survival/
  __init__.py
  models.py                 # SurvivorLegState, enums, shared evidence dataclasses
  target_policy.py          # Phase 1.1 — SurvivorTargetPolicy (ledger-based)
  exit_planning.py          # Phase 1.4 — SurvivalExitPlanner
  reachability.py           # Phase 1.3 — TargetReachabilityScorer
  stall_exit.py             # Phase 1.3b — SurvivorStallDetector
  trailing_stop.py          # Phase 1.2 — SurvivorTrailingStop
  economics.py              # Phase 1.5 — ExitEconomics
  kill_switches.py          # Phase 1.6 — KillSwitchManager
```

Paired-binary **orchestrates** only: thin hooks in `monitor.py` and `paired_binary_run.py`.

---

## 9. Module mapping table

| Objective | Strategy | Risk | Execution | Market data | Runtime | Reporting | Config |
|-----------|:--------:|:----:|:---------:|:-----------:|:-------:|:---------:|:------:|
| **1.0 Market lifecycle** | — | — | — | timing metadata | **Primary** | lifecycle facts | `runtime.strategy_lifecycle.*` |
| **1.1 Target policy (ledger)** | orchestrate | — | exit estimate | — | — | target facts | `survival.target_policy.*` |
| **1.2 Trailing stop** | monitor hook | — | executable | depth/freshness | — | trail facts | `survival.trailing_stop.*` |
| **1.3 Reachability** | — | — | planner ev. | **Primary** | timing | reachability facts | `survival.reachability.*` |
| **1.3b Stall exit** | monitor hook | — | executable | executable bid | timing | stall facts | `survival.stall_exit.*` |
| **1.4 Exit adapter** | — | reduce-only bypass | **Primary** | ExecutableBookView | — | executable exit facts | `survival.exit_planning.*` |
| **1.5 Economics** | entry hook | optional | slippage | spread/depth | — | economics facts | `survival.economics.*` |
| **1.6 Kill switches** | — | **Primary** | — | — | loop guard | kill facts | `survival.kill_switches.*` |
| Phase 2 shutdown | — | bypass | planner | quality | shutdown | force_flatten | `runtime.paired_binary.*` |

---

## 10. Proposed reusable components

| Component | Package | Phase |
|-----------|---------|-------|
| `StrategyRuntimePolicy` | `runtime/strategy_lifecycle.py` | 1.0 |
| `MarketLifecycleGuard` | `runtime/strategy_lifecycle.py` | 1.0 |
| `SurvivorTargetPolicy` | `survival/target_policy.py` | 1.1 |
| `SurvivalExitPlanner` | `survival/exit_planning.py` | 1.4 |
| `TargetReachabilityScorer` | `survival/reachability.py` | 1.3 |
| `SurvivorStallDetector` | `survival/stall_exit.py` | 1.3b |
| `SurvivorTrailingStop` | `survival/trailing_stop.py` | 1.2 |
| `ExitEconomics` | `survival/economics.py` | 1.5 |
| `KillSwitchManager` | `survival/kill_switches.py` | 1.6 |
| `SurvivorLegState` | `survival/models.py` | skeleton |

---

## 11. Config design

### 11.1 Global default (production-safe)

```yaml
# config/strategies/paired_binary.yaml — survival OFF by default
survival:
  enabled: false
```

**Acceptance requirement:** With `survival.enabled: false`, Phase 2 lifecycle validation passes unchanged.

### 11.2 Phase 1 tiny-live scenario overlay

```yaml
# config/scenarios/live_paired_binary_phase1_tiny.yaml (proposed)
survival:
  enabled: true

  target_policy:
    mode: dynamic
    small_loss_max_usd_per_pair: "0.05"
    small_profit_min_usd_per_pair: "0.03"
    max_reasonable_exit_price: "0.98"
    slippage_buffer: "0.005"

  exit_planning:
    require_executable_evidence: true
    allow_partial_survival_exit: true
    min_depth_fraction: "0.8"

  reachability:
    enforcement_mode: advisory

  stall_exit:
    enabled: true
    enforcement_mode: advisory
    min_progress_ratio: "0.35"
    stall_threshold_fraction: "0.40"
    check_after_s: 20
    max_stall_s: 60
    action_when_stalled: downgrade

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

  economics:
    enabled: true
    enforcement_mode: advisory
    min_acceptable_net_usd_per_pair: "-0.02"
    estimated_fee_bps: "0"
    exit_survivor_if_expected_net_below: "-0.05"

  kill_switches:
    per_pair_max_loss_usd: "0.50"
    daily_max_loss_usd: "5.00"
    max_failed_lifecycle_count: 3
    max_consecutive_no_entry: 10
    max_bad_market_quality_streak: 5
    max_manual_intervention_count: 2

runtime:
  strategy_lifecycle:
    mode: market_aware
    exit_clock_source: event_end_ts
    max_runtime_s: null
    fallback_max_runtime_s: 900
    flatten_before_event_end_s: 20
    block_new_entry_phases: [near_close, closed]
    emit_unknown_market_end_warning: true
```

### 11.3 Enabling enforcement (after validation)

Switch individual modules from `advisory` → `enforce` only after replay + tiny-live confirm no regression:

```yaml
survival:
  reachability:
    enforcement_mode: enforce
  stall_exit:
    enforcement_mode: enforce
  trailing_stop:
    enforcement_mode: enforce
```

### 11.4 Deprecations

| Old key | Migration |
|---------|-----------|
| `paired_binary.max_runtime_s` (strategy) | `runtime.strategy_lifecycle.fallback_max_runtime_s`; null when `event_end_ts` known |
| `force_flatten_before_close_s` | Renamed → `flatten_before_event_end_s` |
| `run_until_market_resolution` | Replaced by `mode: market_aware` + `exit_clock_source: event_end_ts` |

---

## 12. Facts / observability design

All survival facts must include sufficient evidence to debug without replaying the full book:

```text
selected target / target mode
current executable bid
touch bid
sweep VWAP
depth fraction (available_depth / exit_size)
seconds_to_close
elapsed since loser exit
progress ratio (stall)
reachability verdict / score
economics verdict
decision action taken
enforcement mode (advisory | enforce)
snapshot_id / planner evidence refs when available
```

### 12.1 Fact types

| Fact type | When |
|-----------|------|
| `survivor_target_selected` | After loser exit; ledger-based mode selection |
| `survivor_target_downgraded` | Mode downgrade from any module |
| `survivor_target_unreachable` | Required price unrealistic but ≤ 1.0 |
| `survivor_target_impossible` | Required price > 1.0 |
| `survivor_reachability_scored` | Periodic survivor tick (deduped) |
| `survivor_progress_evaluated` | Each stall evaluation |
| `survivor_stall_detected` | Stall condition fired |
| `survivor_executable_exit_evaluated` | Material exit planning evaluation |
| `survivor_trailing_stop_armed` | Trail armed (executable gates passed) |
| `survivor_trailing_stop_triggered` | Trail fired |
| `survivor_economics_evaluated` | Entry or survivor tick |
| `survivor_early_exit_triggered` | Forced early exit |
| `strategy_runtime_decision` | Loop continue/hold decision |
| `strategy_runtime_fallback_max_runtime` | Unknown market end |
| `strategy_lifecycle_entry_blocked` | Entry gating fired |
| `strategy_lifecycle_pre_close_flatten_required` | Pre-close window entered |
| `kill_switch_triggered` | Kill switch fired |

### 12.2 Validator updates

- `PHASE1_SURVIVAL_PASS` — survival enabled, lifecycle completes without premature fallback-runtime flatten.
- `PHASE1_RUNTIME_PREMATURE_EXIT` — open survivor flattened before `event_end_ts - flatten_before_event_end_s` due to old max_runtime tick budget.
- Phase 2 classifications unchanged when survival disabled.

---

## 13. Test and validation plan

### 13.1 Unit tests

| File | Coverage |
|------|----------|
| `tests/test_strategy_lifecycle.py` | Known `event_end_ts` → no tick cap; unknown → fallback; pre-close flatten at 20s |
| `tests/test_survival_target_policy.py` | Ledger math; impossible/unrealistic classification; mode selection |
| `tests/test_survival_exit_planning.py` | Executable evidence; partial depth |
| `tests/test_survival_reachability.py` | Verdict boundaries; executable inputs |
| `tests/test_survival_stall_exit.py` | Progress ratio; stall detect; actions |
| `tests/test_survival_trailing_stop.py` | Arm gates; executable trigger |
| `tests/test_survival_economics.py` | Net gate |
| `tests/test_survival_kill_switches.py` | Thresholds |

### 13.2 Integration tests

| File | Coverage |
|------|----------|
| `tests/test_paired_binary_market_lifecycle_runtime.py` | Survivor past old max_runtime when end known |
| `tests/test_paired_binary_survival_monitor.py` | End-to-end with survival enabled, advisory |
| `tests/test_paired_binary_phase2_regression.py` | **survival.enabled: false** → Phase 2 pass |

### 13.3 Replay + live

- Offline replay live1/live3/live5 facts through stall + reachability scorers.
- Tiny-live protocol: 3–5 markets with overlay; pin `event_end_ts`; `max_runtime_s: null`.

### 13.4 Acceptance criteria

- [ ] **With survival disabled, existing Phase 2 lifecycle validation still passes unchanged.**
- [ ] **With `event_end_ts` known, open survivor exposure is not force-flattened only because old `max_runtime_s` elapsed.**
- [ ] **`max_runtime_s` is fallback/process-control only when market lifecycle is known — not normal strategy exit.**
- [ ] Every open-exposure terminal path either exits flat or emits `manual_intervention_required`.
- [ ] `survivor_target_selected` fact emitted after loser exit.
- [ ] Survivor target computation is **ledger-based** (cash/qty inputs in fact payload).
- [ ] Impossible (`> 1.0`) or unrealistic targets classified and downgraded.
- [ ] Reachability and stall modules are **advisory by default**.
- [ ] **No survival enforcement uses touch bid alone**; executable depth/VWAP evidence required.
- [ ] Trailing stop only arms when book freshness, spread, and depth gates pass.
- [ ] `survivor_stall_detected` / `survivor_progress_evaluated` emitted when survivor fails to progress.
- [ ] Pre-close flatten blocks new entries and reduces open exposure before `event_end_ts`.
- [ ] No giant `monitor.py` patch; reusable logic under `survival/` or `runtime/strategy_lifecycle.py`.
- [ ] No strategy exits silently with open exposure (Phase 2 invariant).
- [ ] Force-flatten on shutdown / market close works with reduce-only bypass.

---

## 14. Implementation order

Recommended order — reduces ambiguity and **avoids enforcing survival before executable liquidity is available**:

| Step | Work item | Rationale |
|------|-----------|-----------|
| **1** | **Phase 1.0 — Market-aware lifecycle runtime** | Foundation; fixes live3/live5 premature exit |
| **2** | **Survival package skeleton + `models.py`** | Shared types before modules |
| **3** | **Phase 1.1 — Ledger-based survivor target policy** | Core economics; facts only when survival enabled |
| **4** | **Phase 1.4 — Depth-aware executable exit adapter** | Survival decisions must understand liquidity at size **before enforce** |
| **5** | **Phase 1.3 + 1.3b — Reachability + stall (advisory)** | Scoring without behavior change initially |
| **6** | **Phase 1.2 — Trailing stop (advisory → enforce)** | Requires executable adapter |
| **7** | **Phase 1.5 — Fee/slippage economics gate** | Uses planner + ledger evidence |
| **8** | **Phase 1.6 — Kill switches** | After survival paths stable |
| **9** | **Validator, replay tooling, live tiny protocol** | Classifications + offline replay |
| **10** | **Enable selected `enforce` modes** | Only after replay confirms no regression |

```text
Survival decisions should not be enforced until they understand executable liquidity at intended size.
```

Phase 2 behavior must remain green at step 1 (lifecycle only, survival off) and after every subsequent step with `survival.enabled: false`.

---

## 15. Risks and non-goals

### 15.1 Risks

| Risk | Mitigation |
|------|------------|
| Unknown `event_end_ts` | `fallback_max_runtime_s` + warning |
| Over-aggressive stall/reachability | Advisory default; tune on replay |
| Pre-close window too tight | Start 20s; tune with LatencyChain |
| Touch-bid regression when survival off | Explicit test: Phase 2 regression suite |
| Monitor bloat | LOC review gate; survival/ only |

### 15.2 Non-goals

```text
Do not add Phase 3 prediction signals, external BTC/Chainlink/OBI indicators, ML alpha,
positive-EV tuning, or capital scaling.
Do not remove Phase 2 safety gates.
Do not wait for Polymarket settlement in the trading loop.
```

---

## 16. Final recommendation

**Status: `READY_FOR_PHASE_1_DESIGN_REVIEW`**

Phase 1.0 (market-aware lifecycle) is the **mandatory foundation**, not subphase 1.7. Survival modules default off; executable evidence is required before enforcement; stall detection addresses the observed live-run failure mode directly.

### Open design decisions (for review)

| # | Decision | Options | Recommendation |
|---|----------|---------|----------------|
| 1 | Trailing stop wiring | Inline monitor vs `protection/` overlay registration | Inline for Phase 1.2; revisit overlay if monitor grows |
| 2 | `flatten_before_event_end_s` production value | 20 vs 30s | 20 for validation; tune with LatencyChain |
| 3 | Partial survival exit default | true vs false | `true` for tiny live; document residual risk |
| 4 | `min_survival_window_s` for entry block | 30–60s | 45s for BTC 5m |
| 5 | Package name | `survival/` vs extend `protection/` | **`survival/`** (pair-lifecycle semantics) |

**Docs path:** Repository uses `Docs/Implementation/` consistently. No misspelled `Docs/Implementaion/` folder.

---

## Appendix A — Files reviewed

| Area | Key paths |
|------|-----------|
| Runtime | `paired_binary_run.py`, `paired_binary_shutdown.py`, `paired_binary_recovery.py`, `config.py` |
| Strategy | `monitor.py`, `exit_engine.py`, `pnl.py`, `market_timing.py`, `facts.py` |
| Risk / execution / market_data | `risk/engine.py`, `execution/planner.py`, `market_data/executable_book.py` |
| Evidence | `var/reporting/live_runs_review.json` |
| Validators | `scripts/validate_paired_binary_phase2_live_run.py` |

---

## Appendix B — Document revision summary

| # | Item |
|---|------|
| 1 | Phase 1.0 foundation naming; removed confusing "1.7 lands first" |
| 2 | Tradable-window semantics; config keys; flatten 20s default |
| 3 | Stall/no-progress module (`stall_exit.py`) |
| 4 | Ledger-based target policy + impossible/unrealistic classification |
| 5 | Executable liquidity requirement (cross-cutting §7) |
| 6 | Split entry gating vs open-exposure precedence (§5.8) |
| 7 | `survival.enabled: false` default; overlay rollout |
| 8 | Revised implementation order (exit adapter before enforce) |
| 9 | Expanded acceptance criteria |
| 10 | New facts with required evidence fields |
