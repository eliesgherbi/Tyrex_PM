# Log validation playbook — Phase A / B (operator)

**Purpose:** Practical steps to gather **`run_nautilus.log`** and **`run_tyrex.log`** and use them together to answer **five** operational questions about **restart**, **marks/quotes**, **portfolio exposure resolution**, **exposure scalar behavior**, and **incident clarity**.

**Location:** Durable operator playbook at `Docs/` root. See [logging_system_guide.md](logging_system_guide.md) for log sources and maintainer rules.

**Not in scope:** Proving PnL, exchange correctness, or full market microstructure — see live runbooks and [Implementation/phase_b_operational_validation.md](Implementation/phase_b_operational_validation.md) for broader checklists.

**Prerequisites:** `pip install -e .`, repo root as cwd, `.env` per [OPERATIONS.md](OPERATIONS.md). Runtime YAML must set **`execution_mode`** to `shadow` or `live` (and Polymarket/Nautilus flags as appropriate). Log paths use **`logs/<execution_mode>/`**.

**See also:** [logging_system_guide.md](logging_system_guide.md) (what each file is), [OPERATIONS.md](OPERATIONS.md), [Implementation/phase_b_operational_validation.md](Implementation/phase_b_operational_validation.md).

---

## 1. Purpose

This playbook ties **concrete commands**, **log paths**, **grep patterns**, and **good vs bad** interpretations to five operator questions. It assumes Tyrex’s **source-separated** persistence (`run_tyrex.log` = `tyrex_pm.*`, `run_nautilus.log` = Nautilus-native).

---

## 2. Required run commands

Paths below use in-repo starter configs. Adjust filenames if you use copies.

### 2.1 Default **live** run

**Unix / macOS:**

```bash
cd /path/to/Tyrex_PM
python scripts/run_guru.py \
  --strategy-conf config/strategy/guru_follow.yaml \
  --risk-conf config/risk/guru_follow_risk.yaml \
  --live-conf config/runtime/live_polymarket.yaml
```

**Windows (cmd-style continuation):**

```bat
cd /d E:\polymarket\Tyrex_PM
python scripts/run_guru.py ^
  --strategy-conf config/strategy/guru_follow.yaml ^
  --risk-conf config/risk/guru_follow_risk.yaml ^
  --live-conf config/runtime/live_polymarket.yaml
```

**Logs (when `execution_mode: live` in the runtime YAML):**  
`logs/live/run_tyrex.log`, `logs/live/run_nautilus.log`

---

### 2.2 Default **shadow** run

Shadow is selected **only** by runtime YAML (`execution_mode: shadow`), not by a separate script flag. Example: copy `config/runtime/live_polymarket.yaml` to e.g. `config/runtime/shadow_polymarket.yaml`, set `execution_mode: shadow`, then:

```bash
python scripts/run_guru.py \
  --strategy-conf config/strategy/guru_follow.yaml \
  --risk-conf config/risk/guru_follow_risk.yaml \
  --live-conf config/runtime/shadow_polymarket.yaml
```

**Logs:** `logs/shadow/run_tyrex.log`, `logs/shadow/run_nautilus.log`

---

### 2.3 **Named** live run (`--log-name`)

Preserves the same config paths; only the log **stems** change.

```bash
python scripts/run_guru.py \
  --strategy-conf config/strategy/guru_follow.yaml \
  --risk-conf config/risk/guru_follow_risk.yaml \
  --live-conf config/runtime/live_polymarket.yaml \
  --log-name smoke-2026-04-04
```

**Logs:** `logs/live/smoke-2026-04-04_tyrex.log`, `logs/live/smoke-2026-04-04_nautilus.log`

---

### 2.4 Phase B log-validation risk profiles

Dedicated risk YAMLs (comments inside each file) isolate **B2+B3** vs **B4** without editing the starter `guru_follow_risk.yaml`. Use **`--log-name`** and a **live** runtime with **`polymarket_nautilus_live: true`** and **`polymarket_framework_submit: true`** (e.g. `config/runtime/live_polymarket.yaml` or `config/runtime/live_polymarket_phaseb_validate.yaml`).

- **B2 + B3:** `config/risk/guru_follow_risk_phaseb_b2_b3_validate.yaml`
- **B4 + capital gate:** `config/risk/guru_follow_risk_phaseb_b4_validate.yaml`

---

### 2.5 **Named** shadow run

```bash
python scripts/run_guru.py \
  --strategy-conf config/strategy/guru_follow.yaml \
  --risk-conf config/risk/guru_follow_risk.yaml \
  --live-conf config/runtime/shadow_polymarket.yaml \
  --log-name smoke-shadow-01
```

