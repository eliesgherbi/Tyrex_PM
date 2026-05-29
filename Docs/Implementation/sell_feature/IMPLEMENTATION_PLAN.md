# SELL / exit support — implementation plan (V2-native Tyrex_PM)

**Status (2026-05-21):**

| Phase | Title | State |
|-------|--------|--------|
| **P0** | Docs / design | **Done** |
| **P1** | Pipeline refactor | **Done** — `IntentWorkUnit` / `process_intent_work_unit` (see §7.3) |
| **P2** | Scheduled exit demo + sell_test wiring | **Done** |
| **P3** | Shadow / unit validation | **Done** |
| **P3.5** | Live validation hardening | **Done** — live validated (`sell_test_live_ordering_check` and follow-on runs) |
| **P4** | Allocation ledger | **Done** — live validated via `allocation_test` |
| **P4.1** | Resting SELL lifecycle | **Done** — WS/reconcile promotes fills and releases |
| **P4 auto-pricing** | `allocation_test` auto SELL pricing | **Done** — green validation: **`allocation_test_live_auto_1`** |
| **P5** | Ledger-aware guru SELL parity | **Done** — shadow/unit validated; see [guru_sell_ledger_parity_plan.md](./guru_sell_ledger_parity_plan.md) |
| **P6** | TP/SL overlays | **Deferred** until P5 live validation is stable |

**Current green validation:** `allocation_test_live_auto_1` — Owner A BUY → allocation credited → Owner B blocked (`allocated_available=0` despite wallet qty) → Owner A auto-priced SELL → ledger clean (`allocated=0`, `reserved=0`, `available=0`); no Owner B OMS; no `LEDGER_MISMATCH`.

**Venue assumption:** Polymarket **CLOB V2** (`py-clob-client-v2`, `clob_bridge`, `clob_wallet_sync`, `MarketInfoCache`, user WS + data-api positions repair) — not V1.

---

## 1. Feature objective

Deliver **full native SELL / exit support** as a first-class path through the existing **strategy → intent → risk → OMS → venue truth → facts** pipeline, so the same foundation can later support:

1. Mirror guru SELLs (already partially present via `ExitIntent` from guru activity).
2. Independent exit logic (signal- or rules-driven, non-guru) — **partially present** via `scheduled_exit_demo` and `sell_test`.
3. TP/SL-style overlays as **risk/policy** layers (future; P6 — deferred until P5).
4. Combinations of the above, including **multiple strategies / bots on one wallet** (P4 allocation ledger — **done**).

**Immediate engineering goal:** P5 guru mirror SELL parity is **implemented**. Next: live guru validation, then P6 TP/SL (deferred).

---

## 2. Current codebase assessment

### 2.1 What already exists (reuse, do not duplicate)

| Area | Location | Role today |
|------|----------|------------|
| **Exit / reduce intents** | `core/models.py` — `ExitIntent`, `ReduceIntent` | Same shape as `EnterIntent`; `Intent` union includes them. Native V2 SELL path is live. |
| **Cancel path** | `CancelIntent` → `risk.engine` → `ApprovedCancel` → `pipeline` → `oms.cancel` | Unchanged for exit work. |
| **Guru SELL → exit** | `strategies/guru_follow/exits.py` — `maybe_exit_intent` | Guru mirror SELL sized vs **`guru_follow` allocation** when ledger enabled (P5). |
| **Pipeline refactor (P1)** | `runtime/pipeline.py` — `process_intent_work_unit`, `runtime/intent_work.py` — `IntentWorkUnit` | Shared risk → OMS path for guru signals, scheduled exits, and sell_test. `process_new_guru_signals` delegates per-intent. |
| **Scheduled exit demo (P2)** | `strategies/guru_follow/scheduled_exit_demo.py` | Registers pending exit after successful BUY; `try_arm_live_pending`; `pop_due_work_units`. |
| **sell_test strategy (P2)** | `strategies/sell_test/strategy.py`, `config/strategies/sell_test.yaml` | Standalone BUY → wait for sellable inventory → SELL; shares scheduler with demo via `scheduled_exit_demo_due_loop`. |
| **Runtime wiring (P2)** | `runtime/app.py`, `runtime/coordinator.py` | `scheduled_exit_demo_due_loop`; `coord.scheduled_exit_demo_try_arm` hook; `_run_sell_test_loop`. |
| **Risk engine** | `risk/engine.py` — `evaluate_intent` | SELL: **`inventory.check_inventory_sell`**, deployment, venue min size. **Must not be bypassed.** |
| **Inventory sell gate** | `risk/inventory.py` | `available_to_sell = position_qty - orders_in_flight_by_token`. |
| **In-flight by token** | `execution/order_lifecycle.py` — `register_submit` / `ack_submit` | Released on ack; feeds inventory gate. |
| **Shadow SELL** | `state/shadow_wallet.py` — `apply_shadow_fill` | Shadow path: instant position update after SELL submit. |
| **Live truth** | `state/wallet_store.py` + `ingestion/user_stream.py` + `clob_wallet_sync` + `positions_sync` | Positions from CONFIRMED WS + REST `/positions` wholesale replace. |
| **Facts** | `reporting/schema_v2.py`, `pipeline` | `intent_created`, `risk_decision`, `oms_submit`, `wallet_sync`, `exit_lifecycle`, `allocation_ledger`. |
| **Allocation ledger (P4)** | `state/allocation_ledger.py`, `runtime/allocation_runtime.py` | Per-owner attribution; runtime buy/sell/reserve/clamp hooks wired in pipeline. |
| **allocation_test (P4 validation)** | `strategies/allocation_test/strategy.py` | Live-validated multi-owner block + auto-priced SELL (`allocation_test_live_auto_1`). |

