# Operations — guru follow (v1)

**Doc index:** [README.md](README.md) · **Architecture:** [Architecture.md](Architecture.md) · **Current state:** [Implementation/current_state.md](Implementation/current_state.md) · **Live deployment-budget checklist:** [Implementation/phase_b_operational_validation.md](Implementation/phase_b_operational_validation.md) · **CLI: deployment-budget live run:** [Runbooks/deployment_budget_live_validation.md](Runbooks/deployment_budget_live_validation.md) · **Strategy module:** [modules/strategy/README.md](modules/strategy/README.md)

## Config files

| File | Use |
|------|-----|
| `.env` | **Secrets only:** `POLYMARKET_PK`, `POLYMARKET_FUNDER`, `POLYMARKET_SIGNATURE_TYPE`, L2 API trio. Never commit. |
| `config/strategy/*.yaml` | Guru wallet, **`token_filter`** block (`enabled` + `allowlisted_token_ids`), `copy_scale`, optional strategy dedup path. |
| `config/risk/*.yaml` | Limits, kill switch, notional rules, optional **capital gate** (`capital_gate_enabled`, mins, snapshot ages). |
| `config/runtime/*.yaml` | `trader_id`, **`execution_mode`**, guru polling, logging, CLOB host/chain, **Polymarket / Nautilus flags**, optional **`reporting_enabled`** and **capital observability** keys (`reporting_capital_*` — see `CONFIG_MODEL.md`). |

Field-level reference: [`Docs/CONFIG_MODEL.md`](CONFIG_MODEL.md).

Starter files in-repo (replace guru wallet and token ids before relying on them):

- `config/strategy/guru_follow.yaml`
- `config/risk/guru_follow_risk.yaml`
- `config/runtime/live_polymarket.yaml` — **recommended:** set `guru_ingest_mode: rtds_primary` for live guru follow (see **Guru ingestion** below).
- `config/runtime/rtds_shadow.yaml` — dedicated **shadow** run: fresh `var/rtds_shadow/*` state + `guru_ingest_mode: rtds_shadow` for poll-vs-stream validation.

## Guru ingestion — `guru_ingest_mode`

Tyrex can ingest guru trades from **Polymarket RTDS** (`activity` / `trades`, unfiltered stream + client-side **`proxyWallet`** match to `guru_wallet_address`) and/or from the existing **Data API** poll path. The **`GuruTradeSignal`** topic and **`CopyStrategy`** contract are unchanged.

| `guru_ingest_mode` | Who publishes `GuruTradeSignal` | Typical use |
|--------------------|-----------------------------------|---------------|
| **`rtds_primary`** | **`GuruStreamActor`** (RTDS) when healthy; **`GuruMonitorActor`** (poll) **only** during configured fallback | **Default recommendation** for production: lower detection latency vs poll-only. |
| **`rtds_shadow`** | **Poll** publishes; stream logs **`guru_stream_would_emit`** only (compare coverage/timing) | Gate before enabling primary; use `config/runtime/rtds_shadow.yaml` for clean state. |
| **`poll_only`** | Poll only | Baseline / rollback / no WebSocket. |

**Wallet:** `guru_wallet_address` in strategy YAML must match RTDS payload **`proxyWallet`** (case-insensitive). Validate with:

```bash
python scripts/spike_rtds_activity.py --wallet 0xYourGuruFromStrategyYaml --duration 60
```

**Run examples (repo root, after `pip install -e .`):**

```bash
# Production-shaped: RTDS primary + live execution (runtime YAML must set guru_ingest_mode: rtds_primary)
python scripts/run_guru.py \
  --strategy-conf config/strategy/guru_follow.yaml \
  --risk-conf config/risk/guru_follow_risk.yaml \
  --live-conf config/runtime/live_polymarket.yaml

# RTDS shadow validation (isolated watermark/dedup)
python scripts/run_guru.py \
  --strategy-conf config/strategy/guru_follow.yaml \
  --risk-conf config/risk/guru_follow_risk.yaml \
  --live-conf config/runtime/rtds_shadow.yaml
```

**Post-run reports (Nautilus log, e.g. `logs/live/run_nautilus.log`):**

```bash
python scripts/guru_shadow_report.py logs/live/run_nautilus.log    # rtds_shadow: poll vs would_emit correlation_id
python scripts/guru_primary_report.py logs/live/run_nautilus.log   # rtds_primary: duplicate submits, fallback, gap-fill, latency stats
```

