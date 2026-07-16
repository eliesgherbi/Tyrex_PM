# 00 — Project objective

**Phase:** R1 (active)  
**Engine:** Minimal Tyrex_PM event-driven framework (NautilusTrader is not a dependency)

## Problem

Trade Polymarket binary markets with small capital under fail-closed safety, while sharing one reusable stack across strategies.

## Definition

Tyrex_PM is a **Polymarket-first trading framework**:

- Strategies define data needs, signals, entry/exit decisions, and evidence.
- Shared modules own feeds (normalized), market state, risk, execution, portfolio, lifecycle, persistence, scheduling, and reporting.
- External venues such as Binance supply **reference data only**, not order execution.

## Framework vs strategy

| Framework owns | Strategy owns |
|----------------|---------------|
| Adapters, normalization, event dispatch | Hypothesis and parameters |
| Market / reference state | Interpreting signals |
| Indicators (reusable) and signal types | When to enter / exit |
| Risk, planner, OMS boundary | Typed intents + evidence |
| Portfolio, persistence, recovery | Strategy-private state consistent with portfolio |
| Facts, modes, kill switch | — |

## Explicitly out of immediate scope

- Z-Gap reconstruction (Z1–Z4 after R8)
- Paired-leg / guru / survival migration
- Full backtest platform
- On-chain redemption
- NautilusTrader integration of any kind

## Success

A new strategy should mainly implement signal/decision logic and emit standard intents—not rebuild feeds, OMS, risk, or logging.
