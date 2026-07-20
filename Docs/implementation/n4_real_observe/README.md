# N4 — Real-input OBSERVE

**Status:** planned (no implementation in this planning commit)  
**Milestone folder:** `Docs/implementation/n4_real_observe/`  
**Depends on:** N2 adapters + N3 PTB/basis  
**Unblocks:** N5 real-input SHADOW (engineering gate)

---

## 1. Objective

Run end-to-end **read-only** Z-Gap OBSERVE on real inputs:

- real market discovery;
- real PTB;
- real Chainlink + Binance references;
- real CLOB order books;
- real time;

using the **existing sealed** Z-Gap decision path (`StrategyBinding` → assemble →
`ZGapStrategy` → facts).

Absolutely **no** OMS / order submission.

Support **single-window** and **continuous** modes with correct rollover and
subscription replacement. Acceptance is a bounded engineering validation, not a
profitability study.

---

## 2. Why the milestone exists

F3 proved fixture OBSERVE. Production trust requires proving that real feeds,
PTB, time, discovery, and rollover compose without corrupting epochs or opening
an order path. N4 is the first mode that can generate calibration-grade
counterfactual facts from live markets.

---

## 3. Scope

| Capability | Requirement |
|------------|-------------|
| Single-window OBSERVE | Discover → sync → feeds → evaluate until window end → stop |
| Continuous OBSERVE | Rollover to next window; replace CLOB subscriptions; reset PTB/EWMA policy per window |
| Decision path | Identical pure path as fixture OBSERVE / SHADOW |
| Facts | Decisions, rejections, quality, latency, basis, valuations, counterfactuals |
| Snapshots | Reconstructible sealed decision inputs |
| Operator status | Readable run status + summary report |
| Restart | No duplicate processing of the same sealed decision epoch |
| Config | Explicit real-input observe config (separate from fixture F3) |

**Proposed engineering acceptance sample (configurable):**

```text
N_consecutive_windows = 5   # provisional; freeze in N4 config/acceptance
```

Per window, assert engineering truths in §16 — not PnL significance.

---

## 4. Explicit non-goals

- No OMS, ShadowOMS fills, Portfolio mutation, or live orders
- No hyperparameter optimization as acceptance gate
- No profitability / Brier significance requirement
- No resolution redeem
- No unattended multi-day production service claim
- No Z-Gap branches in RiskEngine/planner/OMS

Extended tuning is **useful** and should be documented as ongoing research —
**not** a blocker for later tiny live (N7) once engineering gates pass.

---

## 5. Dependencies and entry criteria

| Criterion | Milestone |
|-----------|-----------|
| RTDS + Binance + CLOB + discovery | N2 |
| PTB capture + basis alignment | N3 |
| Fixture OBSERVE parity | F3 |
| Same binding as SHADOW | F4 architecture |
| Operator host with stable clock | Ops |

---

## 6. Decisions that must already be frozen

- OBSERVE records intents without OMS dispatch
- Atomic epochs / window identity
- PTB quality labeling for provisional vs confirmed
- One-run vs continuous share adapters (N2)
- Capture rule + confirmation policy (N1/N3)

---

## 7. Responsibility / module ownership

| Concern | Owner |
|---------|-------|
| Orchestration (one-run / continuous) | `runtime/observe_host.py` + new continuous supervisor (generic) |
| Binding / evaluate | `runtime/strategy_binding.py` |
| Strategy economics | `strategies/z_gap` (unchanged formulas) |
| Feeds | N2 adapters |
| PTB / basis | N3 services |
| Facts / report | reporting sink + CLI |
| Intent recording | ObserveHost `_record_observe_intents` only |

---

## 8. Contracts, ports, and data structures to add or evolve

| Item | Change |
|------|--------|
| Observe orchestration | Window session state: `discovering`, `warming`, `active`, `rolling`, `stopped` |
| Rollover event | Explicit market identity swap + store clear / rebind |
| Dedup key | `(window_id, decision_epoch_id)` for restart safety |
| Config | `strategy_kind=z_gap` real observe JSON; `mode=OBSERVE`; no shadow/OMS block |
| Status schema | Operator JSON: feeds ready, PTB quality, last decision, rollover count |

---

## 9. Expected files / modules affected

```text
src/tyrex_pm/runtime/observe_host.py
src/tyrex_pm/runtime/live_runner.py            # generalize or extract rollover
src/tyrex_pm/runtime/strategy_binding.py       # remove fixture-PTB-only assumption when real
src/tyrex_pm/runtime/config.py
src/tyrex_pm/application/cli.py
config/observe_z_gap_real_n4.json              # new (when implementing)
Docs/latest/how_to/run_modes.md                # document real observe
tests/test_n4_*                                # orchestration + no-OMS guards
```

---

## 10. End-to-end data or control flow

