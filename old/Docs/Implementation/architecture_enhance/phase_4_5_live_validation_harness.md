# Phase 4.5 — Live Validation Harness

**Program:** [README.md](README.md) · **Prev:** [phase_4_protection_engine.md](phase_4_protection_engine.md) · **Next:** [phase_4_6_paired_binary_strategy_production_protection.md](phase_4_6_paired_binary_strategy_production_protection.md) · **Wiring plan:** [phase_4_5_live_validation_wiring_plan.md](phase_4_5_live_validation_wiring_plan.md)

> **Operator validation tool — not a production strategy.** Exercises Phase 2–4 paths that `simple_signal_test` cannot reach.

**Status:** harness + live wiring **implemented**. Levels 1–2 **live-validated**; Levels 3/5/6 **live-ready** (operator runs + fact inspection pending). Level 4 **shadow-only synthetic** by design.

---

## 1. Goal

Build a configurable live/shadow validation harness that deliberately produces the actions required to prove the new architecture end-to-end with minimal notional and clear `facts.jsonl` evidence.

`simple_signal_test` is live-validated for Phase 1 + Phase 3 normal entry only. It does **not** exercise:

- `MarketStateStore` runtime feed
- urgent exit planning / FAK
- stale-book deny
- protection registration after allocation-final (CONFIRMED) evidence
- protection TP/SL trigger → urgent `ExitIntent` → planner → OMS

Phase 4.5 closes that gap **before** Phase 5 (portfolio) or Phase 6 (hard kill).

---

## 2. Package layout

```text
src/tyrex_pm/signals/validation_signal.py
src/tyrex_pm/strategies/validation_harness/strategy.py
src/tyrex_pm/runtime/validation_harness_run.py
src/tyrex_pm/runtime/validation_harness_live.py
src/tyrex_pm/runtime/finality_waiter.py
src/tyrex_pm/runtime/protection_supervisor.py
src/tyrex_pm/runtime/market_data_runtime.py
src/tyrex_pm/runtime/protection_runtime.py

config/strategies/validation_harness.yaml
config/strategies/validation_harness_market_data_readonly.yaml
config/strategies/validation_harness_urgent_exit_live.yaml
config/strategies/validation_harness_protection_register_live.yaml
config/strategies/validation_harness_protection_trigger_live.yaml
config/scenarios/shadow_validation_harness.yaml
config/scenarios/live_validation_harness_tiny.yaml
config/scenarios/live_market_data_readonly.yaml
config/scenarios/live_validation_urgent_exit.yaml
config/scenarios/live_validation_protection_register.yaml
config/scenarios/live_validation_protection_trigger.yaml
```

Strategy kind: `validation_harness` (`STRATEGY_KIND_VALIDATION_HARNESS`).

---

## 3. Harness modes

Set `validation.mode` in strategy YAML:

| Mode | Purpose | Live risk | Status |
|------|---------|-----------|--------|
| `normal_entry` | Phase 1 + 3 normal BUY path | Tiny BUY ($5 cap) | **live-validated** |
| `market_data_readonly` | REST bootstrap + snapshot health, no orders | Live read-only | **live-validated** |
| `urgent_exit` | FAK urgent SELL via planner | SELL vs owned allocation | shadow-validated; **live-ready** via dedicated YAML |
| `stale_book_deny` | Planner deny when book stale/missing | Shadow-only synthetic | **shadow-only synthetic** |
| `protection_register_only` | Register TP/SL after allocation-final (CONFIRMED) | Tiny BUY | shadow-validated; **live-ready** via dedicated YAML |
| `protection_trigger_tp` / `protection_trigger_sl` | Take-profit / stop-loss → urgent exit | Shadow with seeded allocation | **shadow-only synthetic** |
| `protection_trigger_live` | Live supervisor tick loop | SELL if market triggers | **live-ready** (6A wiring; 6B market-dependent) |

All modes respect architecture boundaries: intents only, no venue I/O in strategy, all submits through `RiskEngine` → optional planner → `SingleWriterOMS`, all SELLs clamp to allocation.

### Live fixture policy (Correction 3)

When `execution_mode=live`, config/preflight **rejects**: `use_fixture_book=true`, `seed_allocation_qty`, `allow_seed_allocation=true`, manual fake protection registration. Fixtures are allowed in shadow/fixture scenarios only.

