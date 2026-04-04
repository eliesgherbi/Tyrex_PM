# Phase A + B — test coverage vs live validation matrix

**Purpose:** Separate what is **proven by automated tests**, what is **only specified in docs / reasoning**, and what **requires live (or staging) sessions** — for the five operational questions: restart truth, mark coverage, unresolved-portfolio denials, exposure scalar fit, operator clarity.

**Companion:** `phase_b_operational_validation.md` (checklist); this file is **test-evidence** focused.

---

## Legend

| Column | Meaning |
|--------|---------|
| **Tests prove** | Behavior asserted in unit/integration tests under `tests/` (often with mocks). |
| **Docs / reasoning** | Described in `Phase_B_planing.md`, `phase_a_closure.md`, `OPERATIONS.md`, module docstrings — not exercised as end-to-end system behavior over time. |
| **Live validation** | Needs real or production-like Nautilus + Polymarket adapter, time, reconnects, and human log review. |

---

## Matrix (by operational question)

### 1. Restart behavior — “after reboot, does the bot recover truth quickly enough to be usable?”

| Layer | Tests prove | Docs / reasoning | Live validation |
|-------|-------------|------------------|-----------------|
| Tyrex **does not** persist kernel state | — | `load_state=False`, `save_state=False` in compose (`phase_a_closure.md`, `guru_compose.py`). | Whether **your** adapter + venue converge fast enough **after** process restart. |
| Pending from `Cache` (leaves × price) | `test_state_readers.py` (open orders → `OrderSnapshot`); `test_configured_risk.py` (pending from cache); `test_portfolio_exposure.py` (pending leg); `test_phase_a_risk.py` | Contract that terminal orders drop out of `orders_open`. | Empty cache at boot until exec syncs; **duration** of wrong/zero pending. |
| Filled from `Portfolio.net_exposure` | `test_portfolio_exposure.py` (`net_exposure` mocked); `test_phase_a_risk.py` (position reader + token cap) | Adapter must repopulate `Portfolio`. | Post-restart **lag** or **missing positions** until venue reconciliation. |
| Capital / allowance snapshots | `test_phase_a_risk.py`, `test_phase_b_b4_reserve.py`, `test_state_readers.py` (allowance provider pattern) | TTL refresh on evaluate. | First **live** read after reboot; clock vs snapshot age. |
| Compose still builds | `test_guru_compose_build.py` (shadow / live / framework / zero-bootstrap mocked node) | — | Full `run_guru.py` + **real** `node.build()` / connect (not default in tests). |

**Explicit gap:** No test simulates **wall-clock reboot → reconnect → first approve/deny** timeline; mocks assume cache/portfolio **already** populated.

---

### 2. Quote / mark coverage — “does the bot usually have enough prices to calculate portfolio exposure?”

| Layer | Tests prove | Docs / reasoning | Live validation |
|-------|-------------|------------------|-----------------|
| Mark resolution **order** (intent `price_ref`, then LAST/MID/MARK, `mark_price`) | `test_portfolio_exposure.py` (`cache_best_mark_float`, intent mark, unresolved strict) | `Phase_B_planing.md` §4.6; `portfolio_exposure.py` docstring. | Whether **Polymarket BinaryOption** (or your instruments) **actually** populate those cache fields in your Nautilus version. |
| **Strict** mode: non-flat + no mark → incomplete B1 | `test_portfolio_exposure.py` (`test_unresolved_mark_fail_closed_strict`) | Default `fail_on_unresolved_portfolio_exposure=true`. | **Fraction of time** any held instrument lacks a mark in real feeds. |
| **Unsafe** mode: omit instruments, still complete | `test_portfolio_exposure.py` (opt-in omissions) | Underestimate + `omitted_instruments_unresolved_mark`. | Ops tolerance for cap slack. |

**Explicit gap:** Tests use **MagicMock** `Cache` — they do **not** prove adapter publishes quotes for **all** live instruments or after reconnect.

---

### 3. `RISK_PORTFOLIO_EXPOSURE_UNRESOLVED` — “rare and useful vs too frequent and blocking?”

