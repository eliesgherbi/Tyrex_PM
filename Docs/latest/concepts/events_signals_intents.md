# Events, signals, and intents

**Purpose:** keep fact, interpretation, request, authorization, and action distinct.

## Chain (never collapse these)

```text
Event ≠ Indicator ≠ Signal ≠ Decision ≠ Intent
≠ RiskDecision ≠ ExecutionPlan ≠ Command ≠ ExecutionEvent
```

| Concept | Meaning | Producer | Consumer | Kind | Representative types |
|---------|---------|----------|----------|------|----------------------|
| **Event** | Normalized observation from a venue/feed | Adapters | Stores / dispatcher | Fact | Book update, reference trade, user-stream messages |
| **Indicator** | Reusable numeric transform over state | Indicator modules | Signals / strategy | Interpretation | Momentum lookback features |
| **Signal** | Packaged market interpretation | Signal builders | Strategy | Interpretation | Directional momentum signal |
| **Decision** | Strategy’s observe outcome (enter/hold/exit need) | Strategy | Host / intent emission | Interpretation | `ObserveDecision` |
| **Intent** | Economic request (“I want exposure”) | Strategy | Risk | Request | `EnterIntent`, `ExitIntent`, `FlattenIntent` |
| **RiskDecision** | Authorization or denial with reasons | `RiskEngine` | Planner / host | Authorization | approve / deny + `RiskReason` |
| **ExecutionPlan** | Venue-side price, qty, order type, deadlines | Planners | OMS commands | Action design | Sized BUY plan; `LifecycleExitPlan` |
| **Command** | OMS instruction | Planner / runtime | OMS | Action request | `SubmitOrderCommand`, `CancelOrderCommand` |
| **ExecutionEvent** | What the OMS/venue path observed | OMS / transport normalize | OrderStore / FillLedger | Fact | submitted / accepted / rejected / fill |

## Concrete examples

**Market-data update**

1. Polymarket WS book message → adapter normalize → **Event**
2. `BookStore` applies levels → authoritative book state
3. Indicator reads book + reference → **Indicator** values
4. Signal package → **Signal**
5. Strategy may emit no intent (hold)

**Entry request**

1. Strategy emits `EnterIntent` (notional / side need) — **Intent**
2. `RiskEngine` returns deny on `LIVE_TINY` outside the guarded one-shot path, or approve in SHADOW — **RiskDecision**
3. Entry planner sizes fee-inclusive BUY ≤ cap — **ExecutionPlan**
4. Host builds `SubmitOrderCommand` — **Command**
5. OMS returns local `OrderId` only (not venue fill) — then **ExecutionEvents**

**Exit request**

1. Strategy or lifecycle asks to flatten — **Intent** / lifecycle note
2. Exit planner reads **fresh bids**, never reuses BUY limit as SELL price — **ExecutionPlan**
3. FAK SELL command → OMS → settlement facts (`MATCHED` ≠ `CONFIRMED`)

## Rules of thumb

- Strategies must not construct venue wire prices as truth.
- Risk authorizes; it does not own books or OMS.
- Planner owns executable price/qty/order-type.
- Portfolio/fills own economic truth after execution events.
