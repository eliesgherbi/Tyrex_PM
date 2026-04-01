# Config model (v1 operational split)

Secrets stay in **`.env`** (or exported env vars). All YAML is non-secret.

**Context:** [Architecture.md](Architecture.md) · **Config module:** [modules/config/README.md](modules/config/README.md)

## Strategy (`StrategySettings` → `load_strategy_settings`)

| Field | Required | Default | Notes |
|-------|----------|---------|--------|
| `guru_wallet_address` | yes | — | `0x` + 40 hex chars |
| `allowlisted_token_ids` | yes | — | Non-empty list; decimal CLOB token strings; no duplicates |
| `copy_scale` | no | `1.0` | `>= 0`; passed to proportional sizing |
| `strategy_dedup_state_path` | no | — | If set, overrides runtime dedup path for `GuruMonitorActor` only |

## Risk (`RiskSettings` → `load_risk_settings`)

| Field | Required | Default | Notes |
|-------|----------|---------|--------|
| `max_order_quantity` | yes | — | Reject if `intent.quantity` exceeds |
| `max_notional_usd_per_order` | yes | — | Reject if `price_ref * qty` exceeds |
| `max_token_notional_usd_open` | no | unlimited (`null`) | Session exposure per token; see `ConfiguredRiskPolicy` |
| `kill_switch` | no | `false` | If true, all intents rejected |
| `fail_on_missing_price_for_notional` | no | `true` | Fail closed when `price_ref` missing |

Session exposure is updated via `note_fill_assumption` after a successful live submit (best-effort; not a venue position read).

## Runtime (`RuntimeSettings` → `load_runtime_settings`)

| Field | Required | Default | Notes |
|-------|----------|---------|--------|
| `trader_id` | yes | — | Must contain `-` (e.g. `TYREX-GURU-001`) |
| `execution_mode` | no | `shadow` | `shadow` or `live` |
| `guru_poll_interval_seconds` | no | `30` | Data API poll interval |
| `data_api_base_url` | no | `https://data-api.polymarket.com` | Trailing slash stripped |
| `guru_dedup_state_path` | no | `var/guru_dedup.json` | Dedup store for guru trades |
| `logging_level` | no | `INFO` | Nautilus `LoggingConfig.log_level` |
| `clob_host` | no | `https://clob.polymarket.com` | Used for live `ClobClient` when composing |
| `chain_id` | no | `137` | Polygon mainnet default |

## `.env` (secrets only)

See `Docs/Runbooks/polymarket_operator_v1_00.md`:

- `POLYMARKET_PK`
- `POLYMARKET_FUNDER` (if needed for signature type)
- `POLYMARKET_SIGNATURE_TYPE`
- `POLYMARKET_API_KEY` / `POLYMARKET_API_SECRET` / `POLYMARKET_PASSPHRASE` (L2 trio or derive)

Optional: `TYREX_MIN_BUY_NOTIONAL_USD` — live BUY floor in `PolymarketExecutionPolicy` (default `1`).

Optional: `TYREX_PM_DOTENV` — path to alternate env file for `scripts/run_guru.py`.

## Example files

Repo templates (replace guru wallet / tokens before production):

- `config/strategy/guru_follow.yaml`
- `config/risk/guru_follow_risk.yaml`
- `config/runtime/live_polymarket.yaml`
