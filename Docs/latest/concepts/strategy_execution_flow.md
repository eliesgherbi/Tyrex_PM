# Strategy and execution flow

**Purpose:** end-to-end life of data, entries, and exits in the accepted framework.

## Pipeline

```text
Adapters
→ Events
→ State
→ Indicators
→ Signals
→ Strategy
→ Intents
→ Risk
→ Execution plan
→ OMS
→ Execution events
→ Orders / Fills / Portfolio
→ Facts
```

## What strategies may and may not own

| May own | Must not own |
|---------|----------------|
| Hypothesis, parameters, private flags | Venue API clients |
| Interpreting signals into intents | Book reconstruction |
| Evidence attached to intents | Risk authorization |
| Strategy-private state consistent with portfolio | Execution pricing / OMS state |
| | Portfolio truth / persistence |

`ReferenceMomentumStrategy` is a **framework-validation** strategy. It proves integration. It does **not** prove profitability or Z-Gap readiness.

## Life of a market-data update

1. Adapter receives WS/REST payload.
2. Normalize → dispatch event.
3. Book/reference stores update; freshness assessed.
4. Host builds `DecisionSnapshot`.
5. Indicators + signals update.
6. Strategy `on_signal` → optional intents.
7. Facts may record observe decisions even when no order is sent (OBSERVE).

## Life of an entry (SHADOW or LIVE_TINY)

1. Strategy emits `EnterIntent`.
2. Dedup / retry controller may gate repeats.
3. Risk approves or denies.
4. Entry planner sizes quantity and BUY limit; fee-inclusive collateral ≤ configured max (R7 envelope: $5).
5. OMS `submit` → local `OrderId`.
6. Insert ack / match status is **not** inventory.
7. Settlement wait (live): trade status ladder before treating size as acquired.

## Life of an exit (live one-shot)

1. Only after sellable inventory: `min(confirmed_acquired, conditional_balance)`.
2. Fresh bid-side book + fingerprint.
3. `plan_lifecycle_fak_sell` → marketable FAK SELL limit (never entry BUY limit).
4. Bounded no-match retries with new fingerprints.
5. Residual dust recorded; cleanup policy `NONE`.

## Settlement progression

```text
Order insert status  ≠  trade settlement status

MATCHED  →  MINED  →  CONFIRMED
```

| Status | Inventory? | May SELL? |
|--------|------------|-----------|
| MATCHED | No | No |
| MINED | No | No |
| CONFIRMED (+ sellable balance) | Yes | Yes (qty owned) |

Planned / max-estimated shares are never inventory.

## Formal detail

- Spec flow: [`../../specifications/04_event_and_runtime_flow.md`](../../specifications/04_event_and_runtime_flow.md)
- Exit floors: [`../../implementation/r7_exit_floor_policy.md`](../../implementation/r7_exit_floor_policy.md)
- Live success evidence: [`../../implementation/r7_successful_live_acceptance.md`](../../implementation/r7_successful_live_acceptance.md)