**Conclusion:** Native SELL on V2 is implemented and live-validated through P4/P5. Guru mirror SELL sizes against `guru_follow` allocation when the ledger is enabled.

### 2.2 Gaps relative to product goals

1. **Non-guru exits:** **Done** — `scheduled_exit_demo`, `sell_test`, and `allocation_test` validate BUY → arm → SELL in shadow and live (P3.5).
2. **Strategy vs venue inventory:** Allocation ledger **done** (P4). Guru mirror SELL allocation-aware sizing **done** (P5).
3. **`ExitIntent` vs `ReduceIntent`:** Documentation / convention only today.

### 2.3 V2-specific leverage (already in repo)

- **`MarketInfoCache`** + Gamma: tick/size before intent; OMS quantization.
- **`clob_bridge` / `PyClobBridge`:** Single adapter for BUY and SELL.
- **Cold start / readiness:** `first_v2_sync_complete`, user WS staleness — SELL must respect same gates.

### 2.4 Historical live validation finding (2026-05-21) — **resolved in P3.5**

> **Historical note:** The following documents the pre-P3.5 live `sell_test` failure mode. P3.5 (immediate positions refresh, exit lifecycle facts, terminal `is_done`) is complete. Retained for post-mortem context only.

Fresh **live** `sell_test` run (no prior position on test token; prior deployment-cap issue cleared):

| Step | Result |
|------|--------|
| BUY `intent_created` | Yes |
| BUY `risk_decision` | Approved (notional capped $5 → $4; ~23.53 shares after venue min-size bump) |
| BUY `oms_submit` | Yes — CLOB returned `"status": "matched"`, `takingAmount` ≈ **23.52** shares |
| SELL `intent_created` (ExitIntent) | **No** |
| SELL `risk_decision` | **No** |
| SELL `oms_submit` | **No** |
| Facts after BUY | Run ended or was interrupted before SELL chain; no `exit_lifecycle` facts yet |

**Root cause:** Live scheduled-exit arming (`try_arm_live_pending`) depends on **`WalletStore.positions`** showing sellable qty ≥ `planned_sell_size`. The OMS **match response does not update positions**. Post-BUY refresh (`refresh_wallet_coordinated_after_live_submit`) updates **balance and open orders only**, not positions.

**Position visibility paths (all eventually consistent):**

- **User WebSocket** `TRADE` with `status == "CONFIRMED"` → `apply_confirmed_trade_to_wallet` → positions updated → `coord.scheduled_exit_demo_try_arm()` (fast when WS delivers promptly).
- **REST** `data-api/positions` via `venue_refresh_loop` — default interval **`reconcile_interval_s: 30`** (`config/runtime/default.yaml`).
- **Immediate try_arm after BUY ack** — no-op if positions not yet visible.

