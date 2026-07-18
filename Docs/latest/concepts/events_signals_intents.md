# Events, signals, and intents

**Purpose:** keep fact, interpretation, request, authorization, and action distinct.

## Chain (never collapse these)

```text
Event ≠ Indicator ≠ Signal ≠ Decision ≠ Intent
≠ RiskDecision ≠ ExecutionPlan ≠ Command ≠ ExecutionEvent
```

| Concept | Meaning | Producer | Consumer | Kind | Representative active types |
|---------|---------|----------|----------|------|-----------------------------|
| **Event** | Normalized observation | Adapters | Stores / dispatcher | Fact | Book/reference updates (adapter-normalized) |
| **Indicator** | Reusable transform | Indicators | Signals / strategy | Interpretation | Momentum features |
| **Signal** | Packaged interpretation | Signal builders | Strategy | Interpretation | `DirectionalSignal` |
| **Decision** | Observe outcome | Strategy | Host | Interpretation | `ObserveDecision` (defined under `framework_validation`) |
| **Intent** | Economic request | Strategy | Risk / host | Request | `EnterIntent`, `ExitIntent`, `CancelIntent`, `FlattenIntent` |
| **RiskDecision** | Authorize / deny | `RiskEngine` | Planner / host | Authorization | approve/deny + `RiskReason` |
| **ExecutionPlan** | Price/qty/order-type | Planners | Commands | Action design | Sized BUY plan; `LifecycleExitPlan` (R7 exit path) |
| **Command** | OMS instruction | Planner / runtime | OMS | Action request | `SubmitOrderCommand`, `CancelOrderCommand` |
| **ExecutionEvent** | OMS-observed fact | OMS | OrderStore / FillLedger | Fact | `OrderSubmitted`, `OrderAccepted`, `OrderRejected`, `OrderCancelPending`, `OrderCanceled`, `OrderFilled`, `OrderPartiallyFilled`, … |

## Strategy callbacks (active)

`Strategy` protocol (`strategies/protocol.py`):

| Callback | Exists? | Signature (active) |
|----------|---------|---------------------|
| `on_start` | yes | `(context: StrategyContext) -> None` |
| `on_signal` | yes | `(signal, context) -> tuple[ObserveDecision, list[EnterIntent]]` |
| `on_stop` | yes | `(reason: str) -> None` |
| `on_timer` | **no** | not implemented |
| `on_execution_event` | **no** | not implemented |

**Debt:** `ReferenceMomentumStrategy.on_signal` returns `list[IntentLike]` including `ExitIntent` / `FlattenIntent`, which is wider than the protocol’s `list[EnterIntent]`. Treat Exit/Flatten intents as implemented types used by the validation strategy / shadow host, not as protocol-guaranteed returns for every strategy.

## Concrete examples

**Market-data update**

1. Venue payload → adapter normalize → event  
2. `BookStore` / reference store update  
3. Indicator → signal  
4. Strategy may emit no intent

**Entry (shadow / observe path)**

1. `EnterIntent`  
2. `RiskEngine` decision  
3. Plan → `SubmitOrderCommand`  
4. OMS returns local `OrderId`  
5. Execution events update stores

**Exit (LIVE_TINY one-shot)**

R7 orchestration plans SELL from fresh bids after **CONFIRMED** inventory — not via a generic `on_execution_event` callback.

## Rules of thumb

- Strategies must not construct venue wire prices as truth.
- Risk authorizes; it does not own books or OMS.
- Planner owns executable price/qty/order-type.
- Portfolio/fills own **internal** derived economic state; venue evidence can disagree (see [state_lifecycle_recovery](state_lifecycle_recovery.md)).
