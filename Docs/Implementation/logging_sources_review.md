# Logging sources review — Tyrex_PM / `run_guru.py`

**Purpose of this note:** Map **actual** log-producing surfaces in the current codebase, relate them to **Phase A/B validation**, assess **persistence gaps**, and record a **minimal** source-separated persistence design.

**Related:** `logging_workflow_review.md` (workflow + grep guidance), `../OPERATIONS.md` (operator runbook), **[`../logging_system_guide.md`](../logging_system_guide.md)** (stable dev rules + operator quick reference), **[`../log_validation_playbook.md`](../log_validation_playbook.md)** (five validation questions), `guru_run_logging.py` + `scripts/run_guru.py`.

**Implementation status:** As of the **source-separated persistence** step, `run_guru` writes **`run_tyrex.log`** (`tyrex_pm` handler) and **`run_nautilus.log`** (Nautilus `LoggingConfig` file sink). Sections **4** and **7** below retain the **prior** “gaps” narrative where useful but add a **current** column.

---

## 1. Purpose

Operators need logs to validate **restart / Cache convergence**, **marks / quotes**, **B1 portfolio exposure**, **B2/B3/B4 denials**, **capital gate**, and **guru → strategy → execution** flow. **Previously**, a single **root** `FileHandler` mirrored only part of the console (lots of **httpx**, missing **Nautilus component** lines). **Now**, Tyrex and Nautilus use **separate** files (see §4). This note still maps **sources** and **feasibility** for further tuning (e.g. HTTP file, `print` migration).

---

## 2. Current logging sources

The table below reflects **code and library behavior** as wired for `scripts/run_guru.py` → `build_guru_trading_node` → `TradingNode`.

| Source | Where it originates | Typical content | Noise vs signal |
|--------|---------------------|-----------------|-----------------|
| **A. `print()` (stdout/stderr)** | `scripts/run_guru.py`: dotenv/import errors (stderr); post-config banner + optional `phase_a:` line (stdout); `Stopping…`; **`tyrex_pm` / `nautilus` logging path lines** via `announce_guru_run_log_destinations` | Fixed operational context; not structured as `event=` logs | **Low volume, high context** — easy to miss if only log files are reviewed |
| **B. `tyrex_pm.*` stdlib loggers** | Modules use `logging.getLogger(__name__)` under `tyrex_pm`: e.g. `runtime.guru_compose`, `risk.configured`, `runtime.guru_cache_warmup`, `data.data_api_client`, `execution.polymarket_policy` | Phase B startup summary (`tyrex_pm phase_b: …`), risk **warnings** (e.g. portfolio cap underestimate path), warmup INFO, poller backoff **warnings**, legacy execution policy `event=` lines | **Mixed** — several lines are **high signal** for B0–B5 and capital; volume moderate |
| **C. Nautilus `Logger` / `self.log` (components)** | `GuruMonitorActor`, `CopyStrategy` (`self.log.*`), `NautilusGuruExecutionPort` (via strategy logger), Nautilus **kernel / engines / adapters** | Guru poll ticks, `guru_signal_emitted`, `copy_skip`, `live_order_intent`, dynamic activation cap errors, framework submit messages; plus bus/engine boot noise | **High signal** for guru flow + execution path; **very noisy** at INFO for engines/adapters |
| **D. Third-party stdlib loggers (propagate to root)** | e.g. **`httpx`** (“`HTTP Request: …`”), possibly other HTTP stacks | Per-request HTTP traces | **Mostly noise** for Phase A/B validation; occasionally useful for auth/API failures |
| **E. Nautilus `LoggingConfig`** | `guru_compose.py` builds `TradingNodeConfig` with **`LoggingConfig`**: always **`log_level`**; when `run_guru` passes **`GuruNautilusFileLogging`**, also **`log_level_file`**, **`log_directory`**, **`log_file_name`**, **`clear_log_file`** | Drives Nautilus **stdout/stderr** and, when set, the **framework file** sink | Same content class as **C** in the file |

