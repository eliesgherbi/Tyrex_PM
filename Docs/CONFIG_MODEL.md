# Configuration model

**Hub:** [README.md](README.md) · **Architecture:** [Architecture.md](Architecture.md) · **Operations:** [OPERATIONS.md](OPERATIONS.md)

Tyrex_PM has **three** YAML files that get merged in a fixed order, plus environment variables for secrets and a few runtime overrides. Everything is parsed by `runtime/config.py` into the immutable `AppConfig(strategy, risk, runtime, raw)` dataclass.

---

## 1. File layout & merge order

```
config/
  risk/default.yaml                # global risk policy            ── base
  runtime/default.yaml             # supervisors, reporting, mode  ── base
  strategies/<name>.yaml           # strategy knobs                ── base
  scenarios/<name>.yaml            # overlay (deep-merged on top)  ── overlay
```

Resolution (see `load_app_config`):

1. Always loads `config/risk/default.yaml` and `config/runtime/default.yaml`.
2. Loads the strategy YAML passed via `--strategy` (defaults to `config/strategies/guru_follow.yaml`).
3. If `--scenario <name>` is given, resolves to `config/scenarios/<name>.yaml` and **deep-merges** these overlay sections:
   - top-level: `execution_mode`, `reporting`, `supervisors`, `logging` → into `runtime`
   - top-level: `guru`, `filters`, `sizing`, `exits` → into `strategy`
   - blocks: `risk`, `runtime`, `strategy` → into their respective base dicts
4. Hands the merged dicts to `parse_app_config(...)`, which produces `AppConfig`.
5. Environment variables override a tiny subset of supervisor knobs at run start (see §5).

All money / size / price values **must be quoted strings** in YAML; they are parsed via `Decimal(str(v))`.

---

## 2. `risk/` block

Defaults from `config/risk/default.yaml`:

```yaml
notional:
  min_usd: "1"           # reject below this
  max_usd: "4"           # cap or reject above this
  max_policy: cap        # cap | deny

venue_min_size:
  enabled: true
  policy: deny           # deny | bump
  default_min_size: "5"  # Polymarket hard floor (shares)

deployment:
  token_cap_usd: "5"
  portfolio_cap_usd: "15"

capital:
  enabled: true
  max_wallet_age_s: 120

inventory:
  sell_requires_venue_position: true

kill_switch:
  enabled: false

concurrency:
  max_orders_in_flight: 8

readiness:
  require_wallet_sync: true
  max_wallet_age_s_live: 60
  require_heartbeat_live: true
  require_user_ws_live: true
```

| Key | Type | Meaning | Reason code on deny |
|-----|------|---------|---------------------|
| `notional.min_usd` | Decimal | Hard floor on order USD notional | `notional_below_min` |
| `notional.max_usd` | Decimal | Cap on order USD notional | `notional_above_max` (deny) or silent clip (cap) |
| `notional.max_policy` | `cap`\|`deny` | Behavior when above max | — |
| `venue_min_size.enabled` | bool | Run the final pre-submit min-size guard | — |
| `venue_min_size.policy` | `deny`\|`bump` | Below `default_min_size`: block, or raise to floor and re-validate | `below_venue_min_size` |
| `venue_min_size.default_min_size` | Decimal | Venue floor in **shares** used as the fallback when `RiskContext.market_info` has no entry for the token (shadow mode + tests). Live mode prefers the venue's `MarketInfo.min_order_size`; the evidence row always carries `venue_min_size_source = "venue" \| "config_default"`. | — |
| `deployment.token_cap_usd` | Decimal | Per-token long-side cap (positions + open BUYs + in-flight) | `token_deployment_cap` |
| `deployment.portfolio_cap_usd` | Decimal | Total long-side cap across all tokens | `portfolio_deployment_cap` |
| `capital.enabled` | bool | Pre-submit balance/allowance gate (BUYs only) | `insufficient_capital` / `insufficient_allowance` |
| `capital.max_wallet_age_s` | int | Wallet snapshot must be fresher than this for capital gate | `stale_wallet_snapshot` |
| `inventory.sell_requires_venue_position` | bool | SELL is gated on a non-zero venue position | `naked_sell` / `insufficient_inventory` |
| `kill_switch.enabled` | bool | Operator-flipped global block | `kill_switch` |
| `concurrency.max_orders_in_flight` | int | Max simultaneous unacked submits | `concurrency_limit` |
| `readiness.require_wallet_sync` | bool | Wallet must have synced once before live trading | `not_ready` |
| `readiness.max_wallet_age_s_live` | int | Max age for wallet truth in live mode | `stale_wallet_snapshot` |
| `readiness.require_heartbeat_live` | bool | CLOB heartbeat must be healthy in live | `heartbeat_failed` |
| `readiness.require_user_ws_live` | bool | User-WS must be fresh in live | `venue_truth_stale` |