```text
CLI observe --config observe_z_gap_real_n4.json [--continuous]

→ TimeAuthority sync
→ Discover market (current/next per policy)
→ Start feeds (RTDS CL, Binance, CLOB tokens)
→ Warm EWMA / wait PTB class acceptable for OBSERVE
→ On book/ref/timer:
     build DecisionSnapshot
     → ZGapBinding.evaluate (sealed)
     → record StrategyDecision + counterfactual intents (NO OMS)
→ Window end:
     emit window summary
     if continuous: discover next → resubscribe → reset window-local state
     else: stop
→ Graceful shutdown on SIGINT
```

---

## 11. Failure and degraded-mode behavior

| Failure | Behavior |
|---------|----------|
| Feed stale | SKIP/WAIT/BLOCKED per existing readiness; fact reason |
| PTB missing/late | No entry desire; OBSERVE still records evaluations |
| PTB mismatch | Block trading desires; continue diagnostics |
| Rollover discover fail | Stop continuous loop fail-closed; do not keep old tokens |
| Mixed epoch detected | Reject evaluation; fact + alert |
| Accidental OMS wiring | Hard assert / test failure — stop |

---

## 12. Persistence and restart behavior

- OBSERVE does not require Portfolio persistence
- Persist optional: last processed `(window_id, decision_epoch_id)` + config fingerprint
- On restart: do not re-emit identical decision facts for the same epoch
- Mid-rollover crash: restart as new discover; never keep half-swapped token subscriptions

---

## 13. Facts, metrics, and reporting

| Family | Examples |
|--------|----------|
| Quality | freshness, basis, PTB class, clock uncertainty |
| Latency | feed receive delays, evaluate lag |
| Decision | action, reason_code, edges, valuations (counterfactual) |
| Rollover | old/new market_id, resubscribe OK |
| Guard | `oms_dispatch=false` invariant fact/metric |

Operator report: per-window table of market identity, PTB vs display agreement,
token map OK, epoch violations=0, readiness summary, decision counts by reason.

---

## 14. Configuration ownership and units

| Key | Owner | Units |
|-----|-------|-------|
| `btc_window` / `which` | runtime | current\|next |
| `continuous` | runtime | bool |
| `acceptance.windows` | runtime/n4 | count |
| `duration_s` | runtime (one-run cap) | s |
| `z_gap.*` thresholds | strategy config | existing units |
| `ptb.require_confirmed_for_entry` | may be false for OBSERVE eval | bool |
| Output paths | reporting | path |

---

## 15. Test strategy

| Test | Assert |
|------|--------|
| Fake real-input timeline | Decisions match sealed path; zero OMS calls |
| Rollover | Token subscription replaced; no mixed market_id in snapshot |
| Restart dedup | Same epoch not double-facted |
| Architecture | Host has no Z-Gap formulas; no LiveOMS |
| Regression | Fixture F3/F4/F5 still pass |

Net-read smoke: manual operator checklist (§16), not CI-mandatory if secrets/net flaky.

---

## 16. Deterministic acceptance criteria

For the configured consecutive-window sample (default proposal: **5**):

1. **Correct market** each window (slug/Gamma identity matches intended BTC 5m UP/DOWN).
2. **PTB/display agreement** within N3 tolerance (or explicit documented mismatch rate = 0 for acceptance windows).
3. **Valid token mapping** (YES/NO IDs match outcomes).
4. **No mixed epochs** (zero sealed-snapshot mismatches).
5. **Acceptable data freshness** (no silent decide-on-stale; stale → gated reasons only).
6. **Successful rollover** in continuous mode (resubscribe + new window_id).
7. **No unintended order path** (static + runtime guard: OMS submit count = 0).
8. Reconstructible decision snapshots for spot-checked evaluations.
9. Pytest green.

**Non-criteria:** positive expected edge, calibration convergence, threshold tuning.

---

## 17. Expected deliverables

- Real-input OBSERVE config + CLI path
- Continuous rollover supervisor
- Operator report template
- N4 acceptance evidence (window sample)
- Notes on useful-but-non-blocking calibration follow-ups

---

## 18. Stop conditions

- PTB cannot meet agreement criterion and no safe OBSERVE-only provisional policy exists
- Rollover cannot avoid mixed token books
- Any code path can submit orders from OBSERVE
- Clock uncertainty permanently above gate with no ops fix

---

## 19. Remaining risks and decisions

| Item | Recommendation |
|------|----------------|
| Acceptance window count | 5 consecutive; configurable |
| Provisional K in OBSERVE | Allowed with labels; do not claim confirmed |
| Continuous default | Off; operator enables |
| Deployment location | Prefer low-latency stable clock host (open decision #12) |

---

## 20. Expected commit boundary

```text
N4 commit theme:
  "Wire real-input Z-Gap OBSERVE with window rollover"

Include: runtime orchestration, config, tests, N4 acceptance docs
Exclude: ShadowOMS real-input productization (N5), LiveOMS, orders
```
