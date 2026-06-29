# `execution/`

Turns approved intents into venue submits / cancels and keeps the local OMS state in lockstep with venue truth.

## Files

| File | Purpose |
|------|---------|
| `planner.py` | `ExecutionPlanner` (P3 architecture_enhance) — converts a risk-approved intent into a concrete `ExecutionPlan`: passive/normal entry → GTC at strategy limit; urgent/protection exit → FAK at worst-acceptable price from `MarketStateStore`; stale/missing book on an urgent exit → deny unless an explicit fallback is configured. Preserves the pre-check `client_order_id` |
| `models.py` | `ExecutionPlan` / `ExecutionPlanResult` (P3 architecture_enhance) — the planner's output, kept out of `core/` to keep core small |
| `oms.py` | `SingleWriterOMS` — serializes submits and cancels for one wallet onto a single asyncio queue, then awaits the backend. Eliminates double-submit races between concurrent guru signals |
| `adapters.py` | `OMSBackend` Protocol + `ShadowOMS` (returns `"shadow_ack"` / `"shadow_cancel_ack"`) |
| `live_oms.py` | `LiveOMS` — real Polymarket backend that delegates to `venue.polymarket.clob_bridge.PyClobBridge` (V2 SDK) |
| `order_builder.py` | Build the venue-side payload (price, size, side, order style) for a `py-clob-client-v2` post (V2 `OrderArgsV2`) |
| `order_lifecycle.py` | Local OMS state transitions: `register_submit`, `ack_submit`, `release_after_ack`, `remove_resting_order`, `submit_fingerprint_for_intent`, `sync_local_open_orders_from_venue_wallet` |
| `cancel_manager.py` | Cancel helpers (best-effort idempotent) |
| `liquidity_guard.py` | Pre-submit microprice / book-depth sanity checks (currently advisory) |
| `slippage.py` | Pre-submit slippage estimation helpers (currently advisory) |
| `router.py` | Venue routing stub — only Polymarket today; kept so adding a second venue is a single-file change |

## OMS contract

```python
class OMSBackend(Protocol):
    async def submit(self, ap: ApprovedIntent) -> str: ...   # raw venue response or shadow ack
    async def cancel(self, ac: ApprovedCancel) -> str: ...
```

The pipeline always wraps the chosen backend in `SingleWriterOMS(backend)` so the actual submit/cancel call site is a single coroutine; backends do not need to be reentrant.

## Planner pipeline (P3 architecture_enhance)

When `execution.planner.enabled` (requires `market_data.enabled`), the pipeline inserts the planner between risk pre-check and OMS:

```
Intent → RiskEngine.evaluate_intent (pre-check, mints client_order_id)
       → ExecutionPlanner.plan(approved, market_state)        → execution_plan fact
       → risk/planned_order.validate_planned_order(plan)      → risk_decision {phase: planned}
       → SingleWriterOMS.submit                               → oms_submit (planner_reason)
```

**BUY submit ack:** resting orders update `OrderStore` only. Allocation credits require matched fill qty (`allocation_buy_applied_from_fill`). Resting acks emit `order_resting_recorded` on `oms_submit`.

**Urgent reduce-only SELL:** `risk/planned_order.py` + `risk/exits.py` may approve via executable-bid mark fallback when deployment mark is missing but venue position and fresh bid exist (`reduce_only_exit_mark_fallback`).

`validate_planned_order` re-checks notional / deployment caps / capital / inventory / venue-min-size on the *final* plan **without** re-entering `evaluate_intent` and **without** minting a new client order id. It denies non-urgent planner price-worsening; urgent (protection) exits are exempt because marketable pricing is the point. When the planner is disabled the OMS receives the strategy/intent order style unchanged (no `execution_plan` fact).

**Readiness:** normal entry — unit-tested, shadow CLI, **live-validated** (`simple_signal_test`). Urgent FAK — unit-tested, shadow CLI via `validation_harness`, **live-ready** via `validation_harness_urgent_exit_live.yaml`. Stale-book deny — unit-tested, **shadow-only synthetic** via `validation_harness`. **Phase 4.6** paired binary uses GTC entry + FAK exits through the same pipeline ([design](../../Implementation/architecture_enhance/phase_4_6_paired_binary_strategy_production_protection.md)).

## Order lifecycle (local view)

```
register_submit  ──►  provisional row in OrderStore (no vid yet, fingerprint locked)
        │
        ▼
ack_submit  ──────►  vid linked, ack_status set, optional shadow instant fill
        │
        ▼
sync_local_open_orders_from_venue_wallet
                ──►  venue truth catches up; row marked venue_confirmed
        │
        ▼
remove_resting_order  ─►  cancel / fill / unknown_terminal drop
```

The `submit_fingerprint` (sha1 of `token|side|size|price`) blocks duplicate submits while the original is still provisional. See [`state/`](../state/README.md) for the repair / adoption / tombstone state machine that drives `sync_local_open_orders_from_venue_wallet`.

## Live submit flow (live mode)

1. Pipeline calls `SingleWriterOMS.submit(approved_intent)`.
2. `LiveOMS` builds the payload via `order_builder`, then `PyClobBridge.create_and_post_limit` constructs a V2 `OrderArgsV2` and calls `client.create_and_post_order` on a thread (`asyncio.to_thread`).
3. Successful POST returns the venue JSON; `parse_venue_order_id` extracts the `venue_order_id`; `ack_submit` links it locally.
4. Pipeline triggers a coordinated REST refresh (`refresh_wallet_coordinated_after_live_submit`) so the new resting order is visible to the next risk evaluation.

## Adding a new venue

Implement a new `OMSBackend` subclass that talks to the new venue, mirror `venue.polymarket.*` under `venue/<name>/`, and select it in `runtime/app.py::cmd_run`. The OMS contract is intentionally tiny precisely so this is a small change.
