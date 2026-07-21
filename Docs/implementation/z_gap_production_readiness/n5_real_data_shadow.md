# N5 — Real-data SHADOW

**Status:** `PASS_WITH_ENVIRONMENT_BLOCKER` (N5A executed; N5B deferred)  
**Acceptance:** [n5_acceptance_report.md](n5_acceptance_report.md)  
**Document:** `Docs/implementation/z_gap_production_readiness/n5_real_data_shadow.md`  
**Depends on:** N4 real-input OBSERVE engineering acceptance  
**Unblocks:** N6 live execution planning completion / N7 evidence gate

N5A delivers offline deterministic SHADOW composition over N4-aligned inputs
(`S=\hat{C}_t`, sealed Chainlink \(K\)) with `shadow_depth_walk_v1`. Live
real-input SHADOW (N5B) remains environment-blocked on this host (TLS).

---

## 1. Objective

Operate Z-Gap SHADOW with **real market inputs** and **simulated execution**
via the existing ShadowOMS path (extended if needed for depth-walk fills):

- real books/depth, **attested+locked** PTB for entry, references, time;
- simulated fills under the **precise depth-walk model** below;
- full entry → rich/thesis/time/risk exit lifecycle;
- optional simulated resolution (F5 capability, labeled);
- Portfolio + TradeLifecycle + restart/recovery + full reporting;
- prepared-next continuous mode rules from N4 (require flat before promote);

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
| Inputs | Same real adapters/PTB/basis as N4; entry requires attested+locked K |
| Decision path | Same sealed binding as OBSERVE |
| Execution | `ShadowOMS` (+ extensions for depth-walk / partials if needed) |
| Fill model | See **Initial fill model** below (model ID recorded) |
| Exits | Market-rich, thesis, time, risk flatten |
| Resolution | Optional F5 simulated path; capability default OFF (Scope A alignment) |
| Portfolio / lifecycle | Framework owners |
| Restart | `StateSnapshotStore` recovery (extend for real window ids) |
| Labels | `simulated_shadow`, `estimated` fees/PnL |
| Continuous | Prepared-next; **require flat** before promote |

### Initial fill model (implementable)

Model ID (provisional): `shadow_depth_walk_v1`

1. Record decision/plan time.  
2. Apply configured simulated latency → simulated arrival time.  
3. Wait until recorded ingress time has reached arrival (no pre-arrival fill).  
4. Select the latest recorded book with `available_at ≤ arrival` when present;
   otherwise the first book at/after arrival once latency elapsed.  
5. Walk executable depth for requested quantity.  
6. Apply configured additional slippage only if required by config (no double-count with depth walk).  
7. Produce **full fill**, **partial fill**, or **no fill**.  
8. Record model ID and all assumptions on facts.

Never use a book with `available_at > arrival` when a pre-arrival book exists
(no look-ahead).

Constraints:

