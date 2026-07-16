# Tyrex_PM objective

**Status:** Active (Phase 0)  
**Validation target:** Z-Gap only  
**Last updated:** 2026-07-16

## 1. Problem statement

Tyrex_PM exists to trade Polymarket binary markets with small capital under fail-closed safety controls, while allowing multiple strategy hypotheses to share one reusable stack for data, risk, execution, lifecycle, persistence, and reporting.

Z-Gap is the first strategy used to prove that stack is clean enough to extend.

## 2. Precise definition

Tyrex_PM is a **Polymarket trading framework** with:

- a **generic engine path** for intents, risk, execution, portfolio truth, recovery, and observability;
- **Polymarket-specific adapters and domain services** for CLOB books/orders, binary instruments, market windows, fees/tick constraints, RTDS/Chainlink, and price-to-beat;
- **strategy packages** that express hypotheses and emit intents;
- **operations** for scheduling, preflight, kill switches, and live-tiny gates;
- **research** for recording, calibration, and offline analysis.

It is **not** a single-strategy bot permanently. It is also **not** an enterprise multi-venue platform. Scope is Polymarket-first, with external reference feeds only as strategy inputs.

## 3. What belongs where

### Framework (reusable)

- Feed connection and normalization contracts
- Market-state construction, freshness, quality, executable views
- Clock synchronization and uncertainty propagation
- Typed intents and the Intent → Risk → Plan → OMS path
- Order/fill/position reconciliation and restart recovery
- Portfolio / allocation truth (authoritative shared state)
- Lifecycle host (state machine mechanics, not strategy policy)
- Persistence infrastructure
- Facts, metrics, run manifests
- Kill switches and live-mode enforcement hooks
- Window scheduling mechanics (generic over market families)

### Strategy (Z-Gap and future)

- Signal/model state (e.g. σ, fair value, edge)
- Entry and exit hypotheses and parameters
- Interpretation of normalized domain data
- Creation of Enter / Adjust / Cancel / Exit / Flatten intents
- Strategy-specific exit policy selection and precedence
- Strategy-specific evidence fields attached to intents and facts

### Polymarket-specific

- CLOB REST/WS adapters, auth, tick size, min size
- Gamma discovery / instrument resolution for binary YES/NO
- User-stream order/trade events
- RTDS topics used as reference prices
- Price-to-beat capture semantics for Up/Down windows
- Venue fee descriptors required for edge (`fd` curve for Z-Gap)
- Resolution observation (accounting); on-chain redeem is later/out of scope

### Research-only

- Recording lake normalization and notebooks
- Offline calibration reviews and batch analytics
- Replay/backtest engines (not required for Phase 1–3 success)
- Historical parameter studies

## 4. Operational guarantees required for live trading

1. **Fail-closed:** missing or stale evidence denies entry and can force flatten.
2. **Single execution path:** no strategy or operator script submits venue orders outside OMS/engine.
3. **One authority per state object:** instruments, books, orders, fills, positions, PTB, clock, strategy lifecycle.
4. **Reconstructability:** every live decision is recoverable from facts (data → model → gate → intent → risk → plan → ack → fill → position → exit).
5. **Bounded exposure:** live-tiny caps, kill switches, and emergency flatten are enforceable.
6. **Restart honesty:** after process restart, order/position truth converges with the venue or trading stops.
7. **Mode isolation:** observe / shadow / live change dispatch and OMS behavior, not model math.

## 5. Explicitly outside current scope

- Migrating Paired Binary, Guru Follow, or harness strategies into the new contracts
- Full historical backtesting platform
- On-chain redemption automation
- New predictive models or parameter optimization
- Cosmetic repository-wide renaming
- Deleting historical functionality before parity
- Permanent dual-engine (Tyrex + NautilusTrader) architecture

Paired Binary may be inspected only to ensure the engine can later support coordinated multi-leg sagas.

## 6. Capability classification (Z-Gap end-to-end)

Legend: **G** generic engine · **P** Polymarket-specific · **Z** Z-Gap-specific · **O** operational · **R** research

| Capability | Class | Notes |
|------------|-------|--------|
| Market discovery / instrument resolution | P | BTC 5m Up/Down via Gamma |
| CLOB book ingestion | P/G | Adapter P; store/quality G |
| Binance reference ingestion | G/P | External adapter; used by Z |
| Chainlink / RTDS ingestion | P | `crypto_prices_chainlink` |
| PTB capture and attestation | P/Z | Venue window semantics + Z gates |
| Time synchronization | G | Shared clock authority |
| Freshness / data-quality validation | G | Shared gates |
| Market-state construction | G | Books + derived executable views |
| Volatility estimation (σ) | Z | `quant/volatility` |
| Fair-value calculation | Z | `quant/binary_fair_value` |
| Fee-aware edge (`fd`) | Z/P | Z math on Polymarket `fd` |
| Entry / exit evaluation | Z | Emits intents only |
| Trading-intent creation | G contract / Z content | Typed intents |
| Risk validation | G | Fail-closed |
| Execution planning | G | Style/price/quality |
| Order submission | G | Single OMS/engine path |
| Order/fill/position reconciliation | G | Venue truth |
| Partial-fill handling | G | Engine + strategy reaction |
| Cancel / emergency flatten | G path / Z or O policy | Same execution path |
| Strategy lifecycle host | G | States shared; transitions Z-influenced |
| Market-window scheduling | O/P | Shared scheduler; Z session policy |
| State persistence | G | Min strategy blob allowed |
| Restart recovery | G/O | |
| Live-tiny gates / kill switches | O | |
| Facts / metrics / decision evidence | G/O | |
| Calibration samples | Z/R | |
| Recording / research inputs | R | |

## 7. Success definition (framework view)

A future strategy should mainly require:

1. Declaring required data.
2. Implementing signal/decision logic.
3. Emitting standard intents.
4. Defining strategy-specific exit policies.

It must not rebuild feeds, market state, risk, OMS, reconciliation, persistence, scheduling, logging, or live safety controls.
