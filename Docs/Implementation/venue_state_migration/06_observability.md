# Observability

## Fact types

### `venue_state` (single consolidated type — Decision 5)

All operational signals that were previously split across multiple fact types use **`status`** (and related fields) on **`venue_state`**.

| Field | Purpose |
|-------|---------|
| `status` | `ok` \| `stale` \| `error` \| `refresh_timeout` \| `parse_warning` \| `heartbeat` |
| `phase` | Optional; when `status=error`: `positions` \| `orders` \| `cash` |
| `last_positions_success_utc` | Optional ISO timestamp |
| `last_cash_success_utc` | Optional ISO timestamp |
| `position_count`, `resting_order_count` | Integers |
| `cash_ready` | Boolean — mirrors **`venue_state_cash_ready`** |
| `ttl_seconds`, `cash_poll_interval_seconds` | Config echo |
| `detail` | Optional string |

**When emitted:** Periodic heartbeat, stale read detection, HTTP/parse failures, `refresh(force=True)` timeout, parse warnings.

**Registration:** `src/tyrex_pm/reporting/schema/facts_v1.py` — single `frozenset` allowlist for `venue_state` (pattern lines 151–188).

### `venue_state_missing_mark` (cost basis fallback — Decision 1)

Emitted when **filled deployment / exposure** uses **fallback price 0.5** because **mark price is missing**.

| Field | Purpose |
|-------|---------|
| `instrument_id` | Affected instrument |
| `token_id` | Optional |
| `fallback_price` | **`0.5`** (explicit) |

**Registration:** Separate allowlist in `facts_v1.py`.

## Logs

Structured logs on `VenueState` (`tyrex_pm.runtime.venue_state`):

- `event=venue_state_apply` — snapshot applied (debug).
- `event=venue_state_stale_read` — read while stale (warning).
- `event=venue_state_refresh_timeout` — aligns with `venue_state` fact `status=refresh_timeout`.

## Health / readiness (two gates — Decision 3)

**Tradable / READY** (for processes that require Tier A freshness) requires **both**:

1. **`wallet_sync_first_sync_complete`** — existing `WalletSyncHealthAdapter` / actor (`nautilus_live_health.py`, `wallet_sync.py`).
2. **`venue_state_cash_ready`** — `VenueState` property after successful CLOB balance parse (`03_venue_state_design.md`).

Document **which** readiness surface (startup gate vs health API) exposes each predicate in implementation; operators should see **both** green before relying on Tier A after **Step 4**.

**With `venue_state_reads_enabled: false`:** Tier A reads still use cache; **`venue_state_cash_ready`** may still be tracked for **pre-flip** validation.

**Migration flag note:** **`venue_state_reads_enabled`** is **removed Step 5** — not a permanent dual-mode pattern (`02_architecture.md`).

## What changes for existing facts

| Fact | Change |
|------|--------|
| `wallet_sync` | Unchanged. |
| `position_reconciliation` | **Gone** after Step 5. |
| `position` (reporting) | `net_exposure_usd` may change when flag **true** (venue × mark). |

## Operator runbook (short)

1. **`venue_state.status` not `ok`** — check `phase` / `detail`; Data API vs CLOB; `wallet_sync` HTTP flags.
2. **Spike in `venue_state_missing_mark`** — marks missing for active instruments; fix mark source or accept 0.5 fallback risk.
3. **READY stuck** — verify **both** `wallet_sync_first_sync_complete` and **`venue_state_cash_ready`** independently.
4. **Step 4 rollback** — set **`venue_state_reads_enabled: false`**; confirm Tier A returns to cache behavior.
