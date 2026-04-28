# SELL / exit support — implementation plan (V2-native Tyrex_PM)

**Status:** planning only (no implementation in this change set).  
**Venue assumption:** Polymarket **CLOB V2** (`py-clob-client-v2`, `clob_bridge`, `clob_wallet_sync`, `MarketInfoCache`, user WS + data-api positions repair) — not V1.

---

## 1. Feature objective

Deliver **full native SELL / exit support** as a first-class path through the existing **strategy → intent → risk → OMS → venue truth → facts** pipeline, so the same foundation can later support:

1. Mirror guru SELLs (already partially present via `ExitIntent` from guru activity).
2. Independent exit logic (signal- or rules-driven, non-guru).
3. TP/SL-style overlays as **risk/policy** layers (future; not designed here).
4. Combinations of the above, including **multiple strategies / bots on one wallet** (requires clear attribution of exitable quantity).

**Immediate engineering goal:** lock architecture and module boundaries, then ship a **minimal deterministic validation** (§9) before broader features.

---

## 2. Current codebase assessment

### 2.1 What already exists (reuse, do not duplicate)

| Area | Location | Role today |
|------|----------|------------|
| **Exit / reduce intents** | `core/models.py` — `ExitIntent`, `ReduceIntent` | Same shape as `EnterIntent` (token, side, size, limit, order_style); `Intent` union includes them. |
| **Cancel path** | `CancelIntent` → `risk.engine` → `ApprovedCancel` → `pipeline` → `oms.cancel` | Separate branch; unchanged for exit work. |
| **Guru SELL → exit** | `strategies/guru_follow/exits.py` — `maybe_exit_intent` | Maps guru `Side.SELL` to `ExitIntent` sized vs **bot holdings** (`holdings_from_wallet`). |
| **Strategy routing** | `strategies/guru_follow/strategy.py` — `on_guru_signal` | BUY → sizing; SELL → `maybe_exit_intent`. |
| **Pipeline** | `runtime/pipeline.py` — `process_new_guru_signals` | Emits `guru_signal` → strategy → `intent_created` → `risk_decision` → `oms_submit` / `oms_reject` / `oms_cancel`; reconcile + wallet refresh after live submit. |
| **Risk engine** | `risk/engine.py` — `evaluate_intent` | For `EnterIntent` / `ExitIntent` / `ReduceIntent`: notional/pretrade, **deployment caps**, capital (BUY only), **`inventory.check_inventory_sell`** (SELL), **`venue_min_size.evaluate_venue_min_size`**, then `ApprovedIntent`. |
| **Inventory sell gate** | `risk/inventory.py` | `available_to_sell = position_qty - orders_in_flight_by_token`; denies `NAKED_SELL` / `INSUFFICIENT_INVENTORY` when `sell_requires_venue_position`. |
| **In-flight by token** | `execution/order_lifecycle.py` — `register_submit` / `ack_submit` | Increments `OrderStore.in_flight_by_token[token]` for **any** side with `intent.size`; released on ack. Feeds inventory gate so overlapping SELLs cannot oversubscribe venue inventory. |
| **Deployment accounting** | `risk/deployment.py` | Open **SELL** orders do **not** add to deployed USD; pending SELL intent does not add synthetic BUY; BUY-only in-flight reservations from `in_flight.py`. |
| **Venue min size** | `risk/venue_min_size.py` | Applies to SELL and BUY; uses `RiskContext.market_info` (V2 `min_order_size` / tick) when cache present. |
| **Shadow SELL** | `state/shadow_wallet.py` — `apply_shadow_fill` | On `ExitIntent`/`ReduceIntent` SELL: credits USDC, reduces position qty. |
| **Live truth** | `state/wallet_store.py` + user WS (`ingestion/user_stream.py`) + `clob_wallet_sync` + `positions_sync` | Merged open orders; REST positions replace `wallet.positions`; CONFIRMED trades adjust positions. |
| **Reconcile** | `state/reconcile.py` + `pipeline.reconcile_coordinator` | OMS vs venue truth; health + `reconcile` facts. |
| **Facts** | `reporting/schema_v2.py`, `pipeline` | `intent_created`, `risk_decision`, `oms_submit`, `wallet_sync`, etc. |