### Protection registration boundary (Correction 1)

Protection registers only when `fill_state.is_allocation_final(status)` is true (CONFIRMED). `allocation_buy_applied` from MATCHED-only evidence does **not** register protection. Live Level 5 uses `FinalityWaiter` before `register_protection_after_finality`.

---

## 4. Runtime wiring (P4.5)

| Wiring | Location | Status |
|--------|----------|--------|
| `market_data.enabled` → `MarketStateStore` on coordinator | `market_data_runtime.ensure_market_state_store` in `app.py` | **wired** |
| Live REST bootstrap + refresh loop | `bootstrap_market_state`, `market_data_rest_refresh_loop` in `app.py` | **wired** |
| `protection.enabled` → `ProtectionMonitor` | `protection_runtime.init_protection_monitor` in `app.py` | **wired** |
| Register after allocation-final evidence | `pipeline` → `maybe_register_protection_after_buy` (gated on `is_allocation_final`) | **wired** |
| Live finality wait before register | `finality_waiter.wait_for_allocation_final` → `register_protection_after_finality` | **wired** (Level 5) |
| Protection trigger → pipeline | `run_protection_tick` / `ProtectionSupervisor` → `process_intent_work_unit` | **wired** |
| Periodic live tick supervisor (validation) | `protection_supervisor.run_protection_supervisor` | **wired** (Level 6 harness) |
| Periodic live tick supervisor (production guru) | `app.py` background task | **not wired** — deferred to [Phase 4.6](phase_4_6_paired_binary_strategy_production_protection.md) (paired binary first) |

---

## 5. Live validation ladder

### Level 1 — normal entry, planner, tiny live (**live-validated**)

```bash
python -m tyrex_pm.runtime.app run \
  --strategy config/strategies/validation_harness.yaml \
  --scenario live_validation_harness_tiny \
  --run-name validation_live_entry
```

Expected facts: `signal_received`, `intent_created`, `risk_decision`, `execution_plan`, `risk_decision` with `"phase":"planned"`, `oms_submit` with `planner_reason=planner_normal_entry`.

### Level 2 — live read-only MarketStateStore (**live-validated**)

```bash
python -m tyrex_pm.runtime.app run \
  --strategy config/strategies/validation_harness_market_data_readonly.yaml \
  --scenario live_market_data_readonly \
  --run-name validation_market_data_readonly
```

Expected: `market_data_snapshot` health facts with bid/ask/mid; `is_stale: false` while fresh; **no** `intent_created`, **no** `oms_submit`.

### Level 3 — live urgent FAK exit (**live-ready**)

**Precondition:** Level 1 BUY on same token; allocation + venue position > 0.

```bash
python -m tyrex_pm.runtime.app run \
  --strategy config/strategies/validation_harness_urgent_exit_live.yaml \
  --scenario live_validation_urgent_exit \
  --run-name validation_live_urgent_exit
```

Pass: preflight ok → real `MarketStateStore` book → `execution_plan execution_style=FAK planner_reason=planner_urgent_exit_fak` → `oms_submit` SELL → no fixture evidence.

Shadow regression (fixtures explicit):

```bash
python -m tyrex_pm.runtime.app run \
  --strategy config/strategies/validation_harness.yaml \
  --scenario shadow_validation_harness \
  --run-name validation_urgent_exit_shadow
```

(Set `validation.mode: urgent_exit`, `use_fixture_book: true`, `seed_allocation_qty` in strategy YAML.)

### Level 4 — stale-book deny (**shadow-only synthetic**)

Set `validation.mode: stale_book_deny`, `use_fixture_book: true`, `seed_allocation_qty` (shadow only).

Expected: `execution_plan` with `approved=false`, `planner_reason=planner_stale_book`; **no** `oms_submit`.

### Level 5 — live protection register (**live-ready**)

```bash
python -m tyrex_pm.runtime.app run \
  --strategy config/strategies/validation_harness_protection_register_live.yaml \
  --scenario live_validation_protection_register \
  --run-name validation_live_protection_register
```

Pass: `oms_submit` BUY → `health event=finality_wait final=true status=CONFIRMED` → `protection_register`.

### Level 6 — live protection supervisor / trigger (**live-ready**)

