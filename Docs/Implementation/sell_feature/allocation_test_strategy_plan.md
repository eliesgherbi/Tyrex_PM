# Allocation test strategy — planning document (P4 validation toy)

**Status:** Implemented (v1 + live hardening cleanup).  
**Prerequisite:** P4 allocation ledger base scope (complete).  
**Blocks before:** P5 ledger-aware guru SELL, P6 TP/SL overlays.

### Live hardening (post first live run)

1. **Owner B block** waits until `ledger[A] > 0` **and** `wallet_position_qty > 0` (live refreshes Data API `/positions` during wait). Timeout → `allocation_test_timeout_position_visible` / `TIMEOUT_POSITION_VISIBLE`.
2. **Resting SELL** (`status=live`): reservation stays active; `allocation_exit_order_live` emitted; **no** `allocation_sell_applied`. Matched SELL (or shadow instant fill) decrements allocation as before.

### P4.1 exit-order lifecycle (implemented)

- Links `reservation_id` (client_order_id) ↔ `venue_order_id` on SELL OMS ack.
- **User WS:** `UPDATE` (`size_matched`), `TRADE` (`CONFIRMED`), `CANCELLATION` → allocation ledger mutations.
- **Reconcile:** promotes `LocalOrder.size_matched` deltas for reserved exits.
- Events: `allocation_sell_applied`, `allocation_partial_fill_applied`, `allocation_released` (reason=`cancelled`|`reject`), with `source` / `filled_qty` / reserved snapshots.

---

## Project patterns review (current codebase)

This section records how the existing system works today, so the toy strategy can reuse the same seams rather than invent parallel paths.

### 1. Strategy wiring in `runtime/app.py`

`cmd_run` loads config via `load_app_config`, then branches on strategy kind:

| Mode | Detection | Strategy instance | Main loop |
|------|-----------|-------------------|-----------|
| `sell_test` | `app.sell_test is not None` | `SellTestStrategy(app.sell_test)` | `_run_sell_test_loop(...)` |
| `guru_follow` | else | `GuruFollowStrategy(app.strategy)` | guru poll + `process_new_guru_signals` |

Shared setup for all runs:

- `RuntimeCoordinator(wallet, orders, health, …)`
- `AllocationLedger` loaded from `{state_dir}/allocation_ledger.json` and attached to `coord.allocation_ledger`
- `coord.allocation_ledger` attached on every run from `var/state/allocation_ledger.json`
- `JsonlSink` on `runs_dir/<run_name>/facts.jsonl`
- Lifecycle sinks: `exit_lifecycle_sink`, `allocation_ledger_sink` (same sink, same `run_id`)

For `sell_test`, `app.py` also:

- Wires `coord.scheduled_exit_demo_try_arm` → `try_arm_sell_test_pending`
- Starts background `scheduled_exit_demo_due_loop` (polls due SELL work units)
- Runs `_run_sell_test_loop`: live readiness → optional auto-pricing → one BUY via `process_intent_work_unit` → waits until `strat.is_done()` or timeout

**Pattern to reuse:** a dedicated `allocation_test_mode` branch mirroring `sell_test_mode`: instantiate toy strategy, wire try_arm if needed, start `scheduled_exit_demo_due_loop` only for owner-A exit scheduling, dedicated `_run_allocation_test_loop`.

### 2. `sell_test` configuration and execution

**Config:** `config/strategies/sell_test.yaml` with `kind: sell_test`.

Parsed in `runtime/config.py` → `SellTestStrategyConfig` on `AppConfig.sell_test` (mutually exclusive with full guru strategy parsing). Placeholder `StrategyConfig` is synthesized so risk/runtime code paths stay stable.

**Execution flow:**

1. Strategy builds `IntentWorkUnit(EnterIntent BUY, correlation_id, extensions)` via `initial_buy_work_units()`
2. Runtime calls `process_intent_work_unit` once for BUY
3. On successful BUY ack, `on_buy_submit_ack` → `register_after_successful_buy` → pending/armed SELL state
4. Background scheduler drains due SELL via `process_scheduled_exit_demo_due` → another `process_intent_work_unit` for SELL
5. `is_done()` requires terminal SELL outcome (not merely intent built)