**Race with run shutdown:** `sell_test` main loop uses `--max-iterations 60` × 0.5s wait ≈ **30s** after BUY. SELL timer (`sell.delay_s: 3`) starts only **after** arming. If first positions refresh is ~30s, SELL due at ~33s — **after** loop exit. Separately, `is_done()` can become true when `_sell_emitted` is set at **intent construction**, not SELL OMS success, and shutdown cancels `scheduled_exit_demo_due_loop`.

**Not the core blocker:** HTTP/2 `ConnectionTerminated` on SDK `get_open_orders` — transport noise; BUY still matched; REST `/data/orders` fallback succeeds.

---

## 3. Design principles (constraints)

1. **Strategies do not touch the venue** — only produce `Intent`s.
2. **Risk remains pre-trade policy** — **never bypass** `inventory.check_inventory_sell` for live SELL submit.
3. **Execution remains submit/cancel only** — no exit rules in `execution/`.
4. **State owns remembered truth** — `WalletStore` / `OrderStore`; allocation ledger in P4.
5. **Runtime wires and supervises** — polls, timers, hooks, pipeline.
6. **Facts remain the operator surface** — every exit attempt traceable; P3.5 adds exit lifecycle facts (§8.1.D).
7. **Fast signals accelerate arming; risk gate uses confirmed sellable inventory** — see §3.1.

### 3.1 Live inventory awareness — source-of-truth hierarchy

Use fast signals to **trigger refresh and arming sooner**. Use conservative truth for **SELL risk approval**.

| Priority | Source | Role |
|----------|--------|------|
| **1** | **User WebSocket CONFIRMED trade events** | Fastest authoritative position delta when available. Updates `WalletStore.positions`; must call `try_arm` hook immediately after update. |
| **2** | **REST Data API `/positions`** | Slower but authoritative repair/backstop. Periodic (30s) **and** immediate trigger after matched BUY during P3.5 validation. Wholesale replace `wallet.positions`. |
| **3** | **OMS submit response** | Immediate local evidence (`status`, `takingAmount`, `makingAmount`, `orderID`). Record as **match evidence / fill hint** in facts and lifecycle state. **Does not bypass inventory risk gate.** May trigger immediate positions refresh. |
| **4** | **REST open orders / balance allowance** | Collateral and resting-book truth; **not** sufficient for sellable position alone. |
| **5** | **OrderStore in-flight / provisional rows** | Local execution state and reservations; not final venue inventory. |

**Design rule:**

- **Do not bypass** `RiskEngine` / `check_inventory_sell`.
- **Do** use WS CONFIRMED + OMS matched response to trigger **`refresh_positions_from_data_api`** and **`try_arm_live_pending`** sooner.
- **Actual SELL submit** remains: `ExitIntent` → `evaluate_intent` → inventory check → OMS.

**Expected WS path (documented target):**

```
user WS TRADE CONFIRMED
  → WalletStore.positions updated
  → coord.scheduled_exit_demo_try_arm()
  → try_arm_live_pending: if available_to_sell >= planned_sell_size
  → sell.delay_s timer starts (due_mono = now + delay)
```

---

## 4. Chosen architecture for SELL / exit support

### 4.1 Conceptual shape

**Single execution pipeline for all SELLs:** guru mirror, timed exit, future TP/SL → `ExitIntent` / `ReduceIntent` → **`evaluate_intent` → OMS**.

**Two layers of “inventory” (conceptual):**

| Layer | Meaning | Source of truth |
|-------|---------|-----------------|
| **Venue inventory** | Shares the wallet can legally sell | `WalletStore.positions` (+ in-flight in `RiskContext`) |
| **Strategy allocation** (P4) | Shares this strategy instance “owns” | `AllocationLedger` in `state/` — **implemented** |

For guru mirror SELL with allocation ledger enabled, strategy sizing uses **`guru_follow` allocation** (P5); RiskEngine still enforces venue inventory.

### 4.2 Allocation ledger (P4 — **complete**)

Implemented in `state/allocation_ledger.py` + `runtime/allocation_runtime.py`. Runtime hooks in `pipeline.process_intent_work_unit` apply buy/sell/reserve/clamp. Live-validated via `allocation_test` (`allocation_test_live_auto_1`).

---

## 5. Module-by-module responsibility split