**Important distinction:** **B** uses Python **`tyrex_pm.*`** loggers; **`run_tyrex.log`** attaches a handler to **`tyrex_pm`** only, so **D** (httpx) stays **out** of that file while still going to **console** via **root**. **C** is handled by Nautilus’s subsystem and is persisted to **`run_nautilus.log`** when file fields are set — **not** via the Tyrex `FileHandler`.

**Utility helper (not a separate runtime source):** `tyrex_pm.core.logging_config.setup_logging` — documented as for **other scripts**; **`run_guru` does not call it** (uses its own `basicConfig`).

---

## 3. Role of each source in Phase A/B validation

| Investigation | A `print` | B `tyrex_pm` stdlib | C Nautilus components | D HTTP stdlib |
|---------------|-----------|---------------------|------------------------|---------------|
| Restart / Cache convergence | Banner mentions restart doc; not detailed | Warmup / policy lines may hint; **B1 incomplete** often surfaces as **risk reason codes** downstream | Engine/Cache **READY** and ongoing adapter noise — **high volume**, some useful | Low direct value |
| Mark / quote gaps | No | Risk **warnings**, data client backoff | **copy_skip** / exec errors may indirect | Errors sometimes visible as HTTP failures |
| Unresolved portfolio exposure (B1/B2) | `phase_a:` line sets expectations | **`tyrex_pm phase_b:`** + `risk.configured` warnings; denials via strategy logs | **`copy_skip` … `risk_denied`** / reason tokens in CopyStrategy logs | Rare |
| Cap denials (B2/B3) | No | Same as above | **`event=copy_skip` … `risk_denied`** — **primary operator grep surface** | Rare |
| Reserve / capital (B4 / Phase A capital) | `phase_a:` mentions capital gate | `tyrex_pm` risk + capital-related logs | **`copy_skip` / exec path** | Balance endpoints sometimes visible as HTTP |
| Dynamic resolution | No | Warmup / compose | **`guru_dynamic_activation_cap`**, activation path errors | Market/metadata GET spam (**noisy**) |
| Guru signal flow | No | Limited | **`guru_poll_tick`**, **`guru_signal_emitted`** — **essential** | Low |
| Execution path (legacy vs framework) | No | `polymarket_policy` if legacy | **`live_order_intent`**, `LIVE_ORDER_*`, Nautilus exec errors | Some CLOB traffic |

**Takeaway:** For day-to-day Phase B validation, **C (Nautilus component lines)** and selective **B** lines matter most. **D** is rarely worth manual review except when debugging connectivity. **A** is metadata that should appear in **some** persistent artifact for run provenance.

---

## 4. What is currently persisted vs console-only

| Source | Console | **Current** persistence (`run_guru` default) | **Prior** single `run.log` (root handler) — reference |
|--------|---------|-----------------------------------------------|------------------------------------------------------|
| **A** `print()` | Yes | **No** in `*_tyrex` / `*_nautilus` files | Same |
| **B** `tyrex_pm.*` | Yes | **`logs/<mode>/run_tyrex.log`** — `%(message)s` on **`tyrex_pm`** handler | **Yes** on root file (with httpx mix) |
| **C** Nautilus `self.log` / kernel | Yes | **`logs/<mode>/run_nautilus.log`** — Nautilus `LoggingConfig` file sink | **No** (gap) |
| **D** `httpx` etc. | Yes | **No** dedicated file; still visible on **console** via root | **Yes** — noisy |

**Gaps that remain:** **A** (`print` transcript), **D** (no optional `run_http.log` yet), and **no** guaranteed byte-for-byte match between console formatting and either file format.

---

## 5. Feasibility of source-separated persistence