**Conclusion:** There is **no parallel SELL stack to invent**. The correct approach is to **extend** strategy inputs (timing, signals, allocation) and runtime wiring so more exit **intents** are produced and fed through the **same** `evaluate_intent` + OMS path.

### 2.2 Gaps relative to product goals

1. **Non-guru exits:** Nothing schedules `ExitIntent` from timers, internal signals, or TP/SL policy today; only guru SELL rows and manual/strategy tests that call the pipeline directly.
2. **Strategy vs venue inventory:** `GuruFollowStrategy` uses `coord.holdings()` = **venue** `WalletStore.positions`. For **multi-bot / shared wallet**, venue qty is shared; **per-strategy exitable qty** is not modeled — two strategies could issue competing SELLs; second fails at inventory gate (`INSUFFICIENT_INVENTORY`) or worse, ordering races. A **strategy allocation ledger** is needed for clean attribution and proactive sizing (§4, §6).
3. **`ExitIntent` vs `ReduceIntent`:** Both follow identical risk/OMS paths today. Semantic distinction is documentation / strategy convention only (e.g. “full exit” vs “trim”); optional future tightening (e.g. policy hooks) can come later.

### 2.3 V2-specific leverage (already in repo)

- **`MarketInfoCache`** + Gamma: token metadata before intent; tick/size quantization in OMS (`pipeline` passes `market_info` into `oms.submit`).
- **`clob_bridge` / `PyClobBridge`:** Single adapter boundary for post/cancel; SELL orders use same path as BUY.
- **Cold start / readiness:** `RiskContext.first_v2_sync_complete`, user WS staleness, `wallet_sync` facts — SELL intents must respect the same readiness gates (no bypass).

---

## 3. Design principles (constraints)

1. **Strategies do not touch the venue** — only produce `Intent`s (and read abstracted state: holdings, ledger, config).
2. **Risk remains pre-trade policy** — gates and clipping/bump **policy** only; no strategy logic inside `risk/` (no “if TP hit” in the risk engine).
3. **Execution remains submit/cancel only** — no exit rules in `execution/`.
4. **State owns remembered truth** — venue mirror in `WalletStore` / `OrderStore`; durable strategy cursor in `StrategyStore`; **new** allocation state in `state/` (§6).
5. **Runtime wires and supervises** — polls, timers, calling strategy hooks, invoking pipeline, supervising loops.
6. **Facts remain the operator surface** — every exit attempt should be traceable via existing fact types, plus optional payload extensions (§9).
7. **Do not** turn `OrderStore` into an exit policy engine — it tracks local OMS rows and in-flight counters only.

---

## 4. Chosen architecture for SELL / exit support

### 4.1 Conceptual shape

**Single execution pipeline for all SELLs:** any feature (guru mirror, timed exit, future TP/SL) **terminates** in `ExitIntent` or `ReduceIntent` with appropriate `token_id`, `side=SELL`, `size`, `limit_price`, `order_style`, then **`evaluate_intent` → OMS** as today.

**Two layers of “inventory” (conceptual):**

| Layer | Meaning | Source of truth |
|-------|---------|-----------------|
| **Venue inventory** | Outcome shares the wallet can legally sell on Polymarket | `WalletStore.positions` (+ merged `open_orders` / in-flight as already modeled in `RiskContext`) |
| **Strategy allocation** (new) | Shares this **strategy instance** “owns” for exit sizing / attribution | New ledger in `state/` (§6), updated when **this** strategy’s approved enters/exits complete (not raw venue WS for other bots) |

**Risk engine** continues to enforce **venue** non-naked SELL (`inventory.check_inventory_sell`). **Strategies** (and optional future **risk policy modules** that are *not* “strategy engines” — e.g. max single-exit notional) should size intents so that:

`intent.size ≤ available_to_sell_venue` **and** `intent.size ≤ allocation_available_strategy` (when ledger enabled).

