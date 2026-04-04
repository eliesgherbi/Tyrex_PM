# Phase B — operational validation (pre–Phase C stabilization pass)

**Purpose:** Cross-cutting checklist and **honest** risk notes **before** opening Phase C. The design (B0–B5) is in place; the dominant residual risk is **real-session behavior**: restarts, marks, adapter freshness, and whether operators can interpret denials.

**Normative semantics:** `Phase_B_planing.md` §4 (exposure contract). **Code:** `runtime/portfolio_exposure.py` (B1), `risk/configured.py` (B2–B4).

---

## 1. What this pass should answer

| Question | Code-informed answer | What still needs **live** confirmation |
|----------|----------------------|----------------------------------------|
| Do B2/B3/B4 behave correctly over long sessions? | **Yes** given consistent `Cache`/`Portfolio`/py-clob snapshots: same evaluation order every intent; TTL refresh on capital path; B3 counts guru rests from `ExecutionStateReader`. | **Drift:** reconnects, partial adapter updates, or instruments removed from cache while positions exist could change measured exposure until state converges. |
| Are `RISK_PORTFOLIO_EXPOSURE_UNRESOLVED` denials too frequent? | With **default** `fail_on_unresolved_portfolio_exposure=true`, **any** non-flat Polymarket instrument in `Cache` without a resolvable mark (§4.6) makes B1 `complete=false` → B2 **always denies** with that code. That is **intentionally fail-closed**, not a bug. | **Frequency** depends entirely on whether the adapter publishes **LAST/MID/MARK** or `mark_price` for **every** held outcome in your book. Large books + thin marks → more denials until prices warm. |
| Does `E_portfolio = E_pending + abs(E_filled_net)` match operational intuition? | **Documented lock** in §4.3: **`abs(E_filled_net)`** is a **conservative scalar** for the **filled** bucket so the cap is symmetric for net short vs net long at portfolio level; it is **not** “economic liquidation value” or VAR. Per-instrument exposure uses **signed** `net_exposure`. Operators who expect “long-only risk” may find **`abs`** stricter than a long-only book metric — that is the chosen v1 product. | Validate with **your** typical guru positions (one-sided vs hedged). If the scalar systematically feels wrong, that is an **ADR / Phase C** product change, not a silent tweak. |
| Are startup/restart conditions acceptable? | Tyrex builds with **`load_state=False`**, **`save_state=False`**. There is **no** Tyrex-persisted order book; post-boot truth is **venue + Nautilus reconciliation** + optional guru warmup (`phase_a_closure.md` §4). B2/B3 read **current** cache only. | Measure **time-to-first-safe submit**: period where `Portfolio`/`Cache` may be empty or stale vs venue. Acceptable only if ops tolerate **temporary** `RISK_PORTFOLIO_EXPOSURE_UNRESOLVED` or **delayed** trading until books reconnect. |
| Noisy denials / operator confusion? | **Startup line:** `tyrex_pm phase_b: …` (B5). **Portfolio:** warning when `fail_on_unresolved_portfolio_exposure=false` but instruments were omitted (underestimate). B1 aggregate carries `error` text internally but B2 maps incomplete → single **`RISK_PORTFOLIO_EXPOSURE_UNRESOLVED`** without surfacing B1’s `error` to the strategy reason path today — ops must use **logs** if you add B1 debug logging or extend telemetry later. | Grep denials in `copy_skip`; correlate with `Cache` quote gaps and reconnect events. |

---

## 2. Runtime mode notes (shadow vs live)

| Mode | B2 / B3 | B4 reserve |
|------|---------|------------|
| **Shadow** | **Invalid** if configured — compose raises (`OPERATIONS.md`). | **Invalid** if `collateral_reserve_usd > 0`. |
| **Live + framework triple** | Enforced. | Enforced when configured + `capital_gate_enabled`. |
| **Live legacy** | **Invalid** if B2/B3 configured — compose raises. | May be **valid** with capital gate + live allowance provider. |

**Prolonged shadow sessions** with Phase B gates **off** do not exercise B2/B3/B4; stabilization of those gates requires **live framework** test deployments or controlled paper flows where allowed.

---

## 3. Restart and reconciliation (explicit)

1. **Orders:** Open rests should appear in `Cache.orders_open` after exec/client sync; pending leg sums **leaves × price** (`portfolio_exposure._compute_pending`).
2. **Positions:** `Portfolio.is_flat` / `net_exposure` must reflect venue; stale **flat** vs **non-flat** transitions directly change `E_filled_net`.
3. **Marks:** Instruments in cache with positions but **no** price tick may fail strict B1 until data arrives — expect **clustered** denials right after restart, not random flapping, if this is the cause.

**Comfort bar:** If post-restart **deny rate** stays high for minutes without quotes on held instruments, reconciliation or data subscriptions are **too weak for your B2 comfort** — fix upstream (adapter/config) or temporarily widen caps only as a **business** decision, not a code default change.

---

## 4. Adapter freshness / missing state

Typical failure patterns that surface as **`RISK_PORTFOLIO_EXPOSURE_UNRESOLVED`**:

- Missing **`cache_best_mark_float`** path for an instrument (no LAST/MID/MARK, no `mark_price`).
- **`net_exposure`** returns `None` for a non-flat instrument.
- Pending leg: open order with missing or unparsable `price` / `leaves_quantity` (fail-closed for that aggregate).

**B4 (`RISK_ALLOWANCE_UNAVAILABLE`, insufficient reserve):** missing or unparsable py-clob **`balance`** when reserve > 0; **independent** of portfolio marks but same **fail-closed** philosophy.

---

## 5. Suggested live / staging checklist (operators)

1. Enable **`tyrex_pm`** **INFO** (see `run_guru.py`) and capture the **`tyrex_pm phase_b:`** boot line; confirm caps match the risk YAML.
2. Baseline **denial counts per reason** over a session (`risk_denied` / `ReasonCode`).
3. On each **`RISK_PORTFOLIO_EXPOSURE_UNRESOLVED`**, note time since reconnect; inspect whether held instruments had quotes in UI or other tooling.
4. After restart, log **time until first successful** `risk.evaluate` approval for a representative intent (or until stable deny rate).
5. If strict denials dominate with a **large** book, trial **`fail_on_unresolved_portfolio_exposure: false`** only with **conscious** acceptance of **underestimation** (`omitted_instruments_unresolved_mark` warning) — document the ops decision.

---

## 6. Relationship to Phase C

Phase C (follow policy, venue normalize, alternate exposure scalars per ADR) is the right place for **behavioral** tuning once this pass shows **where** pain concentrates (marks vs positions vs scalar definition). **Do not** reinterpret §4.3 **abs** semantics in code without an explicit spec change.

---

## 7. Document index

| Doc | Role |
|-----|------|
| `Phase_B_planing.md` | §4 exposure contract (normative) |
| `OPERATIONS.md` | Phase B runtime matrix + reason cheat sheet |
| `phase_a_closure.md` | Pending leaves, capital, restart |
| `current_state.md` | Hub + roadmap |
| **`phase_ab_test_validation_matrix.md`** | **What tests prove vs docs vs live-only** (Phase A+B) |
| **`logging_workflow_review.md`** | **`run_guru.py`** logging (Tyrex vs Nautilus), grep guide, B1/B2 log gaps |
| **This file** | Pre–Phase C operational risks and checklist |
