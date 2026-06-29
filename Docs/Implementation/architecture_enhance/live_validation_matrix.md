# Live validation matrix — Phases 1–4

**Purpose:** Concrete verification of what was implemented in architecture_enhance Phases 1, 2, 3, 3.5, and 4 — what is **live-runnable now**, what is **shadow/CLI-runnable**, what is **unit-only**, and what is **implemented but runtime-unwired**.

**Labels used throughout:**

| Label | Meaning |
|-------|---------|
| `unit-tested` | Passes pytest only; no CLI/runtime path |
| `fixture-tested` | Exercised via captured fixtures / in-process test harness |
| `shadow-runnable` | `tyrex-pm run` in shadow mode produces real facts |
| `CLI-runnable` | `python -m tyrex_pm.runtime.app run ...` exercises the path |
| `live-read-only-runnable` | Live network read (books/wallet sync) with **no order submit** |
| `tiny-live-order-runnable` | Real CLOB submit with controlled notional |
| `production-live-ready` | Safe to enable in production without missing wiring |

Cross-reference: [architecture_enhance README](README.md) · [OPERATIONS.md](../../OPERATIONS.md)

---

## 1. Executive verdict

| Question | Answer | Status |
|----------|--------|--------|
| **Can we move to Phase 5 now?** | **Yes, with documented gaps.** Phase 5 is read-only portfolio foundation; it does not require live protection or live market-stream wiring. Proceed only after Level 0–1 validation; Levels 2–6 are recommended before enabling planner/protection in production. | **Conditional GO** |
| **Can we live-test Phase 1 now?** | **Yes.** Shadow via `simple_signal_test`; tiny real BUY via `simple_signal_test_live` + `live_simple_signal_test`. Guru path unchanged. | **LIVE-READY** (shadow + tiny live BUY) |
| **Can we live-test Phase 2 now?** | **Partial.** Store + REST bootstrap + ingest parsers are `unit-tested`. No `app.py` wiring for `market_data.enabled`. Live read-only book fetch works via ad-hoc Python (REST), not CLI. Market WS client is a stub. | **IMPLEMENTED-BUT-UNWIRED** / **LIVE-READ-ONLY-READY** (manual script) |
| **Can we live-test Phase 3 now?** | **Partial.** Planner is wired in `pipeline.py`. **Entry** planner path is **CLI-runnable** in shadow (verified). **Urgent exit** + stale-book deny requires populated `coord.market_state` — not attached by `app.py`. Tiny live BUY with planner enabled is possible but store stays empty (entry OK; urgent exits would deny). | **SHADOW-READY** (entry) · **UNIT-ONLY** (urgent/stale) |
| **Can we live-test Phase 4 now?** | **No from runtime CLI.** `protection/` package is `unit-tested` end-to-end in pytest (register → tick → planner → risk → OMS). `ProtectionMonitor.tick` is **not** called from `app.py`; no `protection.enabled` config; no registration hook after CONFIRMED fills. Legacy `tp_sl_test` harness is live-runnable but **does not use** `protection/`. | **IMPLEMENTED-BUT-UNWIRED** |
| **What is blocked by missing wiring?** | Live market-stream supervisor; `coord.market_state` population in runtime; protection monitor loop; protection registration on CONFIRMED; urgent-exit planner smoke from CLI; production TP/SL overlay. | See §4 |
| **What should be fixed before proceeding?** | **Must (before production planner/protection):** wire `MarketStateStore` when `market_data.enabled`; wire `ProtectionMonitor.tick` + CONFIRMED registration. **Should (before Phase 5 merge):** run Level 0–1 ladder; document overclaims (done in phase docs). **Can defer to Phase 5+:** market WS client (`market_ws.py` stub), portfolio attribution. | See §6 |

---

## 2. Phase objective / output / live-evidence table

