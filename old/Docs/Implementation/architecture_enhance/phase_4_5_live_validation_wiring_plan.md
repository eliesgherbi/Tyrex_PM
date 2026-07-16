# Phase 4.5 — Live Validation Wiring Plan

**Program:** [README.md](README.md) · **Parent:** [phase_4_5_live_validation_harness.md](phase_4_5_live_validation_harness.md)

**Status:** **approved · implemented** (Tasks 1–10). Live operator runs for Levels 3/5/6 are **live-ready** — not live-proven until facts are inspected after each command.

> **Procedural now, event-ready later.** Runtime orchestration components (`FinalityWaiter`, `ProtectionSupervisor`) have clean boundaries so a future event bus can call the same methods without rewriting strategies, protection, or the planner.

---

## 0. Approved corrections (locked)

### Correction 1 — Protection registration boundary

Protection registers **only after allocation-final evidence**. For live, this means **CONFIRMED** per `fill_state.is_allocation_final(status)`. `allocation_buy_applied` alone is **not** sufficient when produced from MATCHED-only evidence.

Implementation boundary: `fill_state.is_allocation_final(status)`.

Do **not** say "protection registers after `allocation_buy_applied`" without clarifying that the allocation event must be backed by allocation-final / CONFIRMED evidence.

### Correction 2 — Level 2 status

**Level 2 = live-validated.** Facts from operator runs prove: `MarketStateStore` populated, `best_bid` / `best_ask` / `mid` available, `is_stale: false` while fresh, no `intent_created`, no `oms_submit`.

### Correction 3 — No fixtures in live mode

When `execution_mode=live`, config/preflight **rejects** (fail closed):

- `use_fixture_book=true`
- `seed_allocation_qty` set
- `allow_seed_allocation=true`
- fixture market data injection
- manual fake protection registration (`register_protection_manual`)

| Mode | Fixtures |
|------|----------|
| shadow | allowed (explicit opt-in) |
| fixture scenario | allowed |
| live | **forbidden** |

Stale-book deny (Level 4) remains **shadow-only synthetic** by design.

### Correction 4 — Level 6 result language

| Label | Proven when |
|-------|-------------|
| **Level 6A — supervisor wiring proven** | Real `protection_register`, real `MarketStateStore`, `protection_tick` occurs, no fixture book, no seed allocation, supervisor exits cleanly |
| **Level 6B — TP/SL trigger proven** | `protection_trigger` → `ExitIntent urgency=urgent` → `execution_plan execution_style=FAK` → `risk_decision phase="planned"` → `oms_submit` SELL |

If the market does not move enough to trigger, a run may prove **6A** but not **6B**.

---

## 1. Executive summary

### What is currently blocking true live Level 3?

**Label: IMPLEMENTED · live-ready**

Previously: `inject_fixture_book()` ran unconditionally for `urgent_exit` under live. **Fixed:** live path uses real `MarketStateStore`; fixtures gated to shadow only; dedicated live scenario YAML; preflight allocation/venue checks; config rejects seed/fixture flags in live.

### What is currently blocking true live Level 5?

**Label: IMPLEMENTED · live-ready**

Previously: harness exited before user-WS CONFIRMED; MATCHED-only `allocation_buy_applied` did not register protection. **Fixed:** `FinalityWaiter` polls for allocation-final evidence; `register_protection_after_finality` only when `fill_state.is_allocation_final`; pipeline hook deferred when `wait_for_confirmed=true`.

### What is currently blocking true live Level 6?

**Label: IMPLEMENTED · live-ready (6A); market-dependent (6B)**

Previously: synthetic seed + manual tick only. **Fixed:** `ProtectionSupervisor` periodic loop; `protection_trigger_live` mode; real `MarketStateStore`; no fixtures/seed in live. **6B** still depends on market movement within `max_runtime_s`.

### What is already implemented and should not be rewritten?

**Label: READY**

| Component | Location | Notes |
|-----------|----------|-------|
| Generic signal/strategy dispatch | `pipeline.process_signals` | Keep |
| Validation harness strategy | `strategies/validation_harness/` | Keep — intents only |
| ExecutionPlanner urgent FAK path | `execution/planner.py` | Keep — unit-tested |
| `validate_planned_order` | `risk/planned_order.py` | Keep |
| `fill_state` finality table | `state/fill_state.py` | Keep — authoritative |
| Protection package | `protection/*` | Keep — monitor, registry, trigger_eval, sizing |
| Protection registration hook | `protection_runtime.maybe_register_protection_after_buy` | Keep — extend with waiter, don't bypass |
| Protection tick → pipeline routing | `protection_runtime.run_protection_tick` → `process_intent_work_unit` | Keep |
| MarketStateStore + REST bootstrap | `state/market_store.py`, `market_data_runtime.py` | Keep |
| Live Level 1 + 2 validation | harness `normal_entry`, `market_data_readonly` | **Live-validated** — do not rewrite |

