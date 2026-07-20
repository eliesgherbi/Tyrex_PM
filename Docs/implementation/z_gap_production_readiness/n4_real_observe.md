# N4 — Real-input OBSERVE

**Status:** planned (no implementation in this planning commit)  
**Document:** `Docs/implementation/z_gap_production_readiness/n4_real_observe.md`  
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
| Continuous OBSERVE | **Prepared-next** discover/subscribe before boundary; atomic promote; preserve EWMA |
| Decision path | Identical pure path as fixture OBSERVE / SHADOW |
| Outcome map | Label-based `"Up"`→`UP`, `"Down"`→`DOWN` only |
| Facts | Decisions, rejections, quality, latency, basis, valuations, counterfactuals |
| Snapshots | Reconstructible sealed decision inputs |
| Operator status | Readable run status + summary report |
| Restart | No duplicate processing of the same sealed decision epoch; EWMA warm-up policy |
| Config | Explicit real-input observe config (separate from fixture F3) |
| Timing measurements | Collect latency samples informing Scope A deadlines (N7 freeze) |

### Window-local vs continuous cross-window state

| Window-local (reset on atomic promote) | Continuous cross-window (**preserve**) |
|----------------------------------------|----------------------------------------|
| PTB (quality/lock/readiness) | Binance price history |
| Market / token / condition binding | **EWMA volatility** |
| Entry lineage / thesis confirm | Shared Chainlink/Binance connections |
| Books / window timers | Clock health / connection health |

**Do not reset EWMA at every five-minute rollover.**

### Restart / cold-start warm-up (freeze before N4 implementation)

Choose and document one (or ordered fallback):

1. Restore validated persisted EWMA state; or  
2. Perform bounded market-data backfill; or  
3. Wait through an explicit warm-up period.

