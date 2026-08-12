# 02 — Architecture and ownership

> **Historical (R8).** Current ownership and composition: [`../latest/architecture.md`](../latest/architecture.md). The ObserveHost / ShadowOMS / LIVE_TINY split below is not the supported graph.

**Phase:** R8 (framework acceptance; R7 live closed)  
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
  └── LIVE_TINY: LiveOMS + R7B one-shot; R7C requires MATCHED≠CONFIRMED settlement before exit
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
| LIVE_TINY | LiveOMS + `r7b-live-once` | Operator `--execute-live` only; R7C settlement before SELL |

### R7C / R7C.1 settlement ladder (LIVE_TINY)

```text
Order insert status != trade settlement
MATCHED != CONFIRMED
MINED != CONFIRMED
Planned quantity != acquired quantity
Confirmed fill != immediately sellable balance
Conditional balance authoritative over Data API for execution safety

ENTRY_SUBMITTING → ENTRY_MATCHED → ENTRY_SETTLING → ENTRY_CONFIRMED → ACTIVE
  → EXIT_SUBMITTING → EXIT_MATCHED → EXIT_SETTLING
  → FLAT | FLAT_WITH_DUST | FLAT_EXTERNAL_ACTION
  | MANUAL_INTERVENTION | RESIDUAL_EXPOSURE | UNKNOWN
```

- Inventory only from trade status **CONFIRMED** (not MATCHED/MINED/RETRYING).
- SELL qty = `min(venue_confirmed_acquired, funder_conditional_balance)` only.
- `signature_type=1`: signer signs; funder/proxy owns positions and conditional balances.
- Live `--execute-live` requires a **clean** worktree (no dirty override).
- Authorization: operator CLI flag only (no session nonce / verbatim ceremony).
- Durable safety state: `config/r7/acknowledgment_policy.json` (sealed four identities),
  `var/state/r7/position_acknowledgment.json`, `var/state/r7/lifecycle_residuals.json`.
- Reports under `var/reporting/**` are disposable and must not be the policy source.
- Exit planning (R7E/R7F): fresh bid-side book → marketable FAK SELL limit; never reuse BUY
  `sized.limit_price`. Modules: `execution/polymarket/lifecycle_exit_plan.py`,
  `runtime/r7_lifecycle_policy.py` (exact numeric policy). FAK fill is not guaranteed if
  the book moves after the last snapshot.
