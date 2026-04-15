# 03 — Config Surface

## New YAML keys (runtime YAML)

| Key | Type | Default (live) | Default (shadow) | Validation | Description |
|-----|------|----------------|-------------------|------------|-------------|
| `wallet_sync_enabled` | `bool` | `true` | `false` | Requires `execution_mode=live` | Enable continuous wallet instrument discovery. Shadow mode cannot use it (no exec client). |
| `wallet_sync_poll_interval_seconds` | `float` | `15.0` | — | `>= 5.0` | Interval between wallet sync poll cycles. Lower values give faster discovery but more API calls. |
| `wallet_sync_startup_deadline_seconds` | `float` | `120.0` | — | `>= 30.0` | Max seconds after `on_start` before startup is considered timed out. Readiness gate distinguishes `"startup_wallet_sync_pending"` from `"startup_wallet_sync_timeout"`. |
| `wallet_sync_per_instrument_max_retries` | `int` | `3` | — | `>= 1` | Max cycles a single `condition_id` may fail resolution before being marked terminally unresolvable and excluded from the completeness check. |

## Position reconciliation keys (runtime YAML)

Added by the position reconciliation extension (`docs/implementation/venue_sync_truth/position_reconciliation/04_config.md`).

| Key | Type | Default | Floor | Validation | Description |
|-----|------|---------|-------|------------|-------------|
| `position_reconciliation_enabled` | `bool` | `false` | — | Requires `wallet_sync_enabled=true` | Master switch for position reconciliation pass. |
| `position_reconciliation_shadow_mode` | `bool` | `true` | — | — | When `true`, diffs are computed and facts emitted but `msgbus.send` is skipped. Temporary rollout tool. |
| `data_api_lag_tolerance_seconds` | `float` | `60.0` | `0.0` | Warn if `< 30.0` | If a cache Position's `ts_last` is younger than this, reconciliation is deferred (Race B defense). |
| `position_reconciliation_deferral_max` | `int` | `5` | `1` | — | Max consecutive deferrals before reconciliation proceeds anyway. Stuck at max → `DEGRADED_OMS`. |
| `recently_reconciled_ttl_seconds` | `float` | `60.0` | `0.0` | Warn if `< poll_interval` | After reconciling an instrument, skip re-reconciliation for this duration (Race E defense). |
| `reconcile_venue_has_more` | `bool` | `false` | — | — | Whether to reconcile when venue qty > cache qty. Default `false` (close/reduce only). |

Hard startup validation: `position_reconciliation_enabled=true` requires `generate_missing_orders=true` on `LiveExecEngineConfig` (checked in `_live_exec_engine_config()` in `runtime/guru_compose.py`).

## Changed defaults (runtime YAML)

| Key | Old default (live) | New default (live) | Rationale |
|-----|--------------------|--------------------|-----------|
| `polymarket_use_data_api_for_positions` | `false` | `true` when `wallet_sync_enabled` | Data API `/positions` is a bulk endpoint; more robust for reconciliation. The adapter still scopes output to cached instruments (`execution.py:788`), but with wallet sync ensuring cache coverage, this works correctly. |
| `live_exec_open_check_open_only` | `null` (Nautilus default `true`) | `false` when `wallet_sync_enabled` | Full order history check catches stale rests more reliably. With wallet sync ensuring cache coverage, the API cost is justified. |

## Keys that become less relevant but are NOT removed

| Key | Status | Reason |
|-----|--------|--------|
| `polymarket_instrument_ids` | Still supported | Static instrument list; useful for shadow mode or explicit pinning. Wallet sync is additive. |
| `polymarket_dynamic_instruments` | Still supported | Controls guru-signal-driven dynamic resolution. Wallet sync uses `force_add_instrument` (no cap). |
| `polymarket_dynamic_max_activations` | Still supported | Caps guru-driven resolution only. Wallet sync bypasses it. |
| `polymarket_wallet_position_warmup_max` | Still supported | Compose-time warmup seeds `Cache` before `node.build()`. Wallet sync is the continuous follow-up. |
| `polymarket_startup_token_warmup_max` | Still supported | Guru activity warmup is orthogonal to wallet sync. |
| `exec_position_check_interval_seconds` | Still supported | Controls Nautilus engine position reconciliation interval. Wallet sync does not replace it — it ensures the instrument coverage that makes it effective. |
| `exec_open_check_interval_seconds` | Still supported | Same reasoning. |

## Config validation (`loaders.py`)

New validation rules in `load_runtime`:

```python
if wallet_sync_enabled and mode != "live":
    raise ValueError("wallet_sync_enabled requires execution_mode=live")

if wallet_sync_poll_interval < 5.0:
    raise ValueError("wallet_sync_poll_interval_seconds must be >= 5.0")
```

## Environment variables

No new environment variables. `WalletSyncActor` uses the same py-clob client as the existing `ClobAllowanceStateProvider` (built from `POLYMARKET_API_KEY` / `POLYMARKET_API_SECRET` / `POLYMARKET_PASSPHRASE` / `POLYMARKET_PK` via `build_clob_client_from_env`). The Data API user address uses the same `POLYMARKET_FUNDER` logic as `guru_cache_warmup._follower_positions_api_user`.

## Example live YAML snippet

```yaml
execution_mode: live

# Wallet sync
wallet_sync_enabled: true
wallet_sync_poll_interval_seconds: 15.0
wallet_sync_startup_deadline_seconds: 120.0
wallet_sync_per_instrument_max_retries: 3

# Position reconciliation
position_reconciliation_enabled: true
position_reconciliation_shadow_mode: true   # flip to false after shadow validation
data_api_lag_tolerance_seconds: 60.0
position_reconciliation_deferral_max: 5
recently_reconciled_ttl_seconds: 60.0
reconcile_venue_has_more: false

# Adapter flags (defaults change with wallet_sync_enabled)
polymarket_use_data_api_for_positions: true
live_exec_open_check_open_only: false

# Existing reconciliation intervals (unchanged)
exec_position_check_interval_seconds: 45
exec_open_check_interval_seconds: 20

# Warmup (still runs at compose time)
polymarket_wallet_position_warmup_max: 128
polymarket_startup_token_warmup_max: 32
polymarket_dynamic_instruments: true
polymarket_dynamic_max_activations: 32
```
