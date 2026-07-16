# Survival exit order-type policy — codebase review

Phase 1 enforcement live runs showed FAK survival exits rejected with *no orders found to match*. This note records existing order-type support before adding `SurvivalExitOrderPolicy`.

## Supported order types (Tyrex)

| Type | `OrderStyle` enum | Venue bridge | Notes |
|------|-------------------|--------------|-------|
| **GTC** | Yes | `OrderType.GTC` | Passive entries/exits; can rest on book |
| **FOK** | Yes | `OrderType.FOK` | Marketable entry path in planner |
| **FAK** | Yes | `OrderType.FAK` | Default paired-binary exit style |
| **GTD** | **No** | SDK comment only | Not wired; policy maps GTD → GTC + `local_ttl_s` cancel watchdog |

OMS lives under `src/tyrex_pm/execution/` (`oms.py`, `live_oms.py`), not `src/tyrex_pm/oms/`.

## Paths that hardcode FAK

1. **`ExecutionPlanner._plan_urgent_exit`** — always restyles to `OrderStyle.FAK` (`planner_urgent_exit_fak`). Any exit with `urgency=urgent` and planner enabled becomes FAK unless order policy overrides style (survival patch respects intent `order_style` for FAK/FOK).
2. **`PairedBinaryStrategyConfig.exit_order_style`** — default FAK; `build_exit_work_unit` passes this to `ExitIntent`.
3. **Protection / paired-binary sizing** — urgent exits set `urgency=URGENCY_URGENT`.

Survival enforce previously used the same path: `enforce → _dispatch_exit → try_build_exit → build_exit_work_unit(FAK, urgent)`.

## GTC / GTD placement

- **GTC**: `_plan_passive_exit` when `urgency != urgent`. Resting ack detected via `is_resting_ack()` (`live`, `resting`, `open`, `delayed`).
- **GTD**: Not available. Managed rest uses **GTC + local TTL cancel** as a stand-in.

## Cancel APIs

- **Single order**: `PyClobBridge.cancel_order(orderID=…)` → `LiveOMS.cancel` → `SingleWriterOMS.cancel` → pipeline `CancelIntent` path.
- **`cancelMarketOrders`**: Not implemented anywhere in repo.

## Resting order status

- User WS: `PLACEMENT`, `UPDATE`, `CANCELLATION` → `OpenOrderView`
- REST: `get_open_orders()` via `clob_wallet_sync.refresh_wallet_from_clob`
- Submit ack: `parse_oms_match_evidence` / `is_resting_ack`
- No per-order REST `get_order` wired (`rest_get_order_found` placeholder in reconcile)

## How `urgent_exit_fak` is built

1. Pipeline `_run_execution_planner` with `DecisionContext.URGENT_EXIT`, fresh snapshot, `ExecutableBookView`.
2. Planner `_plan_urgent_exit`: quality gate → staleness → `_resolve_worst_price` → restyle FAK @ worst price.
3. Risk planned validation → `to_place_request` → `LiveOMS.submit`.

FAK retry helpers exist in `market_data/executable_book.py` (`plan_fak_with_fresh_evidence`, `plan_fak_retry_for_remaining`); paired-binary monitor uses `decision_type=fak_retry` on pending retries.

## Integration points for survival order policy

| Component | Role |
|-----------|------|
| `survival/order_policy.py` | Select FAK/FOK/GTC/GTD, reprice, near-close rules |
| `survival/enforcement_dispatch.py` | Retry state, OMS-reject handling, skip logic |
| `strategies/paired_binary/monitor.py` | Policy on enforce + retry + resting watchdog |
| `strategies/paired_binary/exit_engine.py` | `order_style` / `urgency` overrides on build |
| `execution/planner.py` | FOK on urgent path; passive GTC for managed rest |
| `runtime/paired_binary_run.py` | Post-submit OMS-reject hook for survival exits |
| `runtime/pipeline.py` | Existing Intent → Risk → Planner → OMS (unchanged path) |

Post-only is **not** used on any survival exit path; policy rejects post-only if configured.
