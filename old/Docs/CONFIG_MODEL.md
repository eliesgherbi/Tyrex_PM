# Configuration model

**Hub:** [README.md](README.md) · **Architecture:** [Architecture.md](Architecture.md) · **Operations:** [OPERATIONS.md](OPERATIONS.md)

Tyrex_PM has **three** YAML files that get merged in a fixed order, plus environment variables for secrets and a few runtime overrides. Everything is parsed by `runtime/config.py` into the immutable `AppConfig(strategy, risk, runtime, raw)` dataclass.

**Allocation ledger:** required on every run. There is no `allocation_ledger.enabled` toggle; `enabled: false` is rejected at config load. All strategy SELL paths size against per-owner allocation.

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
| `exits.sell_mode` | str | `proportional_to_guru`: guru-scaled size clamped to `guru_follow` allocation + venue `available_to_sell`. `full_bot_position`: sell full **allocated** `guru_follow` position (not wallet-wide). |

### 3.1 Standalone validation strategies (`kind: sell_test` | `kind: allocation_test` | `kind: tp_sl_test`)

These YAML kinds are **mutually exclusive** with full guru-follow parsing. They populate `AppConfig.sell_test`, `AppConfig.allocation_test`, or `AppConfig.tp_sl_test` instead of the guru `strategy` block.

**`config/strategies/allocation_test.yaml`** (`kind: allocation_test`) — P4 ownership toy:

| Key | Meaning |
|-----|---------|
| `token_id` | CLOB outcome token for BUY/SELL |
| `owner_a_id` / `owner_b_id` | Logical allocation owners (default `allocation_test_A` / `allocation_test_B`) |
| `buy.*` | Owner A BUY leg (`notional_usd`, `limit_price`, `order_style`) |
| `owner_b_unauthorized_sell.*` | Owner B block drill (`size_mode`: `match_owner_a_buy` \| `fixed`) |
| `owner_a_sell.*` | Owner A authorized SELL (`delay_s`, `order_style`, `pricing_mode`, `aggression_ticks`, `limit_price` fallback, optional `min_price`) |
| `timeouts.*` | Poll deadlines for allocation visibility, live position, exit completion |
| `run_once` | Stop after one A→B→A cycle |

Intent extensions carry `allocation_owner_id` per leg; Owner B never reaches OMS when allocation is zero.

**Owner A SELL pricing** (default `pricing_mode: auto`):

| Key | Meaning |
|-----|---------|
| `pricing_mode` | `auto` (default) — live run fetches book and picks `best_bid - aggression_ticks * tick_size` so the SELL is marketable; `fixed` — submit at `limit_price` (or buy limit if unset) |
| `aggression_ticks` | Ticks below best bid for SELL auto-pricing (default `0` = at best bid) |
| `limit_price` | Fallback when auto lookup fails; also used in shadow when no live book |
| `min_price` | Optional lower guardrail: refuse to price below this and fall back to `limit_price` |

Live runs emit `health` `allocation_test_pricing` with side `SELL` before Owner A exit submit.

**`config/strategies/tp_sl_test.yaml`** (`kind: tp_sl_test`) — P6 TP/SL validation harness (not production TP/SL):

| Key | Meaning |
|-----|---------|
| `token_id` | CLOB outcome token for BUY + monitor |
| `owner_id` | Allocation owner for BUY credit and SELL clamp (default `tp_sl_test`; isolated from `guru_follow`) |
| `buy.*` | Same shape as `sell_test.buy` |
| `monitor.*` | TP/SL monitor: `price_source` (`fixture` \| `best_bid`; `mark` rejected), `poll_interval_s`, `trigger_mode`, absolute thresholds (`take_profit_price`, `stop_loss_price`) **or** percentage thresholds (`take_profit_pct`, `stop_loss_pct` as decimal fractions e.g. `0.20` = +20%, `trigger_reference: entry_price`), `fixture_prices` (required for `fixture`) |
| `exit.*` | Exit leg: `size_mode` (`full_allocated_position` \| `percent_allocated_position` \| `fixed_size`), `percent`, `fixed_size`, `pricing_mode`, `aggression_ticks`, `min_price`, `limit_price`, `order_style` |
| `timeouts.*` | `inventory_timeout_s`, `trigger_timeout_s`, `completion_timeout_s` |
| `run_once` | Stop after terminal SELL outcome or timeout |