### What is synthetic by design and should stay synthetic?

**Label: SYNTHETIC BY DESIGN**

| Item | Reason |
|------|--------|
| **Level 4 — stale-book deny** | Objective is internal fail-closed planner logic (`stale/missing book → deny → no OMS`). Injecting stale fixture book is the correct test; live fresh book would not prove deny. |
| Shadow `seed_allocation_qty` | Allowed only when `execution_mode=shadow` or explicit `validation.allow_seed_allocation=true` in fixture scenarios |
| Shadow `inject_fixture_book` | Allowed when `execution_mode=shadow` or `validation.use_fixture_book=true` |
| Level 6 **shadow** trigger modes | Keep as architecture regression path alongside live supervisor path |

---

## 2. Current live-validation status table

| Level | Goal | Current status | Real live possible now? | Blocker | Required fix | Risk |
|-------|------|----------------|--------------------------|---------|--------------|------|
| **1** — normal entry + planner | Tiny live BUY through generic path + planner | **READY** · live-validated | Yes | None | None — done | ~$5 BUY |
| **2** — market data read-only | REST bootstrap, snapshot health, no orders | **READY** · live-validated | Yes | Slow shutdown (background WS tasks) | Optional: skip heavy supervisors for readonly mode | None (no orders) |
| **3** — urgent FAK exit | Real SELL vs owned allocation, FAK planner, live book | **IMPLEMENTED · live-ready** | Yes (after Level 1 position) | Operator run + facts not yet inspected | Run `live_validation_urgent_exit`; verify no fixture evidence | Real SELL (~existing position size) |
| **4** — stale-book deny | Planner deny, no OMS submit | **SYNTHETIC BY DESIGN** · shadow-validated | No (not meaningful) | Live fresh book prevents deny unless faked | Keep shadow-only; document pass criteria | None |
| **5** — protection register | BUY → CONFIRMED → `protection_register` | **IMPLEMENTED · live-ready** | Yes | Operator run + facts not yet inspected | Run `live_validation_protection_register`; verify `finality_wait final=true` | ~$5 BUY (duplicate if no existing position) |
| **6A** — protection supervisor wiring | Real register → tick loop → clean stop | **IMPLEMENTED · live-ready** | Yes | Operator run + facts not yet inspected | Run `live_validation_protection_trigger`; verify ticks, no fixtures | None if no trigger |
| **6B** — protection TP/SL trigger | Trigger → FAK SELL | **IMPLEMENTED · market-dependent** | Yes if market moves | Price may not cross threshold | Tight pct or volatile token; `fail_if_no_trigger=false` for wiring-only pass | SELL on trigger (~position size) |

---

## 3. Architecture principles that must be preserved

Every change in this plan MUST preserve:

1. Strategies emit intents only.
2. Strategies do not call venue clients.
3. Strategies do not submit/cancel directly.
4. Protection emits `ExitIntent` only.
5. Protection does not submit/cancel directly.
6. `RiskEngine` remains mandatory.
7. `ExecutionPlanner` remains between risk pre-check and OMS when enabled.
8. `validate_planned_order` remains mandatory after planner.
9. `SingleWriterOMS` remains the only submit/cancel writer.
10. `AllocationLedger` remains mandatory for all SELL paths.
11. Every SELL clamps to owner allocation and venue availability.
12. `MarketStateStore` is the source for live book state when `market_data.enabled=true`.
13. Fixtures may be used only in shadow/fixture scenarios, never silently in live.
14. Runtime remains procedural now, but component boundaries must be event-ready.

---

## 4. Level 3 fix — true live urgent FAK exit

### Current problem

```text
validation_harness urgent_exit can run under live execution, but inject_fixture_book()
is called whenever market_data.enabled=true (validation_harness_run.py ~L239–248).
Order → real LiveOMS; planner worst-price → synthetic book.
```

### Proposed behavior

