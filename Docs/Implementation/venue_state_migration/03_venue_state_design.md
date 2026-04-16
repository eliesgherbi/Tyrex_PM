# VenueState design specification

**Module:** `src/tyrex_pm/runtime/venue_state.py` (new). **No code in this document** — implementation follows in a later change.

## Purpose

Read-only aggregate of **Polymarket venue truth**: outcome positions, resting orders, and collateral — sourced **only** from **direct HTTP** (Data API, CLOB), never from `Cache` / `Portfolio`.

## Non-negotiables (charter)

| Constraint | Implementation note |
|------------|----------------------|
| Direct HTTP | Uses same HTTP surfaces as `WalletSyncActor` (Data API positions, CLOB open orders) plus CLOB `get_balance_allowance` for cash (see `ClobAllowanceStateProvider` pattern in `state_readers.py` lines 339–380). |
| Single new type | One public class **`VenueState`**; no `VenueStateProvider` or extra abstraction layer. |
| Read-only | No order submission, no `cache.add_*`, no `msgbus` publish except **facts** for observability (`venue_state`, `venue_state_missing_mark`). |
| Non-blocking event loop | HTTP runs on **executor** / background tasks; **`refresh(force=True)`** bounded wait (below). |
| No duplicate HTTP with WalletSync | **WalletSyncActor** pushes fetch results into `VenueState` internal store each cycle; **CLOB balance** on **`VenueState`’s own poll schedule** (Decision 2). |

## Cost basis for filled deployment / exposure (Decision 1)

**Replaces** Nautilus-based `position_entry_deployment_usd` when Tier A reads **`VenueState`** (`deployment_budget.py` lines 47–57: `abs(signed_qty) * avg_px_open` from `Position`).

| Rule | Behavior |
|------|----------|
| Primary | **USD notional** = **venue position size** × **mark price** (same mark source already used for `NautilusPositionStateReader.filled_exposure_usd_best_effort` — e.g. caller-supplied mark / cache quote; implementation detail in `state_readers` / deployment budget). |
| Missing mark | Use **fallback price `0.5`** (USD per contract / unit per team decision). Emit fact **`venue_state_missing_mark`** with **`instrument_id`** (and optional **`token_id`**) so operators can see fallback usage. |
| Zero / negative size | Treat as **no position** for that instrument (consistent with venue map). |

## Public API (signatures)

```python
class VenueState:
    def __init__(
        self,
        *,
        clock: Clock,
        config: VenueStateConfig,
        fact_emit: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None: ...

    # --- Positions (venue — Data API normalized to InstrumentId × net qty) ---
    def positions(self) -> Mapping[InstrumentId, Decimal]: ...
    def position_size(self, instrument_id: InstrumentId) -> Decimal | None: ...

    # --- Cash / collateral (CLOB) ---
    def cash_total(self) -> Decimal | None: ...
    def cash_free(self) -> Decimal | None: ...

    # --- Readiness ---
    @property
    def venue_state_cash_ready(self) -> bool:
        """True after at least one successful CLOB balance apply with parseable balance."""

    # --- Resting orders (venue — CLOB open orders) ---
    def orders_resting(self) -> tuple[VenueOrderSnapshot, ...]: ...
    def orders_resting_for_instrument(
        self, instrument_id: InstrumentId
    ) -> tuple[VenueOrderSnapshot, ...]: ...

    # --- Metadata ---
    def last_success_utc(self) -> datetime | None: ...
    def is_stale(self, now: datetime | None = None) -> bool: ...
    def last_error(self) -> str | None: ...

    # --- Ingest (called from WalletSync executor thread, not from strategies) ---
    def apply_positions_and_orders_rows(
        self,
        *,
        position_rows: list[dict[str, Any]],
        orders_raw: list[dict[str, Any]] | None,
        ts_utc: datetime,
    ) -> None: ...

    def apply_clob_balance(self, raw: dict[str, Any], ts_utc: datetime) -> None: ...

    # --- Refresh ---
    async def refresh(self, *, force: bool = False) -> None:
        """
        When force=True: schedule executor work to refresh CLOB balance (and optionally
        positions if configured). Must complete or time out within max_blocking_ms.
        Does not block the event loop for HTTP.
        """
```

**Supporting types:**

- `VenueStateConfig` — frozen dataclass: `ttl_seconds`, `cash_poll_interval_seconds` (default **10.0**, validated **floor 3.0**), `refresh_force_max_blocking_ms` (default **500**), `stale_warning_threshold_seconds`, etc.
- `VenueOrderSnapshot` — frozen: `instrument_id`, `side`, `leaves_qty`, `price` (aligned with `OrderSnapshot` in `state_readers.py`).

## Polling model (Decision 2)

