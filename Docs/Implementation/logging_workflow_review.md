# Logging workflow review — `run_guru.py` and Phase A/B operations

**Scope:** How logging works **today** when running `python scripts/run_guru.py …`, whether it is **sufficient** for operational validation (restart, marks, B1/B2 denials, operator clarity), and **practical gaps**.  
**Not in scope:** New trading logic, Phase C, or large logging redesign. **Update:** `scripts/run_guru.py` persists **Tyrex** logs via a **`tyrex_pm` logger `FileHandler`** and **Nautilus** logs via **`LoggingConfig` file fields** — see §1.1–1.2 and `Docs/OPERATIONS.md` § Guru run log files. **[`logging_sources_review.md`](logging_sources_review.md)** is the implementation-phase source map; **[`logging_system_guide.md`](../logging_system_guide.md)** is the durable guide at `Docs/` root.

**Related:** `phase_ab_test_validation_matrix.md`, `phase_b_operational_validation.md`, `../OPERATIONS.md` § logs / Phase B.

**Durable logging docs (Docs root):** [`../logging_system_guide.md`](../logging_system_guide.md) (maintainer rules + operator quick ref), [`../log_validation_playbook.md`](../log_validation_playbook.md) (run commands + five validation questions).

---

## 1. Logging architecture (as implemented)

### 1.1 Initialization (`scripts/run_guru.py`)

| Step | What happens |
|------|----------------|
| **Stderr `print`** | Config validation errors; pip/dotenv errors; optional **`print`** banner (`tyrex_pm guru run \| mode=…`) and optional **`phase_a:`** one-liner when live + Nautilus + framework submit. |
| **Python `logging`** | After `_merge_dotenv()`: if **`logging.root.handlers`** is empty, **`logging.basicConfig(level=INFO, format="%(message)s")`**. Then **`logging.getLogger("tyrex_pm").setLevel(logging.INFO)`**. |
| **File persistence (Tyrex)** | After successful config load: resolve **`run_tyrex.log`** / **`NAME_tyrex.log`**, ensure dirs, attach **`FileHandler`** to logger **`tyrex_pm`** only (UTF-8, overwrite). Duplicate-safe if the same path is reused. |
| **File persistence (Nautilus)** | Same basename stem for **`run_nautilus.log`**. :func:`build_guru_trading_node` receives **`GuruNautilusFileLogging`** so **`TradingNodeConfig.logging`** sets **`log_directory`**, **`log_file_name`** (stem), **`log_level_file`**, **`clear_log_file`** — framework-native file sink when the kernel initializes (see Nautilus `LoggingConfig`). |
| **Startup lines** | Two stdout **`print`s**: `tyrex_pm logging to …` and `nautilus logging to …`. |

**Implications:**

- **Tyrex package loggers** under the `tyrex_pm.*` namespace inherit visibility when the **`tyrex_pm`** logger is set to INFO (typical child propagation to root).
- **`run_guru.py` does not call** `tyrex_pm.core.logging_config.setup_logging` — that helper exists for other scripts (`format="%(levelname)s %(name)s %(message)s"`) but the entrypoint uses its own **`basicConfig`** rules.
- If something else installed root handlers **before** `basicConfig`, **format/level** may differ from the above (rare in plain `run_guru`).

### 1.2 Nautilus `TradingNode` logging

| Source | Role |
|--------|------|
| **`TradingNodeConfig.logging`** | Built in `runtime/guru_compose.py` from **`RuntimeSettings.logging_level`** plus optional **file** fields when `run_guru` passes **`GuruNautilusFileLogging`** (`log_level`, `log_level_file`, `log_directory`, `log_file_name`, `clear_log_file`). |
| **Nautilus kernel** | High volume: node boot banner, `Cache`/`DataEngine`/`RiskEngine`/`ExecEngine` READY lines, config dumps, often **ANSI-colored** timestamps. |