**Pattern to reuse:** YAML `kind`, dedicated config dataclass, `run_once`, small notional, shadow-first with `live_guru` scenario overlay.

### 3. `IntentWorkUnit` / `process_intent_work_unit`

```python
@dataclass(frozen=True)
class IntentWorkUnit:
    intent: Intent
    correlation_id: str
    intent_fact_extensions: dict[str, Any] = field(default_factory=dict)
```

`process_intent_work_unit(work, …)` always:

1. Emits `intent_created` (merges `intent_fact_extensions` into payload)
2. Runs `evaluate_intent` → `risk_decision` fact
3. On approve: `register_submit` → OMS submit → ack → hooks
4. On deny/reject: strategy-specific cleanup (e.g. `notify_buy_not_submitted`)

**Critical rule for allocation_test:** strategies emit work units; they never call OMS or mutate the ledger directly.

### 4. `allocation_runtime` owner resolution (P4)

Current logic in `resolve_owner_id(strategy, intent, intent_extensions=…)`:

| Condition | Owner ID |
|-----------|----------|
| `type(strategy).__name__ == "SellTestStrategy"` | `sell_test` |
| `extensions["source"] == "sell_test_strategy"` | `sell_test` |
| `extensions["source"] == "scheduled_exit_demo"` | `guru_follow` |
| default (guru path) | `guru_follow` |

Constants live in `runtime/allocation_ids.py` to avoid import cycles.

**Gap for allocation_test:** P4 supports only two production owners (`sell_test`, `guru_follow`). A multi-owner toy strategy needs **per-intent owner** in extensions, e.g.:

```yaml
intent_fact_extensions:
  source: allocation_test_strategy
  allocation_owner_id: allocation_test_A
```

Implementation must extend `resolve_owner_id` to prefer `allocation_owner_id` from extensions when present. This is a **small, non-breaking** hook — not a blocker for planning, but required before the toy strategy can attribute BUY/SELL to A vs B correctly.

### 5. Ledger mutations in `pipeline.py` (P4)

After successful OMS submit (not on risk deny / reject):

| Event | When | Function |
|-------|------|----------|
| BUY allocation | After ack, **before** post-buy hook | `maybe_apply_allocation_buy` |
| SELL reserve | After risk approve, **before** OMS submit | `maybe_reserve_exit_allocation` |
| SELL allocation decrement | After successful OMS submit | `maybe_apply_allocation_sell` (+ releases reservation) |
| Reservation release | SELL OMS reject | `maybe_release_exit_reservation` |
| Venue clamp | After `emit_wallet_sync` | `maybe_clamp_allocations_to_venue` |

Fill qty: BUY uses `match_evidence.takingAmount`; SELL uses `makingAmount` when present; else approved intent size.

**Safety:** RiskEngine `inventory.check_inventory_sell` still runs on every SELL intent regardless of ledger.

### 6. Facts declaration and emission

- Schema constants: `reporting/schema_v2.py` (`FACT_TYPE_*`)
- Builder: `reporting/facts.make_fact(type, run_id, payload, correlation_id=…)`
- Sink: `JsonlSink.write` (flushes each line)

Existing types relevant to allocation_test:

| Fact type | Used for |
|-----------|----------|
| `intent_created` | Every intent |
| `risk_decision` | Approve/deny |
| `oms_submit` / `oms_reject` | Venue ack |
| `allocation_ledger` | `allocation_buy_applied`, `allocation_sell_applied`, `allocation_reserved`, `allocation_released`, `allocation_clamped` |
| `exit_lifecycle` | sell_test arming (optional for owner-A exit if reusing delay/arm pattern) |
| `health` | Run lifecycle (`started`, `stopped`, `sell_test_readiness`, …) |
| `strategy_skip` | Guru filter skips (not ideal for allocation block — prefer explicit toy events) |

### 7. Test structure (sell_test + allocation ledger)

