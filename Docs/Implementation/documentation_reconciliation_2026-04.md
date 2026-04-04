# Documentation reconciliation — April 2026

This file records **doc-vs-code mismatches found** during a reconciliation pass, **corrections applied**, and **remaining uncertainties** (mostly upstream).

---

## 1. Mismatch inventory (pre-correction)

| File | Issue | Was wrong / incomplete | Code / truth |
|------|--------|-------------------------|--------------|
| `road_map.md` | § “Baseline today” | Implied **all** deployments use empty `TradingNode` + py-clob only | **Optional** Path A + **framework submit** + readers; legacy path still exists |
| `road_map.md` | § Concrete Step 4/5 | Roadmap “Step 4 = Phase B” conflated with **engineering Step 4** (framework submit) | Added **naming notes** + pointer to `current_state.md` |
| `Architecture.md` | § A, C, D–G | “Empty exec_clients”, “py-clob only”, no `NautilusGuruExecutionPort`, no position/capital in risk | **Polymarket live clients** when flag set; **three** execution ports; **state_readers** + capital gate |
| `OPERATIONS.md` | § Modes | Live = only `PolymarketExecutionPolicy` | **Framework** vs **legacy** live paths; zero-bootstrap; capital gate |
| `CONFIG_MODEL.md` | Risk + Runtime tables | Missing Polymarket flags + Phase A risk fields; session exposure wording outdated | Extended tables from **`loaders.py`** |
| `step_3_runtime_integration.md` | § Feature flag, C, D | Non-empty `instrument_ids` required; “dual submit” always; “remains for Step 4” list stale | Empty list + framework submit; **Phase A** superseded items |
| `step_4_runtime_integration.md` | § Wiring, D–E | Non-empty `polymarket_instrument_ids` required; Phase B prelude wording | **Zero-bootstrap**; **leaves** + **Phase A** position/capital |
| `step_5_runtime_integration.md` | — | Risk/pending detail not cross-linked | Added **§ H** + hub link |
| `Docs/modules/*/README.md` (runtime, risk, execution) | — | Described pre-Path-A only | Rewritten to **current** ports + readers |
| `strategy/README.md` | Live port | Only py-clob | **NautilusGuruExecutionPort** branch |
| `DEVELOPMENT.md` | Compose | Empty node + py-clob only | Conditional clients + ports |
| `README.md` | Doc list | No hub for migration state | Added **current_state**, roadmap, reconciliation, phase_a |
| `polymarket_cache_seeding_decision.md` | — | No pointer to dynamic/zero-bootstrap | **2026 context** lead-in |

**Duplicates / conflict sources:** Roadmap **numbered steps** vs **engineering step_*_runtime_integration.md** — mitigated via **`current_state.md`** and roadmap **naming notes**.

**2026-04 (Phase B5):** **`OPERATIONS.md`** § Phase B — runtime matrix, inert-settings notes, Phase B **ReasonCode** table, startup **INFO** line; code: **`phase_b_startup.py`**, compose log, **`run_guru.py`** logger **INFO** for `tyrex_pm`.

**2026-04 (pre–Phase C):** **`phase_b_operational_validation.md`** — cross-cutting stabilization checklist (restart, adapter marks, `E_portfolio` / `abs(E_filled_net)` ops notes, B2 denial frequency); **`Phase_B_planing.md`** header updated to **implemented** + pointer to this pass.

**2026-04:** **`phase_ab_test_validation_matrix.md`** — Phase A+B: **pytest-proven** vs **docs-only** vs **live validation** grid (restart, marks, `RISK_PORTFOLIO_EXPOSURE_UNRESOLVED`, scalar, operator clarity).

---

## 2. Files changed (this pass)

- `Docs/Implementation/road_map.md`
- `Docs/Implementation/current_state.md` **(new)**
- `Docs/Implementation/step_3_runtime_integration.md`
- `Docs/Implementation/step_4_runtime_integration.md`
- `Docs/Implementation/step_5_runtime_integration.md`
- `Docs/Implementation/phase_a_closure.md`
- `Docs/Implementation/polymarket_cache_seeding_decision.md`
- `Docs/Implementation/documentation_reconciliation_2026-04.md` **(this file)**
- `Docs/Architecture.md`
- `Docs/OPERATIONS.md`
- `Docs/CONFIG_MODEL.md`
- `Docs/DEVELOPMENT.md`
- `Docs/modules/README.md`
- `Docs/modules/runtime/README.md`
- `Docs/modules/risk/README.md`
- `Docs/modules/execution/README.md`
- `Docs/modules/strategy/README.md`
- `README.md`

**Code / config:** None required for truth alignment (YAML comments already extended in prior Phase A work).

---

## 3. Major corrections (summary)

- **Maintainer hub:** `current_state.md` centralizes architecture matrix, roadmap mapping, failure classes, restart reality.
- **Roadmap:** Historical baseline labeled; **implementation snapshot** table added; **Step 4/5 disambiguation** from engineering milestones.
- **Architecture / OPERATIONS / CONFIG_MODEL:** Describe Nautilus live + framework submit + zero-bootstrap + capital gate + leaves-based pending.
- **Step docs:** Step 3/4 **limitations and “what’s next”** brought in line with Step 5 + Phase A closure.

---

## 4. Unresolved / upstream-blocked doc questions

| Topic | Why documentation cannot be sharper |
|-------|-------------------------------------|
| Exact **order/event latency** and **reconciliation** guarantees | **Nautilus Polymarket adapter** implementation / version dependent |
| Whether **`Portfolio.net_exposure`** always matches **venue** holdings | Requires adapter + market data / position event behavior |
| Safe use of **`load_state` / `save_state`** for Polymarket | Not enabled in Tyrex; product decision + upstream validation |

---

## 5. Recommended docs after Phase B

- **Phase B integration note** when concurrent follow / reserve rules land.
- **`road_map.md` Phase B** subsection — replace snapshot row with “in progress” + link.
- **`CONFIG_MODEL.md`** — new risk/runtime keys for Phase B.
- **Architecture diagram** — optional separate “Phase B control plane” if new runtime components appear.

---

## 6. Maintainer summary (current state)

**Tyrex guru live** may use **legacy py-clob** or **Nautilus framework submit** (mutually exclusive submit semantics per config). **Risk** consumes **injected readers**: pending (**leaves**), **filled** (`net_exposure`), optional **capital gate**. **Strategy stays thin.** **Restart** is **not** durable Tyrex state — **`load_state=False`**. **Phase B** (roadmap) is **next implementation focus** for richer exposure rules; **Phase C** follow-policy knobs remain **intentionally deferred**.
