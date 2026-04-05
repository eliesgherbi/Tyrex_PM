# Operations — guru follow (v1)

**Doc index:** [README.md](README.md) · **Architecture:** [Architecture.md](Architecture.md) · **Current state:** [Implementation/current_state.md](Implementation/current_state.md) · **Phase B live validation:** [Implementation/phase_b_operational_validation.md](Implementation/phase_b_operational_validation.md) · **Tests vs live gaps:** [Implementation/phase_ab_test_validation_matrix.md](Implementation/phase_ab_test_validation_matrix.md) · **Strategy module:** [modules/strategy/README.md](modules/strategy/README.md)

## Config files

| File | Use |
|------|-----|
| `.env` | **Secrets only:** `POLYMARKET_PK`, `POLYMARKET_FUNDER`, `POLYMARKET_SIGNATURE_TYPE`, L2 API trio. Never commit. |
| `config/strategy/*.yaml` | Guru wallet, **`token_filter`** block (`enabled` + `allowlisted_token_ids`), `copy_scale`, optional strategy dedup path. |
| `config/risk/*.yaml` | Limits, kill switch, notional rules, optional **capital gate** (`capital_gate_enabled`, mins, snapshot ages). |
| `config/runtime/*.yaml` | `trader_id`, **`execution_mode`**, guru polling, logging, CLOB host/chain, **Polymarket / Nautilus flags** (`polymarket_nautilus_live`, `polymarket_framework_submit`, instrument lists, dynamic / warmup — see YAML comments). |

Field-level reference: [`Docs/CONFIG_MODEL.md`](CONFIG_MODEL.md).

Starter files in-repo (replace guru wallet and token ids before relying on them):

- `config/strategy/guru_follow.yaml`
- `config/risk/guru_follow_risk.yaml`
- `config/runtime/live_polymarket.yaml` — **recommended:** set `guru_ingest_mode: rtds_primary` for live guru follow (see **Guru ingestion (C1)** below).
- `config/runtime/rtds_shadow.yaml` — dedicated **shadow** run: fresh `var/rtds_shadow/*` state + `guru_ingest_mode: rtds_shadow` for poll-vs-stream validation.

**C1 detail & validation:** [Implementation/plan_C1_Time-to-Follow.md](Implementation/plan_C1_Time-to-Follow.md) · [Implementation/c1_shadow_run_guide.md](Implementation/c1_shadow_run_guide.md)

## Guru ingestion (C1) — `guru_ingest_mode`

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

# C1 shadow validation (isolated watermark/dedup)
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
| **Tyrex stdlib** | `logs/shadow/run_tyrex.log` or `logs/live/run_tyrex.log` | `tyrex_pm.*` package loggers only (Phase B summary, risk warnings, warmup, data API backoff, etc.). `%(message)s` style. **Not** a root catch-all: HTTP client noise (`httpx`) stays **out** of this file (console only unless you attach more handlers). |
| **Nautilus-native** | `logs/shadow/run_nautilus.log` or `logs/live/run_nautilus.log` | Framework file sink via :class:`~nautilus_trader.common.config.LoggingConfig` (`log_directory`, `log_file_name`, `log_level_file`, `clear_log_file`). Component / kernel / adapter lines (`GuruMonitorActor`, `CopyStrategy`, exec path, engines), same **family** as `TYREX-GURU-001.*` console lines. |

Directories are created automatically under the **repo root**.

**Named run (optional):** `--log-name NAME` → `logs/<mode>/NAME_tyrex.log` and `NAME_nautilus.log`. Same validation rules as `--help`. Invalid names exit before the node starts.

At startup, the script prints **two** lines, for example: `tyrex_pm logging to …/run_tyrex.log` and `nautilus logging to …/run_nautilus.log`.

**Gaps (by design for this step):** `print`-only lines (banner, `phase_a:` hint, `Stopping…`) are **not** in either file. There is **no** full stdout transcript. Third-party loggers that only attach to the root logger (e.g. `HTTP Request`) appear on the **console** but are **not** written to `run_tyrex.log`.

**Durable guides:** [logging_system_guide.md](logging_system_guide.md) (sources, file roles, **where to add future logs**), [log_validation_playbook.md](log_validation_playbook.md) (commands + Phase A/B validation with both files).