| Module | Responsibilities for SELL / exit |
|--------|----------------------------------|
| **`strategies/`** | When/how much to sell; pending/armed rows; **`is_done` terminal semantics (P3.5)**. |
| **`state/`** | `WalletStore` / `OrderStore`; P4 allocation ledger. |
| **`runtime/`** | Pipeline, scheduler, **immediate positions refresh after matched BUY (P3.5)**, lifecycle facts, shutdown ordering. |
| **`risk/`** | `check_inventory_sell` — unchanged; no bypass. |
| **`ingestion/`** | User WS CONFIRMED → positions; verify try_arm hook (P3.5.A). |
| **`venue/polymarket/`** | `positions_sync.refresh_positions_from_data_api` — immediate + periodic. |
| **`reporting/`** | Existing facts + **`exit_lifecycle` (P3.5.D)**. |

---

## 6. State model for SELL / exit support

### 6.1 Existing (unchanged)

- **`WalletStore.positions`:** Venue outcome qty (REST replace + CONFIRMED WS deltas).
- **`WalletStore.open_orders`:** Merged WS + REST.
- **`OrderStore`:** Local rows + `in_flight_by_token`.
- **`StrategyStore`:** Guru dedup; optional pending-exit persistence (future).

### 6.2 Strategy allocation ledger (P4 — **implemented**)

Per-owner token qty in `AllocationLedger`; persisted to `var/state/allocation_ledger.json`; facts via `allocation_ledger` fact type. Guru mirror SELL is always allocation-aware (P5).

### 6.3 Scheduled exit queue (implemented — P2)

- **Pending row** after successful BUY `oms_submit` / `ack_submit` (`register_after_successful_buy`).
- **Live:** `_pending_live` until `try_arm_live_pending` promotes to `_armed` with `due_mono`.
- **Shadow:** arms immediately on instant fill.

---

## 7. Runtime flow: trigger → intent → risk → OMS → venue truth → facts

### 7.1 Guru-driven SELL (existing)

`GuruTradeSignal` → `process_new_guru_signals` → `ExitIntent` → `process_intent_work_unit` → OMS → facts.

### 7.2 Scheduled / internal exit (implemented — P2)

1. **Trigger:** `scheduled_exit_demo_due_loop` (~4 Hz) → `pop_due_work_units` / `resolve_due_work_units`.
2. **Live arming gate:** `try_arm_live_pending` when `available_to_sell >= planned_sell_size`.
3. **Risk + OMS:** `process_intent_work_unit` — same as BUY.
4. **P3.5 additions:** immediate positions refresh after matched BUY; lifecycle facts; terminal `is_done`.

### 7.3 Pipeline refactor (P1 — mostly implemented)

Implemented as:

- `IntentWorkUnit` (`runtime/intent_work.py`)
- `process_intent_work_unit()` (`runtime/pipeline.py`) — shared by guru, scheduled exit, sell_test
- `on_buy_submit_ack` hook on `SellTestStrategy`; `_dispatch_post_buy_ack_hook` for guru demo

Original name `process_intent_batch` was not used; behavior equivalent.

---

## 8. Step-by-step implementation phases

| Phase | Scope | State | Outcome |
|-------|--------|--------|---------|
| **P0** | Docs + interface sketch | **Done** | This plan. |
| **P1** | Pipeline refactor | **Mostly done** | `IntentWorkUnit` / `process_intent_work_unit`. |
| **P2** | Scheduled exit + sell_test | **Done** | Demo + sell_test + scheduler loop. |
| **P3** | Shadow / unit validation | **Done** | `tests/test_scheduled_exit_demo.py`, `tests/test_sell_test_strategy.py`, etc. |
| **P3.5** | **Live validation hardening** | **Done** | Deterministic live BUY → SELL; lifecycle facts; terminal completion. |
| **P4** | Allocation ledger | **Done** | Live validated (`allocation_test_live_auto_1`). |
| **P4.1** | Resting SELL lifecycle | **Done** | WS/reconcile fill promotion. |
| **P5** | Ledger-aware guru SELL | **Next** | [guru_sell_ledger_parity_plan.md](./guru_sell_ledger_parity_plan.md) |
| **P6** | TP/SL overlays | **Deferred** | After P5 stable. |

### 8.1 P3.5 — Live validation hardening (**complete**; P3.5.1 ordering fix **complete**)

**Status:** Live validated (`sell_test_live_ordering_check` and prior runs). Immediate REST `try_arm` runs after `pending_registered`; WebSocket remains primary armer when Data API lags.

**Objectives:**