For **single-strategy / single-bot** deployments, ledger can equal venue position for tokens this bot opened, or ledger can be **disabled** and behavior matches today.

### 4.2 Where the allocation ledger lives

**Primary home: `state/`** — e.g. `state/allocation_ledger.py` + persistence alongside `StrategyStore` (same JSON file versioned or separate file in run state dir).

**Why not only `strategies/`?** Persistence, crash recovery, and multi-strategy fairness belong next to `StrategyStore` / run state, not inside guru-specific code.

**Why not `runtime/`?** Runtime should orchestrate *when* to read/write, not own the ledger’s data structures.

**Why not `risk/`?** Risk should not compute strategy attribution; it may consume **summaries** injected into `RiskContext` *only if* we add optional policy gates later (e.g. “exit exceeds strategy cap”). Initial validation (§9) can **omit** ledger enforcement in risk and rely on strategy sizing + venue gate only.

**Split with `strategies/`:** Strategies **read** ledger snapshots passed into `on_guru_signal` / `on_scheduled_exit_tick` and **request** ledger mutations through **pure state operations** applied by **runtime** only after successful `oms_submit` / shadow fill (same place OMS outcomes are already known), to avoid drift.

---

## 5. Module-by-module responsibility split

| Module | Responsibilities for SELL / exit |
|--------|----------------------------------|
| **`strategies/`** | Decide **when** and **how much** to sell: guru mirror (`exits.py`), timed demo, future signal exits. Build `ExitIntent`/`ReduceIntent` only. Use `holdings` + **optional** `allocation_snapshot`. **No** HTTP, **no** CLOB imports. |
| **`state/`** | `WalletStore` / `OrderStore` unchanged roles. **New:** allocation ledger (per strategy run / id), persisted with run state. Optional: extend `StrategyStore` schema for pending scheduled exits (§9). Pure functions to apply ledger on **confirmed strategy outcomes** (enter fill / exit fill / cancel). |
| **`runtime/`** | Call strategy hooks on guru poll **and** on **scheduler tick**. Route resulting intents through a **shared** pipeline helper (refactor from `process_new_guru_signals` — see §7). Pass `live_clob_client`, `market_info_cache`, `coord`, `sink`, `oms` as today. |
| **`risk/`** | Keep `check_inventory_sell`, deployment, `venue_min_size`, capital (BUY-only). Optional later: thin **policy** inputs on `RiskContext` (e.g. `strategy_allocation_remaining`) for multi-bot guardrails — **not** TP/SL logic. |
| **`execution/`** | Unchanged: `register_submit`, `ack_submit`, OMS adapter; SELL already supported. |
| **`venue/polymarket/`** | Unchanged: V2 client, bridge, wallet sync, gamma/market info; ensures SELL payload and min size match venue. |
| **`reporting/`** | Reuse `intent_created`, `risk_decision`, `oms_submit`, `wallet_sync`. Extend payloads with **`provenance`** / `parent_correlation` where useful (§9). New fact type **only if** provenance cannot fit existing schema cleanly. |

---

## 6. State model for SELL / exit support

### 6.1 Existing (unchanged concepts)

- **`WalletStore.positions`:** Venue outcome qty (REST + CONFIRMED WS).
- **`WalletStore.open_orders`:** Merged venue resting book.
- **`OrderStore`:** Local rows + `in_flight_by_token` (BUY and SELL both reserve qty for inventory math).
- **`StrategyStore`:** Guru dedup / watermark.

### 6.2 New: strategy allocation ledger (for multi-bot / future)

**Suggested minimal record (per strategy run id):**

- `by_token: dict[TokenId, Decimal]` — **non-negative** “allocated long” qty this strategy believes it still holds.
- Updates:
  - **On approved EnterIntent (BUY) success** (shadow instant fill or live ack path): `+= size` for that token (or `+= filled` if we later track partial fills explicitly).
  - **On approved ExitIntent/ReduceIntent (SELL) success:** `-= min(size, allocated)` for that token.
  - **Reconciliation:** Optional periodic **clamp** to venue: `allocated[token] = min(allocated[token], venue_qty)` to correct drift if operator trades manually (fact or log on clamp).

