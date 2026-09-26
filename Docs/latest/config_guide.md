# Run-config parameter guide

Role of every YAML key the unified runtime accepts. If this file disagrees with `src/tyrex_pm/runtime/run_config.py` or a plugin schema, trust the code and update this document.

Annotated loadable catalog (ask70 pattern, comments on every field): [`config/runs/generic_parameter_catalog.yaml`](../../config/runs/generic_parameter_catalog.yaml). Operator files: [`ask70_protection_tiny_live.yaml`](../../config/runs/ask70_protection_tiny_live.yaml), [`z_gap_tiny_live.yaml`](../../config/runs/z_gap_tiny_live.yaml), validate-only [`feature_probe_catalog.yaml`](../../config/runs/feature_probe_catalog.yaml).

Related: [running](running.md) (CLI, outcomes, `eval`), [protection](protection.md) (overlay contract), [extending](extending.md) (new strategy + lab fixture), [strategy inputs](strategy_inputs.md) (facts are **not** YAML).

## Rules

- One YAML file per run. Unknown keys fail at load.
- Two owners: **host blocks** are shared; **`strategy.parameters`** are kind-specific. Mixing ask70 keys into `kind: z_gap` (or the reverse) fails.
- XOR pairs cannot both be set (`absolute` vs `pct_from_entry`; `trail_abs` vs `trail_pct`).
- Tiny-live `risk.maximum_total_debit` cannot exceed 5 USDC.
- `InputContract` (which adapters start, when `evaluate()` wakes) lives on the plugin, not in YAML.
- Book `min_order_size` and `tick_size` come from discovery / `BookView`, not YAML.

CLI (`run`): `--live`, `--dotenv`, `--out-dir` (overrides `report_directory` and stamps a folder), `--validate-config`, `--show-config`.

