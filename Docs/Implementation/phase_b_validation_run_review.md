# Phase B validation run review — log evidence (2026-04-04)

## 1. Purpose

This document reviews the **actual** log output from two dedicated Phase B validation runs (`phaseb-b2b3-validate` and `phaseb-b4-validate`). It compares **intended** controls (from the validation YAML profiles) to **observed** traces in:

- `logs/live/phaseb-b2b3-validate_nautilus.log` / `_tyrex.log`
- `logs/live/phaseb-b4-validate_nautilus.log` / `_tyrex.log`

No code or config changes are implied here; this is an operator-facing, evidence-based read.

---

## 2. Run 1 — B2 + B3 validation review

### 2.1 Startup / config evidence

**Tyrex (`phaseb-b2b3-validate_tyrex.log` line 1–2):**

- Warmup: `event=guru_cache_warmup_done … distinct=32 warmed=32 cap=32` — guru cache warmup **completed** (32 tokens, at cap).
- Phase B banner: `framework_truth_eligible=True b1_aggregator_wired=True portfolio_notional_cap_usd=75.0 max_concurrent_guru_resting_orders=2 fail_on_unresolved_portfolio_exposure=True collateral_reserve_usd=0.0 capital_gate_enabled=False`

This matches the **B2/B3 validation profile**: finite portfolio cap **75 USD**, concurrency limit **2**, unresolved portfolio exposure fail-closed **on**, **no** capital gate / **no** collateral reserve.

**Nautilus (`phaseb-b2b3-validate_nautilus.log`):**

- Standard live node bring-up: `CopyStrategy` / `GuruMonitorActor` **READY**, `DataClient-POLYMARKET` / `ExecClient-POLYMARKET` registered — consistent with **live + Polymarket** execution path (framework stack present).
- Portfolio init: account ~`21.055462 USDC.e` free; reconciliation left a **non-flat** instrument with position `net_position=0.002360` on `0x2c30b81444bac352bfd71c06205cc3d5566bd8fb3dce9b0cffeb0273c4ff2027-103139329656013793828819922181573721978242961603120877932544887879956810152574.POLYMARKET` (lines ~301–335).

**Conclusion (startup):** The logs **clearly reflect** the intended validation risk YAML (B2/B3 knobs on, B4 off). Framework-truth eligibility is **on** per Tyrex banner.

### 2.2 Intended behaviors vs traces

| Intended behavior | Appeared? | Evidence | Operational meaning |
|-------------------|-----------|----------|---------------------|
| **B2 — portfolio notional cap** (`E_portfolio + n` vs **75**) | **Not observed** | No `risk_detail=risk_portfolio_notional_cap_exceeded` (or equivalent) in the Nautilus log. | The **numeric cap comparator** never became the binding deny reason in this run. |
| **Fail-closed on unresolved portfolio exposure** (related B2 pre-req / B1) | **Yes — dominant** | Nautilus: many `event=copy_skip … risk_detail=risk_portfolio_exposure_unresolved` (e.g. lines 523, 535, 539, …). Tyrex: `event=tyrex_risk_ops gate=portfolio_unresolved reason=risk_portfolio_exposure_unresolved … b1_error=filled: unresolved mark for non-flat instrument 0x2c30b81444…POLYMARKET` (throughout `_tyrex.log`). | Pre-trade path **refuses** to size portfolio when **E_portfolio** cannot be computed for an in-scope non-flat position; **correlation_id** matches across Tyrex ↔ Nautilus. |
| **B3 — concurrent guru resting orders** | **Not observed** | No `risk_detail=risk_guru_concurrent_resting_orders_limit` in grep of `logs/live`. | Resting-order cap **did not fire**. Likely no path reached “resting count vs limit” because intents were denied earlier (portfolio unresolved / other caps). |
| **Guru workflow (poll → signal → copy decision)** | **Partial** | Many `event=guru_signal_emitted` (Nautilus); **no** `live_order_submit` in this run (0 matches). Decisions show as `copy_skip` with `reason_code=risk_denied`. | **Signals and copy evaluation** ran; **successful submits did not** appear in this capture. |
| **Other competing denies** | **Yes** | Nautilus `copy_skip` also includes `risk_notional_per_order` and `risk_order_qty_limit` (starter risk limits), mixed with unresolved-portfolio denies. | Basic order / notional guards **interleaved** with Phase B portfolio denies; operators must read **`risk_detail`** per line. |