- Make **BUY → position visible → SELL armed → SELL submitted** deterministic enough for live validation.
- Prefer **WebSocket-first** synchronization for speed.
- Use **immediate REST positions refresh** after matched BUY plus periodic backstop.
- Add **facts** explaining why SELL is waiting or firing.
- Fix **completion semantics** so runs do not exit before SELL terminal outcome.

**Out of scope for P3.5:** allocation ledger (P4), TP/SL (P6), bypassing inventory risk gate.

#### A. WebSocket-first arming

- Verify `ingestion/user_stream.py`: `TRADE` + `CONFIRMED` → `apply_confirmed_trade_to_wallet` → positions updated.
- Verify every WS message path that updates positions calls `coord.scheduled_exit_demo_try_arm` (already on `apply_user_ws_message` loop in `user_stream.py`; confirm no gap).
- Document expected path (§3.1).

#### B. Immediate position refresh after matched BUY

- After live BUY `oms_submit` returns `matched` (or partial match), call **`refresh_positions_from_data_api`** immediately — do not wait for `venue_refresh_loop` (30s).
- Invoke in or after `refresh_wallet_coordinated_after_live_submit` / post-ack hook; then **`try_arm_live_pending`** again.
- Complements WS; does not replace it. Balance/open-order refresh unchanged.

**P3.5.1 ordering (live):** Immediate positions refresh and `try_arm` must run **after** the pending SELL row is registered, or the post-buy hook must call `try_arm` again after registration. This ensures `source='immediate_positions_refresh'` can actually arm the row. If refresh happens before `pending_registered`, `try_arm` is a no-op. Observed on first successful live `sell_test`: REST `/positions` ran but no `arm_attempt` with `immediate_positions_refresh` until this fix; WebSocket arming still succeeded.

#### C. Match evidence / fill hint

- Parse OMS JSON: `status`, `takingAmount`, `makingAmount`, `orderID`.
- Record in facts and/or local lifecycle state (operator-visible).
- **P3.5:** match evidence **triggers refresh and logging only** — does **not** bypass inventory risk.
- **P4/P6:** formal `FillLedger` / `InventoryLedger` may consume this.

#### D. Exit lifecycle facts

Add or extend facts (new `exit_lifecycle` fact type in `schema_v2.py` **or** structured `health` payloads — document in `reporting_fact_model.md`):

| Event | Purpose |
|-------|---------|
| `pending_registered` | Pending SELL row created after BUY ack |
| `arm_attempt` | try_arm evaluated |
| `arm_denied` / `waiting_for_inventory` | Not enough sellable qty yet |
| `arm_granted` | Timer started (`due_mono`) |
| `sell_due` | Due row popped for processing |
| `sell_intent_emitted` | ExitIntent work unit built |
| `sell_risk_denied` | Explicit reason from risk |
| `sell_submitted` | oms_submit SELL (or reuse `oms_submit` + correlation) |
| `sell_completed` / `sell_failed` | Terminal outcome |
| `timeout_waiting_for_sellable_inventory` | Position never visible in time |

**Each `arm_attempt` should include:**

- `token_id`, `parent_correlation_id`, `planned_sell_size`
- `wallet_position_qty`, `in_flight_qty`, `available_to_sell`, `required_qty`
- `source`: `websocket` \| `immediate_positions_refresh` \| `periodic_refresh` \| `post_buy_ack`
- `armed`: bool; `reason` if not armed

#### E. Completion semantics

- Fix `SellTestStrategy.is_done()` and equivalent demo semantics: **done = terminal SELL outcome**, not “SELL intent constructed”.
- Terminal outcomes:
  - SELL `oms_submit` succeeded
  - SELL `risk_decision` denied (explicit reason)
  - SELL `oms_reject`
  - Explicit timeout fact emitted
- Do not cancel `scheduled_exit_demo_due_loop` while SELL is due or in-flight.
- Main loop waits for terminal outcome or configured timeout — **not** only `--max-iterations`.
- Fix `_sell_emitted` (set too early in `_build_work_unit`) and `_buy_emitted` (set before risk/OMS — blocks retry on deny).

#### F. Retry / timing behavior

- Validation must allow: BUY match → WS or immediate REST confirm → `sell.delay_s` → SELL risk/OMS.
- Code must **not** rely solely on increasing `--max-iterations`.
- Emit **`timeout_waiting_for_sellable_inventory`** if position never becomes visible within configured window.
- Ops: use **unique `--run-name`** per attempt (overwriting `sell_test_live` loses post-mortems).