**Precondition:** Level 5 completed (or venue position re-register path).

```bash
python -m tyrex_pm.runtime.app run \
  --strategy config/strategies/validation_harness_protection_trigger_live.yaml \
  --scenario live_validation_protection_trigger \
  --run-name validation_live_protection_trigger
```

| Sub-level | Pass criteria |
|-----------|---------------|
| **6A — supervisor wiring proven** | `protection_tick`, real `MarketStateStore`, no fixtures, clean supervisor stop |
| **6B — TP/SL trigger proven** | `protection_trigger` → urgent exit chain → FAK `execution_plan` → `oms_submit` SELL |

Shadow trigger modes (`protection_trigger_tp` / `protection_trigger_sl`) remain architecture regression paths with fixtures.

---

## 6. Component readiness matrix

| Component | Unit-tested | Shadow-runnable | CLI-runnable | Live-read-only | Tiny-live-order | Production-ready |
|-----------|-------------|-----------------|--------------|----------------|-----------------|------------------|
| Generic dispatch (P1) | yes | yes | yes | n/a | yes | yes (entry) |
| MarketStateStore (P2) | yes | yes (fixture inject) | yes | yes (**live-validated**) | n/a | partial (REST refresh) |
| ExecutionPlanner entry (P3) | yes | yes | yes | n/a | yes | yes (live-validated) |
| ExecutionPlanner urgent/FAK (P3) | yes | yes (harness) | yes | n/a | **live-ready** | not live-proven yet |
| Stale-book deny (P3) | yes | yes (harness) | yes | n/a | n/a | shadow-validated |
| Fill finality (P3.5) | yes | indirect | indirect | n/a | indirect | yes (user-WS path) |
| FinalityWaiter (P4.5) | yes | yes | yes (Level 5) | n/a | **live-ready** | harness-only |
| ProtectionEngine (P4) | yes | yes (harness) | yes | n/a | shadow-first | not production-live-ready |
| ProtectionSupervisor (P4.5) | yes | yes | yes (Level 6) | n/a | **live-ready** | harness-only |
| Validation harness (P4.5) | yes | yes | yes | yes | yes | operator tool only |

---

## 7. Tests

```text
tests/test_validation_harness_runtime.py
tests/test_market_data_runtime.py
tests/test_protection_runtime_wiring.py
tests/test_live_harness_wiring.py
tests/test_finality_waiter.py
tests/test_protection_supervisor.py
```

Run:

```bash
pytest tests/test_validation_harness_runtime.py \
       tests/test_market_data_runtime.py \
       tests/test_protection_runtime_wiring.py \
       tests/test_live_harness_wiring.py \
       tests/test_finality_waiter.py \
       tests/test_protection_supervisor.py -q
```

---

## 8. Fact requirements

Paths must be provable from `facts.jsonl`:

| Path | Facts |
|------|-------|
| Signal → intent | `signal_received`, `intent_created` |
| Risk + planner | `risk_decision`, `execution_plan`, `risk_decision` `"phase":"planned"` |
| OMS | `oms_submit` (`planner_reason` when planner enabled) |
| Allocation | `allocation_buy_applied`, `allocation_ledger` |
| Finality wait | `health event=finality_wait` |
| Protection | `protection_register`, `protection_tick`, `protection_trigger` |
| Exit lifecycle | `exit_lifecycle` |
| Preflight | `health event=validation_harness_preflight` |
| Supervisor | `health event=protection_supervisor_stopped` |

---

## 9. Exit criteria (before Phase 5)

Phase 5 may start when the harness can run at minimum:

- [x] `normal_entry` (shadow + live-validated)
- [x] `market_data_readonly` (live-validated)
- [x] `urgent_exit` (shadow + live-ready wiring)
- [x] `stale_book_deny` (shadow synthetic)
- [x] `protection_register_only` (shadow + live-ready wiring)
- [x] `protection_trigger_live` supervisor (live-ready wiring)

Operator live runs for Levels 3/5/6 should be green before treating production TP/SL as live-ready.

---

## 10. Non-goals

- Not a production trading strategy.
- Does not replace `tp_sl_test` regression tests (keep both).
- Does not implement portfolio views (Phase 5) or hard kill (Phase 6).
- Does not mark production TP/SL ready — harness validation only.
