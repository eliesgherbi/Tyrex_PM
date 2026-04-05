# Config model (v1 operational split)

Secrets stay in **`.env`** (or exported env vars). All YAML is non-secret.

**Navigation:** [README.md](README.md) · **Context:** [Architecture.md](Architecture.md) · **Config module:** [modules/config/README.md](modules/config/README.md)

## Strategy (`StrategySettings` → `load_strategy_settings`)

| Field | Required | Default | Notes |
|-------|----------|---------|--------|
| `guru_wallet_address` | yes | — | `0x` + 40 hex chars |
| `token_filter` | yes | — | Mapping (see below); **explicit** filtered vs unfiltered mode |
| `copy_scale` | no | `1.0` | `>= 0`; base scale for sizing (`base_scale` in C2 logs) |
| **`conviction_sizing_enabled`** | no | **`false`** | **C2:** When **true**, follow **entry** quantity uses conviction-weighted `effective_scale` (see `Implementation/plan_C2_Capital-Allocation.md` §4.1). **false** = identical to pre-C2 proportional sizing. |
| **`conviction_sizing_cap`** | no | `2.0` | **C2:** Upper bound on `trade_size / rolling_avg` multiplier; must be **`> 0`** when conviction enabled. |
| **`conviction_sizing_lookback_trades`** | no | `20` | **C2:** Rolling window length (guru **BUY** sizes that passed entry policy only). Must be **`>= 1`** when conviction enabled. |
| **`min_follow_notional_usd`** | no | `0` | **C2:** Policy floor on estimated `price_ref × qty` (USD). **`0`** disables. If **`> 0`** and price missing → policy skip (`min_follow_notional_price_missing`); not Phase B risk. |
| `strategy_dedup_state_path` | no | `null` | If set, overrides runtime dedup path for `GuruMonitorActor` only |

### `token_filter` (required block)

| Key | Required | Notes |
|-----|----------|--------|
| `enabled` | yes | `true` = **filtered**: only listed tokens; `false` = **unfiltered**: all guru tokens pass the strategy gate |
| `allowlisted_token_ids` | yes | List (may be empty when `enabled: false`). When `enabled: true`, must be **non-empty**, unique decimal CLOB token strings. When `enabled: false`, **ignored** for filtering (risk / execution unchanged). |

Empty list does **not** implicitly mean “all tokens” — use `enabled: false` for iteration / shadow testing; use `enabled: true` + explicit ids for controlled follow.

## Risk (`RiskSettings` → `load_risk_settings`)

| Field | Required | Default | Notes |
|-------|----------|---------|--------|
| `max_order_quantity` | yes | — | Reject if `intent.quantity` exceeds |
| `max_notional_usd_per_order` | yes | — | Reject if `price_ref * qty` exceeds |
| `max_token_notional_usd_open` | no | unlimited (`null`) | Session exposure per token; see `ConfiguredRiskPolicy` |
| `kill_switch` | no | `false` | If true, all intents rejected |
| `fail_on_missing_price_for_notional` | no | `true` | Fail closed when `price_ref` missing |
| `capital_gate_enabled` | no | `false` | If **true**, risk requires account snapshot + optional py-clob balance/allowance checks (live). |
| `max_account_snapshot_age_seconds` | no | `30` | Refresh account snapshot when older than this (seconds). |
| `max_allowance_snapshot_age_seconds` | no | `120` | Refresh allowance snapshot when older (used when Phase A mins and/or **B4** ``collateral_reserve_usd > 0``). |
| `min_collateral_balance_usd` | no | `null` | If set, compare to py-clob **`balance`** (requires live + capital gate + allowance provider). |
| `min_allowance_usd` | no | `null` | If set, compare to py-clob **`allowance`**. |
| `fail_on_unresolved_position_for_token_cap` | no | `false` | If **true** and per-token cap finite, deny when **`net_exposure`** cannot be computed for the token. |
| `max_portfolio_notional_usd_open` | no | unlimited (`null`/omitted) | **Phase B B2:** Deny when `E_portfolio + n > C` via **B1** aggregator only. **Framework-only** (B0 compose validation). |
| `fail_on_unresolved_portfolio_exposure` | no | `true` | **B2:** Incomplete aggregate (`complete=false` / no `e_portfolio`) → always `RISK_PORTFOLIO_EXPOSURE_UNRESOLVED`. `false` → B1 may omit some marks; if aggregate still **complete**, cap uses returned `e_portfolio` + **warning** (underestimate); never approves on broken/incomplete B1 output. |
| `max_concurrent_guru_resting_orders` | no | `null` (off) | **Phase B B3:** Deny when open guru-origin rests (Polymarket) are already at ``>=`` this limit. Identity: ``state_readers.is_guru_resting_order`` (tags ``guru_cid=``, else ``TX``+26 hex). **Framework-only** (B0). |
| `collateral_reserve_usd` | no | `0` | **Phase B B4:** After Phase A mins, **BUY** intents require py-clob **`balance` ≥ reserve + n** (same snapshot as ``min_*``). Breach: ``RISK_INSUFFICIENT_FREE_COLLATERAL_AFTER_RESERVE``. Missing snapshot/unparsable balance: fail-closed (``RISK_ALLOWANCE_UNAVAILABLE``). Requires **`capital_gate_enabled: true`**. Invalid when **`execution_mode: shadow`** (compose). |

