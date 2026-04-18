# `execution/`

Turns approved intents into venue submits / cancels and keeps the local OMS state in lockstep with venue truth.

## Files

| File | Purpose |
|------|---------|
| `oms.py` | `SingleWriterOMS` — serializes submits and cancels for one wallet onto a single asyncio queue, then awaits the backend. Eliminates double-submit races between concurrent guru signals |
| `adapters.py` | `OMSBackend` Protocol + `ShadowOMS` (returns `"shadow_ack"` / `"shadow_cancel_ack"`) |
| `live_oms.py` | `LiveOMS` — real Polymarket backend that delegates to `venue.polymarket.clob_execution` |
| `order_builder.py` | Build the venue-side payload (price, size, side, order style) for a `py-clob-client` post |
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
2. `LiveOMS` builds the payload via `order_builder` and posts through `clob_execution.submit_via_py_clob_client` (lazy `py-clob-client` import).
3. Successful POST returns the venue JSON; `parse_venue_order_id` extracts the `venue_order_id`; `ack_submit` links it locally.
4. Pipeline triggers a coordinated REST refresh (`refresh_wallet_coordinated_after_live_submit`) so the new resting order is visible to the next risk evaluation.

## Adding a new venue

Implement a new `OMSBackend` subclass that talks to the new venue, mirror `venue.polymarket.*` under `venue/<name>/`, and select it in `runtime/app.py::cmd_run`. The OMS contract is intentionally tiny precisely so this is a small change.