**Persistence:** JSON next to `strategy_store.json` under run state path (e.g. `allocation_ledger.json`), same save cadence as `save_strategy_store`.

**Validation path 1 (§9):** Can **defer** persistence and multi-bot clamping; in-memory ledger updated only for the demo strategy is enough to prove wiring. Production hardening adds file persistence + clamp facts.

### 6.3 Scheduled exit queue (validation 1)

**Pending exit row (in-memory or in `StrategyStore`):**

- `token_id`, `size`, `due_ts` (monotonic or UTC), `parent_correlation_id` (guru `dedup_key` or intent_id of the BUY).
- Enqueued **after** successful BUY `oms_submit` (or at intent approval — document trade-off: approval vs ack avoids phantom exits if submit fails).

**Recommendation:** enqueue on **`oms_submit` success** (after `ack_submit`) so size and token match what the venue accepted; use `ApprovedIntent.intent.size` and token from that approval.

---

## 7. Runtime flow: trigger → intent → risk → OMS → venue truth → facts

### 7.1 Guru-driven SELL (existing)

`GuruTradeSignal` → `process_new_guru_signals` → `strategy.on_guru_signal` → `ExitIntent` → `evaluate_intent` → OMS → `ack_submit` → wallet refresh (live) → `reconcile_coordinator` → facts.

### 7.2 New: scheduled / internal exit (validation 1+)

1. **Trigger:** Runtime scheduler fires (e.g. every 0.5–1 s, or piggyback on guru poll loop) → `strategy.collect_due_scheduled_exits(now, ctx) -> list[ExitIntent]`.
2. **Intent facts:** For each intent, emit `intent_created` with payload extension `provenance: { "source": "scheduled_exit", "parent_dedup_key": "..." }` (or reuse `correlation_id` = parent dedup).
3. **Risk:** `coord.build_risk_context(app)` → `evaluate_intent` (inventory + venue min + deployment + readiness).
4. **OMS:** Same submit path; `register_submit` reserves in-flight SELL size.
5. **Venue truth:** User WS + REST refresh as today; positions sync updates `WalletStore`.
6. **Ledger update (when enabled):** After successful submit + ack (or shadow fill), runtime calls `ledger.apply_sell(strategy_run_id, token_id, size)`.
7. **Facts:** `risk_decision`, `oms_submit`, `reconcile`, `wallet_sync` as applicable.

### 7.3 Refactor suggestion (implementation detail)

Extract from `process_new_guru_signals` a coroutine:

`process_intent_batch(intents, *, correlation_ids, provenance_meta, ...)` 

so guru loop and scheduler loop **share** risk/OMS/reconcile code paths without duplicating `PolyApiException` handling and `emit_wallet_sync` semantics.

---

## 8. Step-by-step implementation phases

| Phase | Scope | Outcome |
|-------|--------|---------|
| **P0** | Docs + interface sketch | This plan approved; optional ADR in same folder if team wants one page. |
| **P1** | Pipeline refactor | `process_intent_batch` (or equivalent) extracted; `process_new_guru_signals` calls it; **zero behavior change** in tests. |
| **P2** | Scheduled exit queue + demo strategy hook | `GuruFollowStrategy` (or thin subclass) registers pending exit after successful BUY submit; `collect_due_scheduled_exits` returns `ExitIntent`; runtime invokes batch processor on interval. |
| **P3** | Validation 1 tests | Shadow + unit tests (§9); facts assertions. |
| **P4** | Allocation ledger (minimal) | In-memory then persisted; runtime updates on BUY/SELL success; strategy sizes guru SELL vs `min(venue, allocated)` when config flag set. |
| **P5** | Guru SELL parity + multi-bot | Enable ledger-aware `maybe_exit_intent`; clamp + facts on drift. |
| **P6** | Future: TP/SL | Policy/risk overlays **consume** marks + ledger; still emit `ExitIntent` (§10). |