| Phase | Original objective | Implemented output | Runtime path? | CLI command? | Live test now? | Live test type | Expected facts/logs | Pass criteria | Fail criteria | Risk/exposure | Missing wiring | Verdict |
|-------|-------------------|-------------------|---------------|--------------|----------------|----------------|---------------------|---------------|---------------|---------------|----------------|---------|
| **1** | Generic `Signal` → `Strategy.on_signal` → `process_signals` | `Signal` protocol, `StrategyContext`/`StrategyResult`, `process_signals`, `simple_signal_test`, `signal_received`, `fixture_signal_run.py`, guru wrapper | **Yes** — `app.py` routes `simple_signal_test`; guru uses `process_new_guru_signals` → `process_signals` | **Yes** | **Yes** (shadow + tiny live BUY) | shadow / tiny-real-order | `signal_received`, `intent_created`, `risk_decision`, `oms_submit`; no `guru_poll` for simple_signal_test | Facts present; no guru Data API for non-guru kind; clean exit | Guru poll on simple_signal_test; missing facts; risk deny without reason | Shadow: $0. Live: ~$2.50–$10 notional (5-share floor × price; capped by scenario) | None for Phase 1 harness | **CLI-runnable** shadow + **tiny-live-order-runnable** |
| **2** | Shared `MarketStateStore` fed by market ingest + REST bootstrap | `MarketStateStore`, `ingestion/market_stream.py`, `venue/polymarket/book_snapshot.py`, `MarketDataConfig` (default off) | **No** — `coord.market_state` never set in `app.py`; no supervisor | **No** | **Partial** — REST read-only via manual Python only | live-read-only (manual) | N/A from CLI; manual: fresh `best_bid`/`best_ask`, `is_stale=false` | Book levels sane; staleness toggles with age | Empty store; stale forever; REST errors unlogged | **$0** (read-only) | Market stream supervisor; attach store to `coord`; optional REST bootstrap at startup; `market_ws.py` is stub | **IMPLEMENTED-BUT-UNWIRED** · **LIVE-READ-ONLY-READY** (script) |
| **3** | `ExecutionPlanner` between risk pre-check and OMS | `execution/planner.py`, `validate_planned_order`, `execution_plan` fact, `risk_decision phase=planned`, pipeline insert when `execution.planner.enabled` | **Partial** — pipeline yes; store not attached | **Partial** — entry planner via scenario overlay | **Partial** — entry yes; urgent/stale no | shadow (entry) / unit (urgent) | `execution_plan`, `risk_decision` with `"phase":"planned"`, `oms_submit.planner_reason`; stale: `planner_stale_book`, no `oms_submit` | Entry: plan + planned risk + submit. Urgent: deny on stale book | Entry missing plan facts; urgent submits on stale book | Shadow: $0. Live planner BUY: same as Phase 1 live | `coord.market_state` for urgent exits; REST/WS feed for live urgent | **SHADOW-READY** (entry CLI) · **UNIT-ONLY** (urgent/stale CLI) |
| **3.5** | Central fill-finality helper | `state/fill_state.py`; `user_stream._apply_trade` uses `classify` | **Partial** — user WS runs in live `app.py`; helper not used in `maybe_apply_allocation_buy` | **No** dedicated CLI | **Partial** — observable indirectly on live fill via user WS | tiny-live-order (indirect) | User WS trade handling; wallet position updates on CONFIRMED; unit: `classify("CONFIRMED").allocation_final` | CONFIRMED applies wallet credit; MATCHED does not finalize allocation for protection boundary | Protection registers on MATCHED (would be bug — prevented only because protection unwired) | Same as any live BUY | Wire `fill_state.is_allocation_final` into allocation + protection registration hooks | **unit-tested** · live behavior **partially observable** |
| **4** | Production TP/SL overlay (`protection/`) | `protection/{config,registry,monitor,trigger_eval,sizing,lifecycle}.py`; protection facts | **No** — monitor not in `app.py` | **No** | **No** from CLI | not possible (runtime) | Unit/pytest: `protection_register`, `protection_trigger`, `execution_plan`, `oms_submit` | Full chain in pytest | N/A from CLI today | N/A | Monitor tick loop; register on CONFIRMED; `protection.enabled` config; feed `market_state` | **IMPLEMENTED-BUT-UNWIRED** · **unit-tested** |

