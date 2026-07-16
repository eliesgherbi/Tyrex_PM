# 02 — Architecture and ownership

**Phase:** R6A (LiveOMS behind OMS protocol; mutations disabled)  
**Engine:** Minimal Tyrex in-process dispatcher (no NautilusTrader)  
**R5:** `5cc1a30` · **R5.1:** `6b03cc3`

## Flow

```text
Adapters → stores → DecisionSnapshot → momentum/signal
  → ObserveDecision
  → Intent (retry-gated)
  → RiskEngine
  → Planner
  → Command
  → OMS (ShadowOMS | LiveOMS)
  → execution events → OrderStore / FillLedger → Portfolio → Lifecycle
  → Reconciliation (live) → Readiness
  → Facts + persistence
```

## One host

```text
TradingHost = ObserveHost
  ├── OBSERVE: no OMS dispatch
  ├── SHADOW: ShadowOMS (ShadowHost hooks)
  └── LIVE_TINY: LiveOMS injected later; risk dispatch denied until R7
```

Mode changes OMS dispatch only. Signal/strategy orchestration is one path.

## Execution packages

| Package | Role |
|---------|------|
| `execution/protocol.py` | Shared `OMS` |
| `execution/shadow_oms.py` | Deterministic shadow fills |
| `execution/polymarket/` | Live adapter: transport, auth, normalize, reconcile, readiness, LiveOMS |
| `portfolio` / `lifecycle` | Application owners (not adapter-owned) |

## State ownership

| Question | Owner |
|----------|-------|
| Fills | `FillLedger` |
| Orders | `OrderStore` |
| Positions | `Portfolio` |
| Trade phase | `TradeLifecycle` |
| Venue I/O | `PolymarketTransport` |
| Venue↔local compare | `ReconciliationService` |
| Live entry gate | `ExecutionReadiness` |

## Modes

| Mode | OMS | Mutations |
|------|-----|-----------|
| OBSERVE | None | n/a |
| SHADOW | ShadowOMS | Shadow only |
| LIVE_TINY | LiveOMS (R6 wired, R7 enabled) | **Disabled in R6** |