Phases P4–P6 can overlap with product priority; **P1–P3** deliver the “native exit” proof.

---

## 9. Validation step 1: 3-second “copy BUY then SELL” demo

### 9.1 Behavior

When a **guru BUY** is successfully submitted (shadow instant fill or live ack path), schedule **`ExitIntent`** for the **same token** and **same size** (or `min(size, venue_qty)` if clipped) **3 seconds** later (config: `exits.demo_forced_exit_delay_s: 3`, gated by `exits.demo_forced_exit_enabled: true` to avoid accidental production use).

### 9.2 Wiring (implemented; live vs shadow)

1. **Config:** `ExitsConfig` + YAML: `demo_forced_exit_enabled`, `demo_forced_exit_delay_s` (see `config/strategies/guru_follow.yaml` when enabled).
2. **After successful BUY OMS path:** `pipeline._maybe_register_demo_exit_after_buy` → `ScheduledExitDemoState.register_after_successful_buy(...)`.
3. **Shadow + instant fill:** demo arms immediately (`due_mono = now + delay`); `scheduled_exit_demo_due_loop` drains due exits (~4 Hz) without blocking guru polling.
4. **Live:** BUY success only enqueues **`_pending_live`**; the **3-second timer starts** only when `try_arm_live_pending` observes **`available_to_sell >= planned_sell_size`** (same formula as `risk.inventory.check_inventory_sell`: `WalletStore.positions` minus `OrderStore.in_flight_by_token`). That way the delay is anchored to **sellable venue inventory**, not HTTP ack alone.
5. **`try_arm_live_pending` call sites:** `coord.scheduled_exit_demo_try_arm` after (a) user-WS wallet updates, (b) `venue_refresh_loop` / provisional repair after REST wallet + **data-api positions** refresh, (c) initial live bootstrap after first positions sync, (d) immediately after registering a pending row (cheap no-op if REST/WS has not caught up yet).

### 9.2a Why this live trigger is the safest first validation

Submit ack can precede resting fills, delayed matching, or positions visibility. **`available_to_sell`** is exactly what the inventory gate uses for the subsequent `ExitIntent`, so arming the timer only when that quantity reaches the planned sell size guarantees the scheduled SELL is **not** a race against empty `wallet.positions`. Evidence arrives from **data-api/positions** (REST refresh) and/or **user-WS `CONFIRMED`** trades updating `WalletStore` — the same merged truth the risk engine trusts.

### 9.3 State

- **Minimum:** In-memory heap/list on `GuruFollowStrategy` instance (lost on restart — acceptable for demo).
- **Better:** `StrategyStore` list `pending_scheduled_exits: [{token_id, size, due_mono, parent_dedup_key}]` saved with `save_strategy_store`.

### 9.4 Facts proving success

Operator should see, sharing **`correlation_id`** or explicit `parent_dedup_key` linkage:

1. `guru_signal` (BUY)  
2. `intent_created` (EnterIntent)  
3. `risk_decision` (approved)  
4. `oms_submit` (BUY)  
5. **~3s later** `intent_created` (ExitIntent, provenance `scheduled_exit`)  
6. `risk_decision` (approved)  
7. `oms_submit` (SELL)  
8. `wallet_sync` — position qty for token drops (or zero); optional `reconcile` unchanged in steady state  

Shadow mode: `apply_shadow_fill` reduces position after SELL submit.

### 9.5 Tests

| Test | Type | Assert |
|------|------|--------|
| `test_scheduled_exit_demo_registers_after_buy` | Unit | After `notify_buy_executed`, one pending row with correct `due_mono + delay`. |
| `test_scheduled_exit_demo_emits_exit_intent` | Unit | `pop_due_scheduled_exits` past due returns `ExitIntent` with expected size/token. |
| `test_pipeline_demo_exit_shadow_end_to_end` | Integration / golden | Fixture guru BUY → process → advance clock 3s → process batch → SELL `oms_submit` fact; position zero for token. |
| Optional live | Manual | Run with small static BUY; confirm SELL in `v2_wallet_watch.py` / facts.jsonl. |