## Token filter (strategy YAML)

| `token_filter.enabled` | Behavior |
|------------------------|----------|
| **`false`** | **Unfiltered:** strategy accepts **any** guru token id at the signal gate; **`risk`** and **execution** still apply. Use for fast iteration / shadow. |
| **`true`** | **Filtered:** only ids in `allowlisted_token_ids` (must be non-empty); others → `copy_skip` / `not_allowlisted`. Use for controlled follow / prod. |

Disabling the filter does **not** bypass risk limits or live execution policy.

## Follow sizing & worthiness (C2)

**Config:** `config/strategy/*.yaml` — see [CONFIG_MODEL.md](CONFIG_MODEL.md) (`conviction_sizing_*`, `min_follow_notional_usd`, `copy_scale`).

**What changes:** For **BUY entries**, optional **conviction-weighted** scale vs a rolling average of guru sizes (accepted entry path only). Optional **minimum follow notional** skips small intents **before** risk — reasons `min_follow_notional` or `min_follow_notional_price_missing` on `copy_skip` (not Phase B `risk_denied`).

**Conservative enablement:** Leave `conviction_sizing_enabled: false` until sizing is understood in shadow; then enable with a modest `conviction_sizing_cap`. Use `min_follow_notional_usd > 0` only when you want hard policy drops for tiny follows.

**Logs:** `copy_conviction_diag` (**DEBUG**), `copy_skip` with C2 reason codes (see table below). **Design / validation:** [Implementation/plan_C2_Capital-Allocation.md](Implementation/plan_C2_Capital-Allocation.md), [Implementation/c2_validation_readiness_review.md](Implementation/c2_validation_readiness_review.md).

## Execution quality (C3)

**Config:** `config/runtime/*.yaml` — `execution_*` fields in [CONFIG_MODEL.md](CONFIG_MODEL.md).

**Path gate:** C3 applies only when **`polymarket_nautilus_live: true`** and **`polymarket_framework_submit: true`** — i.e. **`NautilusGuruExecutionPort`**. The legacy **`PolymarketExecutionPolicy`** (py-clob) path does **not** run C3 logic.

**What changes (operator view):** Optional pre-submit checks against the book — tick/size normalization **without** increasing quantity above what risk already approved, optional slippage guard vs guru reference, optional clip to top-of-book depth, optional timeout cancel on working limits (timers use `CopyStrategy.on_order_event` → `notify_order_event` on the port).

**Conservative enablement:** Turn on **one** `execution_*` feature at a time; watch **`exec_*`** lines in `run_nautilus.log`. Shadow mode does **not** hit the venue book — validate C3 on a **small live framework** session or rely on unit/integration coverage. **Design:** [Implementation/plan_C3_Execution-Quality.md](Implementation/plan_C3_Execution-Quality.md).

## Modes

| `execution_mode` | Behavior |
|------------------|----------|
| **`shadow`** | Risk active. **`NoOpExecutionPort`** — **no CLOB / no framework orders**. Logs `shadow_order_intent`. |
| **`live`** | Risk active. **Execution path depends on runtime flags** (see below). Strategy still logs `live_order_intent` when an intent reaches the port. |

### Live execution paths (runtime YAML)

| Configuration | Submit path | Orders in Nautilus `Cache` | Typical logs |
|---------------|-------------|----------------------------|--------------|
| `polymarket_nautilus_live: false` (or default) | **`PolymarketExecutionPolicy`** → py-clob `create_and_post_order` | **No** (guru orders not in kernel cache) | `live_order_submit` / `live_order_error` from py-clob policy |
| `polymarket_nautilus_live: true` + **`polymarket_framework_submit: false`** | py-clob policy (same as left) while node may still run data/exec clients | Mixed / not authoritative for guru submits | Same |
| `polymarket_nautilus_live: true` + **`polymarket_framework_submit: true`** | **`NautilusGuruExecutionPort`** → **`submit_order`** | **Yes** for guru framework orders | `event=LIVE_ORDER_SUBMIT` / guru `ReasonCode` from `nautilus_guru_exec`; venue/engine may still emit other errors |

**Zero-bootstrap:** Empty `polymarket_instrument_ids` is **allowed** only with **live + Nautilus live + framework submit**; Tyrex then uses **dynamic** resolution (+ optional `polymarket_startup_token_warmup_max` warmup). See `Implementation/step_5_runtime_integration.md`.

