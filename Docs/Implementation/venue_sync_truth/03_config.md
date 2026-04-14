# 03 — Config Surface

## New YAML keys (runtime YAML)

| Key | Type | Default (live) | Default (shadow) | Description |
|-----|------|----------------|-------------------|-------------|
| `wallet_sync_enabled` | `bool` | `true` | `false` | Enable continuous wallet instrument discovery. Shadow mode cannot use it (no exec client). |
| `wallet_sync_poll_interval_seconds` | `float` | `15.0` | — | Interval between wallet sync poll cycles. Lower values give faster discovery but more API calls. Floor: 5.0. |

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

# Wallet sync (new)
wallet_sync_enabled: true
wallet_sync_poll_interval_seconds: 15.0

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