```text
If execution_mode=live AND market_data.enabled=true:
  urgent_exit MUST use coord.market_state populated by REST bootstrap/refresh.
  Do NOT call inject_fixture_book() unless validation.use_fixture_book=true
  (explicit opt-in — rejected in live by default).

If execution_mode=shadow OR validation.use_fixture_book=true:
  inject_fixture_book() allowed (shadow regression).

If live AND no fresh book in MarketStateStore:
  ExecutionPlanner denies (planner_stale_book / planner_missing_book).
  No OMS submit. Emit execution_plan fact with approved=false.
```

### Config flags

**Strategy (`ValidationHarnessStrategyConfig` extensions):**

```yaml
validation:
  mode: urgent_exit
  owner_id: validation_harness
  token_id: "<token>"
  side: SELL
  size: null                    # default: sell all available allocation (clamped)
  limit_price: "0.01"             # fallback floor for planner if ever enabled
  use_fixture_book: false       # default false; true only shadow/fixture
  allow_seed_allocation: false  # default false; live MUST reject seed_allocation_qty
```

**Scenario (`config/scenarios/live_validation_urgent_exit.yaml`):**

```yaml
execution_mode: live

runtime:
  market_data:
    enabled: true
    max_book_age_s: 5
  execution:
    planner:
      enabled: true
      require_fresh_book_for_urgent: true
      max_book_age_s: 5

risk:
  notional:
    min_usd: "1"
    max_usd: "10"
    max_policy: cap
  deployment:
    token_cap_usd: "15"
    portfolio_cap_usd: "25"
  venue_min_size:
    enabled: true
    policy: deny
    default_min_size: "5"
  inventory:
    sell_requires_venue_position: true
  readiness:
    require_user_ws_live: true
    require_heartbeat_live: true
    require_wallet_sync: true
    max_wallet_age_s_live: 120
```

**Dedicated strategy file (recommended):**

```text
config/strategies/validation_harness_urgent_exit_live.yaml
```

### Code changes (minimal)

| File | Change |
|------|--------|
| `validation_harness_run.py` | Gate `inject_fixture_book` on `use_fixture_book` + not live; live path calls `bootstrap_market_state` only |
| `runtime/config.py` | Add `use_fixture_book`, `allow_seed_allocation` to `ValidationHarnessStrategyConfig`; fail closed if live + seed |
| `validation_harness_run.py` | Preflight: assert `ledger.get_available_allocated(owner, token) > 0` and venue position > 0; emit health fact on failure |
| `market_data_runtime.py` | Optional helper: `assert_fresh_book(coord, token_id) -> bool` for preflight facts |

### Preconditions (operator)

```text
1. Prior Level 1 live BUY credited validation_harness allocation on target token_id.
2. WalletStore.positions shows qty > 0 for same token (positions REST/WS synced).
3. Reconcile has NOT clamped owner allocation to zero (allocation_clamped fact absent or allocated_after > 0).
4. No conflicting open SELL orders on token.
```

Run preflight **after** live bootstrap (wallet + positions sync), **before** emitting urgent exit signal.

### Expected live facts

```text
health                  event=validation_harness_preflight ok=true
signal_received         side=SELL source=validation_harness
intent_created          ExitIntent urgency=urgent
risk_decision           pre-check approved
execution_plan          execution_style=FAK planner_reason=planner_urgent_exit_fak
                        evidence from live book (best_bid in plan evidence)
risk_decision           phase=planned approved
oms_submit              planner_reason=planner_urgent_exit_fak
allocation_ledger       allocation sell / exit reservation lifecycle
exit_lifecycle          optional on matched SELL
health                  status=stopped
```

### Fail criteria

```text
- preflight fails (no allocation or no venue position)
- execution_plan approved=false (stale/missing book)
- risk_decision denied
- no oms_submit
- seed_allocation used in live (config parse error — fail closed)
```

---

## 5. Level 5 fix — live protection register with finality wait

### Current problem

```text
Live BUY submit ack → match_status=matched → allocation_buy_applied may appear (immediate credit policy).
protection_register requires fill_state.is_allocation_final(status) → CONFIRMED only.
MATCHED-only allocation_buy_applied does NOT satisfy the registration boundary.
Harness now waits via FinalityWaiter before register_protection_after_finality.
```

### Proposed component: `runtime/finality_waiter.py`

**Responsibility:** Wait for allocation-final evidence for a specific `(owner_id, token_id, correlation_id)` after a BUY submit. No strategy logic. No venue calls from strategies.