| File | Style |
|------|-------|
| `tests/test_sell_test_strategy.py` | Unit: state machine, shadow e2e via `process_intent_work_unit` + `process_scheduled_exit_demo_due` |
| `tests/test_exit_lifecycle_p35.py` | Integration: pipeline ordering, lifecycle facts, live-matched BUY mocks |
| `tests/test_allocation_ledger.py` | Pure unit: ledger API, persistence, clamp |
| `tests/test_allocation_ledger_integration.py` | Pipeline + ledger: BUY updates ledger, deny/reject skip ledger, SELL reduces, clamp fact |

Common test helpers:

- `parse_app_config(risk=…, strategy=…, runtime=…)` with `allocation_ledger: {}` (required marker block)
- `_wire_coord(tmp_path)`: coordinator + ledger + sinks on `tmp_path/facts.jsonl`
- Shadow OMS, `apply_local_shadow_fill=True` for fast e2e

**Pattern to reuse:** `tests/test_allocation_test_strategy.py` (unit/state) + `tests/test_allocation_test_e2e.py` (golden fact chain in shadow).

---

## 1. Executive summary

P4 introduced a **per-strategy allocation ledger** separate from **venue-wide** `WalletStore.positions`. Production code already clamps `sell_test` / demo exits to allocated qty, but nothing yet **proves** that two logical owners sharing one wallet cannot cross-sell each other's inventory.

The **allocation_test** toy strategy exists to validate:

> **Venue inventory ≠ strategy ownership.**  
> Owner B must not sell tokens that Owner A bought, even when the wallet shows aggregate position.

It runs shadow-first (deterministic, no venue cost), then optionally one small live run before P5 guru SELL sizing and P6 TP/SL.

---

## 2. Proposed strategy name

**Chosen name: `allocation_test`**

| Candidate | Verdict |
|-----------|---------|
| `allocation_test` | **Selected** — parallel to `sell_test`, clearly scoped to P4 validation |
| `ledger_test` | Too generic; could mean persistence/reconcile |
| `allocation_validation_test` | Verbose; awkward for YAML `kind` and CLI |

YAML: `kind: allocation_test`  
Module: `strategies/allocation_test/`  
CLI: `--strategy config/strategies/allocation_test.yaml`

---

## 3. Strategy behavior — state machine

### Happy path

```
INIT
  → OWNER_A_BUY_SUBMITTED        (EnterIntent work unit handed to pipeline)
  → OWNER_A_ALLOCATION_VISIBLE   (allocation_buy_applied; ledger[A] > 0)
  → OWNER_B_UNAUTHORIZED_SELL_ATTEMPTED
  → OWNER_B_SELL_BLOCKED         (no OMS submit for B; lifecycle fact emitted)
  → OWNER_A_SELL_ARMED           (optional: if using delay/arm like sell_test)
  → OWNER_A_SELL_SUBMITTED       (ExitIntent → risk → OMS)
  → OWNER_A_SELL_COMPLETED       (allocation_sell_applied; terminal)
  → DONE
```

### Failure / timeout states

| State | Meaning | Terminal? |
|-------|---------|-----------|
| `BUY_DENIED` | Owner A BUY risk denied | Yes (failure) |
| `BUY_OMS_REJECT` | Owner A BUY oms_reject | Yes (failure) |
| `ALLOCATION_NOT_APPLIED` | BUY succeeded but no `allocation_buy_applied` for A | Yes (failure) |
| `OWNER_B_SELL_NOT_BLOCKED` | Owner B reached `oms_submit` SELL | Yes (failure) |
| `OWNER_A_SELL_DENIED` | Owner A SELL risk denied | Yes (failure) |
| `OWNER_A_SELL_OMS_REJECT` | Owner A SELL oms_reject | Yes (failure) |
| `TIMEOUT_ALLOCATION_VISIBLE` | BUY ok but ledger[A] never > 0 within timeout | Yes (failure) |
| `TIMEOUT_POSITION_VISIBLE` | Live: venue position never visible for A exit | Yes (failure) |
| `TIMEOUT_OWNER_A_EXIT` | A blocked sell never completes | Yes (failure) |
| `LEDGER_MISMATCH` | Post-run ledger snapshot inconsistent with facts | Yes (failure) |

`is_done()` = terminal success **or** explicit failure state recorded (mirrors sell_test terminal semantics).

---

## 4. Owner model

Two logical owners in **one process**, one wallet:

