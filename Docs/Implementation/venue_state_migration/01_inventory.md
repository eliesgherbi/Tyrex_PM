# Step 1 — Tier A / Tier B inventory

This table lists **production `src/`** occurrences of the specified APIs. Tests, spikes, and archived scripts are summarized **after** the main table.

**Legend**

- **Tier A** — Venue truth (wallet positions, venue resting orders for deployment caps, venue cash/collateral for gates). **Must** migrate to data ultimately sourced from **direct Polymarket HTTP** via `VenueState`, exposed through **`state_readers`** (not direct `VenueState` imports in consumers).
- **Tier B** — Bot-local session state (orders this node placed, strategy-scoped lists, shutdown cancel targets). **Stays** on `Cache` / `Portfolio` as today.

---

## Main table (`src/` only)

| File | Line(s) | API used | What it's reading for | Tier | Migration target |
|------|---------|----------|------------------------|------|------------------|
| `src/tyrex_pm/runtime/state_readers.py` | 247–251 | `Cache.orders_open` | `list_open_orders` — venue-wide open orders for deployment caps | **A** | Same method on `NautilusExecutionStateReader`; implementation returns snapshots from `VenueState.orders_resting()` (normalized to `OrderSnapshot`). |
| `src/tyrex_pm/runtime/state_readers.py` | 262 | `Cache.orders_open` | `list_open_orders_for_strategy` — strategy-scoped orders | **B** | No migration (shutdown drain / strategy-local only). |
| `src/tyrex_pm/runtime/state_readers.py` | 274 | `Cache.order` | Single order lookup | **B** | No migration. |
| `src/tyrex_pm/runtime/state_readers.py` | 286–290 | `Cache.orders_open` | Guru-resting count for concurrent-order cap | **B** | No migration — counts **guru-tagged** orders this node believes it placed (`is_guru_resting_order`); not a venue-wide “all rests” question. |
| `src/tyrex_pm/runtime/state_readers.py` | 308–336 | `Portfolio.account` | `AccountSnapshot` for capital / risk | **A** | `NautilusAccountSnapshotProvider.snapshot()` → balances from `VenueState` CLOB collateral (merged with existing py-clob path in `DefaultCapitalStateProvider` per `src/tyrex_pm/runtime/capital/provider.py`). |
| `src/tyrex_pm/runtime/state_readers.py` | 429–436 | `Portfolio.net_exposure` | Marked USD exposure for reporting | **A** | `NautilusPositionStateReader.filled_exposure_usd_best_effort` → `VenueState` position quantities × mark (instrument from cache lookup remains B). |
| `src/tyrex_pm/runtime/deployment_budget.py` | 94–96, 113–138 | `NautilusExecutionStateReader.list_open_orders` → `orders_open` | Pending deployment USD (all rests, per-token) | **A** | Same public methods on `NautilusDeploymentBudget`; reader implementation uses venue resting orders via `state_readers` / `VenueState`. |
| `src/tyrex_pm/runtime/deployment_budget.py` | 149–162 | `cache.positions_open`, `portfolio.is_flat` | Filled deployment — portfolio scope | **A** | `filled_polymarket_usd`: position sizes and “flat” from `VenueState.positions()` + derived deployment via `position_entry_deployment_usd`-equivalent from venue qty × avg from venue or policy (see `07_risks_and_open_questions.md`). |
| `src/tyrex_pm/runtime/deployment_budget.py` | 175–188 | `cache.positions_open`, `portfolio.is_flat` | Filled deployment — token scope | **A** | `filled_usd_for_token` — same as above. |
| `src/tyrex_pm/runtime/layer_a_context.py` | 53–56 | `portfolio.net_position` | Follower long qty for Layer A | **A** | `NautilusLayerAContext.follower_long_qty_for_outcome_token` → `VenueState` net qty for token (via `instrument_id` resolution on cache still B). |
| `src/tyrex_pm/strategy/bot_sell_validate_strategy.py` | 248 | `portfolio.net_position` | Scenario A validation SELL sizing | **A** | Inject / use reader that sources long inventory from `VenueState` through `state_readers` (strategy does not import `VenueState`). |
| `src/tyrex_pm/runtime/wallet_sync.py` | 572–576 | `cache.positions_open` | Reconciliation: cache-side position map | **Delete** (Step 5) | `_build_cache_position_map` — removed with reconciliation; not migrated to Tier A consumer. |
| `src/tyrex_pm/runtime/wallet_sync.py` | 632–649 | `cache.positions_open` | Reconciliation Race B (`ts_last`) | **Delete** | — |
| `src/tyrex_pm/runtime/wallet_sync.py` | 672–677 | `cache.orders_open`, `cache.orders_inflight` | Reconciliation Race C (in-flight SELL) | **Delete** | — |
| `src/tyrex_pm/runtime/wallet_sync.py` | 706 | `cache.positions_open` | Reconciliation action build | **Delete** | — |
| `src/tyrex_pm/runtime/wallet_sync.py` | 761 | `cache.positions_open` | `_apply_reconciliation_actions` | **Delete** | — |
| `src/tyrex_pm/runtime/wallet_sync.py` | 774 | `cache.account_for_venue` | Synthetic close precheck | **Delete** | — |
| `src/tyrex_pm/reporting/position_sample.py` | 32–34 | `NautilusPositionStateReader` → `net_exposure` | `position` fact `net_exposure_usd` | **A** (follows reader) | Same call path once reader uses `VenueState`. |

