# 05 — Validation strategy

**Phase:** R3 observe-only path implemented. Intents/risk/OMS begin R4+.

## Name

`ReferenceMomentumStrategy` (kind: `framework_validation`)

## Purpose

Prove framework composition. Not profitability. Not Z-Gap-in-disguise.

## R3 behavior (implemented)

1. Resolve one configured binary market (fixture JSON or Gamma slug/URL).
2. Consume YES/NO books + Binance BTCUSDT public trades.
3. Build immutable `DecisionSnapshot` with freshness assessments.
4. Indicators: short-horizon momentum \(m_t = P_t/P_{t-L}-1\), mid, spread.
5. Emit `DirectionalSignal`: `UP` / `DOWN` / `FLAT` / `UNAVAILABLE`.
6. Emit observe decision: `WOULD_ENTER_UP` / `WOULD_ENTER_DOWN` / `HOLD` / `SKIP`.
7. Append facts to JSONL (`FactEnvelope`, schema_version=1).

**No** enter/exit intents, risk gate, OMS, orders, fills, or portfolio in R3.

## Signal semantics

| Direction | Meaning |
|-----------|---------|
| UP | Momentum ≥ threshold; books + reference fresh; spreads OK |
| DOWN | Momentum ≤ −threshold; same validity |
| FLAT | Valid data; \|momentum\| < threshold |
| UNAVAILABLE | Uninitialized/stale/future/insufficient history/one-sided book/wide spread |

Strength (optional): \(\min(1, (|m|-θ)/θ)\) when directional.

## Observe-decision semantics

| Decision | From signal |
|----------|-------------|
| WOULD_ENTER_UP | UP |
| WOULD_ENTER_DOWN | DOWN |
| HOLD | FLAT |
| SKIP | UNAVAILABLE |

## Out of scope

PTB · Chainlink attestation · EWMA σ · binary fair value · `fd` fee edge · paired-leg saga · survival/thesis frameworks · intents.

## Later callbacks (R4+)

`on_start`, `on_signal`, `on_timer`, `on_execution_event`, `on_stop` — only those with consumers; intents appear in R4.