| Role | Default ID | Config override |
|------|------------|-----------------|
| Buyer / authorized seller | `allocation_test_A` | `owner_a_id` |
| Unauthorized seller | `allocation_test_B` | `owner_b_id` |

### Mapping to `allocation_runtime`

1. Add constants to `allocation_ids.py`:
   - `ALLOCATION_TEST_INTENT_SOURCE = "allocation_test_strategy"`
   - Default owner constants (or config-driven strings only)

2. Extend `resolve_owner_id`:
   ```python
   owner = (intent_extensions or {}).get("allocation_owner_id")
   if owner:
       return str(owner)
   # existing sell_test / guru_follow fallbacks…
   ```

3. Each `IntentWorkUnit` from the toy strategy includes:
   ```python
   {
     "source": "allocation_test_strategy",
     "allocation_owner_id": cfg.owner_a_id,  # or owner_b_id
     "allocation_test_phase": "owner_a_buy" | "owner_b_unauthorized_sell" | "owner_a_sell",
   }
   ```

**Does not break** existing owners: `sell_test` and `guru_follow` paths unchanged when `allocation_owner_id` absent.

---

## 5. Config design

**File:** `config/strategies/allocation_test.yaml`

```yaml
kind: allocation_test
enabled: true

token_id: "<numeric CLOB token>"

owner_a_id: allocation_test_A
owner_b_id: allocation_test_B

buy:
  enabled: true
  notional_usd: "5"
  pricing_mode: fixed          # shadow: fixed is simpler; live may use auto like sell_test
  limit_price: "0.50"
  order_style: GTC

owner_b_unauthorized_sell:
  enabled: true
  # Size to *attempt* — strategy will clamp to allocated qty (0 for B).
  # Use same token; size = owner A buy size or full wallet position for stress.
  size_mode: match_owner_a_buy   # match_owner_a_buy | fixed
  fixed_size: "10"               # used when size_mode=fixed

owner_a_sell:
  enabled: true
  delay_s: 0                     # shadow: 0; live: 1–3 if arming on venue position
  pricing_mode: auto             # default: marketable SELL at best_bid - aggression_ticks * tick
  aggression_ticks: 0
  limit_price: "0.01"            # fallback when book lookup fails; shadow uses when no live book
  order_style: GTC

run_once: true

timeouts:
  allocation_visible_s: 5        # shadow: immediate; live: allow ledger hook
  position_visible_s: 90           # live only: wait for WS/REST position
  unauthorized_sell_timeout_s: 10
  owner_a_exit_timeout_s: 120
```

**Live-safe defaults:** small notional ($4–5 after risk cap), single token, `run_once: true`, unique `--run-name`.

Config dataclass: `AllocationTestStrategyConfig` on `AppConfig.allocation_test` (mirror `sell_test` pattern).

---

## 6. Runtime wiring

### Recommended approach (minimal)

Add third branch in `app.py`, parallel to `sell_test_mode`:

```python
allocation_test_mode = app.allocation_test is not None
if allocation_test_mode:
    strat = AllocationTestStrategy(app.allocation_test)
elif sell_test_mode:
    strat = SellTestStrategy(app.sell_test)
else:
    strat = GuruFollowStrategy(app.strategy)
```

| Concern | Decision |
|---------|----------|
| New main loop? | Yes: `_run_allocation_test_loop` (orchestrates A buy → B block attempt → A sell wait) |
| Reuse `scheduled_exit_demo_due_loop`? | **Optional for owner-A sell only** if using delay/arm + `pop_due_work_units` pattern; otherwise inline `process_intent_work_unit` for A SELL after position visible |
| Reuse `process_intent_work_unit`? | **Yes** for all intents that actually reach OMS |
| try_arm hook | Only if owner-A sell uses live arming (delay_s > 0); wire `try_arm_allocation_test_pending` like sell_test |

**Shadow path (simplest v1):**

1. `_run_allocation_test_loop` emits owner-A BUY work unit → `process_intent_work_unit`
2. Assert ledger[A] > 0 (poll or immediate after shadow fill)
3. Strategy method `attempt_owner_b_unauthorized_sell(coord)` — **does not** call pipeline if clamped size = 0; emits lifecycle fact
4. Strategy builds owner-A SELL work unit → `process_intent_work_unit`
5. Wait until `is_done()` or timeout