**Count-style sanity check (Nautilus):** `risk_detail=` appears **122** times total in `_nautilus.log`; **`risk_portfolio_exposure_unresolved` appears 65** times — the **largest single deny class** in that file by inspection.

### 2.3 Workflow (Nautilus) vs decision detail (Tyrex)

- **Nautilus:** End-to-end **flow** — guru signals, `copy_skip` with `reason_code` / `risk_detail`, portfolio / reconciliation context.
- **Tyrex:** **Rich** for this run: every `portfolio_unresolved` decision has **`tyrex_risk_ops`** with B1 fields (`b1_pending_complete`, `e_portfolio_present=False`, `b1_error=…`).

Together, the two files **fully explain why** most copy intents stopped: **unresolved mark** on a **non-flat** instrument under `fail_on_unresolved_portfolio_exposure=True`, plus some **per-order** risk hits.

**Gap:** There is **no** line proving the **75 USD** cap was evaluated and exceeded; the pipeline fails **before** that comparison in the common case shown.

### 2.4 Verdict (run 1)

**Partially validated / B2–B3 sub-goals inconclusive.**

- **Validated:** Startup config reflection, framework-truth banner, warmup, guru **signal emission**, and **strong** operational evidence for **`RISK_PORTFOLIO_EXPOSURE_UNRESOLVED`** (`gate=portfolio_unresolved`) with correlation IDs bridging both logs.
- **Not validated (dormant in practice):** **B2 cap exceed** and **B3 concurrent resting** denies — **no log lines** for those reason codes.
- **Why dormant:** Persistent **unresolved portfolio exposure** (and some **qty/notional** limits) **short-circuits** the path before cap / resting logic would bind; **no orders reached submit**, so B3’s resting accounting would rarely matter.

---

## 3. Run 2 — B4 validation review

### 3.1 Startup / config evidence

**Tyrex (`phaseb-b4-validate_tyrex.log` line 1–2):**

- Warmup: same pattern — `distinct=32 warmed=32 cap=32`.
- Phase B banner: `framework_truth_eligible=True b1_aggregator_wired=True portfolio_notional_cap_usd=off max_concurrent_guru_resting_orders=off fail_on_unresolved_portfolio_exposure=True collateral_reserve_usd=25.0 capital_gate_enabled=True`

This matches the **B4 validation profile**: **capital gate on**, **reserve 25 USD**, **B2/B3 effectively off**.

**Nautilus (`phaseb-b4-validate_nautilus.log`):**

- Live Polymarket clients and strategy registered as in run 1.
- Startup portfolio: **0 open positions** (`Initialized 0 open positions` ~line 327) — **unlike** run 1, reducing the “unresolved non-flat instrument” driver seen there.

### 3.2 Intended behaviors vs traces

| Intended behavior | Appeared? | Evidence | Operational meaning |
|-------------------|-----------|----------|---------------------|
| **B4 — reserve / free-after-reserve deny** | **Not observed** | No `risk_detail=risk_insufficient_free_collateral_after_reserve` (or similar) in Nautilus `copy_skip` lines. Grep of `logs/live` for `risk_guru_concurrent`, `risk_portfolio_notional_cap`, `after_reserve`, `account_unavailable` returned **no** hits for this run’s artifacts. | The **explicit B4 deny** path did **not** surface in captured logs. Passing BUYs are **consistent with** balance ≥ `n + reserve`, but that success is **silent** in `*_tyrex.log` (no per-intent `tyrex_risk_ops` lines). |
| **Capital gate / snapshot fail-closed** | **Not observed as deny** | No `risk_insufficient_collateral_balance`, `risk_insufficient_allowance`, `risk_account_unavailable`, or `risk_allowance_unavailable` in sampled `copy_skip` set. | No **logged** capital/allowance **failure** during this window. |
| **Guru workflow → submit** | **Yes** | Multiple `event=live_order_submit … component=nautilus_guru_exec` with `correlation_id=…` (e.g. lines 372–373, 378–379, 384–385, …). `live_order_intent` lines pair with submits. | Framework **submit path exercised**; pre-trade stack **allowed** these intents. |
| **Competing denies** | **Yes** | `copy_skip` shows only **`risk_order_qty_limit`** and **`risk_notional_per_order`** in this file’s `risk_detail=` lines — no Phase B B4 codes. | **Basic** risk YAML limits dominate **skips**, not B4. |
| **Indirect collateral / account evidence** | **Yes (Portfolio)** | `Portfolio: Updated AccountState … total=21.055462 …` then later **`19.125462`** (~444), then **`3.915462`** (~462) USDC.e free. Position builds on `…1383282271921875843174703084123218721999818852892310693271617956389659394464.POLYMARKET` (e.g. net_position stepping 3.86 → 10.74 → …). | Confirms **real spend-down** of USDC and position accumulation — **consistent with** B4-capable live trading, but **not** a substitute for a **logged B4 deny**. |

