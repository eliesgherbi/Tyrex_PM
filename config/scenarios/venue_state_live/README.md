# Scenario: `venue_state_live`

Live runtime config for exercising **VenueState** after the migration (no `position_reconciliation`, no `venue_state_reads_enabled`). Use isolated guru state under `var/scenarios/venue_state_live/`.

## Run (example)

From repo root, after setting env (`POLYMARKET_*`, funder, etc.) and editing `guru_follow.yaml`:

```text
python scripts/run_guru.py ^
  --strategy-conf config/scenarios/venue_state_live/guru_follow.yaml ^
  --risk-conf config/scenarios/venue_state_live/guru_follow_risk.yaml ^
  --live-conf config/scenarios/venue_state_live/live_polymarket.yaml
```

Ensure `var/scenarios/venue_state_live/` exists (or create it) for `guru_watermark.json` / `guru_dedup.json`.

---

## Parameters **specific to this upgrade** (highlighted)

| Key | Value in this scenario | Role |
|-----|------------------------|------|
| **`wallet_sync_enabled`** | `true` | Required on live for `WalletSyncActor` to fetch positions/orders and push into **VenueState** (and cache). |
| **`polymarket_use_data_api_for_positions`** | `true` | Wallet sync uses Data API position rows (feeds VenueState position map). |
| **`venue_state_ttl_seconds`** | `30.0` | Staleness horizon for VenueState refresh behavior (see `venue_state.py`). |
| **`venue_state_cash_poll_interval_seconds`** | `10.0` | CLOB collateral poll cadence; **must be ≥ 3.0** (loader validates). |
| **`venue_state_refresh_force_max_ms`** | `500` | Cap for blocking refresh when forcing cache price reads. |

**Readiness (wired in compose, not YAML keys):** startup stays not-ready until **`wallet_sync` first sync complete** *and* **`venue_state_cash_ready`** (first successful CLOB balance apply). Watch `venue_state` / `wallet_sync` facts and logs.

**Tier A cost basis (filled deployment):** venue position size × **mark**; if mark missing → **0.5** USD + `venue_state_missing_mark` fact.

---

## **Deprecated / do not use** (removed from loaders & code)

Do **not** add these to live YAML; they are **ignored** if present only if your loader passes them through as extras — they are **not** part of `RuntimeSettings` anymore and have **no effect**:

| Removed key | Notes |
|-------------|--------|
| **`venue_state_reads_enabled`** | Migration flag removed in Step 5. Tier A always uses VenueState when `venue_state` is constructed (live + `wallet_sync_enabled`). |
| **`position_reconciliation_enabled`** | Reconciliation pass deleted from `wallet_sync.py`. |
| **`position_reconciliation_shadow_mode`** | Same. |
| **`position_reconciliation_deferral_max`** | Same. |
| **`data_api_lag_tolerance_seconds`** | Was used only for reconciliation deferral logic (removed). |
| **`recently_reconciled_ttl_seconds`** | Reconciliation TTL (removed). |
| **`reconcile_venue_has_more`** | Reconciliation “venue has more” branch (removed). |

**Facts:** `position_reconciliation` is **not** a registered fact type anymore. Use **`venue_state`**, **`venue_state_missing_mark`**, **`wallet_sync`**, **`wallet_sync_unresolvable`**, etc.

**Health:** `position_reconciliation_stuck` is no longer emitted by `NautilusLiveExecutionHealthSource` (stuck deferral path removed with reconciliation).

---

## Contrast with older scenarios

- **`position_reconciliation_validation/`** — historical name; recon keys were stripped from live YAMLs. Prefer **this folder** for a clean “VenueState-only” mental model.
- **`layer_a_follow/`** — still valid; it omits explicit `venue_state_*` keys (defaults apply). This scenario **documents** those defaults explicitly for validation runs.
