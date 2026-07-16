# Target architecture and invariants

**Status:** Active design (not yet fully implemented)  
**Engine boundary:** Unresolved — see `nautilus_decision.md`  
**Date:** 2026-07-16

This document defines rules the code must obey. Where current code violates a rule, that is tracked debt, not a license to keep the violation.

## 1. Desired end-to-end flow

```
Normalized data events
        ↓
Strategy updates model state
        ↓
Strategy evaluates entry / exit
        ↓
Strategy emits typed Intent
        ↓
Shared risk validates Intent
        ↓
Shared execution plans and submits
        ↓
Order / fill events update shared portfolio truth
        ↓
Strategy reacts via common lifecycle callbacks
        ↓
Shared recovery + facts handle operations
```

## 2. Architectural invariants

### 2.1 Strategy invariant

Z-Gap **owns:**

- Signal/model state (σ, FV, edge, thesis confirmation timers)
- Entry and exit hypotheses and parameters
- Interpretation of normalized domain data
- Creation of Enter / Adjust / Cancel / Exit / Flatten intents
- Strategy-specific evidence payloads

Z-Gap **does not own:**

- WebSocket connections or venue authentication
- Raw order submission
- Generic risk checks
- Generic order reconciliation
- Portfolio truth
- Generic persistence infrastructure
- Global clocks
- Logging transport
- Runtime scheduling mechanics (window selection helpers may be called, not reimplemented)

### 2.2 State invariant — one authority each

| State | Target authority | Derived views allowed |
|-------|------------------|------------------------|
| Instruments | Instrument/registry service (engine or domain) | Config mirrors |
| Market books | Market-state store | Executable VWAP snapshots |
| External refs | Signal/reference store | Basis views |
| PTB | PTB service (single writer API) | Read-only snapshots for strategy |
| Clock | Clock authority | Corrected timestamps on events |
| Orders | Order store / engine cache | Strategy order handles |
| Fills | Order/fill ledger | — |
| Positions | Portfolio / allocation truth | Strategy position view |
| Strategy lifecycle | Lifecycle host + strategy private blob | Facts |
| Ops gates | Operations preflight store | — |

**Forbidden:** two writers that can disagree without an explicit reconcile step (current Path A vs Path B books/ledger is a violation).

### 2.3 Intent invariant

Strategy requests economic action through typed intents. It never imports or calls concrete venue clients (`PyClobBridge`, clob HTTP, etc.).

Initial intent set (keep small):

| Intent | Meaning |
|--------|---------|
| `EnterIntent` | Open or increase per plan |
| `CancelIntent` | Cancel working entry/exit order |
| `ExitIntent` | Reduce/close under normal policy |
| `FlattenIntent` | Urgent reduce-to-flat (kill / emergency) |

Add new intent types only when execution semantics genuinely differ.

### 2.4 Execution invariant

All real (and shadow) orders use one path:

```
Intent → risk → execution plan → OMS/engine submit
  → venue ack → fills/reconcile → portfolio update
  → strategy notification → facts
```

Operator scripts and strategy-specific runtimes must not bypass this path.

### 2.5 Data invariant

Raw feed payloads are normalized before strategy logic. Strategy consumes domain objects such as:

- Polymarket executable book (YES/NO)
- Binance BTC reference state
- Chainlink / PTB state
- Clock-quality state
- Market-window state

### 2.6 Lifecycle invariant

Common host states (only operationally meaningful):

```
CREATED → INITIALIZING → OBSERVING → READY
  → ENTERING → ACTIVE → EXITING → FLAT → TERMINAL
```

| Transition | Typical trigger |
|------------|-----------------|
| CREATED → INITIALIZING | Host start, config + instruments |
| INITIALIZING → OBSERVING | Feeds up, clock synced |
| OBSERVING → READY | PTB locked (enforce) or observe-ready |
| READY → ENTERING | Intent accepted / order live |
| ENTERING → ACTIVE | Fill truth shows position |
| ACTIVE → EXITING | Exit intent accepted |
| EXITING → FLAT | Position flat |
| * → TERMINAL | Window end, hard failure, kill complete |

Recovery: on restart, host reconciles portfolio/orders first; strategy private state reloads only if consistent with portfolio truth; otherwise TERMINAL + manual intervention.

### 2.7 Observability invariant

Every important decision must be reconstructable from facts:

data snapshot · clock/freshness · PTB · model · fee/edge · gates · intent · risk · plan · acks · fills · position · exit reason · outcome

## 3. Target component ownership

Names are conceptual; reuse existing packages when responsibilities already match.