---

## 3. Direct live/shadow commands to run

### A. Phase 1 — `simple_signal_test` shadow CLI smoke

| Field | Value |
|-------|-------|
| **Purpose** | Prove generic Signal → Strategy → Intent → Risk → ShadowOMS without guru |
| **Config** | `config/strategies/simple_signal_test.yaml` (no scenario) |
| **Command** | `python -m tyrex_pm.runtime.app run --strategy config/strategies/simple_signal_test.yaml --run-name simple_strat` |
| **Expected logs** | `Loaded simple_signal_test harness...`; `Wrote run to .../simple_strat`; **no** guru wallet warning |
| **Expected facts** | `health`(started,shadow) → `signal_received` → `intent_created` → `risk_decision`(approved) → `allocation_buy_applied` → `oms_submit`(shadow_ack) → `health`(stopped) |
| **Inspect** | `grep signal_received var/reporting/runs/simple_strat/facts.jsonl` · `grep guru_poll` should be empty |
| **Pass** | All facts above; exit 0; no HTTP to Data API `/activity?user=` |
| **Fail** | Guru poll facts; missing `signal_received`; risk deny; crash |
| **Max exposure** | **$0** (shadow) |
| **Stop** | Process exits automatically (`run_once`) |

**Status:** `CLI-runnable` · **verified**

---

### B. Phase 2 — live read-only market data smoke

| Field | Value |
|-------|-------|
| **Purpose** | Prove REST book → `MarketStateStore` → best_bid/ask/mid/staleness |
| **Config** | None in CLI — **IMPLEMENTED-BUT-UNWIRED** for `tyrex-pm run` |
| **Command (manual, read-only)** | From repo root, with `pip install tyrex-pm[live]` and optional `TYREX_PRIVATE_KEY` (public book may work with client): |

```python
import asyncio
from tyrex_pm.core.ids import TokenId
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.venue.polymarket.book_snapshot import bootstrap_market_store_from_rest
from tyrex_pm.venue.polymarket.clob_env import try_create_clob_client

TOKEN = "9059650700126795019827485089957938050581213031053374092199507389736394347163"

async def main():
    client = try_create_clob_client()
    assert client is not None, "need TYREX_PRIVATE_KEY + tyrex-pm[live]"
    store = MarketStateStore(default_max_age_s=5.0)
    n = await bootstrap_market_store_from_rest(store, client, [TOKEN])
    print("bootstrapped", n, "tokens")
    tid = TokenId(TOKEN)
    print("best_bid", store.best_bid(tid), "best_ask", store.best_ask(tid), "mid", store.mid(tid))
    print("stale", store.is_stale(tid))

asyncio.run(main())
```

| **Expected output** | `bootstrapped 1 tokens`; non-null bid/ask; `stale False` immediately after fetch |
| **Pass** | Valid book levels; staleness false then true after `max_book_age_s` sleep |
| **Fail** | `bootstrapped 0`; empty book; uncaught exception |
| **Max exposure** | **$0** (no orders) |
| **Minimal wiring needed for CLI** | In `app.py` when `market_data.enabled`: create `MarketStateStore`, assign `coord.market_state`, call `bootstrap_market_store_from_rest` at startup, optionally start market WS supervisor |

**Status:** `LIVE-READ-ONLY-READY` (manual) · **IMPLEMENTED-BUT-UNWIRED** (CLI)

---

### C. Phase 3 — planner-enabled shadow smoke (entry path)

