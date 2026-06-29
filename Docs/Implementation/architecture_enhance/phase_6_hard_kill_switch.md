# Phase 6 — Hard kill switch

**Program:** [README.md](README.md) · **Prev:** [phase_5_portfolio_foundation.md](phase_5_portfolio_foundation.md)

> **Prerequisite:** [Phase 4.5 — Live Validation Harness](phase_4_5_live_validation_harness.md) must be green for P2–4 CLI paths before portfolio work begins.

> **Procedural now, event-ready later.** Cancel-all flows through OMS; no bus.

---

## 1. Goal

Extend the current **soft** kill switch into an explicit **soft vs hard** model. Hard kill cancels all open orders **through `SingleWriterOMS`** (never a direct venue call), while keeping monitoring/reconcile/reporting alive. All transitions emit facts and are documented in the runbook.

---

## 2. Why this phase exists

Today `risk/kill_switch.py` is **soft only**: when flipped, `RiskEngine` denies new orders (`KILL_SWITCH` reason). The review noted there is **no hard cancel-all path**. For real capital, operators need a single, auditable action that stops trading *and* pulls resting orders without bypassing the single-writer invariant.

---

## 3. Current state

```text
risk/kill_switch.py        → check_kill_switch (boolean gate; soft deny of new intents)
risk/engine.py             → gate #1 kill switch → reason_codes.KILL_SWITCH
core/reason_codes.py       → KILL_SWITCH = "kill_switch"
execution/oms.py           → SingleWriterOMS.cancel(ApprovedCancel) exists (single writer)
execution/cancel_manager.py→ best-effort idempotent cancel helpers
runtime/health_runtime.py  → HealthRuntime holds health/kill flags
venue/polymarket/clob_heartbeat.py → venue cancels all open orders if heartbeat lapses (venue-side safety)
```

A venue-side safety already exists (heartbeat lapse → venue cancels orders). Hard kill is the **bot-initiated, immediate** equivalent that is auditable and config-driven.

---

## 4. Target state

```text
risk/kill_switch.py
  KillMode = soft | hard
  check_kill_switch(ctx) honors mode:
    soft → deny new ENTRY intents; ALLOW protective ExitIntents (if configured)
    hard → deny all non-cancel intents

runtime/  (supervisor or operator command)
  trigger_hard_kill(coord, oms, sink, run_id):
    1. set mode=hard (deny new non-cancel intents)
    2. enumerate open orders from WalletStore (venue truth, merged view)
    3. for each → build ApprovedCancel → SingleWriterOMS.cancel(...)
    4. emit kill_switch facts per step
    5. keep user WS / reconcile / reporting running
  # optional, behind explicit config: emergency-exit positions (NOT default)
```

Definitions (locked):

```text
Soft kill:
- deny new entry intents
- allow protective exits (Phase 4 protection) if configured
- keep user WS, reconcile, reporting alive

Hard kill:
- deny all new non-cancel intents
- cancel all open orders through SingleWriterOMS
- keep monitoring/reconcile/reporting alive
- optional emergency position exit ONLY behind explicit config, off by default
```

---

## 5. Non-goals