**Live path:** reuse sell_test readiness wait + optional auto-pricing for BUY; reuse P3.5 position visibility for A sell if `delay_s > 0`.

---

## 7. Intent flow

### Owner A BUY

| Step | Detail |
|------|--------|
| Intent | `EnterIntent` BUY, `token_id`, size from notional/price |
| Extensions | `source=allocation_test_strategy`, `allocation_owner_id=owner_a_id`, `allocation_test_phase=owner_a_buy` |
| Pipeline | Normal risk → OMS |
| Expected facts | `intent_created`, `risk_decision` approved, `oms_submit`, `allocation_ledger` `allocation_buy_applied` with `owner_id=allocation_test_A` |
| Ledger | `get_allocated(A, token) > 0` |

### Owner B unauthorized SELL

**Recommended approach (cleanest): do not call `process_intent_work_unit` when allocated size is zero.**

1. Strategy computes `planned_size` (e.g. match owner A buy or wallet position qty).
2. Calls `clamp_planned_to_allocated(coord, owner_id=owner_b_id, …)`.
3. If result `<= 0`:
   - Emit **`health`** fact (consistent with `sell_test_readiness`):
     ```json
     {
       "event": "allocation_test_unauthorized_sell_blocked",
       "owner_id": "allocation_test_B",
       "token_id": "...",
       "planned_size": "10",
       "allocated_available": "0",
       "reason": "insufficient_allocation"
     }
     ```
   - Optionally precede with `allocation_test_unauthorized_sell_attempt` health event.
   - Transition to `OWNER_B_SELL_BLOCKED` — **no** `intent_created`, **no** `oms_submit`.
4. If clamp ever returned `> 0` (misconfiguration), strategy should still refuse unless explicitly in a negative-test mode — default config must keep B at zero allocation.

**Why not `strategy_skip`?** That fact is guru-filter oriented. **Why not new fact type (v1)?** `health` + `allocation_ledger` is enough for shadow golden tests; promote to `FACT_TYPE_ALLOCATION_TEST` only if operators need dedup/filtering in summarize.

**RiskEngine note:** Even if a buggy version emitted a SELL intent for B with size > 0 but B allocation = 0, current P4 clamp in strategy should zero the size before building the work unit. Defense in depth: do not build work unit at size 0.

### Owner A SELL

| Step | Detail |
|------|--------|
| Intent | `ExitIntent` SELL, size = min(planned, allocated_available, venue available) |
| Extensions | `allocation_owner_id=owner_a_id`, `allocation_test_phase=owner_a_sell`, parent correlation to A buy |
| Pipeline | reserve → risk (inventory) → OMS |
| Expected facts | `intent_created` SELL, `risk_decision` approved, `allocation_reserved`, `oms_submit`, `allocation_sell_applied`, optional `exit_lifecycle` if arming used |
| Ledger | A allocation → 0 (or residual if partial) |

---

## 8. Facts / observability

### Reuse (required)

| Phase | Facts |
|-------|-------|
| Owner A BUY | `intent_created`, `risk_decision`, `oms_submit`, `allocation_ledger` (`allocation_buy_applied`) |
| Owner B block | `health` (`allocation_test_unauthorized_sell_attempt`, `allocation_test_unauthorized_sell_blocked`) — **no** SELL `intent_created` / `oms_submit` |
| Owner A SELL | `intent_created`, `risk_decision`, `allocation_ledger` (`allocation_reserved`, `allocation_sell_applied`), `oms_submit` |
| Run end | `health` `stopped`, `run_summary.json` |

### Optional

- `exit_lifecycle` for owner-A if reusing sell_test arming/delay in live mode
- `wallet_sync` after live position refresh

### Golden chain (shadow)

```
health: started
intent_created BUY (owner A extensions)
risk_decision BUY approved
oms_submit BUY
allocation_ledger allocation_buy_applied owner_A
health allocation_test_unauthorized_sell_attempt owner_B
health allocation_test_unauthorized_sell_blocked owner_B
intent_created SELL (owner A extensions)
risk_decision SELL approved
allocation_ledger allocation_reserved
oms_submit SELL
allocation_ledger allocation_sell_applied owner_A
health: stopped
```