**Public API (procedural, event-ready):**

```python
@dataclass(frozen=True)
class FinalityWaitResult:
    final: bool
    status: str | None          # CONFIRMED | MATCHED | timeout | ...
    source: str                 # user_ws | rest_repair | shadow_instant
    waited_s: float
    evidence: dict

async def wait_for_allocation_final(
    coord: RuntimeCoordinator,
    *,
    owner_id: str,
    token_id: TokenId,
    correlation_id: str,
    timeout_s: float,
    poll_interval_s: float = 0.5,
    live_clob_client: object | None = None,
) -> FinalityWaitResult
```

### What counts as final?

Per `fill_state.py`:

| Status | `is_allocation_final` | Protection register? |
|--------|----------------------|----------------------|
| MATCHED | no | no |
| MINED | no | no |
| CONFIRMED | yes | yes |
| timeout | no | no |

Shadow instant fill: waiter returns immediately with `source=shadow_instant`, `status=CONFIRMED`.

### What fact proves finality?

Primary:

```text
allocation_buy_applied   — may appear on MATCHED (NOT sufficient alone for protection register)
user-WS trade event      — wallet_store trade with status CONFIRMED matching token+side
fill_state.is_allocation_final(CONFIRMED) → true  — registration boundary
protection_register      — target outcome fact (only after allocation-final evidence)
```

New health fact emitted by waiter:

```text
health  event=finality_wait
        final=true|false
        status=CONFIRMED|timeout|...
        source=user_ws|rest_repair|...
        correlation_id=...
        waited_s=...
```

Optional: reuse existing wallet trade ring buffer / coord health user-WS timestamps rather than new store mutation.

### Wait algorithm (procedural)

```text
1. Record submit_ts and correlation_id from oms_submit.
2. Loop until timeout:
   a. Check coord.wallet recent trades / user-WS ingested CONFIRMED for token BUY.
   b. If CONFIRMED found for matching correlation or client_order_id → return final.
   c. Optional REST repair: refresh wallet/positions if WS quiet > N seconds.
   d. await sleep(poll_interval_s)
3. On timeout → return final=false.
4. Caller decides fail_if_not_confirmed.
```

### Protection registration after wait

**Do not** register inside the strategy. After waiter returns `final=true`:

```text
protection_runtime.register_protection_after_finality(
    coord, app, owner_id, token_id, entry_price, correlation_id, run_id, sink
)
```

This reuses `ProtectionMonitor.register()` — same path as `maybe_register_protection_after_buy` but invoked explicitly once finality is proven (avoids duplicate register on MATCHED hook).

**Alternative (smaller diff):** Keep `maybe_register_protection_after_buy` on pipeline hook for shadow; for live Level 5 mode, **disable** immediate hook register and only register post-waiter. Prefer single registration path to avoid double-register bugs.

### Harness mode behavior

```yaml
validation:
  mode: protection_register_only
  wait_for_confirmed: true
  confirmed_timeout_s: 90
  fail_if_not_confirmed: true
  notional_usd: "5"

protection:
  enabled: true
  take_profit_pct: "0.10"
  stop_loss_pct: "0.05"
  register_on_buy: true   # live: defer to post-waiter when wait_for_confirmed=true
```

Flow:

```text
1. process_signals → BUY → oms_submit (matched likely)
2. FinalityWaiter.wait_for_allocation_final(...)
3. If final → register_protection → protection_register fact
4. If timeout and fail_if_not_confirmed → health fact + non-zero exit / failed run marker
5. health stopped
```

### Timeout behavior

```text
fail_if_not_confirmed=true  → run marked failed; no protection_register; operator reviews facts
fail_if_not_confirmed=false → health finality_wait final=false; run exits cleanly (soft fail)
```

### MATCHED but no CONFIRMED

```text
allocation_buy_applied may exist (current live policy).
protection_register MUST NOT emit.
finality_wait fact: final=false status=MATCHED_OR_PENDING
Operator: check user-WS connectivity; extend timeout; re-run Level 5.
```

### Reconcile clamps allocation

If `allocation_clamped` drives `allocated_after=0` while waiter pending:

```text
Waiter continues on CONFIRMED wallet evidence (venue truth), not ledger alone.
If venue position > 0 and CONFIRMED arrives → register using entry_price from intent.
Emit health fact noting ledger clamp vs venue position mismatch for operator review.
Do not register on ledger-only MATCHED credit if venue position still 0.
```