---

### 8.2 P4 — Allocation ledger (**complete**)

**Status:** Implemented and live-validated. Green run: **`allocation_test_live_auto_1`**.

- Track per-strategy (`owner_id`) token quantity attribution separate from venue `WalletStore.positions`.
- Runtime applies `apply_buy` after successful BUY OMS submit.
- Runtime applies `apply_sell` only when SELL OMS ack is **matched** (or shadow instant fill); **live/resting** SELL keeps `allocation_reserved` active and emits `allocation_exit_order_live` without decrementing `allocated_qty`.
- **P4.1 exit lifecycle:** user WS / reconcile promotes fills (`allocation_sell_applied` / `allocation_partial_fill_applied`) and cancellations (`allocation_released`, reason=`cancelled`) for reserved exit orders linked by `client_order_id` / `venue_order_id`.
- `sell_test`, `scheduled_exit_demo`, and `allocation_test` clamp planned SELL size to `get_available_allocated`.
- `clamp_to_venue_positions` on wallet sync when ledger allocated qty exceeds venue position (manual UI sells).
- Persist to `var/state/allocation_ledger.json`; emit `allocation_ledger` facts.

**Out of scope for P4:** TP/SL (P6), full guru SELL ledger parity (P5 — **next**), bypassing RiskEngine inventory.

**Owner IDs (P4):** `sell_test`, `guru_follow` (demo exits inherit guru owner).

### 8.3 P5 — Ledger-aware guru SELL parity (**complete**)

Implemented in `strategies/guru_follow/exits.py`, `strategy.py`, `pipeline.py`. See [guru_sell_ledger_parity_plan.md](./guru_sell_ledger_parity_plan.md).

- Guru mirror SELL clamps to `get_available_allocated(guru_follow, token)` and venue `available_to_sell`.
- `full_bot_position` = full **allocated** guru_follow position when ledger enabled.
- Facts: `guru_exit_allocation_blocked`, `guru_exit_allocation_clamped`, `guru_exit_sizing` on `intent_created`.
- Reason code: `guru_no_allocated_inventory`.

---

## 9. Validation step 1: 3-second BUY then SELL demo

### 9.1 Behavior

After successful BUY (shadow instant fill or live ack), schedule `ExitIntent` for same token and size (or clipped) **`sell.delay_s` seconds** later after **live arming** (sellable inventory visible).

Config: `exits.demo_forced_exit_*` (guru_follow) or `sell_test.yaml` (`sell.delay_s`).

### 9.2 Wiring (implemented; live arming gap — P3.5)

1. **Config:** `ExitsConfig` / `SellTestStrategyConfig`.
2. **After BUY OMS success:** `register_after_successful_buy` → `_pending_live` (live) or `_armed` (shadow).
3. **Shadow:** instant fill → arm immediately; scheduler drains due exits.
4. **Live:** timer starts only when `available_to_sell >= planned_sell_size` in `WalletStore.positions`.
5. **try_arm call sites today:** post-buy ack, user WS, `venue_refresh_loop`, provisional repair, bootstrap.
6. **P3.5 adds:** immediate `/positions` refresh after matched BUY; lifecycle facts; terminal `is_done`.
7. **P3.5.1:** immediate refresh + `try_arm(source='immediate_positions_refresh')` runs **after** `pending_registered` (post-buy hook), not before — otherwise `try_arm` is a no-op.

### 9.2a Why live arming uses venue positions (unchanged rationale)

Arming on `available_to_sell` aligns the scheduled SELL with what **`check_inventory_sell`** will enforce. P3.5 fixes **how quickly** positions become visible, not **whether** risk is bypassed.

### 9.3 State

- In-memory on strategy instance (demo + sell_test); restart loses pending rows — acceptable for validation.
- Optional persistence in `StrategyStore` — post-P3.5.

### 9.4 Success criteria — shadow

Operator sees (shared `correlation_id`):

1. `intent_created` (BUY EnterIntent)
2. `risk_decision` (approved)
3. `oms_submit` (BUY)
4. ~3s later `intent_created` (ExitIntent)
5. `risk_decision` (SELL approved)
6. `oms_submit` (SELL)
7. `wallet_sync` — position reduced / closed

### 9.4a Success criteria — live sell_test (P3.5 target)