TP/SL overlay emits `ExitIntent` through the same pipeline as `sell_test`; pricing resolves at **trigger time**. Facts use `health` events `tp_sl_*`. See [tp_sl_overlay_plan.md](Implementation/sell_feature/tp_sl_overlay_plan.md).

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
allocation_ledger: {}
market_data:                            # P2 architecture_enhance (dark-launched)
  enabled: false
  max_book_age_s: 5.0
  token_ids: []
execution:                              # P3 architecture_enhance (dark-launched)
  planner:
    enabled: false
    require_fresh_book_for_urgent: true
    max_book_age_s: 5.0
    allow_urgent_exit_fallback: false
```

| Key | Meaning |
|-----|---------|
| `execution_mode` | `shadow` (synthetic fills via `apply_shadow_fill`) or `live` (real CLOB) |
| `shadow_bootstrap.*` | Synthetic USDC seed for shadow runs (no venue sync) |
| `reporting.enabled` / `runs_dir` | Toggle and root for `var/reporting/runs/<run_id>/` |
| `allocation_ledger` | Required marker block. Runtime always tracks per-owner token qty in `var/state/allocation_ledger.json`; all strategy SELL paths clamp to allocated qty (P4/P5). `enabled: false` is rejected at config load. |
| `market_data.enabled` | Dark-launch toggle for `MarketStateStore` (P2). When false, no live book behavior changes. |
| `market_data.max_book_age_s` | Default staleness threshold for `is_stale`; missing books are always treated as stale. |
| `execution.planner.enabled` | Insert `ExecutionPlanner` + `validate_planned_order` between risk pre-check and OMS (P3). **Requires `market_data.enabled: true`** (config load rejects otherwise). When false, the OMS gets the strategy/intent order style unchanged. |
| `execution.planner.max_book_age_s` | Book staleness limit for urgent/protection exits. |
| `execution.planner.allow_urgent_exit_fallback` | If true, an urgent exit with a stale/missing book may fall back to a FAK at the intent's limit price instead of denying. |
| `supervisors.reconcile_interval_s` | Cadence of REST refresh + reconcile in `live_supervisor.venue_refresh_loop` |
| `supervisors.submit_grace_s` | Provisional age below which a missing-from-venue local row is non-blocking (`provisional_pending_venue`) |
| `supervisors.provisional_unknown_terminal_timeout_s` | Provisional age past which an absent row drops as `UNKNOWN_TERMINAL` (when WS fresh and no venue restart) |
| `supervisors.adoption_grace_s` | Window in which a fresh venue order id with no local row is allowed to adopt onto a no-vid provisional submit |
| `logging.level` | Python logging level for the process |

See [LIVE_ARCHITECTURE.md §3](LIVE_ARCHITECTURE.md#3-reconcile-pipeline) for how these knobs interact.

### 4.1 Survival (Phase 1)

Under `runtime.survival` in scenario overlays. **Defaults:** `enabled: false`; all module `enforcement_mode: advisory`; `stall_exit.enabled: false`.

```yaml
runtime:
  survival:
    enabled: true
    monitor_mode: ws_event          # ws_event | hybrid | poll
    survivor_floor:
      enabled: true
      enforcement_mode: advisory    # advisory | enforce
      mode: winner_entry_price
      floor_buffer: "0.00"
      require_fresh_book: true
      max_spread: "0.04"
      min_depth_fraction: "0.8"
    recovery_level:
      desired_buffer: "0.00"
      activation_buffer: "0.00"
    trailing_stop:
      enabled: true
      enforcement_mode: enforce       # scenario-specific; global default advisory
      activation_mode: loss_recovered
      trail_distance: "0.025"
      arm_delay_s: 5.0
    exit_planning:
      require_executable_evidence: true
      min_depth_fraction: "0.8"
    enforcement:
      retry_quality_rejects: false    # global default; true in trailing-enforce scenarios
      max_quality_reject_retries: 10
      quality_reject_retry_backoff_s: 0.25
      abandon_quality_reject_after_s: 10
      order_policy:
        mode: fak_retry
        max_fak_retries: 3
        reprice_on_retry: true
    stall_exit:
      enabled: false                  # legacy path; off in simplified Phase 1