### Phase B — product gates (B0–B4 implemented)

**Normative plan:** `Implementation/Phase_B_planing.md`. **Enforcement:** `ConfiguredRiskPolicy.evaluate` (`src/tyrex_pm/risk/configured.py`) with readers/aggregator injected from `build_guru_trading_node`. There are **no silent skips**: misconfigured framework gates or reserve in shadow fail at **startup** (`ValueError`), not at runtime.

**Framework-truth path** (required for B2 portfolio cap and B3 concurrent guru rests):  
`execution_mode=live` **and** `polymarket_nautilus_live=true` **and** `polymarket_framework_submit=true` — same predicate as `framework_phase_b_eligible` in `config/loaders.py`.

| Runtime posture | B2 `max_portfolio_notional_usd_open` (finite) | B3 `max_concurrent_guru_resting_orders` | B4 `collateral_reserve_usd > 0` |
|-----------------|-----------------------------------------------|----------------------------------------|-----------------------------------|
| **Shadow** | **Invalid** — `build_guru_trading_node` raises | **Invalid** — same | **Invalid** — same (no live py-clob snapshot on node) |
| **Live legacy** (no framework triple: missing any of the three flags above) | **Invalid** if enabled in YAML — compose raises | **Invalid** if enabled — compose raises | **Allowed** if `capital_gate_enabled: true` (reserve uses py-clob **balance** snapshot; see plan §6) |
| **Live + framework triple** | **Enforced** — `NautilusPortfolioExposureAggregator` (B1) must be wired; deny when `E_portfolio + n > C` | **Enforced** — guru resting count via `state_readers.is_guru_resting_order` / `count_guru_resting_orders_open` | **Enforced** after Phase A mins — BUY: `balance >= reserve + n` |

**Upstream-dependent (not Tyrex bugs by themselves):** B1/B2 depend on `Cache` / quotes / `Portfolio.net_exposure` as documented in `phase_a_closure.md` and Phase B §4–§6. B3 guru identity: tier 1 `guru_cid=` tags on order snapshots when present; tier 3 `TX` + 26 hex (`nautilus_guru_exec`) if not.

**Settings that look related but are inert without a gate:**

- `fail_on_unresolved_portfolio_exposure` only affects **B2** when `max_portfolio_notional_usd_open` is **finite**. If the portfolio cap is off (`inf`/unlimited), changing this flag does not change behavior.
- B2 **never** approves on an **incomplete** B1 aggregate (`complete=false` or no `e_portfolio`), even when `fail_on_unresolved_portfolio_exposure=false`; the “unsafe” mode only allows a **complete** aggregate with partial marks (warning + possible underestimate), per plan §4.

**Startup visibility (B5):** After guru + strategy registration, `tyrex_pm.runtime.guru_compose` logs one **INFO** line: `tyrex_pm phase_b: framework_truth_eligible=… b1_aggregator_wired=… portfolio_notional_cap_usd=… max_concurrent_guru_resting_orders=… fail_on_unresolved_portfolio_exposure=… collateral_reserve_usd=… capital_gate_enabled=…`.  
`scripts/run_guru.py` sets the `tyrex_pm` logger to **INFO** (and calls `basicConfig` if the root logger has no handlers) so this line appears without extra operator setup.

**Phase C (split):** **C1** ingest, **C2** follow sizing/worthiness, and **C3** execution-quality MVP are **implemented** (see § **Follow sizing (C2)** and **Execution quality (C3)** above, and `Implementation/current_state.md`). Items that are **still not** Tyrex product defaults — e.g. cooldowns, per-cycle follow caps, broader “venue normalize” beyond C3 MVP — remain **design backlog** in `Phase_B_planing.md` §13; do not assume they exist in code.

**Before risk tuning or framework go-live:** read `Implementation/phase_b_operational_validation.md` — restart reality (`load_state=false`), **B2** dependence on **marks** for every non-flat instrument, **`E_portfolio = E_pending + abs(E_filled_net)`** (plan §4.3), and how to interpret **`RISK_PORTFOLIO_EXPOSURE_UNRESOLVED`** in long live runs.