### Expected live facts

```text
signal_received
intent_created          EnterIntent BUY
risk_decision           pre-check
execution_plan          planner_normal_entry (if planner on)
risk_decision           phase=planned
oms_submit              matched
allocation_buy_applied  (may be on matched)
health                  event=finality_wait final=true status=CONFIRMED
protection_register     owner_id token_id entry_price thresholds
health                  status=stopped
```

---

## 6. Level 6 fix — live protection TP/SL trigger loop

### Current problem

Shadow modes `protection_trigger_tp/sl` prove architecture with synthetic seed + manual tick. Live requires sustained monitoring of real marks until threshold crossed.

### Proposed component: `runtime/protection_supervisor.py`

**Responsibility:** Periodic procedural loop calling `ProtectionMonitor.tick()` and routing work units through existing pipeline. Reusable by validation harness **and** future guru/production loops.

**Public API:**

```python
async def run_protection_supervisor(
    coord: RuntimeCoordinator,
    app: AppConfig,
    *,
    run_id: RunId,
    strategy: object,           # ValidationHarnessStrategy or future owner strategy
    sink: JsonlSink,
    oms: SingleWriterOMS,
    stop: asyncio.Event,
    tick_interval_s: float = 1.0,
    max_runtime_s: float = 180.0,
    stop_after_trigger: bool = True,
    stop_after_exit_submit: bool = True,
    apply_local_shadow_fill: bool = False,
    live_clob_client: object | None = None,
) -> ProtectionSupervisorResult
```

Config on `ProtectionRuntimeConfig` (or runtime block):

```yaml
protection:
  enabled: true
  tick_interval_s: 1
  max_runtime_s: 180
  stop_after_trigger: true
  stop_after_exit_submit: true
  fail_if_no_trigger: false
  take_profit_pct: "0.005"    # tight for validation — operator chooses token with movement
  stop_loss_pct: "0.005"
```

### Live validation mode: `protection_trigger_live`

New harness mode (or extend existing with `validation.trigger_source: live|fixture`):

```text
protection_trigger_live:
  1. Require existing protection_register (from Level 5) OR run combined Level 5+6 scenario.
  2. Do NOT seed_allocation_qty in live.
  3. Do NOT inject_fixture_book in live.
  4. Start ProtectionSupervisor after registration confirmed.
  5. Supervisor ticks until trigger OR max_runtime_s.
  6. On trigger → existing run_protection_tick routing → process_intent_work_unit.
  7. Stop per stop_after_exit_submit.
```

Combined scenario option (single run, higher notional risk):

```text
protection_register_and_monitor:
  BUY → wait CONFIRMED → register → supervisor loop
```

Prefer **two-run ladder** for minimal exposure: Level 5 then Level 6 on same token.

### MarketStateStore snapshots

```text
Supervisor does NOT fetch books itself.
Relies on:
  - market_data_rest_refresh_loop (already in app.py when market_data.enabled)
  - future market WS ingest → MarketStateStore.apply_snapshot

Each tick: ProtectionMonitor._observe reads coord.market_state.best_bid(token).
Stale book → protection_tick with stale_or_missing_book; no false trigger.
```

### Duplicate trigger dedupe

Already in `ProtectionMonitor`: `entry.triggered = True` before emitting exit work unit. Supervisor must not call register twice. Single active entry per `(owner_id, token_id)` in registry.

### ExitIntent routing

Unchanged:

```text
ProtectionMonitor.tick → list[IntentWorkUnit]
  → process_protection_work_units
    → process_intent_work_unit (each)
      → RiskEngine → ExecutionPlanner → validate_planned_order → OMS
```

### Loop stop conditions

```text
1. stop Event set (app shutdown)
2. max_runtime_s elapsed → health protection_supervisor_timeout
3. stop_after_trigger && protection_trigger fact emitted
4. stop_after_exit_submit && oms_submit for protection exit correlation
5. fail_if_no_trigger && max_runtime_s && no trigger → failed run marker
```

### Event-ready mapping (later)

```text
Procedural: supervisor loop sleep → tick
Event-ready: on MarketSnapshotUpdated → ProtectionSupervisor.on_market_snapshot
             on FillConfirmed → ProtectionSupervisor.on_fill (register if pending)
```

### Expected live facts