| Target | Realistic own file? | Difficulty | Notes |
|--------|---------------------|------------|--------|
| **Nautilus (C + engine noise)** | **Yes** — *native* | **Small–medium** | `LoggingConfig` already supports `log_level_file`, `log_directory`, `log_file_name`, `clear_log_file`. Tyrex currently sets only `log_level`. Enabling file fields is the **straightforward** way to persist **`TYREX-GURU-001.*`**-style lines **without** stdout tee hacks. |
| **`tyrex_pm` only (B)** | **Yes** | **Small** | Add dedicated `FileHandler` on logger **`tyrex_pm`** (with propagation unchanged) **or** a `logging.Filter` on a second root handler. Avoid breaking console handlers. |
| **HTTP / third-party (D)** | **Yes** | **Small–medium** | Options: leave on root only; attach dedicated handler to `httpx` + `httpcore`; or filter **out** of the “operator” file via filters. Clean separation is **easier** than merging fairly. |
| **`print` (A)** | **Yes** | **Medium** | Options: (1) **redirect stdout/stderr** for the process — simple conceptually but **risky** (libraries, prompt-like behavior, Nautilus expectations); (2) **migrate** important prints to `logging` — **small, safe** incremental changes; (3) accept **loss** for non-critical lines. |
| **Perfect 1:1 console transcript** | Possible | **High** | Full **tee** of the process terminal stream duplicates **ANSI**, ordering, and non-log writes; usually **not** worth maintaining. |

**Honest limit:** Nautilus file logging and Python stdlib file logging are **two subsystems**. They will **not** automatically share one rotation policy or one timestamp format unless you configure them — that is acceptable for “validation-first” operations.

---

## 6. Recommended file-separation design (minimal)

Goal: **smaller, purpose-built artifacts** without a logging “rewrite.”

| Proposed artifact | Contents | Why it helps | Effort |
|-------------------|----------|--------------|--------|
| **`run_nautilus.log`** (name illustrative) | Nautilus **`LoggingConfig` file sink**: components (`GuruMonitorActor`, `CopyStrategy`, …), engines, adapters — **same family as console Nautilus lines** | **Primary** Phase B validation tail (**signals**, **skips**, **exec**) | **Low** — wire `LoggingConfig` fields when building `TradingNodeConfig` |
| **`run_tyrex.log`** (illustrative) | **`tyrex_pm` logger tree** only: Phase B summary, `risk.configured`, warmup, data API backoff, etc. | **Tyrex-owned** semantics without drowning in HTTP | **Low** — dedicated handler + `run.log` retirement or repurposing |
| **`run_http.log`** *(optional)* | `httpx` / `httpcore` (and peers if desired) | Keeps **operator** files grep-friendly; only needed if HTTP volume hurts | **Low–medium** — per-logger handlers or filters |
| **`run.log` (current) or `run_root.log`** | Whatever remains on **root** if you still want a “catch-all” stdlib sink | Catches **unexpected** libraries; often **mixed** | **Low** if kept — or **drop** if `tyrex_pm` + `httpx` split covers needs |
| **Bootstrap / provenance** | Today’s **`print`** banner + log path + `phase_a:` line | Run identity, mode, gate hints | **Small** — prefer **migrating** 2–3 stdout lines to **`tyrex_pm`** INFO once |

**Naming:** Keep a single operator-visible convention, e.g. `logs/<mode>/<basename>_tyrex.log`, `_nautilus.log`, `_http.log`, aligned with existing `--log-name` stem.

**Noise policy:** For live runs, consider **raising** Nautilus log level for **non-component** namespaces only if `LoggingConfig.log_component_levels` (or equivalent) supports it without hiding `CopyStrategy` / `GuruMonitorActor` — **verify against Nautilus docs** before relying on this in production.

---

## 7. Recommended next implementation step

**Done (this step):** Nautilus **`LoggingConfig` file sink** + Tyrex **`tyrex_pm` `FileHandler`** with **`run_*_tyrex.log`** / **`run_*_nautilus.log`** naming and `--log-name` stems.

**Suggested follow-ups (incremental):**

1. **Optional `run_http.log`** (or raised `httpx` log level) if operators want CLOB/Gamma traces on disk without opening **`run_nautilus.log`** noise filters.
2. **Migrate** 2–3 high-value **`print`** lines (banner / `phase_a:`) to **`tyrex_pm` INFO** if run **provenance** must live next to Phase B lines — small diff, no stdout tee.
3. **Full console transcript** — still **defer** (high cost, low validation ROI).

Defer:

- **Full console transcript** parity — **not** the smallest win.
- **Large** `print` redirection — prefer **targeted** migration of banner lines to logging if provenance must be in files.

---

## Revision history

- **2026-04:** Initial review (design).
- **2026-04:** Source-separated persistence implemented (`run_tyrex.log` / `run_nautilus.log`); §4 and §7 updated.