---

## 10. Validation step 2: virtual TP/SL (next evolution, not designed here)

After P1–P3 stabilize **intent-based exits**:

- **TP/SL** should be implemented as **overlays** that observe **marks** (`RiskContext.mark_prices` / `WalletPosition.avg_price_usd`) and **emit `ExitIntent`/`ReduceIntent`** through the **same** `process_intent_batch` path.
- **Risk** may add **policy-only** gates (max daily exits, min time between exits) — parameters in YAML, not embedded strategy code in `risk/`.
- **Ledger** from §6 ensures partial exits and multi-leg strategies do not confuse **venue** qty with **strategy** qty when multiple overlays coexist.

The implementation plan explicitly **defers** TP/SL sizing rules, mark source priority, and cancel/replace behavior to a follow-on doc once validation 1 is green.

---

## 11. Open questions / risks

| Item | Risk | Mitigation |
|------|------|------------|
| **Enqueue on submit vs on fill** | Enqueue on approve might schedule exit if submit fails | Enqueue on **`oms_submit` success** after `ack_submit`. |
| **Venue min size on exit** | Forced full exit might leave **dust** below venue min | Document; optional “dust sweep” policy later; demo uses sizes ≥ min. |
| **REST positions lag** | BUY just filled; positions REST not updated; SELL denied `INSUFFICIENT_INVENTORY` | Rely on shadow for tests; live: short delay before due time, or trigger exit off user-WS CONFIRMED (future enhancement) — call out in ops notes. |
| **Multiple scheduled exits same token** | Duplicate rows | Dedup key `(token_id, parent_dedup_key)`; cancel/replace policy in strategy. |
| **Ledger vs manual UI trades** | Allocated qty out of sync | Periodic clamp to venue + fact `allocation_clamped`. |
| **Concurrency** | Guru SELL + scheduled exit same token | Inventory + in-flight already mitigate; ordering documented for operators. |

---

## 12. Exact file/module list likely to change

**High likelihood**

- `src/tyrex_pm/runtime/pipeline.py` — extract shared intent processing; optional provenance on fact payloads.
- `src/tyrex_pm/runtime/app.py` — scheduler tick / call `pop_due_scheduled_exits` + batch processor.
- `src/tyrex_pm/strategies/guru_follow/strategy.py` — `notify_buy_executed`, scheduled queue, `pop_due_scheduled_exits`.
- `src/tyrex_pm/runtime/config.py` — `ExitsConfig` fields for demo flags / delays.
- `config/strategies/guru_follow.yaml` (or example) — document demo keys.
- `src/tyrex_pm/state/strategy_store.py` — optional persistence for pending exits.
- `tests/` — new unit + integration tests (§9.5).

**Medium likelihood (P4+)**

- `src/tyrex_pm/state/allocation_ledger.py` (new) + persistence helpers.
- `src/tyrex_pm/strategies/guru_follow/exits.py` — optional `min(venue, allocated)` sizing.
- `src/tyrex_pm/core/models.py` — only if provenance must live on intent (prefer fact payload first to keep intents frozen).
- `src/tyrex_pm/reporting/schema_v2.py` — new fact type only if required.

**Low likelihood for validation 1**

- `src/tyrex_pm/risk/engine.py` / `risk/inventory.py` — only if optional allocation policy gate added.
- `src/tyrex_pm/venue/polymarket/clob_bridge.py` — only if V2 SELL-specific edge case discovered.

---

## Summary

The codebase **already** implements native SELL on V2 through **`ExitIntent` / `ReduceIntent`** and the **same** risk and OMS path as BUY. The **next major feature** is **not** a new stack but **(a)** runtime + strategy support for **non-guru exit triggers**, **(b)** optional **strategy allocation ledger** in `state/` for shared-wallet scale, and **(c)** a **deterministic 3-second demo** to prove end-to-end behavior with facts. TP/SL and richer policy layers **reuse** this foundation by emitting the same intents.