Never silently use an unseeded or stale volatility estimator.

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
- PTB axes: provisional OBSERVE OK with labels; SHADOW/live need attested+locked (N3)
- One-run vs continuous share adapters (N2)
- Capture rule + confirmation policy (N1/N3)
- EWMA warm-up policy (#18)
- Prepared-next lead time (#19)

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
| Observe orchestration | Sessions: `active`, `prepared_next`, `warming`, `stopped` |
| Prepared-next | Discover + validate Up/Down map + prepare CLOB before boundary |
| Atomic promote | Prepared → active; then retire previous market-specific subscriptions |
| Dedup key | `(window_id, decision_epoch_id)` for restart safety |
| Config | `strategy_kind=z_gap` real observe JSON; `mode=OBSERVE`; no shadow/OMS block |
| Status schema | Operator JSON: feeds ready, PTB axes, active/prepared ids, EWMA warm status |
| EWMA persistence | Optional validated snapshot for restart warm-up |

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

→ ClockSyncProvider + TimeAuthority view
→ Start shared feeds (RTDS CL, Binance) — keep across windows
→ Discover + validate active market (Up/Down label map)
→ Subscribe active CLOB tokens
→ Warm EWMA per frozen policy; wait PTB quality acceptable for OBSERVE labels
→ On book/ref/timer (active session only):
     sealed evaluate → counterfactual intents (NO OMS)

Continuous prepared-next (before boundary):
  1. Keep shared Binance/Chainlink running
  2. Pre-compute next 5m window identity
  3. Discover next Gamma market
  4. Validate Up/Down token map and rules
  5. Prepare/subscribe next CLOB books
  6. At boundary: capture next PTB
  7. Atomically promote prepared → active
  8. Retire previous market-specific subscriptions after safe cutover

No order or strategy evaluation may use prepared-next before it becomes active.

→ Graceful shutdown on SIGINT
```

---

## 11. Failure and degraded-mode behavior

| Exposure / situation | Behavior |
|----------------------|----------|
| FLAT (always in OBSERVE inventory sense) | Block “entry desire” when readiness fails; still record diagnostics |
| Feed stale | SKIP/WAIT/BLOCKED reasons; no silent decide-on-stale |
| PTB provisional | Evaluate only with provisional + counterfactual labels |
| PTB mismatch | Entry desire blocked; continue diagnostics |
| Prepared-next discover/map fail | Do not promote; keep active until safe stop; fact + alert |
| Mixed epoch / mixed books | Reject evaluation; fact + alert |
| Restart mid-preparation | Discard incomplete prepared-next; rebuild prep |
| Accidental OMS wiring | Hard assert / test failure — stop |
| Unseeded EWMA | Do not evaluate entry-class decisions until warm-up complete |

---

## 12. Persistence and restart behavior

- OBSERVE does not require Portfolio persistence
- Persist optional: last processed `(window_id, decision_epoch_id)` + config fingerprint
- Optional: validated EWMA snapshot for warm-up on restart
- On restart: do not re-emit identical decision facts for the same epoch
- Restart during preparation: never keep half-swapped token subscriptions; rebuild prepared-next
- Mid-promote crash: reconvene from discover; prevent mixed epochs/books

---

## 13. Facts, metrics, and reporting

| Family | Examples |
|--------|----------|
| Quality | freshness, basis, PTB axes, clock uncertainty, EWMA warm status |
| Latency | feed receive delays, evaluate lag (feeds Scope A timing) |
| Decision | action, reason_code, edges, valuations (counterfactual) |
| Rollover | active/prepared ids, promote OK, retire OK |
| Guard | `oms_dispatch=false` invariant fact/metric |

Operator report: per-window table of market identity, Up/Down map, PTB vs display
agreement, epoch violations=0, readiness summary, decision counts by reason.

---

## 14. Configuration ownership and units

| Key | Owner | Units |
|-----|-------|-------|
| `btc_window` / `which` | runtime | current\|next |
| `continuous` | runtime | bool |
| `acceptance.windows` | runtime/n4 | count |
| `prep_lead_s` | runtime | s (OPEN until measured) |
| `ewma.warm_policy` | runtime | restore\|backfill\|wait |
| `duration_s` | runtime (one-run cap) | s |
| `z_gap.*` thresholds | strategy config | existing units |
| OBSERVE provisional PTB labels | runtime/z_gap | bool / label flags |
| Output paths | reporting | path |

---

## 15. Test strategy

| Test | Assert |
|------|--------|
| Fake real-input timeline | Decisions match sealed path; zero OMS calls |
| Prepared-next promote | No evaluate on prepared; no mixed market_id after promote |
| EWMA preserved across promote | σ continuity (unless warm-up restart path) |
| Restart dedup | Same epoch not double-facted |
| Architecture | Host has no Z-Gap formulas; no LiveOMS |
| Regression | Fixture F3/F4/F5 still pass |

Net-read smoke: manual operator checklist (§16), not CI-mandatory if secrets/net flaky.

---

## 16. Deterministic acceptance criteria

For the configured consecutive-window sample (default proposal: **5**):

1. **Correct market** each window (slug/Gamma identity matches intended BTC 5m Up/Down).
2. **PTB/display agreement** within N3 tolerance (or explicit documented mismatch rate = 0 for acceptance windows); provisional labelled when used.
3. **Valid token mapping** (`"Up"`→`UP`, `"Down"`→`DOWN` by label; rejects invalid maps).
4. **No mixed epochs** (zero sealed-snapshot mismatches; prepared-next never evaluated).
5. **Acceptable data freshness** (no silent decide-on-stale; stale → gated reasons only).
6. **Successful prepared-next rollover** (discover before boundary → atomic promote → retire prior).
7. **EWMA continuity** across windows (or documented warm-up after restart).
8. **No unintended order path** (static + runtime guard: OMS submit count = 0).
9. Reconstructible decision snapshots for spot-checked evaluations.
10. Pytest green.

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
| Provisional K in OBSERVE | Allowed with provisional + counterfactual labels |
| Continuous default | Off; operator enables |
| EWMA warm-up | **OPEN** — restore / backfill / wait (freeze before impl) |
| Prep lead time | **OPEN** — measure; discover before boundary |
| Deployment location | Prefer low-latency stable clock host (decision #12) |

---

## 20. Expected commit boundary

```text
N4 commit theme:
  "Wire real-input Z-Gap OBSERVE with window rollover"

Include: runtime orchestration, config, tests, N4 acceptance docs
Exclude: ShadowOMS real-input productization (N5), LiveOMS, orders
```