**Logs:** `logs/shadow/smoke-shadow-01_tyrex.log`, `logs/shadow/smoke-shadow-01_nautilus.log`

---

### 2.6 Optional environment

- Non-default dotenv: `TYREX_PM_DOTENV=/path/to/.env` (shell) before `python scripts/run_guru.py …`.

---

## 3. How to use `run_nautilus.log` and `run_tyrex.log` together

| File | Best for |
|------|----------|
| **`run_nautilus.log`** | End-to-end **workflow**: guru poll, **`guru_signal_emitted`**, **`copy_skip`**, **`risk_denied`** / **`risk_detail=`**, **`live_order_intent`**, engine/adapter context |
| **`run_tyrex.log`** | Tyrex **policy** and **B1/B2 plumbing**: **`tyrex_pm phase_b:`**, **`event=tyrex_risk_ops`**, portfolio gate snippets (`gate=portfolio`, `gate=portfolio_unresolved`), capital/B4-style ops lines, warmup / data API backoff |

**Correlation:** Prefer **`correlation_id=`** (present in both worlds when an intent is evaluated) to match a Nautilus **`copy_skip`** line with a Tyrex **`tyrex_risk_ops`** line for the same decision.

**Console:** `HTTP Request:` and **`print`** banner lines may **only** appear on the terminal — not a bug in the log files.

---

## 4. Validation of the five Phase A / B questions

For each question: **evidence per file**, **patterns**, **good vs bad**, **limits**.

---

### Q1) Restart behavior acceptable

**Question:** After process start (Nautilus `load_state`/`save_state` off for guru runs), does the node reach a **steady operational state** without persistent “stuck” errors?

| Log | `.nautilus.log` | `.tyrex.log` |
|-----|------------------|--------------|
| **Look for** | Engine / client **READY**-style lines; ongoing **`guru_poll_tick`** / **`event=guru_signal_emitted`** after boot; absence of permanent **ERROR** loops on a single component | **`event=guru_cache_warmup_…`** completion if warmup ran; **`tyrex_pm phase_b:`** present (confirms compose + gate summary emitted) |
| **Good** | Poll ticks and signals resume; transient boot noise then activity | Phase B line present; warmup done or N/A for config |
| **Bad** | Repeated fatal adapter errors; **no** poll ticks long after boot; strategy never logs | Missing Phase B line after successful compose (unexpected); warmup **ERROR** stuck |

**Direct vs inferential:** Mostly **inferential** — logs show **symptoms** (tick flow, errors), not a formal “Cache converged” proof. Pair with [Implementation/phase_a_closure.md](Implementation/phase_a_closure.md) expectations.

---

### Q2) Quote / mark coverage good enough for B2

**Question:** Are prices/marks **available enough** that portfolio / notional logic is not **permanently** blind?

| Log | `.nautilus.log` | `.tyrex.log` |
|-----|------------------|--------------|
| **Look for** | **`reason_code=risk_denied`** with **`risk_detail=`** including **`risk_missing_price`** or position/unmapped instrument codes; **`guru_dynamic_*`** / activation failures | **`event=tyrex_risk_ops`** with portfolio gate; **`WARNING`** from risk about **partial marks** / omitted instruments (`portfolio cap: filled leg used partial marks…`) |
| **Good** | Occasional skips; most intents either pass risk or fail for **cap/concurrency**, not perpetual missing price | Warnings rare; if present, documented understimate path only when **`fail_on_unresolved_portfolio_exposure`** allows |
| **Bad** | **Sustained** **`risk_missing_price`** or unresolved instrument paths for active guru tokens | Frequent **`tyrex_risk_ops`** **`gate=portfolio_unresolved`** (see Q3) |

**Direct vs inferential:** **Mixed** — missing-price skips are **direct** evidence of quote gaps; full “every instrument marked” needs deeper state inspection.

---

### Q3) `RISK_PORTFOLIO_EXPOSURE_UNRESOLVED` manageable or too frequent

**Question:** Is B1/B2 fail-closed behavior **explainable** (sparse) vs **dominating** runs?

| Log | `.nautilus.log` | `.tyrex.log` |
|-----|------------------|--------------|
| **Look for** | **`event=copy_skip`**, **`reason_code=risk_denied`**, **`risk_detail=risk_portfolio_exposure_unresolved`** (enum value in telemetry) | **`event=tyrex_risk_ops`**, **`reason=RISK_PORTFOLIO_EXPOSURE_UNRESOLVED`** (or same token), **`gate=portfolio_unresolved`** with **`b1_complete=`**, **`e_portfolio_present=`**, **`b1_error=`** |
| **Good** | Rare denials; clustered only at startup or after known data gaps | Tyrex lines show **transient** incomplete B1; errors empty or short |
| **Bad** | **High rate** of exposure-unresolved skips for long stretches | **`b1_complete=False`** or **`e_portfolio_present=False`** repeatedly; large **`b1_error`** snippets |