- No direct venue cancel bypassing `SingleWriterOMS` (constraint #7).
- No automatic emergency position liquidation by default (opt-in config only).
- No new global shutdown of the process — reconcile/reporting must stay alive.
- No event bus.
- No multi-venue cancel orchestration.

---

## 6. Files likely to change

```text
src/tyrex_pm/risk/kill_switch.py        # soft/hard mode; protective-exit allowance in soft mode
src/tyrex_pm/risk/engine.py             # gate honors mode (cancel intents still allowed in hard)
src/tyrex_pm/core/reason_codes.py       # add KILL_SWITCH_HARD (or reuse KILL_SWITCH + mode in evidence)
src/tyrex_pm/execution/cancel_manager.py# cancel-all helper that builds ApprovedCancel set
src/tyrex_pm/runtime/app.py             # operator command / signal to trigger hard kill
src/tyrex_pm/runtime/live_supervisor.py # (if hard kill is driven from a supervisor)
src/tyrex_pm/runtime/config.py          # KillSwitchConfig (mode, allow_protective_exits, emergency_exit)
Docs/OPERATIONS.md                      # hard-kill runbook
Docs/reporting_fact_model.md            # kill_switch facts
Docs/modules/risk/README.md             # soft vs hard documentation
```

---

## 7. New files likely to be added

```text
tests/test_hard_kill_switch.py
(optional) src/tyrex_pm/runtime/kill_switch_runtime.py   # orchestration if app.py gets crowded
```

---

## 8. Data model changes

```python
# core/enums.py
class KillMode(str, Enum):
    OFF  = "off"
    SOFT = "soft"
    HARD = "hard"
```

`RiskContext.kill_switch` (currently boolean) generalizes to carry the mode (keep a boolean alias for back-compat during migration). `HealthRuntime` tracks current `KillMode`.

---

## 9. Config changes

```yaml
# config/risk/default.yaml
risk:
  kill_switch:
    mode: off                     # off | soft | hard
    allow_protective_exits: true  # soft mode: let ProtectionEngine exits through
    emergency_exit_positions: false   # hard mode: opt-in market exit of positions (default OFF)
    emergency_exit_urgency: urgent
```

`runtime/config.py`: typed `KillSwitchConfig`. Operators can also flip at runtime via an operator command (see §6) without editing YAML.

---

## 10. Fact/reporting changes

Add fact type `FACT_TYPE_KILL_SWITCH = "kill_switch"` plus cancel-lifecycle facts. Emit:

```text
kill_switch            # {mode: soft|hard, actor, reason, ts}            — on activation/transition
kill_switch_cancel_submitted   # per order: {client_order_id, venue_order_id, token_id}
kill_switch_cancel_completed   # per order: {venue_order_id, result}
kill_switch_cancel_summary     # {requested, acked, failed}              — terminal
```

Reuse existing `oms_cancel` (`FACT_TYPE_OMS_CANCEL`) for the actual OMS cancel rather than duplicating, if the join keys suffice; the kill-switch facts add the operator-level framing. Decision: emit `kill_switch` (activation) + a `kill_switch_cancel_summary`, and rely on existing `oms_cancel` per-order facts to avoid double schema.

---

## 11. Tests to add

```text
tests/test_hard_kill_switch.py
  test_soft_kill_blocks_entries
  test_soft_kill_allows_protective_exits
  test_soft_kill_keeps_reconcile_alive
  test_hard_kill_denies_all_non_cancel_intents
  test_hard_kill_cancels_all_open_orders_through_oms
  test_hard_kill_does_not_call_venue_directly      # asserts SingleWriterOMS is the only path
  test_hard_kill_emits_activation_and_summary_facts
  test_hard_kill_emergency_exit_off_by_default
  test_hard_kill_emergency_exit_when_enabled
```

---

## 12. Acceptance criteria

- Soft kill and hard kill are distinct modes.
- Soft kill blocks new entries but allows protective exits (when configured) and keeps reconcile/reporting alive.
- Hard kill denies all non-cancel intents and cancels all open orders **through `SingleWriterOMS`**.
- No cancel path bypasses `SingleWriterOMS`.
- Every cancel emits facts; activation and summary facts exist.
- Emergency position exit is off by default and only runs behind explicit config.
- A runbook in `OPERATIONS.md` documents how to trigger and recover.

---

## 13. Migration risks

- **Cancel storm vs single writer:** cancel-all must enqueue onto the one OMS queue; it must not spawn parallel cancels. Test the single-path invariant.
- **Cancel/refill race:** hard kill must set `mode=hard` (deny new) **before** issuing cancels, or a strategy could resubmit mid-cancel.
- **Partial-fill cancels:** Polymarket only cancels the unfilled remainder; reconcile must not flag the filled part as drift. Reuse existing tombstone/reconcile machinery.
- **Emergency exit danger:** market-exiting in thin books can realize large slippage. Keep off by default; route through `ExecutionPlanner` (FAK worst price) when enabled.
- **Stuck hard mode:** define recovery (operator resets `mode=off` after reconcile is clean).

---

## 14. Rollback strategy

- `risk.kill_switch.mode=off` restores pre-Phase-6 behavior; soft-only logic is preserved as a subset.
- Land in two commits: (a) soft/hard config + gate behavior (no cancel-all yet); (b) cancel-all orchestration + facts. Roll back (b) to keep soft/hard gating without automated cancel-all.

---

## 15. Dependencies on previous phases

- **Phase 4** — soft kill's "allow protective exits" requires the `ProtectionEngine` exit path to exist.
- **Phase 3** — emergency exit (opt-in) uses `ExecutionPlanner` for FAK worst-price execution.
- **Phase 1** — intents (including cancels) flow through the generic pipeline.

**Explicitly NOT dependent on Phase 5 (portfolio).** Hard kill needs only:

```text
WalletStore.open_orders     # what to cancel
SingleWriterOMS.cancel      # the only cancel writer
risk kill mode              # gate behavior
facts                        # audit
```

It does not need positions views, attribution, or PnL. Emergency position exit stays off by default; when enabled it reuses the Phase 3 planner, not portfolio.

---

## 16. Event-ready design notes

Hard kill is an **operator action** that calls `trigger_hard_kill(...)`, which itself only sets a mode flag and enqueues `ApprovedCancel`s onto `SingleWriterOMS`. A future event bus could publish a `KillSwitchActivated` event consumed by the same orchestration function; the gate (`check_kill_switch`) remains a pure read of `RiskContext.kill_switch`. The cancel path is already single-writer and bus-agnostic. No bus added.
