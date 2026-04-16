# VenueState migration — overview

## Problem statement

Tyrex runs Polymarket strategies in **shared-wallet** and **external-activity** scenarios. Nautilus’s **event-derived** `Cache` / `Portfolio` model assumes the trading node is the sole authority on positions and cash. That assumption fails when:

- External fills, manual trades, or another process moves collateral or outcome tokens.
- The Polymarket adapter refreshes account state only on specific venue paths (e.g. finalized trades), not on synthetic or engine-inferred fills.

The team invested in **position reconciliation** (synthetic closes, race defenses) to patch cache vs venue drift. That approach is **feature-flagged** (`position_reconciliation_*` in `RuntimeSettings`; see `src/tyrex_pm/config/loaders.py` lines 944–1052) but is **structurally unfit** as a long-term source of venue truth.

## Decision recap

1. **Stop** treating Nautilus `Portfolio` / `cache.positions_open` / framework account snapshots as **Tier A** (venue truth) for risk, deployment caps, and strategy sizing.
2. **Introduce** a read-only **`VenueState`** component that reflects **direct HTTP** to Polymarket Data API and CLOB (see constraints in `03_venue_state_design.md`).
3. **Route Tier A reads** through the existing boundary **`state_readers.py`** so consumers do not import `VenueState` directly (`src/tyrex_pm/runtime/state_readers.py` lines 1–23 document the seam).
4. **Keep Tier B** on `Cache` / `Portfolio`: this bot’s orders, fills, lifecycle, shutdown drain — anything that is **session-local** by definition.
5. **Disable** position reconciliation in live configs **before** building Tier A migration (Step 2); **delete** reconciliation code only after live validation (Step 5).

## Decisions made (team memo — pre-implementation)

These decisions are **integrated** across `02`–`07`; they are the authority for implementation.

| # | Decision | One-line rationale |
|---|----------|-------------------|
| 1 | **Cost basis** for filled deployment / exposure from venue positions = **venue size × mark price**; if mark is missing, use **fallback price 0.5** and emit **`venue_state_missing_mark`** | Removes dependence on Nautilus `Position.avg_px_open` (`deployment_budget.py` lines 47–57); bounded behavior when mark unavailable. |
| 2 | **CLOB cash poll** default **10 s**, **floor 3.0 s**, tunable via YAML | Reduces rate-limit pressure vs a 3 s default while keeping cash fresher than WalletSync-only. |
| 3 | **Two readiness predicates** (both required for READY): **`wallet_sync_first_sync_complete`** **and** **`venue_state_cash_ready`** | Instrument discovery and collateral snapshot are independent concerns; neither alone proves Tier A readiness. |
| 4 | **`venue_state_reads_enabled`** (default **`false`**) gates whether `state_readers` Tier A paths read **`VenueState`** vs **cache/portfolio**; **removed in Step 5** — not a permanent dual-mode architecture | Clean rollout and **instant rollback** (YAML flip) before deletion; after Step 5, only the VenueState path remains for Tier A. |
| 5 | **One `venue_state` fact type** with **`status`** (and related) fields — replaces separate heartbeat / stale / error / refresh-timeout fact types | Simpler operator and schema surface; see `06_observability.md`. |
| 6 | **Calendar compression** via **parallel engineering** during Step 2 soak; **soak durations unchanged** (full-length) | Shorter wall-clock without shortening validation windows. |

## Success criteria

| Criterion | Evidence |
|-----------|----------|
| Tier A reads use venue-backed data (when flag true) | `deployment_budget`, `NautilusAccountSnapshotProvider` / capital path, `NautilusLayerAContext`, `bot_sell_validate_strategy`, and `NautilusPositionStateReader` use `state_readers` paths that honor `venue_state_reads_enabled` (see `01_inventory.md`, `02_architecture.md`). |
| No competing Tier A sources when flag true | All Tier A rows in `01_inventory.md` route through the same **`venue_state_reads_enabled`**-aware boundary in `state_readers`; no direct `VenueState` imports outside `state_readers` / compose. After Step 5, flag removed — single VenueState path only. |
| Reconciliation off in production | `position_reconciliation_enabled: false` in all live scenario YAMLs that previously enabled it (see `04_migration.md` Step 2). |
| Reconciliation code + migration flag removed | Step 5: reconciliation deleted; **`venue_state_reads_enabled`** removed from code and config — Tier A is **only** VenueState-backed. |
| Operators can see health | Unified `venue_state` fact + `venue_state_missing_mark`; two-gate readiness documented in `06_observability.md`. |

## Timeline (compressed calendar)

**Target: 4–5 weeks** wall-clock: Step 2 **full-length** soak runs in parallel with Step 3 engineering; Step 4 is a **config flip** after Step 3 is validated; Step 4 **full-length** soak in parallel with Step 5 prep where safe; Step 5 deletion after Step 4 soak passes. See `04_migration.md`.

## Explicit non-goals (from charter)

- **Backtesting** integration for `VenueState`.
- **Upstream Nautilus** patches.
- **New strategy types** or **PnL ledger** accuracy across external activity.

## Document map

| File | Purpose |
|------|---------|
| `01_inventory.md` | Tier A / Tier B inventory (Step 1 gating artifact). **Frozen** — accepted as-is. |
| `02_architecture.md` | Target architecture, **feature-flag pattern** (migration only). |
| `03_venue_state_design.md` | `VenueState` API, cost basis, polling, facts, config, **two-gate** startup. |
| `04_migration.md` | Steps 2–5, rollback, parallel work, **Step 5 flag removal**. |
| `05_tests.md` | Test strategy — flag, cost basis, readiness. |
| `06_observability.md` | **`venue_state`** + **`venue_state_missing_mark`**, health. |
| `07_risks_and_open_questions.md` | **Decisions resolved** + remaining judgment items. |

## Reference implementation context

- **`WalletSyncActor`**: `src/tyrex_pm/runtime/wallet_sync.py` — polls Data API positions / CLOB orders, instrument discovery; contains reconciliation (lines 545–802) to be disabled then removed.
- **`NautilusDeploymentBudget`**: `src/tyrex_pm/runtime/deployment_budget.py` — `filled_*` uses `cache.positions_open` + `portfolio.is_flat` (lines 140–188); `pending_*` uses `NautilusExecutionStateReader.list_open_orders` → `Cache.orders_open` (lines 86–138). **When `venue_state_reads_enabled` is true**, equivalent reads come from **`VenueState`** via `state_readers`; **filled** notional uses **venue size × mark** (fallback **0.5**, fact on missing mark) per Decision 1 — **replacing** `position_entry_deployment_usd` Nautilus position fields for that path.
- **Compose wiring**: `src/tyrex_pm/runtime/guru_compose.py` lines 324–343 constructs readers and deployment budget.
