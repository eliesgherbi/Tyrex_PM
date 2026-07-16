# Phase 4.6 — Production protection runtime integration with paired binary strategy

**Program:** [README.md](README.md) · **Prev:** [phase_4_5_live_validation_harness.md](phase_4_5_live_validation_harness.md) · **Next:** [phase_5_portfolio_foundation.md](phase_5_portfolio_foundation.md)

> **Procedural now, event-ready later.** Strategy emits intents; monitor emits `ExitIntent` only; no bus.

**Status:** **implemented** — shadow-validated via unit tests; live PB-Level ladder **not yet run** (see §11).

---

## 1. Goal

Move from validation-harness proof (Phase 4.5) to a **real production-style strategy** that exercises the live-validated architecture in a long-running loop:

```text
MarketStateStore → Strategy entry eval → RiskEngine → ExecutionPlanner → validate_planned_order → SingleWriterOMS
                 → entry finality → PairedBinaryMonitor → ExitIntent → (same pipeline)
```

Phase 4.5 proved individual paths (normal entry, read-only books, urgent FAK, protection register, protection trigger). Phase 4.6 closes the remaining gap:

```text
ProtectionSupervisor + periodic monitoring proven in validation_harness,
but not yet integrated into a long-running production strategy loop.
```

The paired binary strategy is the first production strategy that **requires** all of the above together: dual-leg entry, allocation-final gating, live book reads, urgent exits, and restart recovery.

---

## 2. Strategy economics (brief)

Enter both YES and NO legs of the same binary market when the pair is not too expensive and both books are clean enough. Buy equal quantity `Q` on each leg.

```text
Objective: winner profit > loser loss + spread/slippage
```

After entry:

| Event | Action |
|-------|--------|
| One leg hits pair stop budget | Urgent SELL that leg; keep the other; set survivor target from pair profit budget |
| Remaining winner bid ≥ winner target | Urgent SELL winner → DONE |
| `max_holding_time_s` elapsed | Urgent SELL all remaining open legs |
| Entry timeout with unpaired fill | Unwind filled leg; FAILED/ABORTED |

---

## 3. Non-negotiable architecture constraints