### Future fact type (not v1)

If `health` events prove too noisy in summarize, add `FACT_TYPE_ALLOCATION_TEST` with structured phases — document in `reporting_fact_model.md` at implementation time.

---

## 9. Test plan

### Unit tests (`tests/test_allocation_test_strategy.py`)

| Test | Assert |
|------|--------|
| `test_owner_a_buy_work_unit_extensions` | `allocation_owner_id == owner_a_id` |
| `test_owner_b_clamp_zero_skips_work_unit` | No `IntentWorkUnit` when B allocation 0 |
| `test_owner_b_blocked_emits_health_fact` | Mock coord + ledger; block method writes health event |
| `test_owner_a_sell_size_clamped_to_allocated` | Size ≤ ledger available |
| `test_is_done_only_after_owner_a_terminal` | Not done after B block; done after A sell terminal |
| `test_failure_owner_b_reached_oms` | Simulated oms_submit for B correlation → strategy failure state |

### Integration / golden (`tests/test_allocation_test_e2e.py`)

| Test | Assert |
|------|--------|
| `test_shadow_full_chain` | Golden fact sequence above |
| `test_no_owner_b_oms_submit` | Zero `oms_submit` with owner B correlation / SELL after B phase |
| `test_ledger_owner_a_zero_at_end` | Load ledger from tmp_path; A qty == 0 |
| `test_buy_denied_no_ledger_mutation` | Risk deny path; no `allocation_buy_applied` |

### Live manual (operator)

```powershell
python -m tyrex_pm.runtime.app run `
  --strategy config/strategies/allocation_test.yaml `
  --scenario live_guru `
  --run-name "allocation_test_$(Get-Date -UFormat %s)"