### Related reads (not in the grep list but coupled)

| File | Line(s) | API | Tier | Notes |
|------|---------|-----|------|-------|
| `src/tyrex_pm/runtime/lifecycle/shutdown_drain.py` | 90+ | `NautilusExecutionStateReader` | **B** | Cancel/drain **this strategy’s** orders; remains cache-backed. |
| `src/tyrex_pm/risk/configured.py` | 818 | `count_guru_resting_orders_open` | **B** | Guru concurrent cap; cache + heuristic. |

### Out of scope for this table

| Location | Reason |
|----------|--------|
| `tests/**/*.py` | Test doubles; updated when production APIs change. |
| `spike_path_a.py` | Development spike; not production Tier A consumer. |

---

## Migration surface summary

| Metric | Value |
|--------|-------|
| **Tier A production sites** (rows requiring migration or reader swap) | **11** distinct logical sites (state_readers 3 concerns, deployment_budget 2 methods, layer_a 1, bot_sell 1, position_sample 1; account + net_exposure + list_open_orders for deployment). |
| **Tier B unchanged** | Execution reader strategy paths, guru count, shutdown drain, `get_order`. |
| **Deleted with reconciliation (Step 5)** | **7** wallet_sync line ranges — not migrated; removed. |
| **Boundary layers** | **2** — `state_readers.py` (primary), `NautilusDeploymentBudget` (uses injected reader; stays as façade). |

**Centralization:** All Tier A migration can be concentrated at **`state_readers.py`** + **`guru_compose.py` wiring** + **single atomic consumer update** for `deployment_budget`, `layer_a_context`, `bot_sell_validate_strategy`, and reporting reader injection.

**Assessment vs expectation:** The migration surface is **not** “50+ scattered strategy sites.” It is **smaller than the feared upper bound** and **centralizable** at `state_readers` + compose. **No stop condition** from Step 1 inventory alone.

**Caveat:** `position_entry_deployment_usd` today uses Nautilus `Position` objects (`avg_px_open`, `signed_qty`) — `src/tyrex_pm/runtime/deployment_budget.py` lines 47–57. Venue rows may not expose identical fields; **cost-basis for filled deployment** may require a defined policy (see `07_risks_and_open_questions.md`).

---

## Go / no-go for Step 2

| Proceed? | Condition |
|----------|-----------|
| **Go** | Team accepts Tier A list and boundary approach; open questions on cost basis acknowledged. |
| **No-go** | If product requires **intra-bar** or **event-time** position truth Polymarket APIs cannot provide — revisit scope (not indicated by current inventory). |
