# Z-Gap functional specification

**Status:** Draft — describes **intended** target behavior; notes current deviations  
**Math source of truth:** existing `quant/*` and `strategies/z_gap/entry_eval.py` (do not “simplify” formulas)  
**Date:** 2026-07-16

## 1. Purpose

Z-Gap trades a single leg on Polymarket BTC Up/Down 5m markets when fee-aware model edge and gates allow. It can run observe-only (facts/calibration) or enforce (tiny live) under operational gates.

## 2. Inputs (normalized)

| Input | Description | Authority |
|-------|-------------|-----------|
| Market window | `market_id`, YES/NO tokens, `event_start_ts`, `event_end_ts`, condition | Instrument/metadata service |
| Executable books | YES/NO best + depth for VWAP/size | Market-state store |
| Binance BTC | Mid/last + freshness | Signal store |
| Chainlink / RTDS | Reference ticks | Signal store |
| PTB | Boundary price + status (`observed`, `locked`, late/missing) | PTB service |
| Clock | Corrected now + uncertainty | TimeAuthority |
| Fee model | Dynamic `fd` from CLOB market info | Fee resolver (`quant/fees.py`) |
| Config | σ, bands, sizing, exit, live_validation | Strategy config |

Strategy must not parse raw WS frames.

## 3. Model state (strategy-owned)

- EWMA volatility estimator and readiness (`sigma_warm`)
- Latest fair value, z-score, edge, fee φ
- Thesis-stop confirmation timers
- Private lifecycle fields consistent with portfolio truth
- Last entry/exit client order ids (handles, not venue clients)

## 4. Decisions

### 4.1 Entry evaluation

Implemented by `evaluate_z_gap_entry` (preserve behavior):

- Require fresh feeds, valid PTB (enforce stricter), model sanity, σ ready
- Compute FV and fee-aware edge
- Apply z-band, tau-band, and other configured gates
- Output: would_enter / skip reasons / evidence

**Invariant:** observe and enforce compute the **same** model decision; mode only affects whether an intent is dispatched.

### 4.2 Entry planning (enforce)

`build_z_gap_entry_plan` + `validate_z_gap_pre_submit`:

- Fixed USD sizing / model-capped quantity
- Limit policy and FAK/IOC constraints
- Pre-submit quality gates

Emits `EnterIntent` (or equivalent) — never calls venue directly.

### 4.3 Exit evaluation (enforce)

Single policy chain (`evaluate_exit_triggers`):

1. Kill switch  
2. Thesis stop  
3. Time flatten (`flatten_before_event_end_s`)

Emits `ExitIntent` or `FlattenIntent`.

## 5. Intents

| Intent | When |
|--------|------|
| Enter | Entry plan accepted by strategy |
| Cancel | Working order abandoned |
| Exit | Normal policy exit |
| Flatten | Kill / emergency / hard fail |

Host: `intent_dispatcher.process` → risk → plan → OMS.

## 6. Gates

### Observe

- Clock sync attempted; warn/fail per config
- Late window start may skip (Path A)
- No OMS

### Enforce / live-tiny (operations)

Fail-closed preflight artifacts and flags (fee spike, binance connectivity, PTB attestation/commissioning, clock sanity, operator approval, config hash, etc.) as implemented in `validate_z_gap_live_config` / orchestrator readiness.

**Target:** same gate set regardless of entry script.

## 7. Lifecycle

See `architecture.md` §2.6. Map current `ZGapLifecycleState` phases onto the common host states without changing fill accounting semantics.

## 8. Facts (minimum)

Must remain able to emit (names may stabilize, semantics preserved):

- feed health, basis, PTB observed/locked
- model snapshot, fee resolved, edge evaluated
- entry eval / skip / plan / submit / fill / outcome
- exit trigger / submit / fill
- reconciliation, terminal summary
- calibration sample (observe)

## 9. Modes

| Mode | Model | Intent dispatch | OMS |
|------|-------|-----------------|-----|
| observe_only | on | no | none |
| shadow | on | yes | ShadowOMS / sim |
| live (tiny) | on | yes | Live OMS / engine |

Code today: `observe_only` | `enforce` only; shadow is via scenario/harness. Target adds explicit shadow mode without a third decision engine.

## 10. Known deviations from target (current code)

1. Two bootstraps (Path A / Path B) with unequal book/ledger/user-stream wiring.  
2. `ZGapStrategy` is not the generic event contract.  
3. Much orchestration lives under `runtime/z_gap_*.py`.  
4. Docs mention `shadow_evaluate`; code does not.

These are migration defects, not accepted long-term behavior.
