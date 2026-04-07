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

### 2.4 Alternate risk profiles (log-focused runs)

Two optional risk YAMLs exercise different gate combinations without editing the starter `guru_follow_risk.yaml`. Use **`--log-name`** and a **live** runtime YAML (e.g. `config/runtime/live_polymarket.yaml` or `config/runtime/live_polymarket_phaseb_validate.yaml`). **`execution_mode: live`** uses Nautilus Polymarket data and execution and `submit_order` for guru orders.

- **Portfolio cap + concurrent resting limit:** `config/risk/guru_follow_risk_phaseb_b2_b3_validate.yaml`
- **Collateral reserve + capital gate:** `config/risk/guru_follow_risk_phaseb_b4_validate.yaml`

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
| **`run_tyrex.log`** | Tyrex **policy** and **Phase B plumbing**: **`tyrex_pm phase_b:`**, **`event=tyrex_risk_ops`**, deployment-cap snippets (`gate=portfolio_cap`, `gate=portfolio_unresolved`), capital/B4-style ops lines, warmup / data API backoff |

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

### Q2) Price reference + deployment data good enough for risk

**Question:** Is **`price_ref`** usually present for notional checks, and is **position/order** state sane enough that deployment caps are not **permanently** blind?

| Log | `.nautilus.log` | `.tyrex.log` |
|-----|------------------|--------------|
| **Look for** | **`reason_code=risk_denied`** with **`risk_detail=`** including **`risk_missing_price`** or instrument/Dynamic-resolve codes; **`guru_dynamic_*`** failures | **`event=tyrex_risk_ops`** with **`gate=portfolio_unresolved`** or token deployment unresolved |
| **Good** | Occasional skips; most denials are **cap/concurrency**, not perpetual missing price | Rare **`portfolio_deploy`/`token_deploy` unresolved** when strict flags on; if **`fail_on_unresolved_*_deployment`** is false, expect possible underestimate instead of deny |
| **Bad** | **Sustained** **`risk_missing_price`** for active guru tokens | Frequent **`RISK_PORTFOLIO_DEPLOYMENT_UNRESOLVED`** / **`RISK_TOKEN_DEPLOYMENT_UNRESOLVED`** (see Q3) |

**Direct vs inferential:** Missing-price skips are **direct**; full book reconciliation needs `Portfolio` / `Cache` inspection beyond logs.

---

### Q3) `RISK_PORTFOLIO_DEPLOYMENT_UNRESOLVED` manageable or too frequent

**Question:** When strict deployment flags are on, is fail-closed behavior **explainable** (sparse) vs **dominating** runs?

| Log | `.nautilus.log` | `.tyrex.log` |
|-----|------------------|--------------|
| **Look for** | **`event=copy_skip`**, **`reason_code=risk_denied`**, **`risk_detail=risk_portfolio_deployment_unresolved`** / **`risk_token_deployment_unresolved`** (or **legacy** `risk_portfolio_exposure_unresolved` in **old** runs) | **`event=tyrex_risk_ops`**, **`gate=portfolio_unresolved`**, deployment facts on **`risk_decision`** / ops snippet |
| **Good** | Rare denials; clustered at startup or after known reconciliation gaps | Transient unparseable positions; clears as `Portfolio` fills in |
| **Bad** | **High rate** of deployment-unresolved skips | Persistent unresolved with no adapter/`Portfolio` progress |

**Direct vs inferential:** **Direct** — reason strings and Tyrex ops fields are authoritative for **why** the gate fired.

---

### Q4) Deployment totals behave sensibly

**Question:** When B2 **approves**, do **`portfolio_deploy` / `token_deploy`** (pending + filled cost basis) look **consistent** with the book (no absurd jumps without explanation)?

| Log | `.nautilus.log` | `.tyrex.log` |
|-----|------------------|--------------|
| **Look for** | **`copy_skip`** with **`risk_portfolio_deployment_exceeded`** / legacy **`risk_portfolio_notional_cap_exceeded`** vs approvals | **`event=tyrex_risk_ops`**, **`gate=portfolio_cap`**, cap / deploy fields on deny lines; **`risk_decision`** facts in reporting |
| **Good** | Denials when **`portfolio_deploy + order_deploy > cap`**; approvals when under cap | Numeric fields **parseable**; denies align with configured cap |
| **Bad** | Cap denies with **zero** deploy but flat book (investigate readers); or cap never fires when book is huge | **Missing** ops detail but Nautilus shows cap skip (inconsistent — investigate) |

**Direct vs inferential:** **Direct** on **deny** path when ops/logging includes scalars. **Approvals** may show less detail — use structured reporting if enabled.

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
- **“Good restart”** and full **deployment** reconciliation are **not** fully provable from logs alone — use operational runbooks and optional reporting summaries.
- **Silent success** on approve paths may leave **fewer** numeric breadcrumbs than deny paths.

---

## 7. Recommended next checks after log review

1. [Implementation/phase_b_operational_validation.md](Implementation/phase_b_operational_validation.md) — live checklist; [Runbooks/deployment_budget_live_validation.md](Runbooks/deployment_budget_live_validation.md) — exact CLI + report fields.
2. [Implementation/phase_a_closure.md](Implementation/phase_a_closure.md) — pending leaves, capital gate, reader sources.
3. [OPERATIONS.md](OPERATIONS.md) — reason-code cheat sheet, execution paths.
4. Re-run with **`--log-name`** when filing an issue so artifacts are not overwritten.

---

## Revision history

- **2026-04:** Initial playbook (documentation only).
- **2026-04:** Moved from `Docs/Implementation/` to `Docs/` as durable operator reference.