**Phase B startup rules (B0):** See `Phase_B_planing.md` §7. Unsupported combinations raise **`ValueError`** at YAML load (reserve vs capital gate) or at **`build_guru_trading_node`** (shadow / legacy vs framework-truth gates).

**Phase B operator matrix (B5):** Which gates apply in shadow vs live legacy vs live framework-submit — see **`OPERATIONS.md`** § *Phase B — product gates*. **Reason code cheat sheet** for portfolio / guru concurrent / reserve denials — same section.

**Pre–Phase C (live validation):** Restart/mark/denial-rate checklist — **`Implementation/phase_b_operational_validation.md`**.

**Per-token open notional:** On **legacy live** (`polymarket_framework_submit: false`), **`note_fill_assumption`** bumps **`_token_open`** after a **successful** py-clob submit. On **framework submit** (`true`), `note_fill_assumption` is a **no-op** for pending; the cap uses **`Cache` open orders** (**remaining / leaves quantity × price**) plus **filled** exposure from **`Portfolio.net_exposure`** (when the position reader is injected — see `phase_a_closure.md`).

## Runtime (`RuntimeSettings` → `load_runtime_settings`)

| Field | Required | Default | Notes |
|-------|----------|---------|--------|
| `trader_id` | yes | — | Must contain `-` (e.g. `TYREX-GURU-001`) |
| `execution_mode` | no | `shadow` | `shadow` or `live` |
| `guru_poll_interval_seconds` | no | `30` | Data API poll interval |
| `data_api_base_url` | no | `https://data-api.polymarket.com` | Trailing slash stripped |
| `guru_state_path` | no | `var/guru_watermark.json` | **Watermark** JSON (`last_seen_ts_ms`) for incremental `/activity` polling |
| `guru_dedup_state_path` | no | `var/guru_dedup.json` | Secondary dedup LRU for trade ids (replays / reorder) |
| `guru_activity_limit` | no | `200` | Page size for `/activity` (1–500) |
| `guru_max_activity_pages_per_poll` | no | `4` | Max pages per poll (bounds work per tick) |
| `guru_startup_backfill_seconds` | no | `0` | Cold start: watermark = now − this many seconds (`0` = only trades **after** boot) |
| **`guru_ingest_mode`** | no | **`poll_only`** | **`rtds_primary`** (recommended for production timing) · **`rtds_shadow`** (validation: poll publishes, stream logs only) · **`poll_only`** (REST only). See [OPERATIONS.md](OPERATIONS.md) § Guru ingestion (C1). |
| `guru_ingest_phase` | no | `"0"` | Optional rollout tag for ops/logging. |
| `guru_rtds_url` | no | `wss://ws-live-data.polymarket.com` | Polymarket RTDS WebSocket URL (`GuruStreamActor`). |
| `guru_rtds_liveness_timeout_seconds` | no | `120` | Force reconnect if no RTDS traffic within this window. |
| `guru_rtds_reconnect_retry_initial_seconds` | no | `1` | First reconnect backoff (seconds). |
| `guru_rtds_reconnect_retry_max_seconds` | no | `60` | Reconnect backoff cap. |
| `guru_rtds_ping_interval_seconds` | no | `5` | RTDS ping interval. |
| `guru_poll_fallback_enabled` | no | `true` | If **true**, `rtds_primary` can switch to **poll** as publisher on stall/reconnect (when implemented path activates fallback). |
| `guru_poll_fallback_interval_seconds` | no | — | Poll interval while fallback active; defaults to `guru_poll_interval_seconds` if omitted. |
| `guru_gap_fill_enabled` | no | `true` | After reconnect, REST `/activity` gap-fill (`GuruStreamActor`). |
| `guru_gap_fill_lookback_seconds` | no | `60` | Gap-fill lookback window. |
| `guru_proxy_wallet_validation_required` | no | `false` | If **true**, stricter guru wallet format checks at startup when enabled in YAML. |
| `guru_stream_queue_drain_interval_ms` | no | `50` | Timer interval draining RTDS queue into the ingest pipeline. |
| `logging_level` | no | `INFO` | Nautilus `LoggingConfig.log_level` |
| `clob_host` | no | `https://clob.polymarket.com` | Used for live `ClobClient` when composing |
| `chain_id` | no | `137` | Polygon mainnet default |
| `polymarket_nautilus_live` | no | `false` | If **true** and `execution_mode: live`, register **Polymarket live DATA + EXEC** factories on `TradingNode`. |
| `polymarket_instrument_ids` | no | `[]` | Nautilus `InstrumentId` strings for `load_ids`. **Empty** allowed only with **live + Nautilus live + `polymarket_framework_submit`** (zero-bootstrap / implicit dynamic). |
| `polymarket_framework_submit` | no | `false` | If **true** (live), guru uses **`NautilusGuruExecutionPort`** (`submit_order`). Requires **`polymarket_nautilus_live`**. |
| `polymarket_dynamic_instruments` | no | `false` | Explicit dynamic resolve; **coerced true** when instrument list empty and framework submit on live. |
| `polymarket_dynamic_max_activations` | no | `32` | Cap on **new** dynamic `Cache` inserts per process. |
| `polymarket_gamma_base_url` | no | `https://gamma-api.polymarket.com` | Gamma HTTP API for condition lookup. |
| `polymarket_gamma_http_timeout_seconds` | no | `15` | Gamma client timeout. |
| `polymarket_startup_token_warmup_max` | no | `32` | Max guru activity tokens to pre-resolve at compose when list empty (`0` = off). |
| **`execution_venue_normalize_enabled`** | no | **`false`** | **C3:** Tick / size-step / min-notional feasibility **without** raising qty above risk-approved intent. |
| **`execution_entry_guard_enabled`** | no | **`false`** | **C3:** Skip if top-of-book moved worse than slippage ticks vs guru reference (**framework path**). |
| **`execution_max_entry_slippage_ticks`** | no | `0` | Max **ticks** (`instrument.price_increment`) against reference; **required &gt; 0** when guard enabled. |
| **`execution_book_depth_clip_enabled`** | no | **`false`** | **C3:** Clip qty to `cap ×` best bid/ask size (single-level MVP). |
| **`execution_book_depth_utilization_cap`** | no | `1.0` | **(0, 1]** when depth clip enabled. |
| **`execution_book_rest_snapshot_enabled`** | no | **`false`** | If no `Cache` L2, allow one **REST** `get_order_book` snapshot for guard/clip. |
| **`execution_book_strict`** | no | **`false`** | If **true**, missing book when guard/clip need it → **skip** (`exec_book_unavailable_skip`). |
| **`execution_limit_timeout_enabled`** | no | **`false`** | **C3:** `clock` timer + `cancel_order` after timeout (**framework** only). |
| **`execution_limit_timeout_seconds`** | no | `30` | Must be **&gt; 0** when timeout enabled. |