Nautilus components (`Strategy`, `Actor`) expose **`self.log`**: that is **Nautilus’s component logger**, **not** a raw `logging.getLogger("tyrex_pm.…")` name. Log lines usually appear with a **trader/component prefix** (e.g. `TYREX-GURU-001.CopyStrategy`). Tyrex code inside those components uses **`self.log`** for the guru monitor and copy strategy; framework execution uses **`self._strategy.log`** from `NautilusGuruExecutionPort`.

### 1.3 Mixed surfaces (summary)

| Surface | Examples | Typical level |
|---------|----------|----------------|
| **`print` (stdout/stderr)** | Boot banner, `phase_a:` line, `Stopping…` | N/A |
| **Standard logging (`tyrex_pm.*`)** | `tyrex_pm.runtime.guru_compose` (Phase B startup line); `tyrex_pm.risk.configured` (portfolio cap **warning**); `tyrex_pm.runtime.guru_cache_warmup`; `tyrex_pm.data.data_api_client` (poller backoff **warning**) | INFO / WARNING |
| **Legacy py-clob path** | `tyrex_pm.execution.polymarket_policy` (`_log`) | INFO / WARNING / ERROR (exceptions) |
| **Nautilus `Actor` / `Strategy` log** | `GuruMonitorActor`, `CopyStrategy`, messages routed through `NautilusGuruExecutionPort` to `self._strategy.log` | INFO / WARNING / ERROR |
| **Nautilus internals** | MessageBus, engines, adapter — often **verbose** at INFO | INFO+ |

**There is no single “Tyrex JSON log”** — operational grepping relies on **`event=`-prefixed** strings inside free-text lines (strategy/guru monitor) **plus** plain messages from **`tyrex_pm`** loggers and **unstructured** Nautilus noise.

### 1.4 Logger names that matter for Tyrex-owned messages

| Logger name (approx.) | Module | Typical messages |
|-----------------------|--------|------------------|
| **`tyrex_pm.runtime.guru_compose`** | `guru_compose.py` | **`tyrex_pm phase_b: …`** (B5 startup summary), INFO |
| **`tyrex_pm.risk.configured`** | `configured.py` | Portfolio cap **underestimate** path: WARNING with omitted instruments |
| **`tyrex_pm.runtime.guru_cache_warmup`** | `guru_cache_warmup.py` | Warmup INFO/WARNING |
| **`tyrex_pm.data.data_api_client`** | `data_api_client.py` | **`event=poller_backoff`** WARNING when HTTP retry backs off |
| **`tyrex_pm.execution.polymarket_policy`** | `polymarket_policy.py` | Legacy live: **`event=…`** INFO/WARNING, submit/error |

**Note:** `portfolio_exposure.py` (B1) and `guru_instrument_dynamic.py` (dynamic resolve) **do not** emit their own operational log lines today — failures surface indirectly (B2 reason code on deny, or framework exec **errors**).

---

## 2. Workflow: what appears step by step (`run_guru.py`)

**Order of operations:**

1. **Dotenv** — no log (silent unless error → stderr print).
2. **Logging setup** — root `basicConfig` if empty; `tyrex_pm` → INFO.
3. **Config load** — `load_strategy_settings` / `load_risk_settings` / `load_runtime_settings`: on failure, **`print` to stderr**, process exits; **no** success log line from loaders.
4. **File paths** — resolve **`run_tyrex.log`** / **`run_nautilus.log`** (or **`NAME_*`**); mkdir for Nautilus target; attach **`tyrex_pm` `FileHandler`**; **`print`** both destination lines.
5. **`print`** banner — mode, trader_id, guru prefix, token filter summary.
6. **Optional `print`** — `phase_a:` line (framework path hint).
7. **`build_guru_trading_node`** (with **`nautilus_file_logging`**) — `validate_phase_b_runtime_contract`; **`LoggingConfig`** includes Nautilus **file** sink; constructs node, risk, readers, strategy, ports; registers actor + strategy; **`_LOG.info(phase_b_startup_summary_line)`** → one **`tyrex_pm phase_b:`** line (**also** in **`run_tyrex.log`** when the handler is already attached).
8. **`node.build()`** — **large volume** of Nautilus INFO: kernel, caches, engines (often with ANSI); component file lines go to **`run_nautilus.log`** when configured.
9. **`node.run()`** — event loop; ongoing logs below.

