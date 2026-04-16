# Target architecture

## Layers

```
                    ┌─────────────────────────────────────────┐
                    │  Strategies, Risk, Reporting hooks     │
                    └─────────────────┬───────────────────────┘
                                      │ inject / compose only
                    ┌─────────────────▼───────────────────────┐
                    │  state_readers.py (boundary)            │
                    │  if venue_state_reads_enabled:          │
                    │      Tier A ← VenueState                │
                    │  else: Tier A ← Cache / Portfolio       │
                    │  (migration only; flag removed Step 5)  │
                    └─────────┬───────────────────┬───────────┘
                              │                   │
              Tier A ─────────▼─────────    ──────▼────── Tier B
                    ┌─────────────────┐    ┌──────────────────┐
                    │  VenueState     │    │  Cache /         │
                    │  (read-only,    │    │  Portfolio       │
                    │   HTTP-backed)  │    │  (session truth) │
                    └────────┬────────┘    └────────┬─────────┘
                             │                      │
                    ┌────────▼────────┐    ┌─────────▼─────────┐
                    │ Data API, CLOB  │    │ ExecEngine events │
                    │ HTTP (executor) │    │ (Nautilus)        │
                    └─────────────────┘    └───────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  WalletSyncActor — instrument discovery + HTTP fetch             │
│  Feeds VenueState snapshot store (no duplicate position/order    │
│  HTTP for the same endpoints in the same tick).                  │
└──────────────────────────────────────────────────────────────────┘
```

## Where `VenueState` lives

- **Module:** `src/tyrex_pm/runtime/venue_state.py` (new).
- **Instantiation:** `guru_compose.py` (parallel to `WalletSyncActor` construction, `src/tyrex_pm/runtime/guru_compose.py` ~360–387) so `VenueState` exists and can be fed before Tier A consumers run.

## Feature flag: `venue_state_reads_enabled` (migration tool only)

**RuntimeSettings / YAML:** `venue_state_reads_enabled: bool`, default **`false`** (`loaders.py` to validate).

| Flag value | Tier A behavior in `state_readers.py` |
|------------|--------------------------------------|
| **`false`** | **Current behavior:** `NautilusExecutionStateReader.list_open_orders` → `Cache.orders_open` (lines 247–251); `NautilusAccountSnapshotProvider.snapshot` → `Portfolio.account` (308–311); `NautilusPositionStateReader.filled_exposure_usd_best_effort` → `Portfolio.net_exposure` (436); deployment filled path uses `cache.positions_open` + `portfolio.is_flat` (`deployment_budget.py` 149–188). **Production unchanged** from pre-migration semantics. |
| **`true`** | **Target behavior:** Tier A methods read **`VenueState`** (positions, resting orders, cash snapshot, size×mark deployment). |

**Important:** This flag is **not** a long-term dual-mode architecture. **Step 5** removes the flag and all cache/portfolio branches for Tier A; only the VenueState-backed path remains. Documentation and comments must **always** state that the flag is **deleted in Step 5**.

**Consumers must not** `from tyrex_pm.runtime.venue_state import VenueState` — only compose and `state_readers` construct/wire it.

## `WalletSyncActor` relationship

**Today:** `_sync_cycle` fetches positions (Data API) and orders (CLOB), resolves instruments, optionally runs `_reconciliation_pass` (`wallet_sync.py` ~305–513, ~545+).

**Target:**

- **Instrument discovery** and HTTP fetch **remain** in `WalletSyncActor`.
- After each successful fetch, the actor **pushes** normalized snapshot data into **`VenueState`** (see `03_venue_state_design.md`). **No second HTTP** for the same bulk positions/orders in the same cycle.
- **Cash / collateral** is refreshed on **`VenueState`’s CLOB poll cadence** (default **10 s**, floor **3.0 s**).

## Capital path

**Today:** `DefaultCapitalStateProvider` merges `NautilusAccountSnapshotProvider` with optional `ClobAllowanceStateProvider` (`src/tyrex_pm/runtime/capital/provider.py` lines 34–100, 126–171).

**Target:** When **`venue_state_reads_enabled`** is **true**, `NautilusAccountSnapshotProvider` (or equivalent boundary) supplies balances from **`VenueState`** CLOB collateral; when **false**, behavior unchanged (`Portfolio.account`). **Step 5:** only VenueState-backed account path for Tier A.

## Readiness (two gates)

**READY** (for trading that assumes Tier A freshness) requires **both**:

1. **`wallet_sync_first_sync_complete`** — existing `WalletSyncActor` / health contract (`wallet_sync.py` ~166–168, `NautilusLiveExecutionHealthSource`).
2. **`venue_state_cash_ready`** — at least one successful CLOB balance apply with parseable collateral (see `03_venue_state_design.md`).

When **`venue_state_reads_enabled`** is **false**, Tier A still reads cache; gates may still surface **`venue_state_cash_ready`** for observability once `VenueState` is wired (Step 3), but **product** may treat READY as today until flag flip — **clarify in ops runbook** (`06_observability.md`).

## Tier B unchanged

- `Cache.order`, `orders_open` for **strategy-scoped** or **guru** heuristics — **always** cache-backed regardless of flag.
- `ShutdownDrainCoordinator` + `list_open_orders_for_strategy` (`shutdown_drain.py` lines 18–21, 90+).
- `ExecEngine` / adapter order lifecycle.

## Nautilus framework behavior (reference)

Polymarket adapter `_update_account_state` sets `AccountBalance` with `locked=0` and `free=total` from `get_balance_allowance` response `balance` only (`nautilus_trader` `adapters/polymarket/execution.py` ~276–292). `CashAccount` for Polymarket uses `calculate_account_state=False`; `Portfolio.update_order` returns early for those accounts (`portfolio.pyx` ~501–502). Tier A does **not** rely on Nautilus to compute locked collateral for resting orders.