**Direct vs inferential:** **Direct** — reason strings and Tyrex ops fields are authoritative for **why** the gate fired.

---

### Q4) Exposure scalar behaves sensibly

**Question:** When B2 **approves**, does **`e_portfolio`** / cap math look **consistent** with expectations (no absurd jumps without explanation)?

| Log | `.nautilus.log` | `.tyrex.log` |
|-----|------------------|--------------|
| **Look for** | **`copy_skip`** with **`risk_detail=risk_portfolio_notional_cap_exceeded`** vs approvals | **`event=tyrex_risk_ops`**, **`gate=portfolio_cap`**, **`e_portfolio=`**, **`intent_notional=`**, **`cap=`**, **`sum=`** on deny lines |
| **Good** | Denials when **`sum > cap`**; approvals when under cap given your book | Numeric fields **parseable**; denies align with configured cap |
| **Bad** | Cap denies with **zero** or nonsense components when book is flat (investigate marks/readers) | **Missing** cap lines but Nautilus shows notional cap skip (inconsistent — investigate) |

**Direct vs inferential:** **Direct** on **deny** path (Tyrex logs publish scalars). **Approvals** often have **less** scalar detail in logs — **inferential** without extra instrumentation.

---

### Q5) Operator experience clear enough during incidents

**Question:** During an incident, can an operator **find** what failed **without** reverse-engineering the binary?

| Log | `.nautilus.log` | `.tyrex.log` |
|-----|------------------|--------------|
| **Look for** | **`event=`** tokens; **`correlation_id=`**; **`CopyStrategy`** **ERROR**; **`guru_poll_error`** | **`tyrex_risk_ops`**; **`tyrex_pm phase_b:`**; **`WARNING`** paragraphs; Phase A / warmup messages |
| **Good** | Skip lines include **`reason_code`** / **`risk_detail`**; errors include component id | Policy denials include **gate** and **reason** |
| **Bad** | Generic exceptions with no **`event=`** or id | No Tyrex line matching an observed deny |

**Direct vs inferential:** **Mostly direct** if grepping **`event=`** / **`tyrex_risk_ops`**; **limit:** raw **`HTTP`** and **`print`** banner exist **only** on console for default setup.

---

## 5. Grep / check recipes

Run from **repo root** (adjust paths for shadow or `--log-name`).

```bash
# Phase B / policy (Tyrex)
rg "tyrex_pm phase_b:|tyrex_risk_ops" logs/live/run_tyrex.log

# Portfolio unresolved vs cap
rg "portfolio_unresolved|portfolio_cap|RISK_PORTFOLIO" logs/live/run_tyrex.log

# Strategy outcomes (Nautilus)
rg "copy_skip|risk_denied|guru_signal_emitted|live_order_intent" logs/live/run_nautilus.log

# Warmup / data API
rg "guru_cache_warmup|poller_backoff" logs/live/run_tyrex.log
```

**Windows:** use `findstr` if `rg` unavailable, e.g.  
`findstr /i "tyrex_risk_ops copy_skip" logs\live\run_tyrex.log logs\live\run_nautilus.log`

---

## 6. Limits of log-based validation

- **No full transcript:** `print` and `httpx` may be **console-only**.
- **Nautilus file format** is framework-defined; not guaranteed to match every ANSI console decoration.
- **“Good restart”** and **complete marks** are **not** fully provable from logs alone — use operational runbooks and optional metrics.
- **Silent success** on approve paths may leave **fewer** numeric breadcrumbs than deny paths.

---

## 7. Recommended next checks after log review

1. [Implementation/phase_b_operational_validation.md](Implementation/phase_b_operational_validation.md) — live checklist, restart reality, mark requirements.
2. [Implementation/phase_a_closure.md](Implementation/phase_a_closure.md) — pending leaves, capital gate, reader sources.
3. [OPERATIONS.md](OPERATIONS.md) — reason-code cheat sheet, execution paths.
4. Re-run with **`--log-name`** when filing an issue so artifacts are not overwritten.

---

## Revision history

- **2026-04:** Initial playbook (documentation only).
- **2026-04:** Moved from `Docs/Implementation/` to `Docs/` as durable operator reference.
