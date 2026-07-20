# N5 — Real-data SHADOW

**Status:** planned (no implementation in this planning commit)  
**Milestone folder:** `Docs/implementation/n5_real_data_shadow/`  
**Depends on:** N4 real-input OBSERVE engineering acceptance  
**Unblocks:** N6 live execution planning completion / N7 evidence gate

---

## 1. Objective

Operate Z-Gap SHADOW with **real market inputs** and **simulated execution**
via the existing ShadowOMS path:

- real books/depth, PTB, references, time;
- simulated fills under explicit latency/slippage assumptions;
- full entry → rich/thesis/time/risk exit lifecycle;
- optional simulated resolution (F5 capability, labeled);
- Portfolio + TradeLifecycle + restart/recovery + full reporting;

with clear `simulated_shadow` and `estimated` labels everywhere economics are
not venue-confirmed.

---

## 2. Why the milestone exists

F4/F5 proved lifecycle on fixtures. Real books change fillability, timing, and
exit feasibility. N5 is the last mode before live that can exercise the full
intent→risk→plan→OMS→portfolio loop without venue mutation — and must honestly
label what cannot be proven without real orders.

---

## 3. Scope

| Area | Plan |
|------|------|
| Inputs | Same real adapters/PTB/basis as N4 |
| Decision path | Same sealed binding as OBSERVE |
| Execution | Current `ShadowOMS` only |
| Fill model | Configurable latency delay, slippage ticks, optional partial fills if ShadowOMS supports |
| Exits | Market-rich, thesis, time, risk flatten |
| Resolution | Optional F5 simulated path; capability default per config |
| Portfolio / lifecycle | Framework owners |
| Restart | `StateSnapshotStore` recovery (extend for real window ids) |
| Labels | `simulated_shadow`, `estimated` fees/PnL |

### What SHADOW cannot prove

| Claim | Status |
|-------|--------|
| Queue priority / adverse selection | **Not proven** |
| Actual venue slippage | **Not proven** |
| Actual fees | **Estimated only** |
| Ack / MATCHED→CONFIRMED races | **Not proven** (N6) |
| Redeem / payout finality | Simulated only if enabled |

---

## 4. Explicit non-goals

- No LiveOMS / real orders
- No treating simulated PnL as live readiness proof of edge
- No continuous unattended “production shadow service” claim without ops design
- No Z-Gap-specific OMS
- No silent upgrade of estimated fees to confirmed

---

## 5. Dependencies and entry criteria

| Criterion | Source |
|-----------|--------|
| N4 acceptance (engineering sample) | Required |
| F4/F5 lifecycle | Required |
| ShadowOMS + ExitPlanner + RetryController | Existing |
| Persistence fingerprinting | Existing |

---

## 6. Decisions that must already be frozen

