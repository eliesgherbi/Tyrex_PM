# Position Reconciliation — Config Surface

## New YAML keys

All keys live under the existing runtime config section (same file as `wallet_sync_*` keys).

| Key | Type | Default | Floor | Description |
|-----|------|---------|-------|-------------|
| `position_reconciliation_enabled` | bool | `false` | — | Master switch for position reconciliation. When `false`, `_reconciliation_pass` is skipped entirely. Independent of `wallet_sync_enabled` — wallet sync must be enabled for position reconciliation to function (positions payload is the source). |
| `data_api_lag_tolerance_seconds` | float | `60.0` | `0.0` | If a cache Position's `ts_last` (most recent event timestamp; `model/position.pxd:91–92`) is younger than this value, the reconciliation is deferred for that instrument. Defends against Race B (Data API lag). Default `60.0` matches the documented upper bound of Data API propagation delay (OQ-6: "several seconds to a minute"). Set to `0.0` to skip the check (aggressive). |
| `position_reconciliation_deferral_max` | int | `5` | `1` | Maximum consecutive cycles an instrument can be deferred due to in-flight orders covering the delta (Race C defense). After this many deferrals, reconciliation proceeds regardless. Surfaced as `position_reconciliation_stuck` health degradation at limit. |
| `recently_reconciled_ttl_seconds` | float | `60.0` | `0.0` | After a reconciliation report is sent for an instrument, skip re-reconciliation for this duration. Defends against Race E (synthetic close re-trigger). |
| `reconcile_venue_has_more` | bool | `false` | — | Whether to send `PositionStatusReport` when venue quantity exceeds cache quantity. Default `false` — only external closes/reductions are reconciled. Set `true` to also accelerate opening reconciliation (requires understanding Race A/D implications). |
| `position_reconciliation_shadow_mode` | bool | `true` | — | When `true` and `position_reconciliation_enabled=true`, the reconciliation pass runs, computes diffs, and emits `position_reconciliation` facts with `reconciliation_sent=false`, but does **not** call `msgbus.send`. This is a temporary rollout tool: operators run in shadow mode to validate diff accuracy against real wallet activity before enabling engine-state mutation. Flip to `false` after validation. Expected to be removed once behavior is validated in production. |

## Validation rules

1. `position_reconciliation_enabled` requires `wallet_sync_enabled` to be `true`.
   If `position_reconciliation_enabled=true` and `wallet_sync_enabled=false`, the config
   loader should raise a validation error at startup.

2. `data_api_lag_tolerance_seconds` must be `>= 0.0`.

3. `position_reconciliation_deferral_max` must be `>= 1`.

4. `recently_reconciled_ttl_seconds` must be `>= 0.0`.

5. `recently_reconciled_ttl_seconds` should be warned (not rejected) if set below
   `wallet_sync_poll_interval_seconds` — a TTL shorter than the poll interval is
   effectively a no-op defense.

6. `data_api_lag_tolerance_seconds` should be warned (not rejected) if set below `30.0`.
   The documented Data API propagation delay (OQ-6) is "several seconds to a minute";
   values below 30s risk acting on stale API data.

7. `position_reconciliation_enabled=True` requires `generate_missing_orders` to be `True`
   on the `LiveExecEngineConfig`. This is a **hard startup error**, not a warning.
   Validation is performed in `_live_exec_engine_config()` in `runtime/guru_compose.py`
   (lines 83–107), which is the only Tyrex function that constructs `LiveExecEngineConfig`.
   If `position_reconciliation_enabled=True` in `RuntimeSettings` and
   `generate_missing_orders` would be `False`, raise a configuration error and refuse to
   start. Since Tyrex never explicitly sets `generate_missing_orders` (it defaults to
   `True` in `LiveExecEngineConfig` per `live/config.py:201`), this validation fires only
   if someone explicitly overrides it to `False`.

## `WalletSyncConfig` changes

```python
@dataclass(frozen=True, slots=True)
class WalletSyncConfig:
    poll_interval_seconds: float = 15.0
    startup_deadline_seconds: float = 120.0
    per_instrument_max_retries: int = 3
    data_api_base_url: str = "https://data-api.polymarket.com"
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    gamma_http_timeout_seconds: float = 15.0
    clob_host: str = "https://clob.polymarket.com"
    # --- Position reconciliation ---
    position_reconciliation_enabled: bool = False
    data_api_lag_tolerance_seconds: float = 60.0
    position_reconciliation_deferral_max: int = 5
    recently_reconciled_ttl_seconds: float = 60.0
    reconcile_venue_has_more: bool = False
    position_reconciliation_shadow_mode: bool = True
```

## Example YAML

```yaml
# Runtime config (live_polymarket.yaml)
wallet_sync_enabled: true
wallet_sync_poll_interval_seconds: 15
wallet_sync_startup_deadline_seconds: 120
wallet_sync_per_instrument_max_retries: 3

# Position reconciliation (new)
position_reconciliation_enabled: true
position_reconciliation_shadow_mode: true   # flip to false after shadow validation
data_api_lag_tolerance_seconds: 60.0
position_reconciliation_deferral_max: 5
recently_reconciled_ttl_seconds: 60.0
reconcile_venue_has_more: false
```

## Config loader changes (`config/loaders.py`)

The `RuntimeSettings` dataclass (or its wallet-sync subsection builder) must:

1. Read the 6 new keys from the YAML runtime section.
2. Apply floor validation.
3. Emit a warning if `position_reconciliation_enabled=true` and
   `wallet_sync_enabled=false`.
4. Emit a warning if `recently_reconciled_ttl_seconds < wallet_sync_poll_interval_seconds`.
5. Emit a warning if `data_api_lag_tolerance_seconds < 30.0`.
6. Pass the fields into `WalletSyncConfig` construction.

## Interaction with `generate_missing_orders`

The engine's `generate_missing_orders` setting (Nautilus `LiveExecEngineConfig`, default
`True` per `live/config.py:201`) must be `True` for position reconciliation to work.
If it is `False`, the engine skips synthetic order generation
(`live/execution_engine.py:2362–2367`) and logs a warning. The reconciliation silently
becomes a no-op.

This is validated as a **hard startup error** in `_live_exec_engine_config()` in
`runtime/guru_compose.py` (lines 83–107). The compose function has access to both
`RuntimeSettings` (which carries `position_reconciliation_enabled`) and the kwargs being
built for `LiveExecEngineConfig`. If `position_reconciliation_enabled=True` and the
resulting `LiveExecEngineConfig` would have `generate_missing_orders=False`, the function
raises a configuration error and refuses to start. See validation rule 7 above.