CLI (`eval`): `--config`, `--fixture` (`lab_evaluate_v1` JSON), optional `--out`. `--live` is rejected. See [running.md](running.md#evaluate-lab-no-live) and [extending.md](extending.md#lab-fixture-before-live).

## When a parameter is added

1. Add the field to the owning dataclass and reject-unknown set (`run_config.py`, `protection/spec.py`, or `strategies/<kind>/schema.py`).
2. Add it here under that block (key, allowed values, role, module).
3. Add it to `config/runs/generic_parameter_catalog.yaml` with a one-line comment.
4. If it looks like an existing knob, add a row under [Lookalikes](#lookalikes--not-duplicates).

Loader map:

| Block | Loader |
|-------|--------|
| Top-level, market, account, risk, lifecycle, directories | `src/tyrex_pm/runtime/run_config.py` |
| `protection` | `src/tyrex_pm/protection/spec.py` |
| `strategy.kind: ask70` | `src/tyrex_pm/strategies/ask70/schema.py` → `Ask70Config` |
| `strategy.kind: z_gap` | `src/tyrex_pm/strategies/z_gap/schema.py` → `ZGapConfig` |
| `strategy.kind: feature_probe` | `src/tyrex_pm/strategies/feature_probe/schema.py` → `FeatureProbeConfig` |
| `market.family` | `src/tyrex_pm/runtime/market_family.py` |

## Top-level

| Key | Role | Module |
|-----|------|--------|
| `schema_version` | Must be `1`. | `run_config.py` |
| `run_name` | Report folder name. Characters: `[A-Za-z0-9._-]+`. | `run_config.py` |
| `state_directory` | SQLite execution journal (replay / recovery). Do not delete to “clear” a blocker. | `persistence/execution_journal.py` |
| `report_directory` | Default report root. CLI `--out-dir` wins. | `reporting/run_report.py` |
| `startup` | Optional. `inspection_budget_s` (60), `prompt_timeout_s` (120), `monitor_interval_s` (15). | `runtime/startup_controller.py` |

`TYREX_POLYGON_RPC_URL` is an environment variable, not a YAML field. When it is set to an HTTP(S) Polygon mainnet JSON-RPC endpoint, startup and `--check-account` use it for read-only `eth_chainId` and `eth_call` payout reads. The URL is not written into reports. A missing URL, a wrong chain, or a failed call leaves the affected market unresolved.

## `strategy`

| Key | Role | Module |
|-----|------|--------|
| `kind` | Registered plugin: `ask70`, `z_gap`, or `feature_probe`. Unknown fails. | `strategies/registry.py` |
| `parameters` | Kind-owned leaves only. See [ask70](#ask70-parameters), [z_gap](#z_gap-parameters), and [feature_probe](#feature_probe-parameters). | plugin schema |

## `protection`

Framework overlay. Arms after confirmed fill. Independent of `evaluate()`. Required for ask70; required for `feature_probe` only when `emit_entry: true`; optional for z_gap. At least one of take-profit, stop-loss, or enabled trailing. Full contract: [protection.md](protection.md).

| Key | Allowed | Role | Module |
|-----|---------|------|--------|
| `mark` | `bid` | Held-token best bid. | `protection/spec.py`, `triggers.py` |
| `size_mode` | `full` | Sell remaining venue-legal size. | `protection/spec.py` |
| `take_profit.style` | `reactive_fak`, `resting_gtc`, `resting_gtd` | Watch-and-FAK vs post-only resting sell. | `protection/` + `trading_runtime.py` |
| `take_profit.absolute` XOR `pct_from_entry` | absolute in `(0, 1)`; pct `> 0` | TP trigger. Never both. | `protection/spec.py` |
| `stop_loss.style` | `stop_market_fak` | Venue has no stop order; overlay FAK-sells. | `protection/triggers.py` |
| `stop_loss.absolute` XOR `pct_from_entry` | same as TP | SL trigger. Never both. | `protection/spec.py` |
| `trailing.enabled` | explicit bool | Off: no trail distance required. | `protection/spec.py` |
| `trailing.activation` | `immediate`, `after_profit` | Trail from fill vs after peak ≥ entry + `activation_profit_abs`. | `protection/triggers.py` |
| `trailing.trail_abs` XOR `trail_pct` | `> 0` | Required when enabled. | `protection/triggers.py` |
| `trailing.activation_profit_abs` | `> 0` | Required only when `activation: after_profit`. | `protection/spec.py` |
| `trailing.mark` | `bid` | Same constraint as `protection.mark`. | `protection/spec.py` |

## `market`

| Key | Role | Module |
|-----|------|--------|
| `family` | Discovery / window model. Only `btc_updown_5m` today. | `runtime/market_family.py` |
| `binance_symbol` | Always parsed. Adapter starts **only** if the strategy `InputContract` needs Binance. Unused at runtime for ask70. | `runtime/composition.py`, Binance adapters |
| `require_ssr_price_match` | Chainlink PTB vs Polymarket SSR `openPrice`. ask70 has no PTB — leave `false`. | `market_runtime.py`, `ssr_ptb_attestation.py` |
| `preparation_lead_s` | Seconds before `event_start` to bind prepared-next and warm order metadata. | `market_data_runtime.py` |
| `maximum_duration_s` | Authoritative trading-duration cap from **prepared window start**, not process start. | `market_data_runtime.py` |
| `evaluation_interval_s` | Clamps `EvaluationScheduler` coalesce/timer (overrides contract `coalesce_s`). Does **not** throttle protection. | `runtime/scheduler.py` |
| `maximum_book_age_ms` | Stale `BookView` is not executable. | `execution/final_gate.py` |
| `maximum_candidate_age_ms` | Monotonic fuse: intent created → POST. Must be **greater than** `lifecycle.prepare_budget_ms`. | `final_gate.py`, lifecycle |
| `entry_dispatch_margin_s` | Seconds before the hard BUY cutoff at which the strategy entry window closes. Finite and `>= 0`. Omitted means `evaluation_interval_s`. Does not change consume, the final gate, or POST. | `run_config.py`, `trading_runtime.py` |

## `account`

Authenticated wallet observation. A failed read is never converted to a zero balance.

| Key | Role | Module |
|-----|------|--------|
| `refresh_interval_s` | Background collateral / token / open-order cycle. | `execution/account_state.py` |
| `snapshot_max_age_s` | Older snapshot is `STALE` (entry blocked). Last-good is kept until then. | `account_state.py` |
| `read_timeout_s` | Diagnostic “slow read” threshold. Does **not** cancel SDK HTTP/2. | `account_state.slow_read_threshold_s` |
| `retry_attempts` | Account-read retry count. | `account_state.py` |
| `retry_base_delay_s` | Backoff between those retries. | `account_state.py` |

## `risk`

Planner debit cap. This is **not** the Polymarket CLOB fee curve.

| Key | Role | Module |
|-----|------|--------|
| `target_notional` | Desired USDC stamped on `EnterIntent`. | `execution/planner.py`, `DecisionContext` |
| `maximum_total_debit` | Hard BUY cap. Tiny-live refuses `> 5`. Planner uses `min(target, cap)`. | `planner.py` `ExecutionRiskPolicy` |
| `fee_reserve_rate` | `spend = desired / (1 + rate)` so fees fit the cap. | `planner.py` |
| `kill_switch_active` | Host kill. Blocks new entry / forces the safety path. Not a strategy exit. | `runtime/capabilities.py` |

## `lifecycle`

Dust / prepare / refresh-wait leaves are **required** (no hidden defaults). `mandatory_exit_before_end_s` must be **greater than** `manual_deadline_before_end_s`.

| Key | Role | Module |
|-----|------|--------|
| `mandatory_exit_before_end_s` | Host `FlattenIntent` (`MANDATORY_FLATTEN`) while tau ≤ this. | `runtime/trading_runtime.py` |
| `manual_deadline_before_end_s` | Still exposed → `MANUAL_INTERVENTION` and stop. | `trading_runtime.py` |
| `evidence_timeout_s` | Wait for venue evidence after a POST. | `execution/lifecycle.py` |
| `reconciliation_interval_s` | REST reconcile cadence while a session is open/uncertain. | `execution/reconciliation.py` |
| `exit_retry_limit` | Max FAK sell retries. ≥ 1. | `lifecycle.py` |
| `exit_retry_budget_s` | Wall budget for those retries. | `lifecycle.py` |
| `minimum_exit_price` | Planner sell floor, in `(0, 1]`. | `execution/planner.py` |
| `sell_size_increment` | Round remaining shares **down** before POST (typically `0.01`). | `execution/exit_sizing.py` |
| `dust_abs_shares` | Classified leftover ≤ this (also vs venue `min_order_size`). | `exit_sizing.py`, `session_state.py` |
| `dust_max_notional` | Leftover × last bid ceiling for `COMPLETED_FLAT_WITH_DUST`. | `exit_sizing.py` |
| `prepare_budget_ms` | Abandon create/sign if slower; no POST (`PREPARE_BUDGET_EXCEEDED`). Must be **less than** `market.maximum_candidate_age_ms`. | gateway prepare |
| `refresh_wait_budget_ms` | If a wallet refresh is in-flight at submit, wait up to this then fail closed (`ACCOUNT_REFRESH_IN_FLIGHT`). `0` = do not wait. | gateway + `account_state.py` |

Shipped YAML sets `tau_min_s: 105` so the plugin band matches the host floor `mandatory_exit_before_end_s + evidence_timeout_s` (90+15). That 105 s figure is the **hard** BUY cutoff: consume, the final gate, and POST refuse a new BUY once remaining time is below it. It is operational policy, not a guarantee that prepare, POST, cancel, or flatten finish inside a reserved margin.

The strategy-facing window closes earlier:

```text
hard cutoff = max(tau_min_s, mandatory_exit_before_end_s + evidence_timeout_s)
soft cutoff = hard cutoff + entry_dispatch_margin_s
```

Shipped Ask70 sets `entry_dispatch_margin_s: 1.0`, so the hard cutoff stays 105 seconds and the strategy window closes at 106 seconds. `tau == 106` may still emit. `tau == 105` is inside the hard gate and outside the strategy window. A queued BUY with `tau < 105` is rejected before session creation. The margin never permits a later entry.

On a 300 s `btc_updown_5m` window with `tau_max_s=290`, hard cutoff 105, and a 1 s dispatch margin:

| Elapsed from `event_start` | tau | What is allowed |
|----------------------------|-----|-----------------|
| 0–10 s | 300–290 | No entry (`tau_max`) |
| 10–194 s | 290–106 | Entry if strategy + infrastructure are ready |
| 194–195 s | 106–105 | Strategy window closed. A BUY already queued is still inside the hard gate |
| 195–210 s | 105–90 | No new BUY. Existing exposure: strategy/protection exits still allowed |
| 210–255 s | 90–45 | Host `MANDATORY_FLATTEN` / drain. No BUY |
| 255–300 s | 45–0 | `MANUAL_INTERVENTION` if still exposed |

A stricter plugin `tau_min_s` (for example 120) raises the hard cutoff at consume and the BUY gate. The strategy window is that hard cutoff plus `entry_dispatch_margin_s`.

## ask70 parameters

`strategy.kind: ask70`. Six core fields are required. Maker fields are optional. After fill, ask70 HOLDs and emits **no exits** — SL/TP/trail must come from `protection:`.

| Key | Role | Module |
|-----|------|--------|
| `entry_ask_threshold` | Enter if YES or NO best ask ≥ this, in `(0, 1)`. | `strategies/ask70/strategy.py` |
| `leg_preference` | `first_hit`, `up_then_down`, `down_then_up`. | `ask70/strategy.py` |
| `max_price_pad` | Taker worst price = ask + pad. Final gate rejects if ask moved above that. | `EnterIntent.max_price` |
| `tau_min_s` / `tau_max_s` | Seconds remaining in the window. Host strategy window uses the soft cutoff. Ask70 also skips outside its own band. | plugin `entry_tau_bounds`, `trading_runtime.py` |
| `max_clock_uncertainty_ms` | Clock-sync ceiling. Host reads via `plugin.max_clock_uncertainty_ms`. There is no `market.max_clock_uncertainty_ms`. | plugin, `market_data_runtime.py` |
| `liquidity_role` | `taker` (FAK market buy) or `maker` (post-only GTC). Default `taker`. | `EnterIntent.liquidity_role` |
| `maker_limit_price` | Rest price, in `(0, 1)`. Only valid when `liquidity_role: maker`. | `EnterIntent.max_price` |

## feature_probe parameters

`strategy.kind: feature_probe`. Seam-proof plugin: books + spot L2 imbalance. Catalog file: [`feature_probe_catalog.yaml`](../../config/runs/feature_probe_catalog.yaml) (`emit_entry: false`, no `protection:`). Not the default CLI config.

| Key | Role | Module |
|-----|------|--------|
| `emit_entry` | Required explicit bool. `false`: SKIP/HOLD with feature evidence only. `true`: may emit one taker `EnterIntent` and **requires** a `protection:` block. | `feature_probe/strategy.py`, `run_config.py` |
| `tau_min_s` / `tau_max_s` | Host `entry_window_open` hook (same as ask70). | plugin `entry_tau_bounds` |
| `max_clock_uncertainty_ms` | Clock-sync ceiling. Host reads via the plugin. | plugin, `market_data_runtime.py` |
| `entry_ask_threshold` | Optional. Used only when `emit_entry: true`. Default `0.70`. | `feature_probe/strategy.py` |
| `max_price_pad` | Optional taker pad when entering. Default `0.08`. | `EnterIntent.max_price` |

## z_gap parameters

`strategy.kind: z_gap`. Omitted leaves use code defaults in `strategies/z_gap/config.py`. Do not paste these under `kind: ask70`.

### `volatility`

EWMA σ on Binance spot trades (`ZGapDriver`, `indicators/ewma_volatility.py`).

| Key | Role |
|-----|------|
| `half_life_s` | EWMA half-life. |
| `min_samples_s` | Warmup before σ is ready. |
| `jump_threshold_sigma` | Lockout if return > this × σ. |
| `sample_interval_s` | Sample cadence. |
| `tau_floor_s` | Floor for vol scaling with remaining time. |
| `sigma_floor` | Per-√second floor so quiet warmup cannot create a micro-σ jump lockout. |

### `entry`

When to emit `EnterIntent` (`strategies/z_gap/valuations.py`, `policies.py`).

| Key | Role |
|-----|------|
| `theta_take` | Minimum edge to take. |
| `theta_fill_floor` | Fill-quality floor. |
| `z_min` / `z_max` | Allowed \|z\| band. |
| `block_abs_z` | Reject extreme \|z\|. |
| `tau_min_s` / `tau_max_s` | Same **role** as ask70 tau (plugin `entry_tau_bounds`). Different YAML path. |
| `expected_slippage_buy` | Model buy slippage. |
| `exit_friction_reserve` | Optional extra exit friction in the entry test. |
| `require_repricing_edge` | Require repricing edge. |
| `tie_epsilon` | Near-tie → no entry. |
| `reject_both_legs_edge` | Reject when both legs look edged. |

### `realization`

Strategy **rich EXIT**. Not protection take-profit.

| Key | Role |
|-----|------|
| `theta_rich` | Rich-exit edge. |
| `min_exit_depth` | Depth check; `0` disables. |
| `expected_slippage_sell` | Model sell slippage. |
| `slippage_included_in_executable_bid` | Bid already embeds depth-walk slippage. |

### `thesis`

Strategy **thesis EXIT**. Not `protection.stop_loss`.

| Key | Role |
|-----|------|
| `p_stop` | Model probability threshold in `[0, 1]`. |
| `stop_confirm_s` | Confirm window (monotonic seconds). |
| `reset_on_stale_model` | Reset thesis state if the model goes stale. |

### `time_resolution`

Strategy **time EXIT**. Not `lifecycle.mandatory_exit_before_end_s`. `resolution_capability_default: true` is rejected at load (automatic redeem is not implemented).

| Key | Role |
|-----|------|
| `flatten_before_event_end_s` | Strategy flatten when tau ≤ this. |
| `sell_vs_resolve_margin` | Prefer sell vs hold-to-resolution by this margin. |
| `resolution_capability_default` | Must stay `false`. |
| `ponr_before_event_end_s` | Point of no return: τ ≤ this while resolution-pending blocks sell exits. |

### `ptb_time_quality`

PTB seal / clock eligibility. Does not relax book freshness or the final gate.

| Key | Role |
|-----|------|
| `basis_max_bps` | Max spot vs Chainlink basis at seal. |
| `max_ptb_lag_ms` | Allowed arrival delay for an immutable `EXACT_AT_START` PTB. |
| `max_clock_uncertainty_ms` | Same **host hook** as ask70 `max_clock_uncertainty_ms`. |

### `friction`

Model EV fees. **Not** `risk.fee_reserve_rate`.

| Key | Role |
|-----|------|
| `fee_curve.fee_rate` | CLOB-style fee rate inside fair value. |
| `fee_curve.exponent` | Fee-curve exponent. |
| `provisional_resolve_penalty_per_share` | Extra resolve penalty in the model. |

## Lookalikes — not duplicates

Setting one does not set the other.

| Looks similar | Actually |
|---------------|----------|
| `tau_min_s` / `tau_max_s` vs `lifecycle.mandatory_exit_before_end_s` vs z_gap `flatten_before_event_end_s` vs `market.entry_dispatch_margin_s` | Tau band = may we **enter**. The hard BUY cutoff is `max(plugin tau_min_s, mandatory_exit_before_end_s + evidence_timeout_s)` (shipped 90+15=105) at consume, the final gate, and POST. The strategy window closes at hard cutoff + `entry_dispatch_margin_s` (shipped Ask70: 106). That 105 s figure is operational policy, not an end-to-end latency SLA. Evidence timeout starts after `post_order` returns. Mandatory = **host** `FlattenIntent`. Z-gap flatten = **strategy** `ExitIntent`. Host flatten wins over strategy time-exit. SELL and protective exits ignore both entry cutoffs. |
| `protection.stop_loss` vs z_gap `thesis.p_stop` | Overlay watches held **bid** and FAK-sells. `p_stop` is a model probability for a strategy thesis exit. |
| `risk.fee_reserve_rate` vs z_gap `friction.fee_curve.fee_rate` | OMS haircut so the $5 cap still covers fees. Model CLOB curve inside z_gap fair value. Planner does not read the model rate. |
| `risk.target_notional` vs `risk.maximum_total_debit` | Desired vs hard cap. Planner uses `min(target, cap)`. |
| ask70 `max_clock_uncertainty_ms` vs z_gap `ptb_time_quality.max_clock_uncertainty_ms` | Same host hook (`plugin.max_clock_uncertainty_ms`), different YAML path. Not a `market:` field. |
| `protection.mark` vs `trailing.mark` | Both must be `bid`. Duplicate on purpose; trailing cannot choose a different mark yet. |
| `market.evaluation_interval_s` vs plugin `evaluate_on` coalesce | YAML replaces contract coalesce/timer in `EvaluationScheduler`. Protection still wakes on every book tick. |
| `dust_abs_shares` vs `sell_size_increment` vs venue `min_order_size` | Increment = how we round a sell. Dust = when leftover may remain. `min_order_size` is from the book. |
| `account.read_timeout_s` vs SDK HTTP timeout | Diagnostic only. Cancelling shared HTTP/2 from an outer timeout is unsafe. |
| `max_price_pad` vs `maker_limit_price` | Taker worst = ask + pad. Maker rest price is explicit. |

## Not in YAML (on purpose)

| Thing | Where it lives |
|-------|----------------|
| Fact ids, `evaluate_on`, adapters to start | Plugin `InputContract` — [strategy inputs](strategy_inputs.md) |
| `min_order_size`, `tick_size` | Discovery / `BookView` |
| `--live`, `--dotenv`, `--out-dir` | CLI `run` |
| `eval --fixture` | CLI `eval` — [extending.md](extending.md#lab-fixture-before-live) |
| `q_edge` parameters | Not a registered plugin |