- Same decision path OBSERVE/SHADOW
- ShadowOMS is paper-only
- Economics labels honesty
- Fill assumption policy id recorded in facts (open decision #7 must be chosen before N5 acceptance)
- Resolution capability default OFF unless explicitly testing hold path

---

## 7. Responsibility / module ownership

| Concern | Owner |
|---------|-------|
| Host | `ShadowHost` |
| Binding | `StrategyBinding` / `ZGapBinding` |
| Risk / plan | RiskEngine, ExecutionPlanner, ExitPlanner |
| OMS | ShadowOMS |
| Portfolio / lifecycle | Portfolio, TradeLifecycle |
| Fill assumptions | ShadowOMS config + facts (not strategy) |
| Real feeds | N2/N3 |

---

## 8. Contracts, ports, and data structures to add or evolve

| Item | Change |
|------|--------|
| Shadow fill model config | `latency_ms`, `slip_ticks`, `partial_fill_mode` |
| Fact labels | Enforce `economics_label=simulated_shadow` on fills/PnL |
| Real-input shadow config | New JSON distinct from F4/F5 fixtures |
| Recovery | Window_id + market_id + config hash continuity on real runs |
| Optional resolution evidence | Real resolution feed **or** continue fixture-style evidence injector behind port (must be labeled) |

---

## 9. Expected files / modules affected

```text
src/tyrex_pm/runtime/shadow_host.py
src/tyrex_pm/runtime/shadow_config.py
src/tyrex_pm/execution/shadow_oms.py
src/tyrex_pm/runtime/config.py
config/observe_shadow_z_gap_real_n5.json
tests/test_n5_*
Docs/latest/how_to/run_modes.md
```

---

## 10. End-to-end data or control flow

```text
Real feeds → sealed evaluate (same as N4)
  → Enter/Exit/Flatten intents
  → RiskEngine → Planner
  → ShadowOMS (simulated fill under model)
  → OrderStore / FillLedger → Portfolio / TradeLifecycle
  → next DecisionContext
  → exits / optional resolution simulation
  → facts + operator report
```

Continuous mode: rollover only when flat (or fail-closed if position open at boundary — **must specify**: recommended **forbid rollover while open**; force time/risk exit before boundary).

---

## 11. Failure and degraded-mode behavior

| Failure | Behavior |
|---------|----------|
| Stale books mid-position | Fail-closed exit policy / BLOCKED per readiness |
| UNKNOWN inventory (sim) | Block; no blind sell |
| Open position at window end without resolution capability | Mandatory time/risk flatten before end |
| Feed loss | Kill or flatten per risk config |
| Restart with exit pending | Resume exit; no re-entry |

---

## 12. Persistence and restart behavior

- Use `StateSnapshotStore` as F4/F5
- Fingerprint includes market_id, window_id, config hash, `runtime_mode=SHADOW`
- Restore orders/fills/portfolio/lifecycle/strategy private slice
- After restore: reconcile internal consistency before new entry

---

## 13. Facts, metrics, and reporting

| Family | Notes |
|--------|-------|
| Full F4/F5 taxonomy | Plus real-input quality/latency |
| Fill model id | Always present |
| PnL | `simulated_shadow` / `estimated` |
| Exit family attribution | rich / thesis / time / risk / resolution |
| Counterfactual vs simulated | OBSERVE-only edges vs SHADOW fills — keep distinct |

---

## 14. Configuration ownership and units

| Key | Owner | Units |
|-----|-------|-------|
| `shadow.fill.latency_ms` | shadow config | ms |
| `shadow.fill.slip_ticks` | shadow config | ticks |
| `shadow.persistence_path` | shadow config | path |
| `risk.max_order_notional` | risk | USDC |
| `z_gap.resolution_capability` | z_gap | bool |
| `rollover.require_flat` | runtime | bool (default true) |

---

## 15. Test strategy

| Scenario | Assert |
|----------|--------|
| Real-input recorded books → entry→rich exit | Terminal flat; labels correct |
| Thesis / time / risk | Same as F4 on recorded real timeline |
| Resolution optional | F5 semantics if enabled |
| Restart active / exit pending | Recovery |
| No LiveOMS import on path | Architecture |
| Acceptance scenarios checklist | Documented below |

### Acceptance scenarios (engineering)

1. Entry + market-rich exit on real books (recorded or live net-read)
2. Thesis invalidation path
3. Time flatten near expiry
4. Kill switch flatten
5. UNKNOWN block
6. Restart recovery
7. Continuous: flat→rollover→second window evaluate (no open cross-window)

---

## 16. Deterministic acceptance criteria

1. Real-input SHADOW completes scenarios in §15 without venue mutation.
2. All economic facts labeled `simulated_shadow` / `estimated` as appropriate.
3. Same sealed decision entrypoint as N4 OBSERVE (shared test).
4. Rollover never carries open simulated position into a new window (default).
5. Fill model parameters recorded and stable for the acceptance run.
6. Explicit written list of **unproven** live effects (queue, real slip, fees, settlement races).
7. Pytest green.

---

## 17. Expected deliverables

- Real-input shadow config + CLI
- Fill-model documentation
- Acceptance scenario evidence pack
- Operator report with simulated PnL clearly marked

---

## 18. Stop conditions

- Simulated fills would require guessing queue position as truth
- Labels missing on PnL facts
- Pressure to enable LiveOMS inside N5
- Open position rollover without explicit approved policy

---

## 19. Remaining risks and decisions

| Decision | Provisional default |
|----------|---------------------|
| SHADOW fill assumptions | Latency + slip ticks; no queue model |
| Partial fills | Follow ShadowOMS capability; strategy still full-exit policy |
| Resolution in N5 acceptance | Optional off by default (Scope A alignment) |
| Continuous shadow | Allowed only with require_flat rollover |

---

## 20. Expected commit boundary

```text
N5 commit theme:
  "Run Z-Gap SHADOW on real inputs with labeled simulated execution"

Include: shadow wiring, fill-model config, tests, N5 acceptance docs
Exclude: LiveOMS enablement, real orders, redeem
```