A **successful** live validation must show in `facts.jsonl`:

1. `intent_created` BUY
2. `risk_decision` BUY approved
3. `oms_submit` BUY with `status` matched or accepted (+ match evidence fields when P3.5.C lands)
4. `exit_lifecycle` **`pending_registered`**
5. `exit_lifecycle` **`arm_attempt`** (one or more)
6. `exit_lifecycle` **`arm_granted`**
7. `intent_created` SELL / ExitIntent
8. `risk_decision` SELL approved
9. `oms_submit` SELL
10. `wallet_sync` or position evidence — qty reduced / closed
11. `health` **stopped** cleanly
12. `run_summary.json` written

**If SELL does not happen**, facts must explicitly show why (no silent stop):

| Reason code (fact payload) | Meaning |
|----------------------------|---------|
| `waiting_for_position` | Pending registered; arm attempts failing |
| `insufficient_inventory` | Arm or SELL risk: qty too low |
| `user_ws_stale` | WS not delivering; may affect readiness |
| `positions_refresh_failed` | Immediate or periodic REST failed |
| `timeout_waiting_for_sellable_inventory` | Gave up waiting for visibility |
| `sell_risk_denied` | Risk blocked SELL |
| `sell_oms_reject` | Venue rejected SELL submit |

### 9.5 Tests

| Test | Type | State |
|------|------|--------|
| `test_scheduled_exit_demo_registers_after_buy` | Unit | **Exists** |
| `test_scheduled_exit_demo_emits_exit_intent` | Unit | **Exists** |
| `test_pipeline_demo_exit_shadow_end_to_end` | Integration | **Exists** (golden) |
| `test_sell_test_strategy.py` | Unit/integration | **Exists** |
| **P3.5 additions** | | |
| Delayed position visibility → arm after inject | Integration | **Planned** |
| WS CONFIRMED triggers arming | Unit | **Planned** |
| Immediate positions refresh triggers arming | Unit | **Planned** |
| SELL intent built but not submitted → `is_done` false | Unit | **Planned** |
| BUY risk denied → no pending SELL | Unit | **Planned** |
| Partial fill → planned sell ≤ takingAmount | Unit | **Planned** |
| Timeout waiting for position → failure fact | Unit | **Planned** |
| Live manual | Manual | After P3.5 code; unique run name |

---

## 10. Validation step 2: virtual TP/SL (P6 — deferred)

After **P5 ledger-aware guru SELL** is stable:

- TP/SL as overlays emitting `ExitIntent` through same `process_intent_work_unit` path.
- Reuse **exit lifecycle** and **InventoryLedger** from P3.5/P4 foundation.
- Risk: policy-only gates (not embedded TP/SL logic in `risk/engine.py`).

Detailed TP/SL design remains a follow-on doc.

---

## 11. Open questions / risks

| Item | Risk | Status / mitigation |
|------|------|---------------------|
| **REST positions lag** | BUY matched; positions not visible; SELL never arms | **Resolved (P3.5).** WS-first arming + immediate `refresh_positions_from_data_api`; lifecycle facts. |
| **WebSocket delay / staleness** | CONFIRMED may arrive late | Mitigation in place; `user_ws_stale` in facts. |
| **Premature `is_done` / shutdown** | Main loop exits before SELL OMS | **Resolved (P3.5).** Terminal-state `is_done`. |
| **Insufficient lifecycle facts** | Operator cannot see arm attempts | **Resolved (P3.5).** `exit_lifecycle` facts. |
| **Guru SELL vs allocation** | Guru exit sizes to wallet; may exceed `guru_follow` allocation | **Resolved (P5).** Guru mirror SELL clamps to `guru_follow` allocation when ledger enabled. |
| **Same run name overwrites artifacts** | Lost post-mortem | Ops: unique `--run-name`; document in OPERATIONS.md. |
| **HTTP/2 ConnectionTerminated** | Noisy SDK transport errors on `get_open_orders` | **Not core blocker**; optional retry in P3.5+; REST `/data/orders` fallback works. |
| **Enqueue on submit vs on fill** | Phantom exit if submit fails | Enqueue on **`oms_submit` success** after `ack_submit` — implemented. |
| **Venue min size on exit** | Dust below min | Document; demo uses sizes ≥ min. |
| **Multiple scheduled exits same token** | Duplicate rows | Dedup by `(token_id, parent_correlation_id)`. |
| **Ledger vs manual UI trades** | Allocated qty drift | P4: clamp + fact. |
| **Concurrency** | Guru SELL + scheduled exit | Inventory + in-flight mitigate. |
| **Deployment cap on repeat tests** | Prior position blocks new BUY | Ops: clear position or use fresh token; not P3.5 code scope. |