**After the node is running:**

| Phase | Component | Representative log evidence |
|-------|-----------|------------------------------|
| **Guru poll tick** | `GuruMonitorActor` | `event=guru_poll_tick component=guru_monitor phase=on_start\|timer\|… sub=fetch` |
| **Poll error** | `GuruMonitorActor` | `event=guru_poll_error` ERROR; then `event=guru_poll_error_backoff` INFO |
| **New guru trade** | `GuruMonitorActor` | `event=guru_signal_emitted correlation_id=… side=… token_id=…` |
| **Strategy filter / policy skip** | `CopyStrategy` | `event=copy_skip … reason_code=<signal reason> detail=…` |
| **Zero qty** | `CopyStrategy` | `event=copy_skip … reason_code=copy_skip detail=zero_qty` |
| **Risk deny** | `CopyStrategy` | `event=copy_skip … reason_code=risk_denied risk_detail=<ReasonCode string>` |
| **Shadow path** | `CopyStrategy` | `event=shadow_order_intent …` |
| **Live intent handoff** | `CopyStrategy` | `event=live_order_intent …` (**before** venue submit) |
| **Framework submit** | `NautilusGuruExecutionPort` via strategy log | `event=LIVE_ORDER_SUBMIT …` or **`GURU_*` / `LIVE_ORDER_ERROR`** |
| **Legacy py-clob** | `polymarket_policy` logger | `event=…` with `component=polymarket_exec` |

**Correlation hook:** Use **`correlation_id`** (guru `source_trade_id`) to tie **`guru_signal_emitted`** → **`copy_skip`** / **`live_order_intent`** → **`LIVE_ORDER_*`** on the same flow.

**Restart / reconnect:** Tyrex does **not** emit a dedicated “reconnected” or “cache reconciled” line. What you see is **Nautilus/adapter** volume (if any) plus **behavioral** clues: bursts of **`risk_denied`** with `RISK_PORTFOLIO_EXPOSURE_UNRESOLVED`, **`guru_poll_error`**, etc.

---

## 3. Operational questions → current log evidence

| Question | Sufficient? | What to inspect | Caveats |
|----------|-------------|-----------------|---------|
| **Restart truth / convergence** | **Weak** | Nautilus engine/cache lines; Tyrex **`tyrex_pm phase_b:`** only at **build**, not per reconnect; denial patterns after reboot | No Tyrex “ready for trade” marker; infer from absence/presence of errors and risk outcomes. |
| **Mark / quote gaps** | **Stronger** | **`copy_skip`** + grep **`event=tyrex_risk_ops gate=portfolio_unresolved`** on logger **`tyrex_pm.risk.configured`**: includes **`b1_error=…`** (truncated), **`b1_pending_complete`**, **`b1_filled_complete`**, **`e_portfolio_present`** | Strategy line still coarse; **Tyrex ops** line carries B1 diagnosis. |
| **Why `RISK_PORTFOLIO_EXPOSURE_UNRESOLVED`** | **Stronger** | Same **`tyrex_risk_ops`** line; **`gate=portfolio`** if no B1 aggregator | No auto-list of every instrument id in omissions (see unsafe **WARNING** still). |
| **Portfolio cap denied** | **Strong** | **`tyrex_risk_ops gate=portfolio_cap`**: **`e_portfolio`**, **`intent_notional`**, **`cap`**, **`sum`** | — |
| **Concurrent guru cap denied** | **Strong** | **`tyrex_risk_ops gate=guru_concurrent`**: **`guru_resting_count`**, **`limit`**, **`correlation_id`** | Strategy **`copy_skip`** unchanged. |
| **Reserve denied** | **Strong** | **`tyrex_risk_ops gate=reserve`**: **`py_clob_balance`**, **`reserve_usd`**, **`intent_notional`**, **`required_free`** | Min collateral / allowance denies also emit **`gate=min_collateral`** / **`gate=min_allowance`**. |
| **Guru order recognized (B3)** | **Weak in logs** | **`LIVE_ORDER_SUBMIT`** includes `client_order_id` (`TX…`); tags include `guru_cid=` in code — not always echoed in log line | **Tier identification** (tag vs TX) is **not** logged on deny; inference from submit line + later cache state. |
| **Dynamic resolve / activation** | **Partial on failure** | **`GURU_DYNAMIC_RESOLVE_FAILED`**, **`GURU_DYNAMIC_ACTIVATION_CAP`**, **`GURU_INSTRUMENT_*`** on strategy log | **Success** path: dynamic controller is **quiet** — only **`LIVE_ORDER_SUBMIT`** confirms progress. |