#### Phase B risk `ReasonCode` strings (operator cheat sheet)

| Code | Meaning | What to check |
|------|---------|---------------|
| `RISK_PORTFOLIO_NOTIONAL_CAP_EXCEEDED` | **Hard deny** — measured `E_portfolio + n` exceeds `max_portfolio_notional_usd_open`. | Intended cap; reduce exposure or raise cap (consciously). |
| `RISK_PORTFOLIO_EXPOSURE_UNRESOLVED` | **Fail-closed data / aggregation** — B1 snapshot incomplete, missing `e_portfolio`, or (with strict defaults) unresolved marks. **Not** “market said no.” | `Cache` instruments/quotes, adapter marks, B1 warnings in logs; see `portfolio_exposure` + `fail_on_unresolved_portfolio_exposure`. |
| `RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT` | **Hard deny** — open guru-origin resting orders already at/over `max_concurrent_guru_resting_orders`. | Expected concurrency cap; resolve or cancel rests before new guru submits. |
| `RISK_INSUFFICIENT_FREE_COLLATERAL_AFTER_RESERVE` | **Hard deny** — B4: py-clob collateral **balance** \< `collateral_reserve_usd + n` on **BUY**. | USDC collateral, reserve setting, intent notional; canonical balance source is py-clob `get_balance_allowance` shape (not `Portfolio.account` dict). |

For Phase A capital codes (`RISK_ACCOUNT_UNAVAILABLE`, `RISK_ALLOWANCE_UNAVAILABLE`, insufficient balance/allowance), see **Capital gate** below and `reason_codes.py`.

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

- `TYREX_MIN_BUY_NOTIONAL_USD` — minimum BUY notional guard in live execution (default `1`).
- Smoke / tooling vars: `Docs/Runbooks/order_lifecycle_v1_02.md`, `examples/order_lifecycle_smoke.py`.

## Logs to grep

| `event=` | Meaning |
|----------|---------|
| `guru_signal_emitted` | New deduped guru trade on the bus; `source=rtds` (stream), `source=poll` (monitor), or `source=gap_fill` — see `component=guru_ingest` |
| `guru_stream_would_emit` | **Shadow only:** stream would publish this `correlation_id` (compare to poll `guru_signal_emitted`) |
| `guru_stream_start` / `guru_rtds_*` | Stream connect, subscribe, reconnect, stall, disconnect (C1) |
| `guru_ingest_fallback_activation` / `guru_ingest_fallback_cleared` | **`rtds_primary`:** poll takeover / cleared when stream healthy again |
| `guru_gap_fill` / `guru_gap_fill_begin` / `guru_gap_fill_error` | REST gap-fill after reconnect |
| `guru_poll_tick` | Poll cycle (`phase=on_start`, `timer`, or `sub=fetch`) |
| `guru_poll_error` | Data API failure for one poll (actor survives, see backoff) |
| `guru_poll_error_backoff` | Sleep before next retry after errors |
| `copy_skip` | Strategy dropped signal (token filter, zero qty, **C2** min-notional / missing price for that gate, risk denied, …). **C2** worthiness skips (INFO): `reason_code=min_follow_notional` or `min_follow_notional_price_missing` with `base_scale`, `effective_scale`, `guru_size_raw`, `rolling_avg_guru_size`, `estimated_notional_usd`. |
| `copy_conviction_diag` | **DEBUG:** per accepted **entry** when `conviction_sizing_enabled` — ratio and scale diagnostics (grep only when log level allows). |
| `shadow_order_intent` | Shadow mode: intent reached execution port (no venue I/O) |
| `live_order_intent` | Live mode: strategy forwarded intent to execution policy |
| `live_order_submit` | Legacy py-clob path: post succeeded |
| `live_order_error` | Legacy path: CLOB / policy error |
| `LIVE_ORDER_SUBMIT` / `LIVE_ORDER_ERROR` | Framework guru path (`nautilus_guru_exec`): structured **`event=`** with **`ReasonCode`** |
| **`exec_entry_guard_skip`** / **`exec_book_unavailable_skip`** / **`exec_venue_normalize_skip`** | **C3** framework path — execution-quality skip (not risk / not C2); see `Implementation/plan_C3_Execution-Quality.md`. |
| **`exec_depth_clip_applied`** | **C3:** intended vs clipped qty logged at **INFO**. |
| **`exec_limit_timeout_cancel`** | **C3:** working limit canceled after **`execution_limit_timeout_seconds`**. |
| `GURU_*` / `RISK_*` in reason | Dynamic resolve, instrument cache, capital gate — see `core/reason_codes.py` |
| `strategy_started` | Strategy boot |
| `tyrex_pm phase_b:` (logger **INFO**, `tyrex_pm.runtime.guru_compose`) | **B5** one-line summary of Phase B gate settings after node wiring |
| **`tyrex_risk_ops`** (logger **INFO**, `tyrex_pm.risk.configured`) | **B1/B2/B3/B4 / capital** deny detail: `gate=…`, `correlation_id`, B1 flags / `b1_error=…`, numeric cap / reserve / concurrent context — **grep alongside** `copy_skip` |