---

## 12. Exact file/module list

### 12.1 Already changed (P1–P3)

- `src/tyrex_pm/runtime/pipeline.py` — `process_intent_work_unit`, scheduled exit loop, post-buy hooks
- `src/tyrex_pm/runtime/app.py` — sell_test loop, scheduler task
- `src/tyrex_pm/strategies/guru_follow/scheduled_exit_demo.py`
- `src/tyrex_pm/strategies/sell_test/strategy.py`, `pricing.py`
- `src/tyrex_pm/runtime/config.py` — `ExitsConfig`, `SellTestStrategyConfig`
- `config/strategies/sell_test.yaml`, `config/strategies/guru_follow.yaml`
- `tests/test_scheduled_exit_demo.py`, `tests/test_sell_test_strategy.py`, `tests/test_sell_test_pricing.py`

### 12.2 P3.5 — likely changes

| File | Changes |
|------|---------|
| `runtime/pipeline.py` | Post-BUY ack; immediate positions refresh; lifecycle fact emission; scheduler lifecycle |
| `runtime/app.py` | sell_test loop completion / shutdown; avoid cancelling scheduler mid-SELL |
| `strategies/sell_test/strategy.py` | `is_done` terminal semantics; pending/armed/terminal state; arm instrumentation |
| `strategies/guru_follow/scheduled_exit_demo.py` | Shared lifecycle instrumentation |
| `ingestion/user_stream.py` | Verify CONFIRMED → positions + try_arm hook |
| `venue/polymarket/positions_sync.py` | Reusable immediate refresh helper if needed |
| `runtime/coordinator.py` | Safe try_arm after position update (hook already exists) |
| `reporting/schema_v2.py` | `exit_lifecycle` fact type if not using `health` |
| `Docs/reporting_fact_model.md` | Document new fact payload |
| `Docs/OPERATIONS.md` | Unique run-name; live sell_test checklist |
| `tests/` | P3.5 regression suite (§9.5) |

### 12.3 P4 — allocation ledger (implemented)

- `state/allocation_ledger.py` — ledger + JSON persistence
- `runtime/allocation_runtime.py` — owner resolution, pipeline hooks, facts
- `runtime/pipeline.py` — buy/sell/reserve/clamp wiring
- `runtime/coordinator.py`, `runtime/app.py`, `runtime/config.py`
- `strategies/sell_test/strategy.py`, `strategies/guru_follow/scheduled_exit_demo.py` — sizing clamp
- `reporting/schema_v2.py` — `FACT_TYPE_ALLOCATION_LEDGER`
- `tests/test_allocation_ledger.py`, `tests/test_allocation_ledger_integration.py`

### 12.4 P5 — ledger-aware guru SELL (planned)

- `strategies/guru_follow/exits.py` — allocation + venue clamp on guru mirror SELL
- `strategies/guru_follow/strategy.py`, `runtime/pipeline.py` — pass `coord`, emit sizing facts
- `core/reason_codes.py` — `guru_no_allocated_inventory`
- `tests/test_guru_exit_allocation_sizing.py` — new unit/integration tests
- Full checklist: [guru_sell_ledger_parity_plan.md](./guru_sell_ledger_parity_plan.md)

### 12.5 P6+ (unchanged intent)

- `risk/engine.py` — optional allocation policy inputs only (TP/SL overlays)

---

## Summary

Tyrex_PM **implements** native V2 SELL through **`ExitIntent` / `ReduceIntent`**, shared **`process_intent_work_unit`**, **`scheduled_exit_demo`**, **`sell_test`**, and **`allocation_test`**. Shadow/unit validation (P3) and live validation hardening (P3.5) are complete. The **allocation ledger (P4)**, resting SELL lifecycle (P4.1), and auto-pricing validation are complete — green run **`allocation_test_live_auto_1`**.

**Current next phase:** **P6 TP/SL overlays** (deferred until P5 live guru validation is stable). P5 guru allocation-aware SELL is implemented in code and covered by unit/integration tests.