| Signal | Behavior |
|--------|----------|
| **WalletSync cycle** | Calls `apply_positions_and_orders_rows` after successful HTTP (same data as today’s `_sync_cycle` path). |
| **TTL** | `positions` / `orders_resting` reads use last snapshot if within `ttl_seconds`. |
| **Background CLOB cash poll** | **`venue_state_cash_poll_interval_seconds`**, default **10.0**, **minimum 3.0** (loader validation). Tunable in YAML. |
| **WalletSync interval** | Unchanged (e.g. **15.0** s) — positions/orders freshness tied to WalletSync; cash freshness tied to CLOB poll. |

## Threading

| Thread | Work |
|--------|------|
| **Nautilus actor / event loop** | Strategy callbacks, `VenueState` **read** methods (lock-protected) — **O(1)**. |
| **Executor / asyncio.to_thread** | CLOB balance HTTP; WalletSync sync cycle. |
| **Rule** | Internal state protected by **`threading.Lock`** or equivalent for cross-thread **apply** vs **read**. |

## `refresh(force=True)` — maximum blocking

- **Default:** `refresh_force_max_blocking_ms = 500`.
- On timeout: **do not** block further; retain prior snapshot; emit **`venue_state`** fact with **`status=refresh_timeout`** (and `detail`); set `last_error` diagnostic.

## Error handling

| Condition | Behavior |
|-----------|----------|
| Data API failure | `VenueState` positions slice stale/empty per policy; **`venue_state`** fact with **`status=error`**, **`phase=positions`** (or equivalent), `detail`. |
| CLOB balance failure | Cash `None`; **`venue_state`** with **`status=error`**, **`phase=cash`**. |
| Partial parse | **`venue_state`** with **`status=parse_warning`**; `detail` lists dropped keys/instruments if useful. |

## Startup sequencing (Decision 3)

1. **Construct** `VenueState` in `guru_compose` **before** `NautilusDeploymentBudget` / `DefaultCapitalStateProvider` / strategies.
2. **Register** `WalletSyncActor` to call `apply_positions_and_orders_rows` after successful HTTP.
3. **Start** CLOB cash polling so `apply_clob_balance` can flip **`venue_state_cash_ready`**.
4. **Readiness gate:** **`READY`** requires **`wallet_sync_first_sync_complete`** **and** **`venue_state_cash_ready`** (both predicates exposed to `StartupReadinessGate` / health — see `06_observability.md`). **`wallet_sync_first_sync_complete`** is existing behavior (`wallet_sync.py` ~166–168). **`venue_state_cash_ready`** is defined on `VenueState` above.

**Note:** While **`venue_state_reads_enabled`** is **false**, Tier A reads still use cache; the two gates still validate that **`VenueState`** is populated for observability and for a safe flag flip on Step 4.

## Fact emission (Decision 5 — consolidated `venue_state`)

**Single fact type `venue_state`** — use **`status`** (and optional fields) instead of separate `venue_state_stale`, `venue_state_error`, `venue_state_refresh_timeout` types.

| Field | Purpose |
|-------|---------|
| `status` | `ok` \| `stale` \| `error` \| `refresh_timeout` \| `parse_warning` \| `heartbeat` (periodic no-op signal) |
| `phase` | When `status=error`: `positions` \| `orders` \| `cash` (optional) |
| `last_positions_success_utc` | Optional ISO timestamp |
| `last_cash_success_utc` | Optional ISO timestamp |
| `position_count`, `resting_order_count` | Integers |
| `cash_ready` | Boolean (mirror `venue_state_cash_ready`) |
| `ttl_seconds`, `cash_poll_interval_seconds` | Config echo for operators |
| `detail` | Optional string |

Emit on: heartbeat timer, stale read path, errors, refresh timeout, parse warnings.

**Separate fact `venue_state_missing_mark`** (cost basis only): emitted when **fallback price 0.5** is used (Decision 1). Fields e.g. `instrument_id`, `token_id` (optional), `fallback_price: 0.5`.

**Registration:** `src/tyrex_pm/reporting/schema/facts_v1.py` — one allowlist for `venue_state`, one for `venue_state_missing_mark` (`06_observability.md`).

## Config surface (YAML → `RuntimeSettings`)

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `venue_state_reads_enabled` | bool | **`false`** | **Migration only** — Tier A routing in `state_readers`; **removed Step 5**. |
| `venue_state_ttl_seconds` | float | `30.0` | Staleness for position/order snapshot reads. |
| `venue_state_cash_poll_interval_seconds` | float | **`10.0`** | CLOB balance poll; **loader floor 3.0**. |
| `venue_state_refresh_force_max_ms` | int | `500` | `refresh(force=True)` bound. |

**Reconciliation:** `position_reconciliation_enabled: false` in Step 2 — orthogonal keys.

## Interface with `WalletSyncActor`

**WalletSyncActor** remains the **single** fetcher for Data API positions + CLOB orders; **VenueState** is updated via `apply_positions_and_orders_rows`. **VenueState** runs **CLOB-only** balance polling on its own timer (**10 s** default, floor **3 s**). This avoids duplicate bulk position HTTP and satisfies “VenueState polls Polymarket directly” for cash.