Gate order is documented in [Architecture.md §7](Architecture.md#7-the-riskengine-gate-sequence).

---

## 3. `strategy/` block (`guru_follow`)

Defaults from `config/strategies/guru_follow.yaml`:

```yaml
guru:
  wallet: "0x...."                      # required for live polling
  data_api_poll_interval_s: 5
  data_api_limit: 50

filters:
  exclude_untradeable_markets: false
  token_allowlist: []                   # empty = no allowlist filter
  min_notional_usd: "700"
  significance_min_notional_usd: "0"
  min_conviction_score: "0"

sizing:
  static_enabled: true                  # BUY uses static_amount_usd, ignores copy_scale + conviction
  static_amount_usd: "5"
  copy_scale: "1.0"
  conviction:
    enabled: false
    score_min: "0"
    score_max: "1"
    min_multiplier: "0.5"
    max_multiplier: "2.0"

exits:
  dust_notional_usd: "0.5"
  sell_mode: proportional_to_guru       # proportional_to_guru | full_bot_position
```

| Key | Type | Meaning |
|-----|------|---------|
| `guru.wallet` | str | Polymarket address being copied (Data API user filter) |
| `guru.data_api_poll_interval_s` | float | Poll interval for `data-api/activity` |
| `guru.data_api_limit` / `data_api_max_pages_per_poll` | int | Pagination |
| `filters.token_allowlist` | list[str] | If non-empty, only these `token_id`s are allowed |
| `filters.min_notional_usd` | Decimal | Skip guru trades smaller than this notional |
| `filters.significance_min_notional_usd` | Decimal | "Significant" threshold for proportional sells |
| `filters.min_conviction_score` | Decimal | Skip below this conviction |
| `filters.exclude_untradeable_markets` | bool | Calls Gamma to verify market tradeability before submit |
| `sizing.static_enabled` | bool | If true, BUYs use `static_amount_usd` notional; otherwise mirror guru with `copy_scale * conviction` |
| `sizing.static_amount_usd` | Decimal | Fixed BUY notional when static |
| `sizing.copy_scale` | Decimal | Multiplier on guru notional |
| `sizing.conviction.*` | various | Linear interp between `min_multiplier` and `max_multiplier` over `[score_min, score_max]` |
| `exits.dust_notional_usd` | Decimal | Suppress dust SELLs |
| `exits.sell_mode` | str | `proportional_to_guru` mirrors guru's exit fraction; `full_bot_position` always exits the whole local position |

---

## 4. `runtime/` block

Defaults from `config/runtime/default.yaml`:

```yaml
execution_mode: shadow                  # shadow | live  (set by scenarios)
shadow_bootstrap:
  usdc_balance: "1000000"
  usdc_allowance: "1000000"
reporting:
  enabled: true
  runs_dir: var/reporting/runs
supervisors:
  reconcile_interval_s: 30
  submit_grace_s: 15
  provisional_unknown_terminal_timeout_s: 60
  venue_confirm_provisional_timeout_s: 60   # back-compat alias for above
  adoption_grace_s: 5
logging:
  level: INFO
```

| Key | Meaning |
|-----|---------|
| `execution_mode` | `shadow` (synthetic fills via `apply_shadow_fill`) or `live` (real CLOB) |
| `shadow_bootstrap.*` | Synthetic USDC seed for shadow runs (no venue sync) |
| `reporting.enabled` / `runs_dir` | Toggle and root for `var/reporting/runs/<run_id>/` |
| `supervisors.reconcile_interval_s` | Cadence of REST refresh + reconcile in `live_supervisor.venue_refresh_loop` |
| `supervisors.submit_grace_s` | Provisional age below which a missing-from-venue local row is non-blocking (`provisional_pending_venue`) |
| `supervisors.provisional_unknown_terminal_timeout_s` | Provisional age past which an absent row drops as `UNKNOWN_TERMINAL` (when WS fresh and no venue restart) |
| `supervisors.adoption_grace_s` | Window in which a fresh venue order id with no local row is allowed to adopt onto a no-vid provisional submit |
| `logging.level` | Python logging level for the process |

See [LIVE_ARCHITECTURE.md §3](LIVE_ARCHITECTURE.md#3-reconcile-state-machine) for how these knobs interact.

---

## 5. Environment variables

Secrets live in `.env`; they are loaded by `python-dotenv` in `runtime/app.py`.

| Variable | Purpose |
|----------|---------|
| `TYREX_PRIVATE_KEY` (or `POLYMARKET_PK`) | EVM private key for CLOB signing |
| `TYREX_FUNDER` (or `POLYMARKET_FUNDER`) | Proxy / funder address (required when `signature_type=1`) |
| `TYREX_SIGNATURE_TYPE` (or `POLYMARKET_SIGNATURE_TYPE`) | `0` (EOA) or `1` (proxy / email-wallet) |
| `TYREX_CLOB_HOST` | Override CLOB endpoint (default `https://clob-v2.polymarket.com` — V2 staging; flipped to `https://clob.polymarket.com` on V2 cutover day) |
| `TYREX_CHAIN_ID` | Override chain id (default `137` Polygon) |
| `TYREX_HEARTBEAT_ID` (or `POLYMARKET_HEARTBEAT_ID`) | Optional heartbeat client id |
| `POLYMARKET_API_KEY` / `_API_SECRET` / `_PASSPHRASE` | Optional pre-derived API creds; otherwise derived from the key |

Operator overrides (mirror `runtime.supervisors.*`):

| Variable | Mirrors |
|----------|---------|
| `TYREX_SUBMIT_GRACE_S` | `submit_grace_s` |
| `TYREX_PROVISIONAL_UNKNOWN_TERMINAL_TIMEOUT_S` | `provisional_unknown_terminal_timeout_s` |
| `TYREX_ADOPTION_GRACE_S` | `adoption_grace_s` |
| `TYREX_VENUE_CONFIRM_GRACE_S` | back-compat alias for `submit_grace_s` |
| `TYREX_VENUE_CONFIRM_PROVISIONAL_TIMEOUT_S` | back-compat alias for `provisional_unknown_terminal_timeout_s` |

When set, env values are read **per reconcile** by `pipeline._reconcile_kw` so live tweaks don't require a restart.

---

## 6. Scenarios

Built-in scenarios in `config/scenarios/`:

| File | Purpose | Notable overrides |
|------|---------|-------------------|
| `shadow_guru.yaml` | Default development / golden-test mode | `execution_mode: shadow`, fast guru poll (`1 s`), large synthetic USDC bootstrap |
| `live_guru.yaml` | Live guru-follow on Polymarket | `execution_mode: live`, raises `deployment.token_cap_usd` to `100` |
| `live_attest.yaml` | One-shot post + cancel attestation | `capital.enabled: false`, `venue_min_size.enabled: false`, relaxed `notional` band, `require_user_ws_live: false` |

Add new scenarios as small overlays — never duplicate the defaults wholesale.

---

## 7. How scope discovers config

```
runtime/app.py::cmd_run
  └─ load_app_config(repo_root, strategy_file, scenario_file)
       └─ deep-merges YAML, then parse_app_config(...)
            ├─ StrategyConfig → strategies/guru_follow/strategy.py
            ├─ RiskConfig     → risk/engine.py + per-policy modules
            ├─ RuntimeConfig  → runtime/coordinator.py + supervisors
            └─ raw            → kept for forensic dumps in manifest.json
```

The parsed `AppConfig` is **frozen**; nothing mutates it after `cmd_run` starts. New runtime parameters must therefore land in `config.py` and be threaded explicitly through the coordinator.