```text
protection_register       (from Level 5 or combined scenario)
protection_tick           deduped observed_price
protection_trigger        take_profit | stop_loss
intent_created            ExitIntent urgency=urgent
risk_decision             pre-check + phase=planned
execution_plan            FAK planner_urgent_exit_fak
oms_submit
allocation_ledger         sell applied / reservation released
exit_lifecycle
health                    protection_supervisor_stopped reason=trigger|timeout|submit
```

### Level 6 live risk note

Market may not move enough to trigger within `max_runtime_s`. `fail_if_no_trigger: false` default — run may prove **Level 6A** (supervisor wiring) without **Level 6B** (TP/SL trigger). Operator may re-run with tighter pct or more volatile token. **Do not** fake price in live.

### Level 6A vs 6B pass criteria

**Level 6A — supervisor wiring proven:**

```text
protection_register (real, not manual fixture)
protection_tick with real MarketStateStore marks
no fixture book evidence
no seed_allocation evidence
health protection_supervisor_stopped reason=timeout|trigger|submit
```

**Level 6B — TP/SL trigger proven** (requires market movement):

```text
protection_trigger
intent_created ExitIntent urgency=urgent
execution_plan execution_style=FAK planner_reason=planner_urgent_exit_fak
risk_decision phase=planned
oms_submit SELL
```

---

## 7. Event-ready design

| Component | Procedural now | Event-ready later | Input | Output | Side effects allowed | Side effects forbidden |
|-----------|----------------|-------------------|-------|--------|----------------------|------------------------|
| **MarketStateStore update** | REST refresh loop / `apply_snapshot` | `on_market_snapshot(MarketSnapshot)` | snapshot | none | update store slice | venue submit, ledger mutate |
| **FinalityWaiter** | `wait_for_allocation_final(...)` poll loop | `on_trade_event(TradeFillRecord)` → resolve pending wait | correlation_id, token, timeout | `FinalityWaitResult` | health facts | register protection directly without monitor |
| **ProtectionSupervisor** | `run_protection_supervisor` asyncio loop | subscribe `MarketSnapshotUpdated`, `FillConfirmed` | `ProtectionSupervisorConfig`, coord | `ProtectionSupervisorResult` | health facts, call tick | venue I/O, direct OMS |
| **ProtectionMonitor.tick** | called by supervisor / harness | `on_market_snapshot` → tick | coord, registry state | `list[IntentWorkUnit]` | protection facts | submit, ledger mutate |
| **ValidationHarnessStrategy** | `on_signal` from harness runner | `on_signal(ValidationSignal)` unchanged | signal, ctx | `StrategyResult` | none | all side effects |
| **ExecutionPlanner** | `plan(approved, market_state)` | `on_plan_request(ApprovedIntent, MarketSnapshot)` | approved intent, store | `ExecutionPlanResult` | none | OMS |
| **process_intent_work_unit** | await from pipeline | `on_intent_work_unit(IntentWorkUnit)` | work unit | facts + OMS | risk, planner, OMS, ledger hooks | strategy mutation |

**Design rule:** New loops (waiter, supervisor) are **orchestration only**. They call existing pure-ish methods; they do not duplicate risk/planner/OMS logic.

---

## 8. Exact implementation plan

### Task 1 — config and scenario files

| | |
|-|-|
| **New files** | `config/strategies/validation_harness_urgent_exit_live.yaml`, `validation_harness_protection_register_live.yaml`, `validation_harness_protection_trigger_live.yaml`, `config/scenarios/live_validation_urgent_exit.yaml`, `live_validation_protection_register.yaml`, `live_validation_protection_trigger.yaml` |
| **Changed** | `runtime/config.py` — extend `ValidationHarnessStrategyConfig`, `ProtectionRuntimeConfig` |
| **Behavior** | Explicit live configs; fail closed live+seed |
| **Tests** | Config parse tests for new flags |
| **Risk** | Low |
| **Rollback** | Delete new YAML; revert config parse |

### Task 2 — stop fixture book injection in live urgent_exit

| | |
|-|-|
| **Changed** | `validation_harness_run.py` |
| **Behavior** | Live + `use_fixture_book=false` → never inject |
| **Tests** | `test_live_urgent_exit_does_not_inject_fixture_book`, `test_fixtures_not_used_in_live_unless_explicitly_allowed` |
| **Risk** | Low — may expose stale book denies if bootstrap slow |
| **Rollback** | Revert gating |

### Task 3 — live MarketStateStore book usage for urgent planner