Risk denials appear on `copy_skip` with `reason_code=risk_denied` and the policy reason string; use **`tyrex_risk_ops`** for **why** (marks, caps, counts, balances). See `Implementation/logging_workflow_review.md`.

## Rollout validation (baseline vs canary)

Use the same guru + risk YAML; change **one** surface at a time (ingest mode → framework submit → C2 → C3).

1. **C1 — ingest:** Run **`rtds_shadow`** (`rtds_shadow.yaml`) and compare poll vs stream with `guru_shadow_report.py`. Then **`rtds_primary`** and `guru_primary_report.py` (fallback, gap-fill, duplicate checks). Spike: `scripts/spike_rtds_activity.py`.
2. **Phase B — risk:** Confirm `tyrex_pm phase_b:` startup line matches intent. Grep **`tyrex_risk_ops`** + `RISK_*` during a short live session. Follow **`phase_b_operational_validation.md`** before scaling.
3. **C2 — sizing:** In **shadow**, enable conviction / min-notional incrementally; inspect `copy_skip` and (if DEBUG) `copy_conviction_diag`. See **`c2_validation_readiness_review.md`** and unit tests under `tests/unit/`.
4. **C3 — execution:** Requires **live framework** path. Enable **one** `execution_*` flag per canary; grep **`exec_*`** / **`LIVE_ORDER_*`** in `run_nautilus.log`. See **`plan_C3_Execution-Quality.md`**.

**Baseline vs canary:** Keep a **baseline** Nautilus log from a known-good run; for each change, diff event rates (`guru_signal_emitted`, `copy_skip`, `shadow_order_intent` / `live_order_intent`, `exec_*`, `RISK_*`).

## Troubleshooting

- **No `shadow_order_intent` / `live_order_intent`:** if filtered mode, check `token_filter` vs guru `asset`; if unfiltered, look for `zero_qty`, `risk_denied`, etc.
- **Guru polling:** follower uses **`GET /activity`** (`type=TRADE`) with a **watermark** (`guru_state_path`), not full `/trades` history. `guru_startup_backfill_seconds: 0` means only trades **after** the first boot watermark; increase for a short warm-up window. On API errors see **`guru_poll_error`** / **`guru_poll_error_backoff`** (the bot keeps running).
- **Guru duplicates:** dedup store (`guru_dedup_state_path`); delete file for full replay in dev only. Watermark file controls incremental progress (`guru_state_path`).
- **RTDS primary but no `guru_signal_emitted source=rtds`:** verify `guru_wallet_address` matches RTDS **`proxyWallet`** (spike script); check `guru_rtds_*` / fallback lines in Nautilus log; confirm `guru_ingest_mode` and network reachability to `guru_rtds_url`.
- **Live immediate rejects:** `live_order_error` (py-clob) or **`LIVE_ORDER_ERROR`** / venue message (framework). Check **`TYREX_MIN_BUY_NOTIONAL_USD`**, tick/min size, **capital gate** reasons, balance/allowance, and **“orderbook does not exist”** (venue — market/token may be inactive).
- **Risk denies with `risk_*` / `RISK_*`:** Tyrex `ConfiguredRiskPolicy` (limits, capital gate, unresolved position when configured) — not the same as Nautilus **RiskEngine** denials logged by the adapter.
- **Config validation errors:** messages cite the YAML path and field; see `CONFIG_MODEL.md`.
