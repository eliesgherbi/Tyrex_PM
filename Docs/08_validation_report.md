# 08 — Validation report

## Checkpoints

| Phase | Commit | Message |
|-------|--------|---------|
| R1 | `630bac2acf67961a30b4be014d1df0434af967f1` | reset project with isolated legacy tree and minimal skeleton |
| R2 | `ccccc969bb4877ae97e6e56c656b839739034425` | add deterministic event-driven core contracts |
| R3 | `8b8f34f8a275d0986fa1988e6f617094d5fc6cf9` | add read-only market data and momentum strategy slice |
| R4 | `9813001465db1fd188a4e00a3c82a24fa2cb4292` | add intent risk and dry execution planning |

`.env` SHA256 (unchanged): `27210C97AE37101DE48570130BBB517E572F3EB75160B5FC4C05CF178B91F772`  
`var/` gitignored · no `old/` imports · no NautilusTrader

---

## R4 completion (summary)

Dry intent → risk → plan. Live dry-plan: 781 signals → **1 intent / 1 plan**. See git history of this file for full R4 tables.

---

## R5 completion report

**R4 checkpoint:** `9813001465db1fd188a4e00a3c82a24fa2cb4292`  
**Pytest:** **124 passed** (no network in default suite)

### Modules created / extended

```text
src/tyrex_pm/core/{commands,execution_events,intents,ids}.py
src/tyrex_pm/execution/{protocol,order_store,fill_ledger,shadow_oms}.py
src/tyrex_pm/portfolio/portfolio.py
src/tyrex_pm/lifecycle/trade_lifecycle.py
src/tyrex_pm/persistence/snapshot.py
src/tyrex_pm/planning/exit_planner.py
src/tyrex_pm/runtime/{shadow_config,shadow_host,live_shadow}.py
config/observe_shadow_r5.json
tests/test_r5_*.py
```

### OMS protocol

`OMS.submit/cancel/stop` — `ShadowOMS` now; live adapter reserved for R6.

### Commands / events

Commands: `SubmitOrderCommand`, `CancelOrderCommand`.  
Events: `OrderSubmitted`, `OrderAccepted`, `OrderRejected`, `OrderPartiallyFilled`, `OrderFilled`, `OrderCancelPending`, `OrderCanceled`.

### Ownership

| Concern | Owner |
|---------|-------|
| Fills | `FillLedger` |
| Order state | `OrderStore` |
| Positions | `Portfolio` |
| Trade phase | `TradeLifecycle` |

### Shadow fill model

Visible-depth marketable limits; residual cancel optional; fee model `shadow_zero_fee_v1` by default; no queue/latency/impact; books not mutated.

### Intents added

`ExitIntent`, `CancelIntent`, `FlattenIntent` (plus existing `EnterIntent`).

### Lifecycle

`FLAT ↔ ENTRY_PENDING ↔ ACTIVE ↔ EXIT_PENDING`; `TERMINAL` at window end.  
Eligibility uses lifecycle/portfolio — not `last_signal_direction` alone.

### Risk extensions

Portfolio view required for shadow OMS path; pending/active blocks entry; kill switch denies entry, permits flatten; exit cannot increase exposure.

### Persistence

Schema v1 atomic JSON snapshot; rejects corrupt/mismatched market/config; restores orders/fills/portfolio/lifecycle/dedup/strategy epoch.

### Public live shadow evidence (no trading)

| Item | Value |
|------|-------|
| Market | `btc-updown-5m-1784227200` — Bitcoin Up or Down 2:40–2:45PM ET |
| Duration | ~50s |
| Signals | 2123 (UP 356, DOWN 1, FLAT 1310, UNAVAILABLE 456) |
| Intents | ENTER 89 / EXIT 488 |
| Risk | 88 approved / 489 denied (mostly `DUPLICATE_INTENT` on exits) |
| Plans | 3 PLANNED / 85 `INSUFFICIENT_DEPTH` |
| Commands | **3** |
| Lifecycle | `FLAT→ENTRY_PENDING→ACTIVE` (×2), `ACTIVE→EXIT_PENDING→FLAT` (×1) |
| Final | Residual shadow exposure possible if exit unplannable; not venue inventory |
| Fee model | `shadow_zero_fee_v1` |
| Artifacts | `var/reporting/r5/live_shadow_facts.jsonl`, `var/state/r5_shadow_snapshot.json` (gitignored) |
| Private/trading calls | **None** |

Shadow fills/commands are **not** profitability evidence.

### Confirmations

No private endpoints · no real order signing · no wallet credential reads · no `old/` · no NautilusTrader · R4 dry path still works without ShadowOMS · distribution excludes `old/` · `.env` unchanged.

### Known limitations

- No queue-position / latency / impact model
- Visible fills against visible depth only
- Max-loss exit deferred
- Shadow performance is not profitability evidence
- Live venue reconciliation belongs to R6
- Repeated DOWN/UP while FLAT retries entry each tick after planning failure (dedup forgotten) — expected retry policy; may be rate-limited later

### Proposed R6 scope

1. Live Polymarket OMS adapter implementing the same `OMS` protocol.  
2. Authenticated submit/cancel (credentials from env; never logged).  
3. Venue order/fill reconciliation into `OrderStore` / `FillLedger`.  
4. Keep `ShadowOMS` for fixture/offline validation.  
5. Still no Z-Gap strategy logic.

**Stop before R6.**