| Field | Value |
|-------|-------|
| **Purpose** | Prove planner + validate_planned_order + OMS for normal BUY |
| **Config** | `config/strategies/simple_signal_test.yaml` + `config/scenarios/shadow_planner_simple_signal_test.yaml` |
| **Command** | `python -m tyrex_pm.runtime.app run --strategy config/strategies/simple_signal_test.yaml --scenario shadow_planner_simple_signal_test --run-name simple_strat_planner_shadow` |
| **Expected facts** | `execution_plan` (`planner_normal_entry`) → `risk_decision` with `"phase":"planned"` → `oms_submit` with same `client_order_id` + `planner_reason` |
| **Inspect** | `grep execution_plan var/reporting/runs/simple_strat_planner_shadow/facts.jsonl` · `grep '"phase":"planned"'` |
| **Pass** | All three fact types; `client_order_id` matches between plan and submit |
| **Fail** | No `execution_plan`; direct OMS path (planner disabled); planned risk missing |
| **Max exposure** | **$0** (shadow) |
| **Note** | Entry planner **does not require** populated `MarketStateStore`. Config requires `market_data.enabled=true` but store may remain empty for BUY entries. |

**Status:** `CLI-runnable` · **verified** (run `planner_shadow_verify`)

---

### D. Phase 3 — stale urgent exit denial smoke

| Field | Value |
|-------|-------|
| **Purpose** | Urgent exit + stale/missing book → deny → no OMS submit |
| **CLI command** | **None today** — no runtime path emits urgent `ExitIntent` with planner enabled + stale store |
| **Unit command** | `python -m pytest tests/test_execution_planner.py::test_stale_book_blocks_urgent_exit tests/test_execution_planner.py::test_missing_book_blocks_urgent_exit -q` |
| **Expected evidence (unit)** | `approved=False`, `reason=planner_stale_book` / `planner_missing_book` |
| **Expected facts (if wired)** | `execution_plan` denied OR no plan; `risk_decision` denied; **no** `oms_submit` |
| **Pass** | Deny reason is planner stale/missing book |
| **Fail** | OMS submit on stale book |
| **Max exposure** | **$0** in unit tests |
| **Minimal wiring for CLI** | Attach `MarketStateStore` with intentionally stale snapshot; trigger urgent exit via `ProtectionMonitor.tick` or dedicated harness |

**Status:** `unit-tested` · **IMPLEMENTED-BUT-UNWIRED** for CLI

---

### E. Phase 3.5 — live fill-finality relevance

| Field | Value |
|-------|-------|
| **Purpose** | Confirm CONFIRMED vs MATCHED semantics for allocation/protection boundary |
| **Unit command** | `python -m pytest tests/test_fill_finality.py -q` |
| **Live indirect test** | Run tiny live BUY (§F); watch user WS / wallet facts for CONFIRMED trade before allocation credit in ledger |
| **Gap** | `maybe_apply_allocation_buy` in `pipeline.py` still credits on shadow instant fill or submit-ack sizing — **does not** gate on `fill_state.is_allocation_final` for live. Protection registry **does** gate on CONFIRMED, but registration hook is unwired. |
| **Smallest live test** | Live BUY with `pricing_mode: auto` (marketable); grep facts for fill progression; compare `allocation_buy_applied` timing vs user WS CONFIRMED |
| **Pass (live)** | Wallet/position updates only after CONFIRMED (via user_stream) |
| **Fail** | Protection would register on MATCHED if hook were wired incorrectly |

**Status:** `unit-tested` · live **partially observable** · registration boundary **not wired**

---

### F. Phase 1 live — tiny real BUY (`simple_signal_test`)