| | |
|-|-|
| **Changed** | `validation_harness_run.py`, optional `market_data_runtime.py` preflight |
| **Behavior** | Bootstrap before urgent signal; deny if not fresh |
| **Tests** | `test_live_urgent_exit_uses_market_state_store`, `test_live_urgent_exit_denies_when_live_book_missing` |
| **Risk** | Medium — live SELL depends on book freshness |
| **Rollback** | Task 2 revert |

### Task 4 — FinalityWaiter component

| | |
|-|-|
| **New** | `runtime/finality_waiter.py` |
| **Behavior** | Poll WS/ wallet for CONFIRMED; emit health facts |
| **Tests** | `test_finality_waiter_returns_on_confirmed`, `test_finality_waiter_times_out` |
| **Risk** | Medium — timing flakes in CI; use mocked coord |
| **Rollback** | Remove module; harness skips wait |

### Task 5 — protection registration wait path

| | |
|-|-|
| **Changed** | `validation_harness_run.py`, `protection_runtime.py` |
| **Behavior** | Level 5 mode: BUY → wait → register; defer MATCHED-only register |
| **Tests** | `test_protection_register_waits_for_confirmed`, `test_protection_register_does_not_register_on_matched_only`, `test_protection_register_emits_fact_after_finality` |
| **Risk** | Medium — duplicate register if hook not deferred |
| **Rollback** | Disable wait flag default false |

### Task 6 — ProtectionSupervisor / tick loop

| | |
|-|-|
| **New** | `runtime/protection_supervisor.py` |
| **Changed** | `app.py` (start supervisor when protection.enabled + mode requires it), `validation_harness_run.py` |
| **Behavior** | Periodic tick; route exits through pipeline |
| **Tests** | `test_protection_supervisor_ticks_until_trigger`, `test_protection_supervisor_routes_exit_intent_through_pipeline`, `test_protection_supervisor_dedupes_triggers`, `test_protection_supervisor_stops_after_exit_submit`, `test_protection_supervisor_timeout_no_trigger` |
| **Risk** | Medium — live loop duration; ensure stop on shutdown |
| **Rollback** | Feature flag `protection.supervisor.enabled=false` |

### Task 7 — validation harness modes update

| | |
|-|-|
| **Changed** | `validation_harness_run.py`, `config.py` mode constants |
| **New modes** | `protection_trigger_live` or `trigger_source: live` |
| **Behavior** | Shadow modes unchanged; live modes forbid seed/fixture |
| **Tests** | Mode matrix tests |
| **Risk** | Low |
| **Rollback** | Revert mode enum |

### Task 8 — facts/reporting updates

| | |
|-|-|
| **Changed** | `schema_v2.py` (if new fact types needed — prefer health payload events first) |
| **New health events** | `finality_wait`, `validation_harness_preflight`, `protection_supervisor_stopped` |
| **Tests** | Fact emission in integration tests |
| **Risk** | Low |
| **Rollback** | Remove payload keys |

### Task 9 — tests

| | |
|-|-|
| **New** | `tests/test_finality_waiter.py`, `tests/test_protection_supervisor.py`, extend `tests/test_validation_harness_runtime.py` |
| **Risk** | None |
| **Rollback** | N/A |

### Task 10 — docs

| | |
|-|-|
| **Changed** | `phase_4_5_live_validation_harness.md`, `OPERATIONS.md`, `live_validation_matrix.md`, this plan → status approved |
| **Risk** | None |

---

## 9. Required tests

All tests below must be added **before or with** implementation (TDD-friendly).

```text
test_live_urgent_exit_does_not_inject_fixture_book
test_live_urgent_exit_uses_market_state_store
test_live_urgent_exit_denies_when_live_book_missing
test_shadow_urgent_exit_can_use_fixture_book_explicitly
test_fixtures_not_used_in_live_unless_explicitly_allowed

test_finality_waiter_returns_on_confirmed
test_finality_waiter_times_out
test_protection_register_waits_for_confirmed
test_protection_register_does_not_register_on_matched_only
test_protection_register_emits_fact_after_finality

test_protection_supervisor_ticks_until_trigger
test_protection_supervisor_routes_exit_intent_through_pipeline
test_protection_supervisor_dedupes_triggers
test_protection_supervisor_stops_after_exit_submit
test_protection_supervisor_timeout_no_trigger
```

Additional recommended:

```text
test_live_config_rejects_seed_allocation_qty
test_validation_harness_preflight_fails_without_allocation
test_protection_supervisor_uses_live_market_state_not_fixture
```