Inherited from [README.md §4](README.md#4-global-non-negotiable-constraints). Phase 4.6 adds no exceptions.

| Rule | Paired binary behavior |
|------|------------------------|
| Strategy emits intents only | Entry BUY intents from `evaluate_entry`; no venue I/O |
| No direct OMS from strategy or monitor | All submits via `process_intent_work_unit` |
| No store mutation from strategy | `WalletStore`, `OrderStore`, `AllocationLedger` updated only by pipeline/runtime hooks |
| `RiskEngine` mandatory | Every intent, including monitor exits |
| `ExecutionPlanner` mandatory when enabled | Entry GTC; exits FAK |
| `validate_planned_order` mandatory after planner | Preserves pre-check `client_order_id` |
| `MarketStateStore` is live book source | No hidden fixture book in live |
| No seed allocation in live | Preflight rejects synthetic shortcuts (mirror P4.5 live policy) |
| Every SELL clamps | `min(planned, owner_allocation, venue_available_to_sell)` via `protection/sizing.py` reuse |

---

## 4. Package layout (proposed)

### 4.1 Strategy package

```text
src/tyrex_pm/strategies/paired_binary/
  __init__.py
  config.py          # PairedBinaryConfig dataclass + YAML parse
  strategy.py        # PairedBinaryStrategy — entry eval, state machine facade
  state.py           # PairedBinaryState, leg snapshots, persistence load/save
  entry_eval.py      # Pure: pair_cost, spreads, freshness → allow/deny + reason
  lifecycle.py       # State transitions, entry-price tracking, winner targets
  monitor.py         # PairedBinaryMonitor.tick → list[ExitIntent]
  sizing.py          # Entry qty helpers; delegates exit clamp to protection/sizing
```

Strategy kind: `paired_binary` (`STRATEGY_KIND_PAIRED_BINARY`).

### 4.2 Runtime wiring (proposed)

```text
src/tyrex_pm/runtime/paired_binary_run.py       # Main loop: entry → finality → monitor
src/tyrex_pm/runtime/paired_binary_live.py      # Live preflight guards (no fixtures/seeds)
src/tyrex_pm/runtime/paired_binary_recovery.py  # Startup reconcile + state recovery
```

Reuses existing:

```text
market_data_runtime.py      # MarketStateStore bootstrap + REST refresh
finality_waiter.py          # Per-leg allocation-final wait after BUY
protection_runtime.py       # run_protection_tick pattern (not generic TP/SL register)
pipeline.process_intent_work_unit
```

### 4.3 Config files (proposed)

```text
config/strategies/paired_binary.yaml
config/scenarios/shadow_paired_binary.yaml
config/scenarios/live_paired_binary_tiny.yaml
```

Example strategy YAML:

```yaml
kind: paired_binary

paired_binary:
  owner_id: paired_binary
  market_id: "<market>"
  yes_token_id: "<yes_token>"
  no_token_id: "<no_token>"

  position_size: "100"
  max_pair_entry_cost: "1.02"
  max_spread_yes: "0.02"
  max_spread_no: "0.02"

  pair_stop_loss_pct: "0.02"
  pair_take_profit_pct: "0.05"
  slippage_buffer: "0.005"
  reject_if_spread_exceeds_loss_budget: true
  max_holding_time_s: 1800

  entry_order_style: GTC
  exit_order_style: FAK

  entry_fill_timeout_s: 60
  abort_unpaired_entry: true
  unwind_partial_entry: true
  min_effective_pair_qty: "5"

  run_once: false
  max_markets: 1
  tick_interval_s: 1.0
```

Live tiny overlay (`live_paired_binary_tiny.yaml`) — **forces small exposure** (must not inherit the conceptual 100-share default):

```yaml
paired_binary:
  position_size: "5"
  max_holding_time_s: 30
  min_effective_pair_qty: "5"
```

Risk scenario must cap notional: `max_notional >= position_size × (yes_ask + no_ask)` per leg pair (both legs).

---

## Pair-level percentage PnL model

Paired binary uses **one** exit model: percentages of total pair entry cost (`yes_entry + no_entry`).

```yaml
pair_stop_loss_pct: "0.02"    # 2% of pair_cost = max planned loser loss budget
pair_take_profit_pct: "0.05"  # 5% of pair_cost = planned survivor profit budget
slippage_buffer: "0.005"
reject_if_spread_exceeds_loss_budget: true
```

At fill time:

```text
pair_cost     = yes_entry + no_entry
loss_budget   = pair_cost × pair_stop_loss_pct
profit_budget = pair_cost × pair_take_profit_pct
```

Stop (each leg, while both active):

```text
planned_stop_price  = loser_entry - loss_budget
trigger_stop_price  = planned_stop_price + slippage_buffer   # fires earlier
if loser_bid <= trigger_stop_price → stop trigger
```

Survivor take-profit (after loser sold):

```text
planned_target_price  = survivor_entry + profit_budget
trigger_target_price  = planned_target_price + slippage_buffer
if survivor_bid >= trigger_target_price → TP trigger
```

**Expected net PnL** if both exits fill at planned prices:

```text
desired_net_profit_per_pair = pair_cost × (pair_take_profit_pct - pair_stop_loss_pct)
expected_pnl_total        = position_size × desired_net_profit_per_pair
```

Example: `pair_stop_loss_pct=0.02`, `pair_take_profit_pct=0.05`, `pair_cost=1.01` → planned net **≈ +3% of pair cost** (+$0.0303/pair unit), emitted in `paired_binary_pnl_plan`.

**Entry gate:** before BUY, reject if `leg_spread + slippage_buffer > estimated_loss_budget` (`YES_SPREAD_EXCEEDS_LOSS_BUDGET` / `NO_SPREAD_EXCEEDS_LOSS_BUDGET`).

**Activation safety:** after both legs filled and sellable, reject arming if `entry - activation_bid + slippage_buffer > actual_loss_budget` → unwind + `FAILED` (`paired_binary_activation_rejected_loss_budget`).

**Winner repricing:** after realized loser exit, survivor target adjusts so `realized_loser_loss + desired_net_profit` is preserved (`paired_binary_winner_target_repriced`).

**Limitations:** this is a **plan**, not a guarantee. Live PnL can differ due to slippage, partial fills, book depth, stale data, and survivor target not being reached. Facts: `paired_binary_pnl_plan`, `paired_binary_stop_plan`, `paired_binary_winner_target_plan`, `paired_binary_realized_pnl`.

### Cashflow-based realized PnL

The authoritative realized PnL is computed from matched cashflows (`sell_cash_total - buy_cash_total`), stored per leg at OMS/user-WS match time in `LegRuntime.entry_cash` / `exit_cash`. Average prices are display fields only: `avg_price = cash / qty`.

Book bid, trigger price, limit price, and planned price are **never** used as authoritative realized PnL. If required cashflows are missing, emit `paired_binary_realized_pnl_unavailable` with `missing_fields` — no silent fallback to price reconstruction.

Polymarket UI cash values may differ from OMS facts due to display, rounding, fees, or net/gross accounting. The bot reports OMS cashflow PnL; wallet-balance-reconciled PnL may be added later.

**Deprecated (hard config error):** `loser_stop_loss`, `winner_take_profit`, `stop_reference`, `min_stop_after_activation_s`.

---

## 4.5 Paired binary lifecycle semantics — trigger vs submit vs fill

Production paired binary distinguishes five stages for every exit (stop-loss, take-profit, timeout):

```text
1. trigger detected     — book/timeout condition met; survivor target may be recorded
2. exit work unit created — ExitIntent built with clamped size > 0
3. exit order submitted — risk + planner approved; OMS ack received
4. exit order matched/filled — venue/user-WS fill evidence; allocation sell applied
5. position closed      — owner allocation for leg is zero; survivor-only phase or DONE
```

**Rules enforced in code:**

| Stage | State phase examples | Facts |
|-------|---------------------|-------|
| Filled, not sellable | `BOTH_LEGS_FILLED` | `paired_binary_waiting_for_sellable_inventory` |
| Monitor armed | `BOTH_LEGS_ACTIVE` | `paired_binary_activation_reference`, `paired_binary_monitor_started` |
| Trigger, no submit | `STOP_PENDING_*`, `TP_PENDING_*`, `TIMEOUT_PENDING` | `paired_binary_exit_trigger_pending`, `paired_binary_exit_submit_blocked` |
| Submit ack | `EXITING_*` | `paired_binary_exit_submit_attempt`, `oms_submit` |
| Flat leg | `ONLY_*_ACTIVE` or `DONE` | allocation / venue reconcile |

**Do not** transition to `EXITING_*` on trigger alone. `EXITING_*` requires stage 3 (OMS submit ack).

**Sellability gate:** `BOTH_LEGS_ACTIVE` requires both legs allocation-final **and** `venue_available_to_sell >= effective_qty` for each leg. Stop-loss reference defaults to `activation_bid` (bid at monitor start), not entry fill ask, to avoid instant spread triggers.

**Strategy semantics (this phase):** dual loser-stop while `BOTH_LEGS_ACTIVE`; single winner take-profit on survivor after loser sold; timeout active in all non-terminal monitor phases including pending/exiting recovery.

### Live validation parameters vs production parameters

`live_paired_binary_tiny.yaml` uses **code-path validation** defaults, not profit-seeking defaults:

- `stop_reference: activation_bid`, `min_stop_after_activation_s: 1.0` — avoids immediate spread stop on first tick
- `loser_stop_loss: 0.02` with `max_spread_*: 0.01` — stop wider than typical spread
- `entry_order_style: FAK`, `exit_order_style: FAK` — fast fills for tiny size
- `position_size: 5`, tight notional caps — minimal live exposure

**Production** may use wider stops, GTC entry, longer holds. If `stop_reference: entry_fill_price`, ensure `loser_stop_loss > entry spread + safety buffer` — otherwise the first monitor tick can fire stop because entries fill at **ask** and exits reference **bid**.

---

## 5. State machine

### 5.1 States

```text
IDLE                  — no active pair; may evaluate entry
ENTRY_PLANNED         — entry allowed; intents being built (transient)
YES_ENTRY_PENDING     — YES BUY submitted; awaiting fill/finality
NO_ENTRY_PENDING      — NO BUY submitted; awaiting fill/finality
BOTH_ENTRY_PENDING    — both BUY intents submitted; awaiting both legs
BOTH_LEGS_FILLED      — both legs allocation-final; waiting for sellable venue inventory
BOTH_LEGS_ACTIVE      — both legs sellable; dual stop-loss monitor armed
STOP_PENDING_YES      — YES stop/timeout triggered; exit not yet submitted
STOP_PENDING_NO       — NO stop/timeout triggered; exit not yet submitted
TP_PENDING_YES        — YES take-profit triggered; exit not yet submitted
TP_PENDING_NO         — NO take-profit triggered; exit not yet submitted
TIMEOUT_PENDING       — max-hold exit triggered; one or both exits not yet submitted
ONLY_YES_ACTIVE       — YES still open, NO closed (NO stop-loss fired; YES held for winner target)
ONLY_NO_ACTIVE        — NO still open, YES closed (YES stop-loss fired; NO held for winner target)
EXITING_YES           — urgent SELL YES in flight
EXITING_NO            — urgent SELL NO in flight
EXITING_BOTH          — max-hold or abort unwind; both exits in flight
DONE                  — flat; no open legs for this pair
FAILED                — unrecoverable error or abort after unwind attempt
```

### 5.2 Minimum transitions (required)

```text
IDLE
  → BOTH_ENTRY_PENDING     after both entry intents submitted to pipeline

BOTH_ENTRY_PENDING
  → BOTH_LEGS_ACTIVE       both legs allocation-final with qty >= effective_entry_qty
  → EXITING_* / FAILED     entry timeout, reject, or unpaired partial (see §5.3)

BOTH_LEGS_ACTIVE
  → ONLY_NO_ACTIVE         YES stop-loss exit accepted/applied
  → ONLY_YES_ACTIVE        NO stop-loss exit accepted/applied
  → EXITING_BOTH           max_holding_time_s exceeded

ONLY_YES_ACTIVE
  → DONE                   YES take-profit or timeout exit complete
  → EXITING_YES            take-profit or timeout SELL submitted

ONLY_NO_ACTIVE
  → DONE                   NO take-profit or timeout exit complete
  → EXITING_NO             take-profit or timeout SELL submitted

EXITING_*
  → DONE / ONLY_* / FAILED per leg completion and residual inventory
```

`ENTRY_PLANNED`, `YES_ENTRY_PENDING`, `NO_ENTRY_PENDING` are used internally when tracking legs independently before both submits are confirmed.

### 5.3 Partial fills and two-leg entry (critical)

Each leg tracks:

```text
leg_state: pending | partial | final | failed | unwinding
target_qty: Q
filled_qty: from allocation + order evidence
allocation_final_qty: qty credited with is_allocation_final
entry_vwap: volume-weighted average fill price (preferred over limit price)
submit_ts, finality_ts
```

**Effective entry qty** for BOTH_LEGS_ACTIVE:

```text
effective_qty = min(yes_allocation_final_qty, no_allocation_final_qty)
```

**Minimum effective pair quantity (locked):**

If `effective_qty < min_effective_pair_qty` (config, or venue min size when unset), do **not** enter `BOTH_LEGS_ACTIVE`. Unwind any filled quantity, emit `paired_binary_unwind`, mark `FAILED` or `ABORTED`. Avoids treating dust or non-sellable partials as a valid paired position.

If fills differ above the minimum, the strategy holds the **matched pair quantity** as the active pair; excess on the heavier leg is unwound immediately (see below).

#### Case matrix

| Situation | Behavior |
|-----------|----------|
| YES fills Q, NO fills Q (both final) | → `BOTH_LEGS_ACTIVE` |
| YES partial, NO zero within timeout | If `abort_unpaired_entry`: urgent SELL YES (partial qty); → `FAILED` + `paired_binary_unwind` fact |
| NO partial, YES zero within timeout | Symmetric unwind NO |
| YES fills Q, NO rejected/denied at risk/OMS | Unwind YES if `unwind_partial_entry`; → `FAILED` |
| YES fills Q, NO resting past `entry_fill_timeout_s` | Cancel resting NO if possible; unwind YES; → `FAILED` |
| YES partial p, NO partial q, p≠q, both final | Enter `BOTH_LEGS_ACTIVE` at `min(p,q)`; urgent unwind `|p-q|` on heavier leg immediately |
| One leg MATCHED but not CONFIRMED at timeout | Do **not** treat as final; wait until timeout policy fires; if still not final, cancel/unwind per config |
| Both stop-loss same tick | Deterministic tie-break (§7.2) |

**Minimum acceptable production behavior (locked):**

```text
If one leg fills and the other fails/does not fill within entry_fill_timeout_s:
  unwind the filled leg using urgent exit
  mark run FAILED (or ABORTED sub-reason)
  never leave an unintended naked one-sided position during entry
```

Unwind exits use the same pipeline as all other exits: monitor/strategy emits urgent `ExitIntent` → `process_intent_work_unit`.

### 5.4 Exit state semantics

- Transition to `ONLY_*_ACTIVE` occurs after the loser SELL is **accepted by OMS** (submit ack), not merely on intent creation. Allocation credit for the sold leg may lag; monitor must not double-sell (dedup by state + `triggered` flags per leg).
- `EXITING_*` states prevent re-entrant stop/take-profit on the same leg until submit completes or fails.
- On submit failure, emit `paired_binary_state_change` with reason; retry on next tick with dedup (max N retries configurable later; default: retry until timeout).

---

## 6. Entry logic

### 6.1 Book reads

For configured `yes_token_id` / `no_token_id`, read from `StrategyContext.market_state` (`MarketStateStore`):

```text
yes_bid, yes_ask, no_bid, no_ask
last_update_ts, is_stale(token)
```

Compute:

```text
pair_cost   = yes_ask + no_ask
yes_spread  = yes_ask - yes_bid
no_spread   = no_ask - no_bid
```

### 6.2 Entry gate

Entry allowed only if:

```text
pair_cost <= max_pair_entry_cost
yes_spread <= max_spread_yes
no_spread <= max_spread_no
both asks and bids exist
both books fresh (not is_stale)
state == IDLE (or run_once re-entry policy disabled)
```

If blocked, emit one deduped fact per evaluation cycle:

```text
paired_binary_entry_skip
```

Suggested reason codes (add to `core/reason_codes.py`):

```text
PAIR_COST_TOO_HIGH
YES_SPREAD_TOO_WIDE
NO_SPREAD_TOO_WIDE
YES_BOOK_STALE
NO_BOOK_STALE
MISSING_YES_ASK
MISSING_NO_ASK
MISSING_YES_BID
MISSING_NO_BID
PAIRED_BINARY_NOT_IDLE
```

If allowed, emit fact `paired_binary_entry_eval` then create **two** `EnterIntent`s:

```text
BUY YES  Q  (order_style = entry_order_style, default GTC)
BUY NO   Q  (same)
```

Both traverse:

```text
process_intent_work_unit → RiskEngine → ExecutionPlanner → validate_planned_order → SingleWriterOMS
```

Emit `paired_binary_entry_submitted` with shared `pair_correlation_id` linking both intents. Each leg/action keeps its own `client_order_id` (minted by risk pre-check) and leg-specific correlation for the work unit; facts include `pair_correlation_id`, `leg`, and `client_order_id`.

### 6.3 Pricing

- Entry limit price: ask from `MarketStateStore` at eval time (or planner crosses spread per existing entry path).
- Strategy does **not** compute FAK worst price; planner owns exit pricing.

---

## 7. Protection / monitoring design

### 7.1 Decision: Option A — `PairedBinaryMonitor` (recommended)

**Chosen:** Option A — strategy-specific monitor in `strategies/paired_binary/monitor.py`.

**Rejected Option B rationale:** Generalizing `ProtectionSupervisor` / `ProtectionRegistry` for paired-leg state would conflate two concerns:

1. **Generic TP/SL overlay** (Phase 4): per `(owner_id, token_id)` registration after single-leg BUY finality.
2. **Paired binary lifecycle** (Phase 4.6): dual-leg entry coordination, asymmetric stop (loser fixed delta) + winner target, max-hold on combined position, entry unwind.

Option B would push paired-binary state machine concepts into `protection/`, violating the "strategy-agnostic overlay" boundary and making `ProtectionRegistry` keyed entries insufficient (need paired correlation, shared `market_id`, cross-leg tie-break).

**What we reuse from Phase 4/4.5:**

| Component | Reuse |
|-----------|-------|
| `protection/sizing.compute_exit_sizing` | SELL clamp |
| `protection/lifecycle.build_exit_work_unit` | Optional helper for urgent exit work units |
| `run_protection_tick` routing pattern | Monitor returns work units → `process_intent_work_unit` |
| `ProtectionSupervisor` loop structure | `paired_binary_run.run_paired_binary_loop` mirrors interval + stop semantics |
| `MarketStateStore` staleness rules | Skip tick if either leg book stale; deduped fact |

**What is new:**

```text
PairedBinaryMonitor.tick(coord, state, cfg) -> list[IntentWorkUnit]
  reads paired strategy state (not ProtectionRegistry)
  evaluates stop-loss, take-profit, max-hold
  emits ExitIntent only (urgency=urgent)
  never calls OMS
```

Production runtime integrates the monitor in the **strategy runner loop**, not inside `validation_harness`. The generic `ProtectionMonitor` remains available for simple single-token TP/SL on other strategies (future guru integration).

### 7.2 Stop-loss logic

When `state == BOTH_LEGS_ACTIVE`, use **bid** as exit reference:

```text
yes_exit_price = yes_bid
no_exit_price  = no_bid
```

Triggers:

```text
yes_stop = yes_entry - loser_stop_loss
no_stop  = no_entry - loser_stop_loss

if yes_exit_price <= yes_stop → SELL YES urgent
if no_exit_price  <= no_stop  → SELL NO  urgent
```

After loser exit accepted:

```text
ONLY_NO_ACTIVE  + no_target = no_entry + winner_take_profit   (if YES stopped)
ONLY_YES_ACTIVE + yes_target = yes_entry + winner_take_profit  (if NO stopped)
```

**Same-tick dual stop (deterministic tie-break):**

1. Compute `yes_loss = yes_entry - yes_bid`, `no_loss = no_entry - no_bid`.
2. Exit the leg with **larger absolute loss** first (single intent this tick).
3. If equal loss: exit the leg with **wider spread** (`ask - bid`).
4. If still tied: exit **NO** first (documented convention; stable across restarts).

Emit `paired_binary_leg_stop` once per leg (deduped).

### 7.3 Winner take-profit

```text
ONLY_YES_ACTIVE: if yes_bid >= yes_target → SELL YES urgent → DONE after flat
ONLY_NO_ACTIVE:  if no_bid  >= no_target  → SELL NO  urgent → DONE after flat
```

Emit `paired_binary_winner_target` when target hit (deduped).

### 7.4 Max holding time

Anchor: `pair_opened_ts` = timestamp when state entered `BOTH_LEGS_ACTIVE` (or recovered equivalent).

When `now - pair_opened_ts >= max_holding_time_s`:

| State | Action |
|-------|--------|
| `BOTH_LEGS_ACTIVE` | SELL YES + SELL NO urgent |
| `ONLY_YES_ACTIVE` | SELL YES urgent |
| `ONLY_NO_ACTIVE` | SELL NO urgent |

→ `EXITING_BOTH` or leg-specific exiting states; fact `paired_binary_timeout_exit`.

### 7.5 Monitoring fact dedup

Do not emit monitoring facts every tick. Dedup keys:

```text
(entry_skip, reason, pair_cost_bucket)
(state_change, from, to)
(leg_stop, leg, trigger_price_bucket)
(winner_target, leg)
(timeout_exit)
```

Use price bucketing (e.g. 4 decimal places) consistent with existing protection tick dedup.

---

## 8. Runtime integration

### 8.1 Production loop (`paired_binary_run.py`)

```text
startup:
  validate live preflight (no fixtures/seeds)
  ensure MarketStateStore (market_data.enabled=true)
  paired_binary_recovery.reconcile_on_startup → state + paired_binary_recovered fact
  bootstrap REST books for yes + no tokens

loop while running and state not terminal:
  refresh market store (REST loop already running in background)
  if state in {IDLE}:
      result = strategy.evaluate_entry(ctx)
      process each intent via process_intent_work_unit
      on submit → BOTH_ENTRY_PENDING, start entry_fill_deadline
  if state in entry pending states:
      update leg fill progress from allocation + order store
      check entry_fill_timeout → unwind path
      wait for both legs allocation-final (FinalityWaiter per leg or combined helper)
      → BOTH_LEGS_ACTIVE, record yes_entry/no_entry, pair_opened_ts
  if state in active/monitor states:
      work = monitor.tick(coord, state, cfg)
      for w in work: await process_intent_work_unit(w)
      update state from lifecycle helpers
  sleep(tick_interval_s)

shutdown:
  emit health stopped fact
```

Wire in `app.py`:

```text
strategy_kind == paired_binary → run_paired_binary_loop(...)
```

Requires: `market_data.enabled`, `execution.planner.enabled`, `protection.enabled` optional (monitor is strategy-local; generic protection register **not** used for paired exits).

### 8.2 Procedural now → event-ready later

| Today (procedural) | Future (event-driven) |
|--------------------|------------------------|
| `while running: sleep(tick_interval_s); monitor.tick()` | `on MarketSnapshotUpdated(yes/no): monitor.tick()` |
| Entry eval on timer when `IDLE` | `on MarketSnapshotUpdated: evaluate_entry_if_idle()` |
| Poll allocation for entry finality | `on allocation_buy_applied / fill CONFIRMED: advance leg state` |
| `FinalityWaiter` poll in entry phase | `on fill finality event: resume entry state machine` |

Methods stay unchanged; only the caller moves from timer to subscription.

### 8.3 Entry finality

After both BUY submits:

1. Track each leg's fill progress via user WS + `allocation_ledger`.
2. Use `FinalityWaiter.wait_for_allocation_final` per leg (or batch wait with timeout = `entry_fill_timeout_s`).
3. Record `yes_entry` / `no_entry` as VWAP from fill evidence; fallback order in §9.2.
4. Only then start `pair_opened_ts` and enable stop/take-profit monitor.

---

## 9. Restart / recovery semantics

### 9.1 Startup reconcile

On startup (before loop):

```text
1. refresh wallet + allocation ledger from disk + venue REST
2. read persisted state file `var/state/paired_binary/<owner_id>/<market_id>.json` (if exists)
3. inspect allocation for owner_id on yes_token_id and no_token_id
4. classify recovered state:
     both qty > 0  → BOTH_LEGS_ACTIVE (or ONLY_* if persisted says loser already exited)
     only YES > 0  → ONLY_YES_ACTIVE (or EXITING_YES if sell in flight)
     only NO > 0     → ONLY_NO_ACTIVE
     neither       → DONE (or IDLE if run_once=false and policy allows re-entry)
5. emit paired_binary_recovered fact with recovery source metadata
```

### 9.2 Entry price recovery (preferred order)

```text
1. persisted paired strategy state file (yes_entry, no_entry, yes_target, no_target, pair_opened_ts)
2. AllocationLedger metadata if available (future: attach entry_vwap on allocation_buy_applied)
3. venue WalletStore avg_price fallback per token
```

**Never** silently reset entry price to current market bid/ask. If fallback used, fact must include:

```text
entry_price_source: persisted | ledger | venue_avg
```

### 9.3 Persistence file schema (minimal)

```json
{
  "owner_id": "paired_binary",
  "market_id": "...",
  "yes_token_id": "...",
  "no_token_id": "...",
  "state": "BOTH_LEGS_ACTIVE",
  "yes_entry": "0.48",
  "no_entry": "0.51",
  "yes_target": null,
  "no_target": null,
  "pair_opened_ts": 1710000000.0,
  "pair_correlation_id": "...",
  "yes_entry_client_order_id": "...",
  "no_entry_client_order_id": "...",
  "schema_version": 1
}
```

Persistence path (locked): `var/state/paired_binary/<owner_id>/<market_id>.json` — keyed by **owner_id + market_id** so the same owner can run multiple markets without collision.

Write atomically on every state change (async debounce ≤1s acceptable).

### 9.5 Pair-level correlation (locked)

Use one `pair_correlation_id` for the whole paired trade (entry, unwinds, stops, take-profit, timeout). Each submit keeps its own risk-minted `client_order_id`. Facts include `pair_correlation_id`, `leg` (`yes`|`no`), and `client_order_id`. Unwinds/exits preserve `pair_correlation_id` but never reuse client order ids.

### 9.4 In-flight orders on restart

Reconcile open orders for owner fingerprints. If EXITING_* and venue shows resting SELL, adopt state; if flat, advance to DONE/ONLY_*.

---

## 10. Facts / reporting

Add to `reporting/schema_v2.py` (implementation phase):

| Fact type | When |
|-----------|------|
| `paired_binary_entry_eval` | Entry allowed; includes pair_cost, spreads |
| `paired_binary_entry_skip` | Entry blocked; reason code |
| `paired_binary_entry_submitted` | Both BUY intents handed to pipeline |
| `paired_binary_state_change` | Any state transition |
| `paired_binary_leg_stop` | Stop-loss triggered |
| `paired_binary_winner_target` | Take-profit triggered |
| `paired_binary_timeout_exit` | Max hold exceeded |
| `paired_binary_unwind` | Entry abort unwind |
| `paired_binary_done` | Terminal success |
| `paired_binary_recovered` | Startup recovery |

Common payload fields:

```text
market_id, yes_token_id, no_token_id, owner_id, state,
yes_bid, yes_ask, no_bid, no_ask, pair_cost, yes_spread, no_spread,
yes_entry, no_entry, active_leg, target_price, reason,
pair_correlation_id, leg, client_order_id, entry_price_source
```

Existing pipeline facts (`intent_created`, `risk_decision`, `execution_plan`, `oms_submit`, `allocation_*`, `exit_lifecycle`) must appear for every submit.

---

## 11. Live validation ladder (PB-Levels)

Before claiming production-ready, run:

### PB-Level 1 — read-only entry scan

**Config:** `live_paired_binary_tiny.yaml` + read-only mode flag (`paired_binary.entry_dry_run: true`) or shadow with live books.

**Proves:** MarketStateStore YES/NO reads; pair_cost/spreads; `paired_binary_entry_eval` / `paired_binary_entry_skip` with reason codes.

**Pass:** No `oms_submit`. Facts show correct allow/deny.

### PB-Level 2 — tiny paired entry

**Proves:** Two-leg BUY; risk/planner/OMS; allocation on both legs; finality; `BOTH_LEGS_ACTIVE`; `paired_binary_entry_submitted`.

**Pass (does not require flat inventory):**

```text
both BUY legs submitted
both legs allocation-final
YES qty and NO qty paired at effective_qty
state = BOTH_LEGS_ACTIVE
effective_qty >= min_effective_pair_qty
no unpaired excess remains, or excess unwind was submitted
no naked one-sided entry position remains unintentionally
```

Flat inventory is **not** a PB-Level 2 pass criterion — that belongs to PB-Level 3 (timeout) and PB-Level 5 (winner exit).

### PB-Level 3 — forced max holding timeout

**Config:** `max_holding_time_s: 30` (tiny live).

**Proves:** `paired_binary_timeout_exit`; urgent FAK on all active legs; `DONE`; position flat or exit lifecycle clearly in progress.

### PB-Level 4 — loser stop-loss path

**Config:** Tight `loser_stop_loss` or select volatile market.

**Proves:** One leg stop; `paired_binary_leg_stop`; state becomes `ONLY_YES_ACTIVE` or `ONLY_NO_ACTIVE` correctly (remaining leg still open); winner target set.

**Note:** If market does not cooperate, shadow fixture scenario may prove logic — do **not** claim live-proven without real trigger facts.

### PB-Level 5 — winner take-profit path

After PB-Level 4 (or shadow continuation).

**Proves:** `paired_binary_winner_target`; remaining leg sold; `paired_binary_done`.

---

## 12. Tests required (implementation phase)

### Unit

```text
entry eval accepts good pair cost
entry eval rejects high pair cost
entry eval rejects wide YES spread
entry eval rejects wide NO spread
entry eval rejects stale book
entry creates two BUY intents
entry does not submit directly
partial YES fill then NO fail triggers unwind
partial NO fill then YES fail triggers unwind
both legs active state transition
minimum effective qty below threshold triggers unwind/failure
YES stop-loss emits urgent SELL YES
NO stop-loss emits urgent SELL NO
ONLY_YES take-profit emits SELL YES
ONLY_NO take-profit emits SELL NO
max holding timeout exits both legs
SELL sizes clamp to allocation
restart recovery both legs
restart recovery only YES
restart recovery only NO
persistence path uses owner_id and market_id
facts emitted and deduped
dual stop-loss tie-break deterministic
live preflight rejects fixtures/seeds
```

### Integration

```text
paired strategy uses MarketStateStore
paired exits use ExecutionPlanner FAK
paired exits route through process_intent_work_unit
paired monitor does not call OMS directly
live preflight rejects fixtures/seeds
```

Proposed files:

```text
tests/test_paired_binary_entry_eval.py
tests/test_paired_binary_state_machine.py
tests/test_paired_binary_monitor.py
tests/test_paired_binary_runtime.py
tests/test_paired_binary_recovery.py
```

---

## 13. Files likely to change (implementation)

```text
src/tyrex_pm/strategies/paired_binary/          (new package)
src/tyrex_pm/runtime/paired_binary_run.py       (new)
src/tyrex_pm/runtime/paired_binary_live.py      (new)
src/tyrex_pm/runtime/paired_binary_recovery.py  (new)
src/tyrex_pm/runtime/config.py                  STRATEGY_KIND + PairedBinaryConfig
src/tyrex_pm/runtime/app.py                     route paired_binary kind
src/tyrex_pm/core/reason_codes.py               entry skip reasons
src/tyrex_pm/reporting/schema_v2.py             fact types
config/strategies/paired_binary.yaml
config/scenarios/shadow_paired_binary.yaml
config/scenarios/live_paired_binary_tiny.yaml
tests/test_paired_binary_*.py
Docs/* (this phase + module READMEs)
```

**Explicitly not changing in 4.6:**

- `guru_follow` strategy logic
- Generic `protection/` TP/SL math (reuse sizing only)
- Event bus wiring

---

## 14. Risks / open questions

| # | Risk / question | Mitigation / proposal |
|---|-----------------|----------------------|
| 1 | Dual GTC entry: one leg fills, other rests | `entry_fill_timeout_s` + cancel + unwind (§5.3) |
| 2 | Partial fill mismatch | Trade at `min(q_yes, q_no)`; unwind excess immediately |
| 2b | effective_qty below min | Unwind all; FAILED — see `min_effective_pair_qty` |
| 3 | Restart without persistence | Require persistence before live tiny; fail closed if entries unknown |
| 4 | Stop fires during EXITING_* | State guards + per-leg `triggered` flag |
| 5 | Book stale during urgent exit | Planner stale deny; retry next tick; max-hold still applies |
| 6 | Risk cap for pair notional | Scenario must set `max_notional` ≥ pair_cost × Q × 2 |
| 7 | Cancel resting entry leg | Use `CancelIntent` through pipeline if OMS supports; else FAK SELL after fill |
| 8 | `run_once: false` re-entry after DONE | Default false for production tiny; document cooldown policy |
| 9 | Guru loop still without generic protection supervisor | Out of scope; separate follow-up after 4.6 |

**Approved (locked):**

- Option A — `PairedBinaryMonitor`; no generic `ProtectionRegistry` paired lifecycle in this phase.
- Dual stop tie-break: larger loss → wider spread → NO first.
- Partial mismatch: `effective_qty = min(YES, NO)`; unwind excess on heavier leg.
- Naked entry prevention mandatory.
- Persistence/recovery mandatory before live tiny.
- Entry price order: persisted → ledger metadata → venue avg; never silent market fallback.
- `pair_correlation_id` for audit; per-leg `client_order_id`.
- Persistence: `var/state/paired_binary/<owner_id>/<market_id>.json`.

---

## 15. Exit criteria (before Phase 5 production claims)

Phase 5 may proceed in parallel for read-only portfolio work, but **production planner/protection enablement on guru** should wait for:

- [ ] PB-Level 1–3 live-green on operator runs
- [ ] PB-Level 4–5 live-green **or** shadow-proven with documented market caveat
- [ ] Recovery test: kill process mid-`BOTH_LEGS_ACTIVE`, restart, monitor resumes
- [ ] No naked entry positions in any test scenario
- [ ] All §12 tests green

---

## 16. Non-goals

- Not a market-making or dynamic hedging engine
- No automatic market selection (single configured market in 4.6)
- No portfolio PnL views (Phase 5)
- No hard kill switch changes (Phase 6)
- No guru_follow integration in this phase
- No ML / prediction signals

---

## 17. Implementation status

**Implemented** (allocation clamp grace + entry reconciliation fix, 2026-06-26). Shadow-validated with clean state dir. Live PB-Level 2/3 ready for operator re-run after clearing stale `var/state/paired_binary/` FAILED persistence from prior live attempt.

---

## 18. Live truth sources and monitoring reliability

### Source priority (live)

| Concern | Primary | Repair / fallback | Notes |
|---------|---------|-------------------|-------|
| Fill evidence (fast) | User-WS `TradeFillRecord` (`MATCHED`/`MINED`/`CONFIRMED`) | OMS submit `matched` ack | OMS ack is not allocation-final alone |
| Fill finality | User-WS `CONFIRMED` (`fill_state.is_allocation_final`) | REST positions after lag | Wallet positions updated on CONFIRMED trades |
| Owner allocation | `AllocationLedger` (attribution) | `repair_allocation_to_target` from finality/venue | **Not** absolute venue truth |
| Venue inventory | `WalletStore.positions` (REST + WS) | data-api positions refresh loop | Used for SELL gate + clamp |
| Order book | `MarketStateStore` (REST bootstrap + refresh) | fixture books shadow-only | Monitor reads bid/ask here |

### Allocation clamp (post-fix)

After each `wallet_sync`, `clamp_to_venue_positions` runs. When `allocated_qty > venue_qty` **and** `venue_qty == 0`, clamp is **skipped** if the entry has a recent BUY within `runtime.allocation_ledger.clamp_grace_s_after_buy` (default **90s**). Emits `allocation_clamp_skipped_recent_buy`. After grace expires, over-allocation is still clamped down for safety.

BUY credits record metadata: `last_buy_applied_ts`, `provisional_until_ts`, `last_buy_correlation_id`, `last_buy_match_status`.

### Finality repair

When user-WS shows `CONFIRMED` BUY qty but ledger is below that (e.g. erroneous clamp), `repair_allocation_from_finality` raises ledger to confirmed qty. Emits `allocation_repaired_from_finality`.

### Paired entry completion (post-fix v2 — order vs allocation separation)

While `BOTH_ENTRY_PENDING`, completion uses **fill evidence only** via `entry_fill_lifecycle.resolve_leg_fill_snapshot` + `entry_qty_reconcile`:

1. User-WS `CONFIRMED` (allocation-final)
2. User-WS `MATCHED` / OMS matched qty on local order
3. Venue + ledger aligned (shadow instant fill or post-repair)

**Does not** activate on: submit ack alone, `ack_status=live`, ledger credit without fill proof, or single-leg resting order.

Emits `paired_binary_entry_qty_reconciled`. Resting BUY emits `allocation_buy_skipped_unfilled_order` + `order_resting_recorded` on `oms_submit`.

### Reduce-only urgent exit risk policy

Urgent reduce-only SELL denied for `deployment_mark_unknown` is re-evaluated in `validate_planned_order` using executable bid from planner book evidence (`risk.exits.allow_reduce_only_mark_fallback`, default true). Emits `reduce_only_exit_mark_fallback` / `mark_source=executable_bid` in planned-phase `risk_decision`.

### EXITING recovery (post-fix)

Startup `recover_on_startup` inspects venue qty + open sell orders for persisted `EXITING_*`. Emits `paired_binary_recovered` with `recovery_action`. Flat inventory + `FAILED` → `IDLE`; flat + `EXITING_*` → `DONE`.

### Entry timeout / unpaired

On `entry_fill_timeout_s`, if only one leg has confirmed/venue inventory, urgent unwind is attempted (`paired_binary_entry_timeout_unwind` + `paired_binary_unwind`). Both legs confirmed → repair and activate, not `FAILED`.

### Monitor start

When reconciled `effective_pair_qty >= min_effective_pair_qty`, state → `BOTH_LEGS_ACTIVE`, emits `paired_binary_monitor_started`. Monitor ticks read `MarketStateStore` books; SELL size = `min(planned, owner allocation, venue available)` via `clamp_exit_size`.

### Double-sell prevention

Paired state machine (`EXITING_*`, leg `triggered` flags) + exit reservations on `AllocationLedger` + `pair_correlation_id` on all intents.

### Restart recovery

Persisted state: `var/state/paired_binary/<owner_id>/<market_id>.json` + `var/state/allocation_ledger.json` + venue positions. Clear FAILED persistence before shadow re-run after a failed live entry.

### Remaining fragility (explicit)

- **Multiple truth layers** (ledger, wallet, WS, REST) can still transiently disagree; repair relies on CONFIRMED WS or visible REST positions.
- **SELL on resting ack**: live SELL allocation decrement still waits for match evidence (see `maybe_note_allocation_exit_order_live` TODO).
- **Owner attribution on WS trades** is by token_id only (no per-trade owner_id on venue); safe when one strategy owns a token per run.
- **Venue+ledger aligned** entry path requires both wallet position and ledger credit — prevents resting-order false activation but still depends on correct BUY fill crediting.