| Component | Responsibility | Provided by (tentative) | Allowed deps | Forbidden deps |
|-----------|----------------|-------------------------|--------------|----------------|
| `application` | CLI, config load, start/stop | Tyrex | all hosts | strategy math |
| `engine` | Events, intent dispatch, risk, OMS/portfolio | **NT or slim Tyrex** (PoC) | adapters via interfaces | `strategies.z_gap` |
| `adapters` | Polymarket CLOB, Binance, RTDS | Tyrex and/or NT | venue SDKs | strategy decisions |
| `market_data` | Normalized books, freshness, executable views | Tyrex (thin over engine books if NT) | engine/adapters | z_gap entry/exit |
| `domain/polymarket` | Binary instruments, windows, PTB, venue constraints | Tyrex | adapters, market_data | OMS submit |
| `strategies/z_gap` | Model, entry/exit policies, lifecycle hooks | Tyrex | domain contracts, engine interfaces | concrete venue clients |
| `operations` | Scheduling, preflight, kill switches, modes | Tyrex | engine, domain | entry math duplication |
| `reporting` | Facts, metrics, summaries | Tyrex | engine events | venue clients |
| `research` | Record normalize, calibration offline | Tyrex | reporting outputs | live OMS |

### Dependency direction

```
application
    ↓
strategy + operations
    ↓
domain contracts
    ↓
engine interfaces
    ↓
adapters
```

- Generic engine modules must not import Z-Gap.
- Adapters must not decide entry/exit.
- Operator scripts must not contain trading decisions (only mode/flags and host invocation).

## 4. Strategy contract (conceptual)

Exact API depends on engine decision; semantic contract is fixed:

```python
class Strategy(Protocol):
    def on_start(self, context: StrategyContext) -> None: ...
    def on_market_data(self, event: MarketDataEvent) -> Sequence[Intent]: ...
    def on_timer(self, event: TimerEvent) -> Sequence[Intent]: ...
    def on_order_event(self, event: OrderEvent) -> Sequence[Intent]: ...
    def on_fill(self, event: FillEvent) -> Sequence[Intent]: ...
    def on_position(self, event: PositionEvent) -> Sequence[Intent]: ...
    def on_stop(self, reason: StopReason) -> None: ...
```

Host always does:

```python
for intent in strategy_callbacks(...):
    intent_dispatcher.process(intent)  # risk → plan → OMS → facts
```

Z-Gap-specific behavior stays in Z-Gap policies/state, not in the generic Protocol.

## 5. One control path for Z-Gap (target)

Until PoC decides the engine, the **operational** target is:

1. **One** operator entry: extend `tyrex-pm` (or a thin script that only calls the host) — not two bootstraps.
2. **One** scheduler for window selection.
3. **One** feed bootstrap that always starts: CLOB books + Binance + RTDS + PTB + clock.
4. **One** observe/enforce loop host.
5. Mode (`observe_only` / `shadow` / `live`) changes OMS and gates, not model code.

Path A and Path B must converge before Phase 3 tiny-live on the new architecture.

## 6. Exit policy interface (Z-Gap)

Single interface for Z-Gap (do not carry protection + survival frameworks):

```python
class ExitPolicy(Protocol):
    def evaluate(self, ctx: ExitContext) -> ExitDecision | None: ...
```

Precedence (documented, tested): kill_switch → thesis_stop → time_flatten → (future) market_close.

## 7. Mapping note

Current valuable implementations to **reuse**, not rewrite:

- `quant/*` mathematics
- `strategies/z_gap/entry_eval.py`, `exit_eval.py`, `ptb_policy.py`, `lifecycle.py`
- `ingestion/price_to_beat_tracker.py`, `runtime/time_authority.py`
- `risk/engine.py`, `execution/oms.py`, `pipeline.process_intent_work_unit` (if Tyrex spine retained)
- Facts emitters and schema types already used by Z-Gap

Current structures to **collapse**:

- Dual Path A / Path B bootstraps
- Strategy decisions living in many `runtime/z_gap_*.py` files (move behind strategy/operations hosts)
- Doc claims of universal `on_signal` for Z-Gap

## 8. Engine decision gate

No Phase 2–4 architecture commits that assume NautilusTrader **or** a permanent custom engine until `nautilus_decision.md` records pass/fail for tracks A–E and an explicit choice:

- Adopt NautilusTrader as generic foundation, **or**
- Retain and simplify Tyrex spine, **or**
- Temporary bridge with removal date.

Permanent dual-engine is rejected.