Dedup id when `transactionHash` is present: **`transactionHash:asset`** (multi-leg safe). See `guru_parse.ingest_source_trade_id`.

**Operational:** Unfiltered RTDS message rate is host-dependent; confirm CPU/memory during soak. Spike script estimates global msg/s; production path filters by guru wallet in-process.

## Run (after `pip install -e .`)

From repo root:

```bash
python scripts/run_guru.py ^
  --strategy-conf config/strategy/guru_follow.yaml ^
  --risk-conf config/risk/guru_follow_risk.yaml ^
  --live-conf config/runtime/live_polymarket.yaml
```

(Unix: line continuation with `\`.)

Optional: `TYREX_PM_DOTENV=/path/to/.env` to load a non-default env file.

### Guru run log files (default)

`scripts/run_guru.py` persists logs in **two** UTF-8 files per run (overwritten each time for the default stem), by **source** — **console output is unchanged.**

| Sink | Default path (`execution_mode`) | Contents |
|------|---------------------------------|----------|
| **Tyrex stdlib** | `logs/shadow/run_tyrex.log` or `logs/live/run_tyrex.log` | `tyrex_pm.*` package loggers only (compose summary, risk warnings, warmup, data API backoff, etc.). `%(message)s` style. **Not** a root catch-all: HTTP client noise (`httpx`) stays **out** of this file (console only unless you attach more handlers). |
| **Nautilus-native** | `logs/shadow/run_nautilus.log` or `logs/live/run_nautilus.log` | Framework file sink via :class:`~nautilus_trader.common.config.LoggingConfig` (`log_directory`, `log_file_name`, `log_level_file`, `clear_log_file`). Component / kernel / adapter lines (`GuruMonitorActor`, `CopyStrategy`, exec path, engines), same **family** as `TYREX-GURU-001.*` console lines. |

Directories are created automatically under the **repo root**.

**Named run (optional):** `--log-name NAME` → `logs/<mode>/NAME_tyrex.log` and `NAME_nautilus.log`. Same validation rules as `--help`. Invalid names exit before the node starts.

At startup, the script prints **two** lines, for example: `tyrex_pm logging to …/run_tyrex.log` and `nautilus logging to …/run_nautilus.log`.

**Gaps (by design):** `print`-only lines (banner, `phase_a:` hint, `Stopping…`) are **not** in either file. There is **no** full stdout transcript. Third-party loggers that only attach to the root logger (e.g. `HTTP Request`) appear on the **console** but are **not** written to `run_tyrex.log`.

**Durable guides:** [logging_system_guide.md](logging_system_guide.md) (sources, file roles, **where to add future logs**), [log_validation_playbook.md](log_validation_playbook.md) (commands + validation with both files).

## Token filter (strategy YAML)

| `token_filter.enabled` | Behavior |
|------------------------|----------|
| **`false`** | **Unfiltered:** strategy accepts **any** guru token id at the signal gate; **`risk`** and **execution** still apply. Use for fast iteration / shadow. |
| **`true`** | **Filtered:** only ids in `allowlisted_token_ids` (must be non-empty); others → `copy_skip` / `not_allowlisted`. Use for controlled follow / prod. |

Disabling the filter does **not** bypass risk limits or live execution policy.

## Follow sizing & conviction

**Config:** `config/strategy/*.yaml` — see [CONFIG_MODEL.md](CONFIG_MODEL.md) (`conviction_sizing_*`, `copy_scale`). **Per-order min/max notional** (too small / too big / clip / bump) live only in **risk** YAML (`min_notional_usd_per_order`, `min_notional_policy`, `max_notional_usd_per_order`, `max_notional_policy`).

**What changes:** For **BUY entries**, optional **conviction-weighted** scale vs a rolling average of guru sizes (accepted entry path only). There is **no** strategy-stage “minimum follow notional”; small-trade handling is entirely via risk (`deny` vs `cap` on the minimum).

**Conservative enablement:** Leave `conviction_sizing_enabled: false` until sizing is understood in shadow; then enable with a modest `conviction_sizing_cap`.

**Logs:** `copy_conviction_diag` (**DEBUG**), `copy_skip` when strategy or risk rejects.

## Execution quality (live book hooks)

**Config:** `config/runtime/*.yaml` — `execution_*` fields in [CONFIG_MODEL.md](CONFIG_MODEL.md).

**Path gate:** Optional book hooks apply on **`execution_mode: live`** (`NautilusGuruExecutionPort` / framework `submit_order`).

**What changes (operator view):** Optional pre-submit checks against the book — slippage guard vs guru reference, optional clip to top-of-book depth, optional timeout cancel on working limits (timers use `CopyStrategy.on_order_event` → `notify_order_event` on the port). **Not configurable:** limit price and quantity are always snapped to the instrument tick / size step before submit (internal grid fit; no “alignment mode” knob).

**Conservative enablement:** Turn on **one** `execution_*` feature at a time; watch **`exec_*`** lines in `run_nautilus.log`. Shadow mode does **not** hit the venue book — validate on a **small live** session or rely on unit/integration tests (`tests/unit/test_c3_execution.py`, `tests/test_nautilus_guru_exec.py`).

## Modes

| `execution_mode` | Behavior |
|------------------|----------|
| **`shadow`** | Risk active. **`NoOpExecutionPort`** — **no CLOB / no framework orders**. Logs `shadow_order_intent`. |
| **`live`** | Risk active. **`NautilusGuruExecutionPort`** → Nautilus **`submit_order`** (Polymarket data + exec clients on the node). Strategy logs `live_order_intent` when an intent reaches the port; guru orders appear in the Nautilus `Cache` when the adapter accepts them. |

**Zero-bootstrap:** Empty `polymarket_instrument_ids` on **live** implies **dynamic** resolution (+ optional `polymarket_startup_token_warmup_max` warmup). See `runtime/guru_instrument_dynamic.py`.

**Obsolete YAML:** `polymarket_nautilus_live` and `polymarket_framework_submit` are **removed** — the loader raises if either key is present.

### Deployment-budget risk (live framework)

**Deployment budget:** Portfolio and token caps use **USD deployed** (pending rests + filled cost basis), not marked exposure. See `Docs/CONFIG_MODEL.md` § Risk and `runtime/deployment_budget.py`.

**Enforcement:** `ConfiguredRiskPolicy.evaluate` (`src/tyrex_pm/risk/configured.py`) with execution/position readers and `NautilusDeploymentBudget` from `build_guru_trading_node`. There are **no silent skips**: misconfigured framework-only gates or reserve in shadow fail at **startup** (`ValueError`), not at runtime.

**Framework-truth path** (required for finite portfolio cap, concurrent guru rests, and collateral reserve):  
`execution_mode=live` — same predicate as `framework_phase_b_eligible` in `config/loaders.py`.

| Runtime posture | Finite `max_portfolio_notional_usd_open` | `max_concurrent_guru_resting_orders` | `collateral_reserve_usd > 0` |
|-----------------|------------------------------------------|--------------------------------------|------------------------------|
| **Shadow** | **Invalid** — `build_guru_trading_node` raises | **Invalid** — same | **Invalid** — same (no live py-clob snapshot on node) |
| **Live** | **Enforced** — deny when `portfolio_deploy + order_deploy > C` | **Enforced** — guru resting count via `state_readers.is_guru_resting_order` / `count_guru_resting_orders_open` | **Enforced** — BUY: `balance >= reserve + n` |

**Unresolved deployment:** With **finite** portfolio cap, `fail_on_unresolved_portfolio_deployment: true` (default) denies when the portfolio **filled** sum cannot be computed (`RISK_PORTFOLIO_DEPLOYMENT_UNRESOLVED`). When **false**, unresolvable legs count as **0** (underestimate). Per-token: `fail_on_unresolved_token_deployment`. Portfolio cap **off** (`inf`): these flags do not apply to portfolio math.

**Startup visibility:** After guru + strategy registration, `tyrex_pm.runtime.guru_compose` logs one **INFO** line: `tyrex_pm phase_b: framework_truth_eligible=… deployment_budget_wired=… portfolio_deployment_cap_usd=… max_concurrent_guru_resting_orders=… fail_on_unresolved_portfolio_deployment=… fail_on_unresolved_token_deployment=… collateral_reserve_usd=… capital_gate_enabled=…`.  
(`phase_b` is the **logger message prefix** for this compose summary — not a separate runtime mode.)  
`scripts/run_guru.py` sets the `tyrex_pm` logger to **INFO** (and calls `basicConfig` if the root logger has no handlers) so this line appears without extra operator setup.

**Backlog (not in code unless you add it):** e.g. cooldowns, per-cycle follow caps — do not assume they exist.

**Before risk tuning or framework go-live:** read `Implementation/phase_b_operational_validation.md` — restart reality (`load_state=false`), open-order / position reconciliation, and how deployment denials appear in logs.

#### Risk `ReasonCode` strings (operator cheat sheet)

| Code | Meaning | What to check |
|------|---------|---------------|
| `RISK_ORDER_DEPLOYMENT_EXCEEDED` | Per-order deploy above `max_notional_usd_per_order` and **`max_notional_policy: deny`**. With **`cap`** (default), risk **clips** quantity instead — see reporting / `risk_decision` deploy-adjust fields. | If denying: raise cap or switch to `cap`. If clipping: expected when guru-sized deploy exceeds follower max. |
| `RISK_MIN_ORDER_NOTIONAL` | **BUY:** deploy below `min_notional_usd_per_order` and **`min_notional_policy: deny`** (default). With **`min_notional_policy: cap`**, risk **bumps** qty up when feasible. | Business floor — not venue `min_quantity`; venue grid issues → `exec_instrument_quantize_skip` or lifecycle **DENIED** after submit. |
| `RISK_ORDER_DEPLOYMENT_INFEASIBLE` | Min **bump** and max **clip** cannot both be satisfied (e.g. min \> max after policies). | Loosen min/max or policies. |
| `RISK_TOKEN_DEPLOYMENT_EXCEEDED` | Per-token: existing deployment + this order \> `max_token_notional_usd_open`. | Rests + open position on that outcome; cap. |
| `RISK_PORTFOLIO_DEPLOYMENT_EXCEEDED` | Portfolio: total deployment + this order \> `max_portfolio_notional_usd_open`. | All tokens pending+filled; cap. |
| `RISK_TOKEN_DEPLOYMENT_UNRESOLVED` / `RISK_PORTFOLIO_DEPLOYMENT_UNRESOLVED` | Strict: cannot parse **filled** deployment for token or portfolio. | Nautilus positions / avg open; or relax `fail_on_unresolved_*_deployment`. |
| `RISK_PORTFOLIO_NOTIONAL_CAP_EXCEEDED` | **Legacy** value; same business row as portfolio deployment cap in old telemetry. | Same as `RISK_PORTFOLIO_DEPLOYMENT_EXCEEDED`. |
| `RISK_PORTFOLIO_EXPOSURE_UNRESOLVED` | **Legacy** — marked-exposure path removed; may appear only when reading **old** artifacts. | Prefer `RISK_PORTFOLIO_DEPLOYMENT_UNRESOLVED` for new runs. |
| `RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT` | Open guru rests at/over `max_concurrent_guru_resting_orders`. | Concurrency cap; cancel or wait. |
| `RISK_INSUFFICIENT_FREE_COLLATERAL_AFTER_RESERVE` | py-clob **balance** \< `reserve + n` on **BUY** (collateral reserve). | Collateral, reserve, notional. |

For capital-gate codes (`RISK_ACCOUNT_UNAVAILABLE`, `RISK_ALLOWANCE_UNAVAILABLE`, insufficient balance/allowance), see **Capital gate** below and `reason_codes.py`.

### Capital gate (`config/risk/*.yaml`)

When **`capital_gate_enabled: true`** (live):

- Risk requires **`Portfolio.account(POLYMARKET)`** present (refreshed on a TTL).
- If **`min_collateral_balance_usd`** or **`min_allowance_usd`** is set, risk reads **py-clob** `get_balance_allowance` (same source as `verify_polymarket_auth` pattern) and **fails closed** on missing / insufficient / unparseable values when required.

### After restart

`TradingNode` is built with **`load_state=False`, `save_state=False`**. Expect **adapter-driven** reconciliation; **`Portfolio` / open orders** may be incomplete briefly. Optional warmup seeds `Cache` from guru activity. **Normal:** provider “no load_ids” warnings for empty lists. **Investigate:** persistent `risk_denied` with capital codes after long idle — refresh snapshots / check `.env`.

## Before live

1. Complete auth verification: `python scripts/verify_polymarket_auth.py`.
2. Supervised order smoke: `examples/order_lifecycle_smoke.py` and `Docs/Runbooks/order_lifecycle_v1_02.md`.
3. Set conservative **`risk`** YAML (`max_*`, `kill_switch` test).
4. Set `execution_mode: live` only in **runtime** YAML — strategy and risk files unchanged.
5. If **`token_filter.enabled: true`**, confirm listed token ids match resolution / CLOB `asset` strings.
6. Ensure `var/` (dedup state) is writable.

## Environment variables (non-secret / tooling)

- Minimum **BUY** notional for Tyrex live/shadow: **`min_notional_usd_per_order`** in **risk** YAML (default **`0`** = no floor). Execution normalization no longer reads `TYREX_MIN_BUY_NOTIONAL_USD`.
- Smoke / tooling vars: `Docs/Runbooks/order_lifecycle_v1_02.md`, `examples/order_lifecycle_smoke.py` (example script may still reference env for local experiments).

## Logs to grep

| `event=` | Meaning |
|----------|---------|
| `guru_signal_emitted` | New deduped guru trade on the bus; `source=rtds` (stream), `source=poll` (monitor), or `source=gap_fill` — see `component=guru_ingest` |
| `guru_stream_would_emit` | **Shadow only:** stream would publish this `correlation_id` (compare to poll `guru_signal_emitted`) |
| `guru_stream_start` / `guru_rtds_*` | Stream connect, subscribe, reconnect, stall, disconnect |
| `guru_ingest_fallback_activation` / `guru_ingest_fallback_cleared` | **`rtds_primary`:** poll takeover / cleared when stream healthy again |
| `guru_gap_fill` / `guru_gap_fill_begin` / `guru_gap_fill_error` | REST gap-fill after reconnect |
| `guru_poll_tick` | Poll cycle (`phase=on_start`, `timer`, or `sub=fetch`) |
| `guru_poll_error` | Data API failure for one poll (actor survives, see backoff) |
| `guru_poll_error_backoff` | Sleep before next retry after errors |
| `copy_skip` | Strategy dropped signal (token filter, zero qty, risk denied, …) or conviction diagnostics context on skips. Per-order size floors/ceilings are applied in **risk**, not as a separate strategy “worthiness” gate. |
| `copy_conviction_diag` | **DEBUG:** per accepted **entry** when `conviction_sizing_enabled` — ratio and scale diagnostics (grep only when log level allows). |
| `shadow_order_intent` | Shadow mode: intent reached execution port (no venue I/O) |
| `live_order_intent` | Live mode: strategy forwarded intent to execution policy |
| `live_order_submit` | Legacy py-clob path: post succeeded |
| `live_order_error` | Legacy path: CLOB / policy error |
| `LIVE_ORDER_SUBMIT` / `LIVE_ORDER_ERROR` | Framework guru path (`nautilus_guru_exec`): structured **`event=`** with **`ReasonCode`** |
| **`exec_entry_guard_skip`** / **`exec_book_unavailable_skip`** / **`exec_instrument_quantize_skip`** | Execution submit prep — book or instrument-grid skip (**not** strategy sizing). Legacy logs may show **`exec_venue_normalize_skip`**. |
| **`exec_depth_clip_applied`** | Intended vs clipped qty logged at **INFO**. |
| **`exec_limit_timeout_cancel`** | Working limit canceled after **`execution_limit_timeout_seconds`**. |
| `GURU_*` / `RISK_*` in reason | Dynamic resolve, instrument cache, capital gate — see `core/reason_codes.py` |
| `strategy_started` | Strategy boot |
| `tyrex_pm phase_b:` (logger **INFO**, `tyrex_pm.runtime.guru_compose`) | One-line **compose summary** of deployment-budget / gate settings after node wiring |
| **`tyrex_risk_ops`** (logger **INFO**, `tyrex_pm.risk.configured`) | Risk deny detail: `gate=…`, `correlation_id`, deployment / cap / reserve / concurrent context — **grep alongside** `copy_skip` |

Risk denials appear on `copy_skip` with `reason_code=risk_denied` and the policy reason string; use **`tyrex_risk_ops`** for **why** (marks, caps, counts, balances).

## Rollout validation (baseline vs canary)

Use the same guru + risk YAML; change **one** surface at a time (ingest mode → follow sizing → execution hooks).

1. **Ingest:** Run **`rtds_shadow`** (`rtds_shadow.yaml`) and compare poll vs stream with `guru_shadow_report.py`. Then **`rtds_primary`** and `guru_primary_report.py` (fallback, gap-fill, duplicate checks). Spike: `scripts/spike_rtds_activity.py`.
2. **Deployment-budget risk:** Confirm `tyrex_pm phase_b:` startup line matches intent. Grep **`tyrex_risk_ops`** + `RISK_*` during a short live session. Follow **`phase_b_operational_validation.md`** before scaling.
3. **Sizing:** In **shadow**, enable conviction / min-notional incrementally; inspect `copy_skip` and (if DEBUG) `copy_conviction_diag`. Unit tests: `tests/unit/test_c2_capital_allocation.py`, `tests/unit/test_copy_strategy_shadow.py`.
4. **Execution hooks:** Requires **live**. Enable **one** `execution_*` flag per canary; grep **`exec_*`** / **`LIVE_ORDER_*`** in `run_nautilus.log`. Tests: `tests/unit/test_c3_execution.py`.

**Baseline vs canary:** Keep a **baseline** Nautilus log from a known-good run; for each change, diff event rates (`guru_signal_emitted`, `copy_skip`, `shadow_order_intent` / `live_order_intent`, `exec_*`, `RISK_*`).

## Troubleshooting

- **No `shadow_order_intent` / `live_order_intent`:** if filtered mode, check `token_filter` vs guru `asset`; if unfiltered, look for `zero_qty`, `risk_denied`, etc.
- **Guru polling:** follower uses **`GET /activity`** (`type=TRADE`) with a **watermark** (`guru_state_path`), not full `/trades` history. `guru_startup_backfill_seconds: 0` means only trades **after** the first boot watermark; increase for a short warm-up window. On API errors see **`guru_poll_error`** / **`guru_poll_error_backoff`** (the bot keeps running).
- **Guru duplicates:** dedup store (`guru_dedup_state_path`); delete file for full replay in dev only. Watermark file controls incremental progress (`guru_state_path`).
- **RTDS primary but no `guru_signal_emitted source=rtds`:** verify `guru_wallet_address` matches RTDS **`proxyWallet`** (spike script); check `guru_rtds_*` / fallback lines in Nautilus log; confirm `guru_ingest_mode` and network reachability to `guru_rtds_url`.
- **Live immediate rejects:** `live_order_error` (py-clob) or **`LIVE_ORDER_ERROR`** / venue message (framework). Check **`min_notional_usd_per_order`** (risk deny **`RISK_MIN_ORDER_NOTIONAL`** before submit), **`exec_instrument_quantize_skip`** (grid / venue min-q vs risk qty), **capital gate** reasons, balance/allowance, and **“orderbook does not exist”** (venue — market/token may be inactive).
- **Risk denies with `risk_*` / `RISK_*`:** Tyrex `ConfiguredRiskPolicy` (limits, capital gate, unresolved position when configured) — not the same as Nautilus **RiskEngine** denials logged by the adapter.
- **Config validation errors:** messages cite the YAML path and field; see `CONFIG_MODEL.md`.

## Structured reporting (observability)

When **`reporting_enabled: true`** in runtime YAML, each run writes **`var/reporting/runs/<run_id>/`** (`manifest.json`, `facts.jsonl`, …). By default **`run_id`** is a random UUID (unique folder per run). Pass **`--reporting-run-id my-label`** to `scripts/run_guru.py` to use a readable folder name instead (same safe character rules as **`--log-name`**: letters, digits, `._-` between segments). Reusing the same id **overwrites** that directory on the next run.

- **Field reference:** [`reporting_fact_model.md`](reporting_fact_model.md) — join keys, fact semantics, capital fields overview.
- **Post-run:** from repo root, `python -m tyrex_pm.reporting summarize --run-dir var/reporting/runs/<run_id>` produces **`summary.json`** / **`summary.md`** (capital rollups, guru-vs-us, execution histograms).
- **Capital gate off:** risk may still **approve** without pre-venue wallet checks; facts record **`capital_gate_enabled: false`** and observability mode; venue may still **DENIED** for insufficient balance—compare **`balance_canonical_usd`** to lifecycle `reason` text.