| Field | Value |
|-------|-------|
| **Purpose** | Prove generic path live end-to-end with real CLOB submit |
| **Config** | `config/strategies/simple_signal_test_live.yaml` + `config/scenarios/live_simple_signal_test.yaml` |
| **Prerequisites** | `.env` with `TYREX_PRIVATE_KEY`; `pip install tyrex-pm[live]`; edit `token_id` |
| **Command** | `python -m tyrex_pm.runtime.app run --strategy config/strategies/simple_signal_test_live.yaml --scenario live_simple_signal_test --run-name simple_strat_live_<ts>` |
| **Expected facts** | `simple_signal_test_readiness`(ok) → `simple_signal_test_pricing` → `signal_received` → `intent_created` → `risk_decision` → `oms_submit`(real venue response + `venue_order_id`) → optional `simple_signal_test_fill_wait`(filled:true) → `allocation_buy_applied` if filled |
| **Pass** | `oms_submit` with venue order id; readiness ok; no guru poll |
| **Fail** | Readiness timeout; `oms_reject`; bootstrap_not_complete |
| **Max exposure** | **~$2.50–$10** notional (`notional_usd: 5`, capped by scenario `max_usd: 10`; Polymarket min **5 shares** → at $0.50 ≈ $2.50 floor) |
| **Stop** | Process exits after one shot; cancel resting order manually on Polymarket UI if unfilled |
| **Optional env** | `TYREX_SIMPLE_SIGNAL_TEST_FILL_WAIT_S=45` (default) · `TYREX_SIMPLE_SIGNAL_TEST_READINESS_S=60` |

**Status:** `tiny-live-order-runnable` · **not verified in this doc run** (requires operator credentials)

---

### G. Phase 4 — protection shadow/live smoke

| Field | Value |
|-------|-------|
| **Can protection be tested live from runtime?** | **No.** `ProtectionMonitor.tick` is not called from `app.py`. No `protection.enabled` in config. No registration after CONFIRMED. |
| **Unit command (full chain)** | `python -m pytest tests/test_protection_engine.py -q` |
| **Expected evidence (pytest / manual harness)** | `protection_register` → `protection_tick` (deduped) → `protection_trigger` → `execution_plan`(FAK) → `risk_decision phase=planned` → `oms_submit` |
| **Legacy substitute (NOT Phase 4 package)** | `tp_sl_test` + `live_tp_sl_test` — live BUY + monitor + exit, but uses `strategies/tp_sl_test/`, not `protection/` |
| **Minimal missing wiring** | 1) `protection.enabled` + policy config · 2) instantiate `ProtectionRegistry`/`ProtectionMonitor` on coordinator · 3) on user WS CONFIRMED (or `allocation_buy_applied` with finality check), call `register_if_allocation_final` · 4) periodic `ProtectionMonitor.tick` fed by `coord.market_state` · 5) route returned work units through `process_intent_work_unit` |

**Status:** `IMPLEMENTED-BUT-UNWIRED` · `unit-tested`

---

## 4. Implemented but not live-wired inventory

