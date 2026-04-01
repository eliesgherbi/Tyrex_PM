Proposed specification to achieve your objective.

Here is the specification I would recommend for V1.

**Title**

# Polymarket Trading Platform v1.1 — Modular NautilusTrader Base with Copy Strategy Reference Implementation

## 1. Objective

Build a stable, modular NautilusTrader-based trading platform that rewrites the current functional copy-trading tutorial into a clean architecture. The first implemented strategy is a simple copy strategy. The primary success criterion is architectural quality, runtime stability, and backtest/live compatibility, not strategy sophistication.

## 2. Non-goals for V1

- not building the best guru selection model
- not building advanced portfolio optimization
- not building full dashboard infrastructure
- not building AI strategies yet
- not perfecting execution economics beyond a simple robust baseline

## 3. Architectural principles

- backtest/live parity
- separation of concerns
- venue-specific logic isolated from business logic
- fail-closed risk controls
- strategy-agnostic platform core
- minimal but extensible interfaces

## 4. Top-level modules

### 4.1 Platform Core

- domain models
- config
- shared utilities
- persistence contracts
- telemetry hooks

### 4.2 Data Module

- live market data via Nautilus Polymarket adapter
- guru activity feed via Data API polling
- instrument metadata
- market universe filters
- historical data loaders

**Deliverables:**

- GuruMonitorActor
- MarketDataService
- InstrumentService
- HistoricalGuruLoader
- HistoricalMarketLoader

### 4.3 Signal Module

- entry/exit hypothesis generation
- abstract interfaces for different signal types

**Deliverables:**

- EntrySignalPolicy
- ExitSignalPolicy
- GuruFollowEntryPolicy
- GuruMirrorExitPolicy

### 4.4 Risk Module

- pre-trade validation
- budget sizing
- exposure limits
- reserve balance guard
- emergency stop

**Deliverables:**

- RiskPolicy
- SizingPolicy
- PortfolioGuard
- CopyRiskPolicy

### 4.5 Execution Module

- order intent translation
- market vs limit workflow
- partial fill handling
- reconciliation
- safe startup/shutdown

**Deliverables:**

- ExecutionPolicy
- PolymarketExecutionPolicy
- OrderReconciliationService

### 4.6 Indicator Module

- reusable feature/indicator computation framework
- optional in V1 implementation, but interface must exist

### 4.7 Strategy Module

- strategy composition root
- strategy base contract
- first reference strategy implementation

**Deliverables:**

- BaseComposableStrategy
- CopyStrategy

### 4.8 Runtime Module

- BacktestRuntime
- LiveRuntime

### 4.9 Reporting Module

- runtime logs
- backtest reports
- trade/fill analytics
- skipped-signal diagnostics

## 5. V1 reference strategy behavior

The reference CopyStrategy shall:

- subscribe to GuruTradeSignal
- **on guru buy:**
  - evaluate entry policy
  - compute naive proportional size
  - pass through risk validation
  - execute via market-if-within-band, else limit fallback
- **on guru sell:**
  - evaluate mirrored exit policy
  - compute proportional exit
  - pass through risk/execution
- emit telemetry for every decision and skip reason

This keeps the strategy simple but complete.

## 6. V1 runtime scope

### Backtest

- replay historical guru signals
- replay historical order book/trade data
- configurable simulated latency
- output P&L, fill stats, slippage, skip reasons

### Live

- one TradingNode
- one guru monitor actor
- one copy strategy instance
- one notifier actor
- structured logs
- safe restart with persisted strategy state

## 7. Explicit separation rules

- **Data module** may fetch and publish data, but never place orders.
- **Signal module** may generate hypotheses, but never enforce portfolio safety.
- **Risk module** may approve/deny/resize intent, but never speak venue protocol.
- **Execution module** may submit/cancel/modify orders, but never decide strategy thesis.
- **Strategy module** orchestrates these modules, but should not reimplement their internals.

That one page alone will prevent a lot of future architectural drift.

## 8. V1 acceptance criteria

The architecture is successful when:

- the current tutorial copy logic can be reimplemented without a monolithic loop
- the same strategy class runs in backtest and live
- guru monitoring, signal generation, risk checks, and execution are independently testable
- execution logic is isolated from copy-strategy logic
- the platform can later host a second non-copy strategy without major refactor