### 3.3 Workflow (Nautilus) vs decision detail (Tyrex)

- **Nautilus:** Carries **almost the entire story** for run 2 — signals, skips, submits, `live_order_error` (exchange/min-notional style warnings, e.g. `est=0.43 min=1.0`), and **account balance transitions**.
- **Tyrex:** Only **two substantive lines** after warmup: **warmup done** + **Phase B banner**. **No** `tyrex_risk_ops` for allowed or denied intents in the captured file.

**Gap:** For **B4 validation**, **`_tyrex.log` does not show** per-decision gate diagnostics this run, so **Tyrex alone cannot prove** reserve math; operators rely on **Nautilus** + implicit “submit happened ⇒ risk passed.”

### 3.4 Verdict (run 2)

**Partially validated / B4 deny path inconclusive.**

- **Validated:** Config banner (**capital gate + 25 USD reserve**), warmup, **continuous guru workflow**, and **many successful framework submits**; **Portfolio** lines show **meaningful USDC drawdown** during trading.
- **Not validated:** **`RISK_INSUFFICIENT_FREE_COLLATERAL_AFTER_RESERVE`** (or explicit Tyrex B4 ops lines) — the **intended deny signature** for B4 reserve did **not** appear.
- **Why B4 may look “quiet”:** Wallet retained enough headroom above **25 + n** for the BUY sizes that passed other checks, or denies were dominated by **qty/notional** before B4 bound.

---

## 4. Overall assessment

| Run | Config in logs | Primary observable Phase B–related behavior | Intended “named” gate |
|-----|----------------|-----------------------------------------------|------------------------|
| B2/B3 | **Clear match** | **`risk_portfolio_exposure_unresolved`** / `gate=portfolio_unresolved` | **B2 cap exceed, B3** — **not seen** |
| B4 | **Clear match** | **Live submits** + USDC/position movement; **no B4 reason in skips** | **B4 reserve deny** — **not seen** |

**Logging system usefulness:**

- **Run 1:** The **Nautilus + Tyrex split works well** — correlation IDs and `tyrex_risk_ops` make **portfolio_unresolved** diagnosable.
- **Run 2:** **Nautilus is sufficient for workflow**; **`_tyrex.log` is insufficient** for B4 operational proof beyond the **startup banner** (no per-event Tyrex risk lines in the sample).

---

## 5. Remaining gaps / next recommended steps

1. **B2 cap exceed:** Re-run when **portfolio exposure is fully resolvable** so evaluation reaches **`E_portfolio + n` vs cap**; expect `risk_portfolio_notional_cap_exceeded` / `gate=portfolio` (not only `portfolio_unresolved`). Clearing or fully marking the **non-flat** instrument that caused unresolved marks in run 1 would help.
2. **B3 concurrent resting:** Needs **successful submits** that leave **≥ limit** guru-origin **resting** orders simultaneously; run 1’s early denies prevented that. Consider a quieter market slice, higher per-order caps (without changing semantics beyond validation intent), or a controlled scenario where **portfolioUnresolved** does not dominate.
3. **B4 reserve deny:** After USDC **free** drops near **`reserve + typical n`**, or temporarily raise **`collateral_reserve_usd`**, expect `risk_insufficient_free_collateral_after_reserve` in **`copy_skip`**; consider whether Tyrex should emit **`tyrex_risk_ops` on allow** for capital/B4 in validation modes (future product decision — **not** implemented in this review).
4. **Reduce competing noise:** Large guru notionals will keep hitting **`risk_order_qty_limit` / `risk_notional_per_order`**; for **cleaner** Phase B logs, tune **copy_scale** or validation risk YAML **max_order_quantity / max_notional_usd_per_order** *only when appropriate for validation* (config-only next step).

---

## 6. Final verdicts (summary table)

| Run | Verdict |
|-----|---------|
| **B2/B3 validation** | **Partially validated** — **portfolio unresolved fail-closed** is well demonstrated; **B2 cap exceed** and **B3 concurrent cap** **not demonstrated** in these logs. |
| **B4 validation** | **Partially validated** — **capital gate + reserve configured**, **submits and collateral movement** observed; **B4-specific deny** and **Tyrex per-event B4 traces** **not demonstrated**. |