---

## 4. Assessment: is this operationally strong enough?

### 4.1 What is already good

- **Consistent `event=` prefix** on strategy and guru monitor lines — easy **`grep event=copy_skip`**, **`event=guru_signal_emitted`**, **`event=LIVE_ORDER_SUBMIT`**.
- **`correlation_id`** threads guru trade → risk → execution on the happy path and many skips.
- **Phase B startup** **`tyrex_pm phase_b:`** gives a **single factual snapshot** of configured gates (when `tyrex_pm` logging is visible).
- **Explicit reason strings** for risk denials (`risk_detail=…`) cover **which gate** fired at the **ReasonCode** level.
- **Separate** legacy vs framework execution log components (`polymarket_exec` vs `nautilus_guru_exec`).

### 4.2 What is noisy or fragmented

- **Nautilus INFO** default is **very chatty** (kernel, engine config dumps). Tyrex’s high-signal lines are **buried** unless filtered.
- **Two families of loggers**: `tyrex_pm.*` (stdlib) vs **Nautilus component** `self.log` (different prefixes, often ANSI).
- **`run_guru` `print`** lines vs **`logging`** lines vs Nautilus — operators must know **three** channels for “boot truth.”

### 4.3 What is hard to correlate

- **B1 failure subtypes** all collapse to **`RISK_PORTFOLIO_EXPOSURE_UNRESOLVED`** at the strategy.
- **No structured trace id** beyond `correlation_id` (good for guru-driven flow, **no** inner span for “risk evaluate #2”).
- **Restart vs transient mark loss** — same outward symptom (**unresolved** deny); logs don’t say *why* without deeper digging.

### 4.4 Residual blind spots

- **`copy_skip`** still carries only **`risk_detail=<ReasonCode>`** — pair it with **`tyrex_risk_ops`** on **`tyrex_pm.risk.configured`** (same correlation_id) for B1/B2/B3/B4 **numeric** context.
- **Unsafe** portfolio path: **WARNING** for omissions remains; strict denies now have **INFO** `tyrex_risk_ops` with **`b1_error`**.
- **Restart / reconnect** still has no Tyrex milestone line (unchanged).

### 4.5 Verdict for Phase A/B operational validation

**Good enough for common risk denials** when operators **`grep tyrex_risk_ops`** (or filter `tyrex_pm.risk.configured`) alongside **`event=copy_skip`**. **Restart / convergence** and **full** B1 per-instrument detail remain **inferential** or **truncated**.

---

## 5. Logging gaps (after `tyrex_risk_ops` pass)

1. **`copy_skip`** still omits B1 detail — correlate with **`event=tyrex_risk_ops`** (same **`correlation_id`**).
2. **Tyrex vs Nautilus** stream mixing unchanged — use **`grep tyrex_risk_ops`** / logger filter **`tyrex_pm.risk.configured`**.
3. **Restart / reconnect** — no dedicated Tyrex line; **dynamic / B3 tier** — still weak.

---

## 6. Further incremental ideas (not required now)

1. Optional **single structured field** on **`copy_skip`** pointing to ops detail (e.g. **`tyrex_diag=1`**) — would require `CopyStrategy` change.
2. **`logging_level: WARNING`** in runtime YAML to thin Nautilus — document tradeoff (`OPERATIONS.md`).
3. JSON or **dedicated `tyrex_pm.ops` logger** only if ops scale demands it.

---

## 7. Operator quick reference — how to read the logs

### Boot / start

1. **`tyrex_pm guru run | …`** (`print`) — confirms CLI args resolved.
2. **`phase_a: …`** (`print`) if framework live — pending/filled/capital hint.
3. **`tyrex_pm phase_b: …`** — confirm Phase B gate **settings** (if `tyrex_pm` logging visible).
4. Nautilus **kernel torrent** — expect **READY** lines; **not** Tyrex-specific.
5. **`event=strategy_started …`** — strategy registered.
6. **`event=guru_poll_tick … phase=on_start`** — actor started.

### Normal polling

- **`event=guru_poll_tick … sub=fetch`** each cycle; then often **silence** if no new trades.

### Normal flow (approve)

- **`event=guru_signal_emitted correlation_id=…`**
- **`event=live_order_intent`** or **`event=shadow_order_intent`**
- Framework: **`event=LIVE_ORDER_SUBMIT correlation_id=…`**

### Normal denial (risk)

- **`event=copy_skip … reason_code=risk_denied risk_detail=<code>`** — strategy (`self.log`).
- **Same deny:** grep **`event=tyrex_risk_ops`** on **`tyrex_pm.risk.configured`** for **`gate=`** (`portfolio_unresolved`, `portfolio_cap`, `guru_concurrent`, `reserve`, `min_collateral`, `min_allowance`, …) and numeric context.

### Policy / filter skip (pre-risk)

- **`event=copy_skip`** with **`reason_code=`** not `risk_denied` — token filter, entry/exit decision.

### Common failures

- **`event=guru_poll_error`** — Data API / network; then **`guru_poll_error_backoff`**.
- **`event=GURU_INSTRUMENT_*`**, **`GURU_DYNAMIC_*`** — resolution/activation (framework path).
- **`event=LIVE_ORDER_ERROR`** — min notional, missing price, or downstream submit issues.

### What to grep first

```text
event=guru_signal_emitted
event=copy_skip
reason_code=risk_denied
tyrex_risk_ops
event=LIVE_ORDER_SUBMIT|LIVE_ORDER_ERROR
tyrex_pm phase_b:
event=guru_poll_error
```

Add **`correlation_id=`** when tracing a specific guru trade.

---

## 8. File index (code referenced)

| File | Role in logging |
|------|-----------------|
| `scripts/run_guru.py` | `basicConfig`, `tyrex_pm` level, prints |
| `src/tyrex_pm/runtime/guru_compose.py` | `tyrex_pm.runtime.guru_compose` INFO (Phase B line) |
| `src/tyrex_pm/strategy/copy_strategy.py` | `self.log` — `copy_skip`, intents |
| `src/tyrex_pm/strategy/base.py` | `strategy_started` |
| `src/tyrex_pm/data/guru_monitor.py` | `self.log` — poll ticks, signals, errors |
| `src/tyrex_pm/execution/nautilus_guru_exec.py` | `self._strategy.log` — submit/errors |
| `src/tyrex_pm/execution/polymarket_policy.py` | stdlib `_log` — legacy path |
| `src/tyrex_pm/risk/configured.py` | WARNING portfolio underestimate; **INFO** **`event=tyrex_risk_ops`** on B1/B2/B3/B4/capital denies (`_ops_snippet`, `_TYREX_RISK_OPS_EVENT`) |
| `src/tyrex_pm/data/data_api_client.py` | backoff WARNING |
| `src/tyrex_pm/core/logging_config.py` | Optional helper (not used by `run_guru`) |

---

*Document version: written against Tyrex_PM Phase A+B completion; aligns with `phase_ab_test_validation_matrix.md` (tests vs live gaps).*