- No queue-position claim.  
- No fill if executable depth is absent.  
- Never use a future book earlier than simulated order arrival (no look-ahead).  
- Deterministic replay from ordered book ingress + model params.  
- All P&L remains `simulated_shadow`; fees `estimated` unless a confirmed field exists (it will not in SHADOW).  
- Partial-entry / partial-exit / residual recovery must be testable even if ShadowOMS needs extension.

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
- Fill model `shadow_depth_walk_v1` (decision #7) frozen before N5 acceptance
- Real entry requires attested+locked PTB (N3)
- Resolution capability default OFF unless explicitly testing hold path
- Prepared-next + require_flat (N4)

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
| Shadow fill model config | `model_id`, `latency_ms`, `extra_slip_ticks`, depth-walk flags |
| Partial fill / residual | Portfolio + lifecycle handle confirmed residual only |
| Fact labels | Enforce `economics_label=simulated_shadow` on fills/PnL |
| Real-input shadow config | New JSON distinct from F4/F5 fixtures |
| Recovery | Window_id + market_id + config hash continuity on real runs |
| Optional resolution evidence | Real resolution feed **or** fixture-style injector (labelled) |

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
Real feeds → sealed evaluate (same as N4; entry needs attested+locked PTB)
  → Enter/Exit/Flatten intents
  → RiskEngine → Planner
  → ShadowOMS depth-walk fill model (shadow_depth_walk_v1)
  → OrderStore / FillLedger → Portfolio / TradeLifecycle
  → next DecisionContext
  → exits / optional resolution simulation
  → facts + operator report
```

Continuous mode: prepared-next as N4; **require flat** before promote (force
time/risk exit before boundary). Never evaluate prepared-next.

---

## 11. Failure and degraded-mode behavior

| Exposure state | Failure behavior |
|----------------|------------------|
| FLAT | Block new exposure when PTB/basis/feeds not ready |
| ACTIVE with confirmed inventory | Continue risk management; seek safe exit (book loss → explicit exit/escalation policy) |
| Inventory UNKNOWN | Reconcile; never guess quantity; no blind sell |
| Entry order ambiguous | N/A (sim model); treat no-fill / partial via lifecycle |
| Exit partially filled | Manage only confirmed residual quantity |
| Resolution committed | Remain pending; do not fabricate a sell |

| Failure | Behavior |
|---------|----------|
| Stale books while ACTIVE | Seek safe exit / escalate per risk; do not open new risk |
| PTB/basis degrade while FLAT | Block entry |
| PTB/basis degrade while ACTIVE | Do not prevent exit management |
| Open position at window end (capability OFF) | Mandatory time/risk flatten before end |
| No depth at simulated arrival | No fill; consume lineage per policy |
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
| `shadow.fill.model_id` | shadow config | string |
| `shadow.fill.latency_ms` | shadow config | ms |
| `shadow.fill.extra_slip_ticks` | shadow config | ticks |
| `shadow.persistence_path` | shadow config | path |
| `risk.max_order_notional` | risk | USDC (hard max) |
| `z_gap.resolution_capability` | z_gap | bool |
| `rollover.require_flat` | runtime | bool (default true) |

---

## 15. Test strategy

| Scenario | Assert |
|----------|--------|
| Real-input recorded books → entry→rich exit | Terminal flat; labels correct |
| Depth-walk full / partial / no-fill | Deterministic from recorded books + latency |
| Partial entry + residual recovery | Confirmed qty only |
| Partial exit + residual | Confirmed residual only |
| Thesis / time / risk | Same as F4 on recorded real timeline |
| Resolution optional | F5 semantics if enabled |
| Restart active / exit pending | Recovery |
| No look-ahead book | Reject fill using book before arrival time |
| No LiveOMS import on path | Architecture |

### Acceptance scenarios (engineering)

1. Entry + market-rich exit on real books (recorded or live net-read)
2. Depth-walk full fill, partial fill, and no-fill
3. Partial entry + residual recovery; partial exit + residual
4. Thesis invalidation path
5. Time flatten near expiry
6. Kill switch flatten
7. UNKNOWN block
8. Restart recovery
9. Continuous: flat → prepared-next promote → second window (no open cross-window)

---

## 16. Deterministic acceptance criteria

1. Real-input SHADOW completes scenarios in §15 without venue mutation.
2. All economic facts labeled `simulated_shadow` / `estimated` as appropriate.
3. Same sealed decision entrypoint as N4 OBSERVE (shared test).
4. Prepared-next never evaluates; promote requires flat.
5. Fill model `shadow_depth_walk_v1` parameters recorded; no look-ahead books; no queue claim.
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
| SHADOW fill model | `shadow_depth_walk_v1` (latency → book@arrival → depth walk) |
| Partial fills | Supported in tests; strategy still prefers full-exit policy |
| Resolution in N5 acceptance | Optional off by default (Scope A alignment) |
| Continuous shadow | Allowed only with require_flat + prepared-next |
| Entry PTB | Attested and locked |

---

## 20. Expected commit boundary

```text
N5 commit theme:
  "Run Z-Gap SHADOW on real inputs with labeled simulated execution"

Include: shadow wiring, fill-model config, tests, N5 acceptance docs
Exclude: LiveOMS enablement, real orders, redeem
```