```

Verify:

- `facts.jsonl` golden chain
- `var/state/allocation_ledger.json` — A returns to 0; B never credited
- No second SELL `oms_submit` for owner B

Use fresh token or `tyrex-pm reset-state` + clear positions if re-running.

---

## 10. Acceptance criteria

**P4 allocation_test is validated when:**

- [ ] Owner A BUY produces `allocation_buy_applied` for `allocation_test_A`
- [ ] Owner B cannot sell despite wallet holding venue inventory from A's buy
- [ ] No `oms_submit` SELL attributed to owner B
- [ ] Owner A SELL completes through RiskEngine + inventory + OMS
- [ ] `allocation_sell_applied` decrements A; final A allocation 0 (or documented residual)
- [ ] RiskEngine inventory gate still exercised on A SELL (not bypassed)
- [ ] Facts make the three phases obvious without reading code
- [ ] Shadow golden test passes in CI
- [ ] One optional small live run passes (operator choice)

---

## 11. Phase 4 — Optional manual/external clamp (planned, not v1)

**Not implemented in the first toy strategy.**

Manual validation procedure (future doc in OPERATIONS.md):

1. Run allocation_test through owner A BUY (ledger[A] = X, wallet = X)
2. Externally reduce wallet position (manual UI sell or inject lower position in shadow test)
3. Trigger `emit_wallet_sync` / positions refresh
4. Expect `allocation_ledger` `allocation_clamped` with `allocated_before > venue_qty`
5. Owner A sell size must respect clamped allocation

Shadow test: inject `WalletPosition` with lower qty, call `maybe_clamp_allocations_to_venue`, assert fact + ledger.

---

## 12. File list (future implementation — do not create yet)

| File | Purpose |
|------|---------|
| `src/tyrex_pm/strategies/allocation_test/strategy.py` | State machine, work unit builders, B-block logic |
| `src/tyrex_pm/strategies/allocation_test/__init__.py` | Exports |
| `config/strategies/allocation_test.yaml` | Operator config |
| `src/tyrex_pm/runtime/config.py` | `AllocationTestStrategyConfig`, parse `kind: allocation_test` |
| `src/tyrex_pm/runtime/allocation_ids.py` | Source constant + default owner ids |
| `src/tyrex_pm/runtime/allocation_runtime.py` | `allocation_owner_id` in `resolve_owner_id` |
| `src/tyrex_pm/runtime/app.py` | `allocation_test_mode`, `_run_allocation_test_loop` |
| `tests/test_allocation_test_strategy.py` | Unit |
| `tests/test_allocation_test_e2e.py` | Shadow golden |
| `Docs/reporting_fact_model.md` | `health` allocation_test events (if not new fact type) |
| `Docs/OPERATIONS.md` | Live checklist + optional clamp drill |

**Out of scope:** P5 guru exits, P6 TP/SL, strategy-direct ledger mutation.

---

## 13. Boundaries (explicit)

Do **not**:

- Implement allocation_test in this planning task
- Implement P5 guru SELL parity or P6 TP/SL
- Bypass `RiskEngine` / `inventory.check_inventory_sell`
- Mutate ledger inside strategy modules
- Import venue clients into `state/` or strategies

---

## Planning review summary

### Files reviewed

| Area | Files |
|------|-------|
| Runtime entry | `src/tyrex_pm/runtime/app.py` (`cmd_run`, `_run_sell_test_loop`, scheduler) |
| Config | `src/tyrex_pm/runtime/config.py`, `config/strategies/sell_test.yaml`, `config/runtime/default.yaml` |
| Pipeline | `src/tyrex_pm/runtime/pipeline.py`, `src/tyrex_pm/runtime/intent_work.py` |
| Allocation | `src/tyrex_pm/state/allocation_ledger.py`, `src/tyrex_pm/runtime/allocation_runtime.py`, `src/tyrex_pm/runtime/allocation_ids.py` |
| Reference strategy | `src/tyrex_pm/strategies/sell_test/strategy.py` |
| Facts | `src/tyrex_pm/reporting/schema_v2.py`, `Docs/reporting_fact_model.md` |
| Tests | `tests/test_sell_test_strategy.py`, `tests/test_allocation_ledger.py`, `tests/test_allocation_ledger_integration.py`, `tests/test_exit_lifecycle_p35.py` |
| Plan context | `Docs/Implementation/sell_feature/IMPLEMENTATION_PLAN.md` |

### Proposed architecture

Standalone **`AllocationTestStrategy`** with explicit three-phase orchestration in `_run_allocation_test_loop`, reusing **`process_intent_work_unit`** for A BUY and A SELL only. Owner B “sell attempt” is a **strategy-side guard** that emits **`health`** lifecycle facts and never enqueues a work unit when allocation is zero.

### Missing P4 hooks (small, not blockers)

| Hook | Needed for |
|------|------------|
| `resolve_owner_id` reads `allocation_owner_id` from extensions | Per-intent A vs B attribution |
| `AllocationTestStrategyConfig` + `AppConfig.allocation_test` | YAML loading |
| `app.py` branch + loop | Run orchestration |

Existing P4 pipeline hooks (buy/sell/reserve/clamp) are **sufficient** once owner resolution is extended.

### Implementation risks

| Risk | Mitigation |
|------|------------|
| Owner B accidentally reaches OMS if size > 0 built | Never build work unit when clamp returns 0; golden test asserts zero B oms_submit |
| Live: A sell before position visible | Reuse sell_test arming/timeouts for A only; B phase runs after A allocation visible |
| Ledger attribution wrong | Golden test checks `owner_id` on every `allocation_ledger` fact |
| Confusion with wallet inventory | Document in facts: `allocated_available=0` vs `wallet_position_qty>0` on B block event |
| Reusing `scheduled_exit_demo_due_loop` with wrong strategy type | Either duck-type `pop_due_work_units` on allocation_test state or call A SELL inline in v1 shadow |

### Exact next step (if plan approved)

1. Extend `resolve_owner_id` + `allocation_ids.py` for `allocation_owner_id` extension field.  
2. Add `AllocationTestStrategyConfig` and `kind: allocation_test` parsing.  
3. Implement `AllocationTestStrategy` + shadow golden test **before** wiring `app.py` live loop.  
4. Wire `app.py` shadow path; run full pytest.  
5. Add `config/strategies/allocation_test.yaml` + OPERATIONS live checklist.  
6. Optional: one live validation run.

---

*Document version: 1.0 — planning only, no implementation.*