**Derived (not YAML):** `polymarket_token_to_instrument` — built from non-empty `polymarket_instrument_ids`.

**C3 scope:** `src/tyrex_pm/execution/nautilus_guru_exec.py` only; **`PolymarketExecutionPolicy`** (legacy py-clob) unchanged. See **`Implementation/plan_C3_Execution-Quality.md`**.

## `.env` (secrets only)

See `Docs/Runbooks/polymarket_operator_v1_00.md`:

- `POLYMARKET_PK`
- `POLYMARKET_FUNDER` (if needed for signature type)
- `POLYMARKET_SIGNATURE_TYPE`
- `POLYMARKET_API_KEY` / `POLYMARKET_API_SECRET` / `POLYMARKET_PASSPHRASE` (L2 trio or derive)

Optional: `TYREX_MIN_BUY_NOTIONAL_USD` — minimum BUY notional in **both** legacy policy and **`NautilusGuruExecutionPort`** (default `1`).

Optional: `TYREX_PM_DOTENV` — path to alternate env file for `scripts/run_guru.py`.

## Example files

Repo templates (replace guru wallet / tokens before production):

- `config/strategy/guru_follow.yaml`
- `config/risk/guru_follow_risk.yaml`
- `config/runtime/live_polymarket.yaml` — set **`guru_ingest_mode: rtds_primary`** for production-shaped RTDS ingestion (see [OPERATIONS.md](OPERATIONS.md)).
- `config/runtime/rtds_shadow.yaml` — **isolated** watermark/dedup paths for C1 shadow validation without touching normal `var/guru_*.json` state.