| Layer | Tests prove | Docs / reasoning | Live validation |
|-------|-------------|------------------|-----------------|
| Code path: incomplete / no `e_portfolio` → **that** reason | `test_phase_b_b2_portfolio_cap.py` (`complete=False`, `e_portfolio=None`, no aggregator); unsafe still denies incomplete | Single reason string from risk policy; B1 `error` detail **not** forwarded to strategy reason today. | **Denial rate**, time-of-day, correlation with reconnects / new instruments. |
| Cap breach uses **different** code | `RISK_PORTFOLIO_NOTIONAL_CAP_EXCEEDED` tests in same file | — | — |

**Explicit gap:** No test measures **frequency** or **duration** of unresolved state; no load/stress over thousands of intents.

---

### 4. Exposure scalar — “does `E_portfolio` match how you want the follower to behave?”

| Layer | Tests prove | Docs / reasoning | Live validation |
|-------|-------------|------------------|-----------------|
| **Formula** `E_portfolio = E_pending + abs(E_filled_net)` | `test_portfolio_exposure.py` (`test_e_portfolio_is_pending_plus_abs_filled`; signed filled sum in `test_filled_net_sums_signed_net_exposure`) | Plan §4.3 rationale (`abs` for symmetric cap vs net short). | **Business** fit: multi-leg book, hedges — does scalar **feel** right for *your* follow policy (product decision). |
| B2 uses `e_portfolio + n` vs cap | `test_phase_b_b2_portfolio_cap.py` (allow/deny edge, `e_portfolio+n` not double-counting pending) | §4.5 | — |

**Explicit gap:** No test encodes “correct” product preference — only **locked** math.

---

### 5. Operator experience — “when something goes wrong live, can humans understand and act?”

| Layer | Tests prove | Docs / reasoning | Live validation |
|-------|-------------|------------------|-----------------|
| Stable `ReasonCode` strings | Assertions in risk tests (`ReasonCode.RISK_*`) | `OPERATIONS.md` cheat sheet; `reason_codes.py` comments. | Real logs: **signal-to-noise**, multiple lines per deny, Nautilus vs Tyrex logger mix. |
| Phase B startup summary line | `test_phase_b_b5_startup_log.py` | `phase_b_startup.py`; `run_guru.py` sets `tyrex_pm` INFO. | Ops **notice** the line; interpret fields vs YAML. |
| `copy_skip` / `risk_denied` plumbing | Indirect via architecture / strategy tests | Documented in `OPERATIONS.md`. | End-to-end grep on **your** log files. |

**Explicit gap:** B1 aggregate **human-readable `error`** (e.g. `"filled: unresolved mark for …"`) is **not** asserted to appear on the same channel as the copy skip reason; operators may need **debug logging** or future telemetry to distinguish mark vs position vs pending parse failures **without** reading code.

---

## Summary table (Phase A + B features)

| Feature | Tests prove (representative files) | Live validation still needed |
|---------|-----------------------------------|------------------------------|
| B0 compose/runtime validation | `test_phase_b_b0_validation.py`, `test_phase_b_b2_portfolio_cap.py` (B0 path), `test_phase_b_b3_*`, B4 loader tests | — |
| B1 aggregation | `test_portfolio_exposure.py` | Real cache/adapter mark availability, multi-instrument books |
| B2 portfolio cap | `test_phase_b_b2_portfolio_cap.py` (mocked B1) | Denial rate + restart |
| B3 guru concurrent rests | `test_phase_b_b3_concurrent_guru_orders.py` | Tag vs TX fallback with **real** orders |
| B4 reserve | `test_phase_b_b4_reserve.py` | Real balance vs reserve in USD |
| B5 logging | `test_phase_b_b5_startup_log.py` | Visibility in your logging stack |
| Phase A pending leaves | `test_phase_a_risk.py`, `test_state_readers.py`, `test_configured_risk.py` | Open-order visibility after reconnect |
| Phase A token cap + position reader | `test_phase_a_risk.py` | `net_exposure` timeliness |
| Phase A capital gate | `test_phase_a_risk.py`, allowance/account in `test_state_readers.py` | Live account + py-clob alignment |
| Compose wiring | `test_guru_compose_build.py` | Full `TradingNode` run + network |

---

## Second step (your plan)

Running **`run_guru.py`** and **inspecting logs** falls entirely in the **live validation** column for: restart timing, mark gaps, denial frequency, and operator clarity — it does not replace the **gaps** above unless you add **repeatable** scenarios (e.g. scripted restart + fixture) in a future test harness.