```

| Key | Meaning |
|-----|---------|
| `survival.enabled` | Master toggle; false = no survival evaluation |
| `monitor_mode` | `ws_event` wakes monitor on book updates; `poll` uses tick interval only |
| `*.enforcement_mode` | `advisory` = facts only; `enforce` = OMS reduce-only exit on trigger |
| `enforcement.retry_quality_rejects` | Latch pending intent when enforce skipped for `quality_reject` |
| `enforcement.order_policy.*` | FAK retry / managed REST for survival exits |

Full operator guide: [Implementation/Survivor_target/phase1_parameter_guide.md](Implementation/Survivor_target/phase1_parameter_guide.md).

### 4.2 Strategy lifecycle (paired-binary)

Under `runtime.strategy_lifecycle`:

```yaml
runtime:
  strategy_lifecycle:
    mode: market_aware
    flatten_before_event_end_s: 20
    block_new_entry_phases: [near_close, closed]
    min_survival_window_s: 45
```

Pre-close flatten **preempts** pending survival exit intents (`survival_enforce_exit_abandoned` reason `pre_close_flatten_preempted`).

### 4.3 Market data (Phase 2 extensions)

When `market_data.enabled: true`, scenarios may also set:

| Key | Meaning |
|-----|---------|
| `market_data.mode` | `ws_primary` for live paired-binary (REST bootstrap + recovery only) |
| `market_data.quality.enforcement_mode` | `enforce` applies gate rejections |
| `market_data.quality.require_ws_primary_for_entry` | Block entry on non-WS book source |

Paired with `execution.planner.enabled: true` for executable depth at submit time.

---

## 5. Environment variables

Secrets live in `.env`; they are loaded by `python-dotenv` in `runtime/app.py`.

| Variable | Purpose |
|----------|---------|
| `TYREX_PRIVATE_KEY` (or `POLYMARKET_PK`) | EVM private key for CLOB signing |
| `TYREX_FUNDER` (or `POLYMARKET_FUNDER`) | Proxy / funder address (required when `signature_type=1`) |
| `TYREX_SIGNATURE_TYPE` (or `POLYMARKET_SIGNATURE_TYPE`) | `0` (EOA) or `1` (proxy / email-wallet) |
| `TYREX_CLOB_HOST` | Override CLOB endpoint (default `https://clob.polymarket.com` — post-cutover V2 production). Stale `https://clob-v2.polymarket.com` overrides are rewritten to the production host with a startup warning. |
| `TYREX_CHAIN_ID` | Override chain id (default `137` Polygon) |
| `TYREX_HEARTBEAT_ID` (or `POLYMARKET_HEARTBEAT_ID`) | Optional heartbeat client id |
| `POLYMARKET_API_KEY` / `_API_SECRET` / `_PASSPHRASE` | Optional but recommended post-cutover pre-created CLOB API credentials. If all three are set, runtime uses them directly and skips API-key creation/derive. These are not `POLY_BUILDER_*` or `RELAYER_*` credentials. |

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
| `live_guru.yaml` | Live guru-follow on Polymarket | `execution_mode: live` only (inherits risk defaults from `config/risk/default.yaml`) |
| `live_attest.yaml` | One-shot post + cancel attestation | `capital.enabled: false`, `venue_min_size.enabled: false`, relaxed `notional` band, `require_user_ws_live: false` |
| `live_paired_binary_phase1_trailing_enforce.yaml` | Phase 1 survival experiment: trailing enforce + quality-reject retry | `survival.enabled: true`, `trailing_stop.enforcement_mode: enforce`, `survivor_floor.enforcement_mode: advisory` |
| `live_paired_binary_tiny.yaml` / `live_paired_binary_ws_primary*.yaml` | Phase 2 WS-primary paired-binary live | `market_data.mode: ws_primary`, planner enabled |

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