| Component | File(s) | Test coverage | Runtime wiring | CLI exercises it? | Missing wiring | Risk if ignored | Fix now? |
|-----------|---------|---------------|----------------|-------------------|----------------|-----------------|----------|
| **Market stream ingest** | `ingestion/market_stream.py` | `test_market_stream_ingest.py` | **None** in `app.py` | No | Supervisor loop + WS client (`market_ws.py` is stub) | Planner/protection blind on live books | Defer to pre-production planner/protection |
| **REST book bootstrap** | `venue/polymarket/book_snapshot.py` | `test_market_stream_ingest.py` | **None** in `app.py` | Manual Python only | Call at startup when `market_data.enabled` | Stale/missing books on urgent exits | **Should fix** before live protection |
| **MarketStateStore on coordinator** | `state/market_store.py`, `coordinator.market_state` | Unit + planner tests set manually | **`coord.market_state` never assigned in `app.py`** | No (except entry planner ignores empty store) | Assign store in `cmd_run` when `market_data.enabled` | Urgent planner always denies live | **Should fix** before live protection |
| **Market WS client** | `venue/polymarket/market_ws.py` | None (stub) | Stub only | No | Full WS implementation | No live book stream | Defer (Phase 10 note in file) |
| **ExecutionPlanner CLI (entry)** | `execution/planner.py`, `pipeline.py` | Unit + **CLI verified** | Pipeline yes | **Yes** with `shadow_planner_simple_signal_test` | None for entry | — | Done |
| **ExecutionPlanner CLI (urgent)** | same | Unit only | No store | No | Store + urgent intent source | False confidence if only entry tested | Defer until protection wired |
| **validate_planned_order** | `risk/planned_order.py` | `test_validate_planned_order.py` | Via pipeline when planner on | With planner scenario | None | — | Done |
| **fill_state helper** | `state/fill_state.py` | `test_fill_finality.py` | Used in `user_stream` only | Indirect on live | Not used in `maybe_apply_allocation_buy` / protection hook | Allocation credit timing vs protection boundary | Defer to protection wiring |
| **ProtectionMonitor.tick** | `protection/monitor.py` | `test_protection_engine.py` | **Not in `app.py`** | No | Timer loop in runtime | Production TP/SL nonfunctional | **Must fix** before live protection |
| **Protection registration** | `protection/registry.py` | Unit tests | **Not in pipeline/runtime** | No | Hook on CONFIRMED fill | Never registers | **Must fix** before live protection |
| **Protection ExitIntent → pipeline** | `protection/lifecycle.py` | Unit tests call `process_intent_work_unit` directly | **Not in `app.py`** | No | Feed tick results to pipeline | Exits never fire | **Must fix** before live protection |
| **protection.enabled config** | N/A | N/A | **Does not exist** | No | Add config block | Cannot disable/enable overlay | **Should add** with wiring |
| **tp_sl_test harness** | `strategies/tp_sl_test/` | Dedicated tests + `app.py` loop | **Yes** (legacy) | Yes | N/A (separate from `protection/`) | Confusion with Phase 4 package | Document only |
| **guru_follow generic dispatch** | `pipeline.process_new_guru_signals` | `test_generic_signal_dispatch.py` | Yes | Yes | None | — | Done |
| **simple_signal_test fixture runner** | `runtime/fixture_signal_run.py` | `test_simple_signal_test_runtime.py` | Yes | Yes | None | — | Done |

---

## 5. Live validation ladder

| Level | Proves | Command | Expected facts | Stop condition | Max notional | Required before Phase 5? | Required before prod planner/protection? |
|-------|--------|---------|----------------|----------------|--------------|--------------------------|----------------------------------------|
| **0 — default no-regression** | Existing guru/sell tests still pass | `python -m pytest tests/test_generic_signal_dispatch.py tests/test_simple_signal_test_runtime.py -q` | pytest green | Any failure | $0 | **Yes** | Yes |
| **1 — simple_signal_test shadow** | Phase 1 generic CLI path | `python -m tyrex_pm.runtime.app run --strategy config/strategies/simple_signal_test.yaml --run-name simple_strat` | §3A facts | Missing `signal_received` | $0 | **Yes** | Yes |
| **2 — market data live read-only** | Phase 2 REST → store | Manual Python §3B | Printed bid/ask; stale false | bootstrap 0 tokens | $0 | Recommended | **Yes** |
| **3 — planner shadow entry** | Phase 3 planner + planned risk | `python -m tyrex_pm.runtime.app run --strategy config/strategies/simple_signal_test.yaml --scenario shadow_planner_simple_signal_test --run-name simple_strat_planner_shadow` | §3C facts | No `execution_plan` | $0 | Recommended | **Yes** |
| **4 — tiny live BUY (planner off)** | Phase 1 live + readiness | §3F without planner scenario | `oms_submit` + venue id | `oms_reject` / readiness fail | ~$2.50–$10 | Optional | Yes |
| **5 — tiny live BUY + planner** | Phase 3 live entry | §3F + add planner/market_data to scenario overlay | §3C facts on live submit | Same as 4 | ~$2.50–$10 | Optional | Yes |
| **6 — protection register + trigger** | Phase 4 overlay | **Blocked** — run `pytest tests/test_protection_engine.py` until wired | protection_* facts | N/A from CLI | N/A | No (for Phase 5) | **Yes** |
| **7 — live TP/SL trigger** | End-to-end protection live | **Blocked** until §4 wiring + Level 6 shadow | `protection_trigger` + exit `oms_submit` | Any naked sell deny | TBD | No | **Yes** |

