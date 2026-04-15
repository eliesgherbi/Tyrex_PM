# Venue Sync Truth — Implementation Plan

## Problem statement

Tyrex_PM needs to be reliably aware of **all** wallet-level exposure on Polymarket — positions, resting orders, and collateral — regardless of whether the bot placed the order or a human did through the UI. Today, the Nautilus Polymarket adapter is **cache-scoped by design**: user-WS events, order/fill/position reports, and reconciliation all filter on `self._cache.instruments(venue=POLYMARKET)`. Markets not in `Cache` are structurally invisible. This is incompatible with shared-wallet operation or with manual venue-side activity while connected.

## Proposal reviewed

A three-layer solution was proposed:

1. **Layer 1 — Continuous instrument discovery.** A wallet-watcher Actor polls py-clob for wallet-scoped positions/orders, diffs against `Cache`, loads missing instruments, opens WS channels, and requests targeted reports.
2. **Layer 2 — Authoritative `WalletTruthProvider`.** A read-side abstraction that owns a local map of wallet state refreshed by the poller and by Nautilus events, consumed by `NautilusDeploymentBudget` as the authoritative view.
3. **Layer 3 — Upstream adapter patch.** Out of scope.

## Proposal validation summary

After reading every relevant source file in the Tyrex tree and the installed `nautilus_trader` 1.222.0 package, the proposal's core assumptions are **mostly correct** with the following corrections:

### Confirmed accurate

- The adapter drops WS order/trade messages when `self._cache.instrument(instrument_id) is None` (`execution.py:1282–1289`, `1420–1428`).
- `generate_position_status_reports` only reports on instruments already in cache (`execution.py:668`).
- `generate_order_status_reports` skips orders whose instrument is not in cache (`execution.py:409–415`).
- `_maintain_active_market` is an `async` private method on `PolymarketExecutionClient` (`execution.py:274`) — subscribes a condition_id to the user WS.
- The existing `GuruInstrumentDynamicController` and `CacheInstrumentActivator` (`guru_instrument_dynamic.py`) already handle resolving token_id → BinaryOption → Cache insertion, including `force_add_instrument` that bypasses the activation cap.
- `NautilusDeploymentBudget` reads exclusively from `Cache.orders_open` and `Cache.positions_open` (`deployment_budget.py:94`, `149`).
- Tyrex sets `load_state=False, save_state=False` (`guru_compose.py:293–294`).
- The existing warmup (`guru_cache_warmup.py`) is **compose-time only** — runs before `node.build()` — not continuous.

### Corrections to the proposal

1. **`_maintain_active_market` is not callable from an Actor.** The proposal says the wallet-watcher "triggers `_maintain_active_market`." This is a **private async method** on `PolymarketExecutionClient`, an internal adapter component. An Actor registered on the node **cannot** call it directly. **Correction:** After adding an instrument to `Cache`, the Actor must trigger WS subscription through a different mechanism — either by submitting a targeted `GenerateOrderStatusReport` command for that instrument (which calls `_maintain_active_market` internally, `execution.py:554`), or by scheduling a reconciliation cycle that will pick up the new instrument. See `04_lifecycle.md` for the chosen approach.

2. **Layer 2 (`WalletTruthProvider` as a parallel truth map) is overengineered and introduces dual-truth risk.** The proposal describes a `WalletTruthProvider` that "owns a local map of `{condition_id → {size, avg_cost, resting_orders, collateral_locked}}`" and is "refreshed by the Layer 1 poller and by Nautilus events when available." This is architecturally a **second OMS** regardless of whether it submits orders. It duplicates the position/order state that `Cache`/`Portfolio` already owns, creates a merge/consistency problem, and violates the explicit constraint of "no parallel OMS." **Correction:** Layer 2 is replaced with a simpler design: the wallet-watcher Actor is the **only** new component. Its job is to ensure `Cache` contains all relevant instruments. Once instruments are in `Cache`, the **existing** `NautilusDeploymentBudget`, `NautilusExecutionStateReader`, and `NautilusAccountSnapshotProvider` read correct state — because reconciliation, WS, and reports all work correctly for cached instruments. The deployment budget does not need a second source; it needs the adapter's cache-scope gap closed. See `01_architecture.md`.

3. **`use_data_api_for_positions: true` does not solve the core problem.** The proposal lists this as a supporting config change. In reality, `use_data_api` switches the adapter from per-instrument CLOB balance queries to bulk Data API `/positions` (`execution.py:670–693`). The Data API path fetches positions using `self._user_address` — but it then iterates **only** `instrument_ids` from `self._cache.instruments(venue=POLYMARKET)` to build the output (`execution.py:788–791`). **The output scope is still cache-filtered.** The improvement is marginal (more robust fetching), not structural. It should still be enabled, but it is not a fix. See `07_open_questions.md` for the deeper issue.

4. **`open_check_open_only: false` is beneficial but not required by this design.** The proposal lists it as a supporting change. With continuous instrument discovery ensuring cache coverage, the existing reconciliation machinery works. Setting `open_check_open_only: false` improves staleness recovery for edge cases where WS messages are lost, but it is an orthogonal hardening measure, not a dependency of the wallet-sync design.

## Correctness guarantee

After any venue-side action on a market the wallet has ever touched, the deployment budget reflects it within 30 seconds typically and 120 seconds under worst-case Data API propagation lag. This is eventual consistency, not real-time mirroring. See `04_lifecycle.md` "Latency bounds" for the component breakdown.

## Plan structure

| File | Contents |
|------|----------|
| `01_architecture.md` | Target architecture, components, data flow, ownership |
| `02_components.md` | New and modified components with method signatures |
| `03_config.md` | YAML surface changes |
| `04_lifecycle.md` | Startup ordering, shutdown, reconnect, reconciliation interaction |
| `05_migration.md` | Ordered migration steps |
| `06_tests.md` | Test plan |
| `07_open_questions.md` | Items requiring upstream changes or further investigation |

## Explicit non-goals

- Upstream NautilusTrader / Polymarket adapter patches (tracked in `07_open_questions.md`).
- Parallel OMS, shadow reconciliation truth, or private counters.
- Using Polymarket web UI as a code-level source of truth.
- Relying on `load_state` persistence to paper over the cache-scope gap.
- "Fallback" or "keep old path working alongside new" dual modes.
- Accounting for positions held by different wallets on the same node.
