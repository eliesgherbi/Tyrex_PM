# 02 — Architecture and ownership

**Phase:** R5.1 (unified TradingHost + retry)  
**Engine:** Minimal Tyrex in-process dispatcher (no NautilusTrader)  
**R5 checkpoint:** `5cc1a306ee168f18df40e2107e78785d5a097364`

## Flow

```text
Adapters → stores → DecisionSnapshot → momentum/signal
  → ObserveDecision
  → Enter/Exit/Flatten intent (lifecycle-aware)
  → RiskEngine (entry vs exit asymmetry + portfolio view)
  → ExecutionPlanner / ExitPlanner
  → SubmitOrderCommand / CancelOrderCommand
  → ShadowOMS → execution events
  → FillLedger + OrderStore → Portfolio → TradeLifecycle
  → Facts + atomic snapshot persistence
```

R4 dry path remains when `shadow.enable_oms` is false/absent (`ObserveHost` only).

## Packages

| Package | Role |
|---------|------|
| `core` | Ids, events, modes, intents, commands, execution events |
| `engine` | Dispatcher |
| `adapters` / `market_data` / `indicators` / `signals` | R3 (unchanged math) |
| `strategies` | Lifecycle-aware `ReferenceMomentumStrategy` |
| `risk` | Policies including portfolio exposure |
| `planning` | Entry + exit dry planners |
| `execution` | `OMS` protocol, `OrderStore`, `FillLedger`, `ShadowOMS` |
| `portfolio` | Long-only positions from fills |
| `lifecycle` | Host-owned trade lifecycle |
| `persistence` | Atomic JSON snapshot |
| `runtime` | `TradingHost`=`ObserveHost`; `ShadowHost` OMS hooks; shared `live_runner` |
| `reporting` | JSONL facts |

## State ownership (non-competing)

| Question | Owner |
|----------|-------|
| Which fills happened? | `FillLedger` |
| What is the order’s state? | `OrderStore` |
| What do we own? | `Portfolio` |
| Trade eligibility phase? | `TradeLifecycle` (host) |
| Intent semantic keys | `IntentDedupRegistry` |
| Books / reference | Market/reference stores |

Facts observe authoritative state; they never own it.

## Modes

| Mode | Intent | Risk | Plan | OMS |
|------|--------|------|------|-----|
| OBSERVE | Hypothetical | Dry | Optional dry | No |
| SHADOW | Yes | Yes | Yes | ShadowOMS when enabled |
| LIVE_TINY | Denied until R6/R7 | Fail-closed | No | No |

## Dispatcher priorities (execution path)

1. Order store / fill ledger (~100)  
2. Portfolio (~90)  
3. Lifecycle (~80)  
4. Strategy feedback via host evaluate  
5. Reporting (facts)
