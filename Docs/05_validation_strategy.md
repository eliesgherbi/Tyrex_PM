# 05 — Validation strategy

**Phase:** Specified in R1; implemented observe in R3, shadow in R4–R5, tiny-live in R7.

## Name

`ReferenceMomentumStrategy` (kind: `framework_validation`)

## Purpose

Prove framework composition. Not profitability. Not Z-Gap-in-disguise.

## Behavior

1. Select one BTC Up/Down 5m Polymarket window.
2. Consume YES/NO books + Binance BTC reference.
3. Indicators: short-horizon momentum, mid, spread, freshness.
4. Emit `DirectionalSignal` when feeds fresh, spread within limit, momentum above threshold.
5. Emit `EnterIntent` for the corresponding token (tiny notional).
6. Shared risk enforces mode, allowlist, freshness, size, price/spread bounds, kill switch, duplicate guard.
7. Shared planner + OMS (shadow/live) execute.
8. Exit on signal reverse/flat, max hold time, max loss, flatten-before-window-end, or kill → `FlattenIntent` / `ExitIntent`.

## Out of scope for this strategy

PTB · Chainlink attestation · EWMA σ · binary fair value · `fd` fee edge · paired-leg saga · survival/thesis frameworks.

## Minimal strategy callbacks (target)

`on_start`, `on_signal`, `on_timer`, `on_execution_event`, `on_stop` — only those with consumers.
