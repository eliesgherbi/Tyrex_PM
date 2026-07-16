# 02 — Architecture and ownership

**Phase:** R4 (observe + intents + risk + dry plans)  
**Engine:** Minimal Tyrex in-process dispatcher (no NautilusTrader)  
**R3 checkpoint:** `8b8f34f8a275d0986fa1988e6f617094d5fc6cf9`

## Flow

```text
Adapters → stores → DecisionSnapshot → momentum/signal
  → ObserveDecision (R3, unchanged math)
  → EnterIntent (transition policy)
  → RiskEngine (fail-closed policies + dedup)
  → ExecutionPlanner (dry plan only)
  → Facts JSONL
```

No OMS submit. `LIVE_TINY` denied until R5 portfolio/OMS exist.

## Packages

| Package | Role |
|---------|------|
| `core` | Ids, events, modes, `EnterIntent` |
| `engine` | Dispatcher |
| `adapters` / `market_data` / `indicators` / `signals` | R3 (unchanged math) |
| `strategies` | Protocol + `ReferenceMomentumStrategy` transitions |
| `risk` | Policies, engine, dedup registry |
| `planning` | Dry `ExecutionPlan` |
| `runtime` | Single observe host |
| `reporting` | JSONL facts |

## State ownership

| State | Owner |
|-------|-------|
| Books / reference | MarketStateStore / ReferenceDataStore |
| Momentum buffer | ShortHorizonMomentum |
| Last signal direction / epoch | Strategy (in-memory; persist R5) |
| Intent semantic keys | `IntentDedupRegistry` (risk) |
| Kill switch | Host (fed into immutable RiskContext) |
| Orders / positions | **Not in R4** |

## Modes

| Mode | Intent | Risk | Plan | OMS |
|------|--------|------|------|-----|
| OBSERVE | Hypothetical | Dry | Optional dry | No |
| SHADOW | Yes | Yes | Yes (dry) | No — R5 |
| LIVE_TINY | Denied | Fail-closed | No | No |

Mode does not alter indicators or signal mathematics.