**Polymarket minimum:** 5 shares. At limit price $0.50 → **$2.50** minimum notional. Default risk `venue_min_size.default_min_size: 5` enforces this.

---

## 6. Blockers before Phase 5

### Must fix before Phase 5

| Item | Status |
|------|--------|
| `simple_signal_test` guru fallback | **Fixed** |
| Phase 1 CLI runnable | **Done** |
| Docs accurately label unwired components | **This review + phase doc updates** |

**Hard answer: Phase 5 (read-only portfolio foundation) may proceed after Level 0 + Level 1 pass.** Missing market-stream and protection wiring are **not** hard blockers for Phase 5 code merge if Phase 5 stays read-only.

### Should fix before enabling production planner / protection live

| Item | Priority |
|------|----------|
| Wire `MarketStateStore` onto `coord` when `market_data.enabled` | High |
| REST bootstrap at startup for configured `token_ids` | High |
| Wire `ProtectionMonitor.tick` + registration on CONFIRMED | High |
| Add `protection.enabled` + policy config | High |
| Market WS supervisor (or document REST-only refresh loop) | Medium |
| Gate `maybe_apply_allocation_buy` on `fill_state.is_allocation_final` for live | Medium |
| CLI smoke for urgent stale-book deny | Low (unit tests suffice initially) |

### Can defer until Phase 6+

| Item |
|------|
| Full `market_ws.py` implementation |
| Production trailing stops |
| Portfolio PnL (Phase 5 scope) |
| Hard kill switch (Phase 6) |
| Unify `tp_sl_test` onto `protection/` package |

---

## 7. Documentation corrections applied

Phase docs and README now use precise labels:

- **Implemented and CLI-runnable** — Phase 1 `simple_signal_test`; Phase 3 entry planner (shadow scenario)
- **Implemented and shadow-runnable** — Phase 3 pipeline (with overlay)
- **Implemented but runtime-unwired** — Phase 2 market supervisor; Phase 4 protection monitor; Phase 3 urgent exit CLI
- **Implemented but requires scenario overlay** — Phase 3 planner (requires `market_data.enabled` + `execution.planner.enabled`)
- **Deferred** — Phase 5 portfolio, Phase 6 kill switch, `market_ws.py` full wire

See updated files listed in the final summary below.

---

## Appendix: pytest equivalents (when CLI is blocked)

```bash
# Phase 1
python -m pytest tests/test_generic_signal_dispatch.py tests/test_simple_signal_test_runtime.py -q

# Phase 2
python -m pytest tests/test_market_state_store.py tests/test_market_stream_ingest.py -q

# Phase 3
python -m pytest tests/test_execution_planner.py tests/test_validate_planned_order.py -q

# Phase 3.5
python -m pytest tests/test_fill_finality.py -q

# Phase 4
python -m pytest tests/test_protection_engine.py -q
```

**Total architecture-enhance tests:** 74 tests across the above files (run all with one command):

```bash
python -m pytest tests/test_generic_signal_dispatch.py tests/test_simple_signal_test_runtime.py \
  tests/test_market_state_store.py tests/test_market_stream_ingest.py \
  tests/test_execution_planner.py tests/test_validate_planned_order.py \
  tests/test_fill_finality.py tests/test_protection_engine.py -q
```