---

## 10. Live commands after implementation

### Level 3 — live urgent exit

```bash
python -m tyrex_pm.runtime.app run \
  --strategy config/strategies/validation_harness_urgent_exit_live.yaml \
  --scenario live_validation_urgent_exit \
  --run-name validation_live_urgent_exit
```

| | |
|-|-|
| **Preconditions** | Level 1 BUY on same token; `validation_harness` allocation > 0; venue position > 0; fresh market book |
| **Expected facts** | preflight ok → signal_received SELL → intent_created urgent → execution_plan FAK → oms_submit → allocation sell |
| **Pass** | `oms_submit` with `planner_urgent_exit_fak`; no fixture book evidence in plan |
| **Fail** | preflight fail; planner deny; no oms_submit |
| **Max notional** | Existing position size (~$5 from prior BUY) |
| **Stop** | Single iteration; process exits after SELL submit + grace |
| **Cleanup** | None if fully matched SELL; verify position flat in UI |

### Level 5 — live protection register

```bash
python -m tyrex_pm.runtime.app run \
  --strategy config/strategies/validation_harness_protection_register_live.yaml \
  --scenario live_validation_protection_register \
  --run-name validation_live_protection_register
```

| | |
|-|-|
| **Preconditions** | `.env` live creds; user-WS enabled; protection.enabled true |
| **Expected facts** | oms_submit BUY → finality_wait final=true → protection_register |
| **Pass** | `protection_register` within `confirmed_timeout_s` |
| **Fail** | finality_wait timeout; no protection_register when fail_if_not_confirmed |
| **Max notional** | ~$5 BUY |
| **Stop** | After register or timeout |
| **Cleanup** | Position + registered protection remain for Level 6 |

### Level 6 — live protection trigger

```bash
python -m tyrex_pm.runtime.app run \
  --strategy config/strategies/validation_harness_protection_trigger_live.yaml \
  --scenario live_validation_protection_trigger \
  --run-name validation_live_protection_trigger
```

| | |
|-|-|
| **Preconditions** | Level 5 completed; `protection_register` active; market moving or tight TP/SL pct; allocation + venue position |
| **Expected facts** | protection_tick → protection_trigger → urgent exit chain → oms_submit SELL |
| **Pass** | Trigger + FAK SELL submit within `max_runtime_s` |
| **Soft pass** | Ticks observed, no trigger, `fail_if_no_trigger=false` — wiring OK, market flat |
| **Fail** | Supervisor error; trigger but no oms_submit; duplicate trigger double-sell |
| **Max notional** | Full protected position on trigger |
| **Stop** | stop_after_exit_submit or max_runtime_s |
| **Cleanup** | Verify protection entry cleared after trigger; position reduced |

---

## 11. What should not be done

Do **not**:

- create another isolated bespoke strategy loop
- bypass `process_signals`
- bypass `process_intent_work_unit`
- bypass `RiskEngine`
- bypass `ExecutionPlanner`
- bypass `validate_planned_order`
- bypass `SingleWriterOMS`
- make protection submit orders directly
- silently use fixture books in live mode
- seed fake allocation in live mode
- call venue clients from strategies/protection
- mark production TP/SL ready before periodic supervisor is integrated in `app.py` for long-running modes

---

## 12. Open questions / risks

| Question | Recommendation |
|----------|----------------|
| Level 3: sell full allocation or fixed size? | Default sell `min(allocation, venue)`; cap via `validation.size` optional |
| Level 5: defer hook register vs duplicate path? | Single path: when `wait_for_confirmed`, skip immediate hook on MATCHED |
| Level 6: combined 5+6 vs two runs? | Two runs default; combined behind explicit flag |
| Level 6: market may not trigger | Document soft pass; operator picks volatile token or tight pct |
| Readonly slow shutdown | Separate optional task: skip user-WS for readonly mode |
| Market WS vs REST only | Phase 4.5 wiring plan assumes REST refresh sufficient for validation; WS market ingest is enhancement not blocker |

---

## Approval checklist

- [x] Architecture principles preserved (§3)
- [x] Level 4 remains synthetic-only (§1, §2)
- [x] FinalityWaiter boundary approved (§5, §7)
- [x] ProtectionSupervisor boundary approved (§6, §7)
- [x] Live commands and pass criteria approved (§10)
- [x] Corrections 1–4 applied (§0)
- [x] Tasks 1–10 implemented (§8)
