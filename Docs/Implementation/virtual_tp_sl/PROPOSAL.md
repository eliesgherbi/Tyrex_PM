# Virtual take-profit / stop-loss on Polymarket — implementation proposal

**Location:** `Docs/Implementation/virtual_tp_sl/` (in-flight implementation folder; see [README.md](README.md)).  
**Status:** design / audit (code-evidence based).  
**Constraint:** Polymarket has no native stop, bracket, OCO, or conditional orders; contingency is Tyrex-owned.  
**Project alignment:** Tier A = `VenueState` (wallet sync); Tier B = Nautilus `Cache` / `Portfolio` / order lifecycle — per `Docs/LIVE_ARCHITECTURE.md`.

---

## 1. Architecture recommendation

**Primary recommendation:** introduce a **Tyrex-owned `VirtualExitManager` (runtime + execution scope)** that:

1. **Arms** protected lots only after **observed BUY entry fills** (Tier B order events, optionally cross-checked with Tier A).
2. **Evaluates** TP/SL against an **executable sell-side reference** (e.g. cache bid / last, with book option mirroring `NautilusGuruExecutionPort` patterns).
3. **Submits** ordinary **SELL** `OrderIntent`s through the **same trusted path** as guru exits: `ConfiguredRiskPolicy.evaluate` → `NautilusGuruExecutionPort.submit_intent` (live).

**Thin strategy hook (required by Nautilus, not “fat strategy”):** `CopyStrategy.on_order_event` is already the centralized ingress for framework `OrderEvent`s and forwards to the execution port (`notify_order_event`). Add a **one-line delegation** to `VirtualExitManager.on_order_event` so fill/cancel/deny visibility stays correct. All state machine, persistence, and trigger logic live in **`src/tyrex_pm/runtime/`** (or `src/tyrex_pm/execution/` for submit-specific helpers), **not** in guru signal handling or Layer A.

**Why not a second Nautilus `Actor` alone:** guru monitor/stream and wallet sync are `Actor`s with timers, but **order fill streams for the copy strategy are delivered to the registered `Strategy`** (`CopyStrategy.on_order_event`). There is **no** separate Tyrex component today that receives all `OrderFilled` events for the strategy without going through the strategy (see `copy_strategy.py`). An actor could poll `Cache` or subscribe to internal bus topics, but that duplicates framework wiring and races the strategy path. **Delegate from strategy; own logic in service.**

**Why not embed in `ConfiguredRiskPolicy`:** risk is **pre-trade gating** and deployment math (`configured.py`). Virtual exits are **post-fill, continuous monitors** with their own state machine, timers, and reconciliation — mixing them into risk blurs boundaries documented in `Docs/Architecture.md` / `Docs/LIVE_ARCHITECTURE.md` (signal vs risk vs runtime).

**Why not inside `CopyStrategy` beyond a hook:** `CopyStrategy` is explicitly the **guru bus → Layer A → sizing → risk → execution** pipeline (`copy_strategy.py`, module docstring). TP/SL is **orthogonal** to guru signals; keeping hundreds of lines there violates the “thin strategy” principle called out in Layer A docs (`Docs/Implementation/LayerA_Filters/00_general_plan.md`).

---

## 2. Code-evidence audit: live path

### 2.1 Guru signal ingestion

| Step | Evidence |
|------|----------|
| Poll path | `GuruMonitorActor` polls Data API, builds pipeline, timer in `on_start` | `src/tyrex_pm/data/guru_monitor.py` (`GURU_TRADE_TOPIC`, `GuruSignalPipeline`, `clock.set_timer`) |
| RTDS path | `GuruStreamActor` WebSocket worker + drain timer | `src/tyrex_pm/data/guru_stream_actor.py` |
| Publish | `GuruSignalPipeline.try_publish` → `msgbus.publish(topic, sig)` + dedup/watermark | `src/tyrex_pm/data/guru_ingest_pipeline.py` |

### 2.2 Internal signal creation

- **`GuruTradeSignal`** dataclass: `src/tyrex_pm/core/types.py`.
- **Topic:** `tyrex_pm.guru.GuruTradeSignal` (`guru_monitor.py`).

### 2.3 Layer A filters

- **`LayerAOrchestrator.run`** branches `entry` vs `exit`, token → static → significance (entry) or exit filter (exit) | `src/tyrex_pm/signal/layer_a/orchestrator.py`.
- **`CopyStrategy._on_guru_trade`** classifies BUY/SELL, runs orchestrator, emits `layer_a_filter` / skip facts | `src/tyrex_pm/strategy/copy_strategy.py`.

### 2.4 Risk evaluation

- **`CopyStrategy._handle_branch`** calls `self._risk.evaluate(intent)` after startup gate | `copy_strategy.py`.
- **`ConfiguredRiskPolicy.evaluate`** — kill switch, health, deploy clip, capital, **SELL inventory gate**, token/portfolio caps (with SELL bypass), concurrent guru rests | `src/tyrex_pm/risk/configured.py` (`_sell_exit_inventory_gate`, `_evaluate_impl`).

### 2.5 Live order submission

- **`CopyStrategy`** → `self._execution.submit_intent(intent_risk, mode=...)` | `copy_strategy.py`.
- **`NautilusGuruExecutionPort.submit_intent`** — resolve instrument, optional book guard/clip, `order_factory.limit`, `submit_order(..., POLYMARKET_CLIENT_ID)` | `src/tyrex_pm/execution/nautilus_guru_exec.py`.

### 2.6 Order / fill / position event handling

- **`CopyStrategy.on_order_event`** — skips reconciliation-flag events, calls `super()`, **`emit_order_event_facts`**, **`emit_position_snapshot`** (uses `cache.price(..., PriceType.LAST)`), **`self._execution.notify_order_event(event)`** | `copy_strategy.py`.

### 2.7 VenueState / WalletSync reads

- **Compose:** `VenueState` constructed when `live and wallet_sync_enabled`; passed to `NautilusExecutionStateReader`, `NautilusAccountSnapshotProvider`, `NautilusPositionStateReader`, `NautilusDeploymentBudget`, `WalletSyncActor`, `NautilusLayerAContext` | `src/tyrex_pm/runtime/guru_compose.py`.
- **`WalletSyncActor`** — timer-driven sync, writes to `VenueState.apply_positions_and_orders_rows` | `src/tyrex_pm/runtime/wallet_sync.py` (see `on_start` / `venue_state`).
- **`VenueState`** — positions, resting `OrderSnapshot`s, cash, `is_stale()` | `src/tyrex_pm/runtime/venue_state.py`.

### 2.8 Startup hydration and restart recovery

- **Guru dedup/watermark** loaded in compose before actors start | `guru_compose.py` (`GuruDedupStore`, `GuruWatermarkStore`, `load` / `persist`).
- **Nautilus node:** `TradingNodeConfig(..., load_state=False, save_state=False)` | `guru_compose.py` — **no** framework persistence of local virtual orders/state today.
- **`WalletSyncActor`:** “The actor has no persistent state of its own” | `wallet_sync.py` docstring — **Tier A snapshots rehydrate from HTTP each run**, not from Tyrex virtual exit state.

---

## 3. Best attachment points for `VirtualExitManager`

| Need | Where it exists today | Proposed use |
|------|----------------------|--------------|
| Entry fills visible | `CopyStrategy.on_order_event` → `OrderFilled` via `emit_order_event_facts` | Delegate `OrderFilled` (and partial fill accumulation) to manager |
| Managed position / lot | After fill: `cache`, `portfolio`, `VenueState.position_size` | Create lot keyed by `instrument_id` + `entry_client_order_id` (+ `guru correlation` if known) |
| Market price updates | `cache.price(instrument_id, PriceType.LAST)` in strategy; book via `resolve_book_top` in exec | Manager reads cache (and optional REST book like `nautilus_guru_exec`) on timer or after data events |
| Timers | `strategy.clock.set_timer` in `NautilusGuruExecutionPort._schedule_limit_cancel`; `WalletSyncActor`, `GuruMonitorActor` | Manager schedules `evaluate_triggers` timer (or piggyback on existing periodic cadence) |
| Sell intents | Same as guru: `ConfiguredRiskPolicy.evaluate` + `NautilusGuruExecutionPort.submit_intent` | Manager builds `OrderIntent(side=SELL, signal_kind="exit" or new kind)` — see §7 |
| Partial fills | `OrderFilled` `last_qty` / lifecycle in `reporting/order_events.py` | Accumulate filled qty per entry order id; arm only when **cumulative** entry fill ≥ policy minimum |
| Cancel / reject | `OrderCanceled`, `OrderRejected`, `OrderDenied` in `emit_order_event_facts` | Manager marks lot “entry incomplete” or “exit failed”; retry policy |
| Wallet drift | `VenueState.positions()` vs internal `protected_qty` | Reconcile: clamp, pause, or disarm with fact |

---

## 4. Nautilus / Tyrex features already present (relevant to v1)

| Feature | Evidence |
|---------|----------|
| Order events / fills | `CopyStrategy.on_order_event`, `reporting/order_events.py` (`OrderFilled` → `fill` fact) |
| Cache / portfolio | `NautilusGuruExecutionPort` uses `self._strategy.cache`; `NautilusLayerAContext` uses `portfolio` / `venue_state` |
| Timers | `nautilus_guru_exec.py` `clock.set_timer` for limit cancel; actors use `set_timer` |
| **`OrderFactory`** | `self._strategy.order_factory.limit(...)` in `nautilus_guru_exec.py` |
| Emulated / native contingent orders | **No** matches for bracket / OCO / contingent / virtual orders in `src/` (grep) — **must be Tyrex logic** |
| Persistence for virtual state | **None** today; `load_state=False`, `save_state=False` in compose |

---

## 5. Risk gates: “risk-reducing exit” path

**Existing behavior (important):** For `SELL`, `ConfiguredRiskPolicy._evaluate_impl`:

1. Runs **`_sell_exit_inventory_gate`** — requires `deployment_budget.filled_usd_for_token` and `order_deploy <= filled` (prevents naked / oversized sells) | `configured.py`.
2. May **bypass additive token/portfolio open-cap checks** when `_sell_additive_open_cap_bypass_enabled()` | `configured.py`.

So **virtual exits should not be blocked by entry-oriented deployment caps** in the same way as BUYs; they still must:

- Pass **inventory verification** (sellable qty vs Tier A/Tier B truth).
- Respect **kill switch**, **capital gate** (if applicable to sells), **health gate** (with `allow_exit_when_degraded_oms` semantics), **per-order max notional**, **concurrent guru resting** if virtual exits use the same tag/correlation rules, etc.

**Recommendation:** add an explicit **`OrderIntent` discriminator** (e.g. `origin: Literal["guru","virtual_tp","virtual_sl"]` or dedicated `reason_code` prefix) so facts and future risk tweaks (e.g. exempt virtual exits from “guru concurrent” if needed) are traceable. **Do not** weaken `_sell_exit_inventory_gate`.

---

## 6. Proposed state machine (virtual exits)

States per **managed lot** (long-only v1):

```text
UNARMED ──(entry OrderFilled cumulative qty > 0)──► ARMED_BOTH
   │                        │
   │                        ├──(price hits TP)──► FIRING_TP ──► COMPLETE | PARTIAL_EXIT
   │                        ├──(price hits SL)──► FIRING_SL ──► COMPLETE | PARTIAL_EXIT
   │                        └──(external flat / drift)──► DISARMED_DRIFT | REDUCED
   │
   └──(entry denied / canceled before fill)──► CANCELLED
```

Rules:

- **ARMED_BOTH:** both TP and SL active; **first trigger disarms the sibling** (single winner).
- **PARTIAL_EXIT:** after virtual exit partial fill, either **re-arm** remaining qty with **same** TP/SL pct from **remaining cost basis** or **recenter** from policy (v1: simplest — recompute stop/target notional from **remaining qty × entry_vwap**).
- **FIRING_***:** idempotent submit guard (one outstanding virtual exit order per lot unless retrying).

---

## 7. v1 functional design

| Requirement | Design |
|-------------|--------|
| Fixed SL % / TP % | Config on strategy or runtime (typed YAML); applied to **entry VWAP** after fills |
| Long only | Ignore short / negative `net_position` |
| Arm only after actual fill | Observe `OrderFilled` for **entry** client order ids registered when guru BUY submits |
| Executable sell-side price | Prefer **best bid** from cache book or REST snapshot (mirror exec book helpers); fallback `PriceType.LAST` with stale guard |
| Trusted live path | Build `OrderIntent` → `risk.evaluate` → `execution.submit_intent` (same instances as `CopyStrategy`) |
| Disarm sibling | On trigger, cancel sibling **logical** arm; no exchange OCO |
| Partial fills | Accumulate entry; on exit partial, update qty and re-evaluate |
| External / manual drift | Compare `VenueState.position_size` (Tier A) vs protected qty; if wallet smaller, **clamp**; if larger, **do not auto-expand** protected qty without policy |
| Restart | Persist lots to JSON; on startup after `wallet_sync_first_sync_complete`, **rebuild** from disk + live positions; drop stale lots |

---

## 8. State model (managed lot)

**Suggested fields (dataclass / JSON rows):**

| Field | Purpose |
|-------|---------|
| `lot_id` | Stable UUID (persisted) |
| `instrument_id` | `str` |
| `token_id` | Outcome token |
| `entry_guru_correlation_id` | Guru `source_trade_id` when known (from `OrderCorrelationRegistry`) |
| `entry_client_order_id` | Tyrex submit id (`TX…` hash in `nautilus_guru_exec`) |
| `entry_side` | `"BUY"` |
| `entry_qty_cum` | Cumulative filled entry qty |
| `entry_vwap` | VWAP of entry fills |
| `tp_pct`, `sl_pct` | From config at arm time (freeze) |
| `tp_price`, `sl_price` | Derived at arm / update |
| `armed` | bool |
| `sibling_disarmed` | `"none"` \| `"tp"` \| `"sl"` after first fire |
| `exit_in_flight_coid` | Optional str |
| `version` | Schema int |

**Keying:**

- **Primary:** `lot_id` (persisted).
- **Correlation:** `entry_client_order_id` + `instrument_id` maps to guru correlation via existing `OrderCorrelationRegistry` (`guru_compose.py`, `nautilus_guru_exec.py` `register`).

**Persistence:**

- New file e.g. `virtual_exit_state_path` in **`RuntimeSettings`** (alongside `guru_dedup_state_path`) — atomic write (tmp + replace), same pattern as dedup store.

**Rebuild on restart:**

1. Load JSON lots with `armed=True`.
2. Wait for readiness (`StartupReadinessGate` / wallet sync — same as trading).
3. For each lot: resolve `instrument_id` in cache; read Tier A `VenueState.position_size`; **clamp** `entry_qty_cum` to min(persisted, venue long qty).
4. If position flat → mark `COMPLETE` / drop.
5. Recompute TP/SL prices from `entry_vwap` and config.

**Tier A vs Tier B:**

- **Tier A** authoritative for **sellable size** and drift (`VenueState`).
- **Tier B** authoritative for **which orders filled** and **exit order lifecycle** (`OrderFilled`, `Cache.order`).

---

## 9. Execution behavior (exits)

| Topic | Recommendation |
|-------|----------------|
| Order type | Default **GTC limit** at aggressive price (mirror guru: `order_factory.limit` + quantize) — same as `nautilus_guru_exec.py`. Optional **FAK/marketable** if Polymarket adapter supports via future flag (verify adapter API before promising). |
| Config | `virtual_exit_order_style: aggressive_limit | market_fak` (v1 implement aggressive limit only) |
| Partial exit fill | Update lot qty; if remainder > min size, **re-submit** after cooldown; emit `virtual_exit` facts |
| Retry / cooldown | Exponential backoff on `REJECTED`/`DENIED`; cap retries; **stale quote refresh** before retry |
| Facts | `virtual_exit_arm`, `virtual_exit_trigger`, `virtual_exit_submit`, `virtual_exit_reconcile`, `virtual_exit_disarm` (+ reuse `fill`, `order_lifecycle`, `risk_decision`) |

**`NautilusGuruExecutionPort` change:** today `ClientOrderId` is derived **only** from guru `correlation_id` (`_client_order_id_from_guru_correlation`). Virtual exits need **non-colliding** ids and tags (e.g. `virt_tp=<lot_id>`). Add **`submit_intent_virtual`** or optional `client_order_id_factory` / `tags` on `OrderIntent`.

---

## 10. Concrete patch plan (ordered)

1. **Types:** extend `OrderIntent` with optional `intent_origin` / `virtual_lot_id` (or parallel field) | `core/types.py`.
2. **Config:** `VirtualExitSettings` + `StrategySettings.virtual_exit` or `RuntimeSettings` flags; loader + YAML | `config/loaders.py`, `config/strategy/*.yaml`, `config/runtime/*.yaml`.
3. **`VirtualExitManager`:** new module `src/tyrex_pm/runtime/virtual_exit/manager.py` (state machine, persistence, trigger eval).
4. **Persistence:** `VirtualExitStore` (JSON) | `src/tyrex_pm/runtime/virtual_exit/store.py`.
5. **Compose:** construct manager, inject into `CopyStrategy` | `guru_compose.py`.
6. **`CopyStrategy`:** `set_virtual_exit_manager`, call `mgr.on_order_event` + optional timer registration in `on_start` | `copy_strategy.py`.
7. **`NautilusGuruExecutionPort`:** virtual submit id + tags | `nautilus_guru_exec.py`.
8. **Risk (minor):** if concurrent guru cap blocks virtual exits, add exception by `intent_origin` | `configured.py` (only if observed in tests).
9. **Reporting:** fact payloads in `reporting/` (new emitter helper).
10. **Tests:** unit tests manager + store; integration shadow with `NoOpExecutionPort` recording SELL intents; golden-path fill simulation.

**Operator migration:**

- Enable via strategy/runtime YAML; set `virtual_exit_state_path`.
- **No** change to guru watermark/dedup.
- On first deploy, empty store; existing positions **optional** “adopt” policy (v2) — v1 only protects **new** entries after feature on.

---

## 11. Risks and edge cases

| Risk | Mitigation |
|------|------------|
| Stale prices (LAST) | Stale TTL; prefer book; block trigger if `VenueState.is_stale` |
| Double-submit TP+SL | Single-winner state + in-flight guard |
| Guru + virtual exit race | Both go through risk; inventory gate prevents oversell |
| Restart mid-flight | Persist `exit_in_flight_coid`; reconcile with `Cache.order` |
| Partial entry then crash | VWAP + cum qty from persisted + fill replay from facts optional v2 |
| `max_concurrent_guru_resting_orders` | Virtual orders may need tagging / exemption |
| Shutdown drain | Respect `ExecutionLifecycleStatus` — virtual exits are SELL; may still run under degraded rules |

---

## 12. Why this beats alternatives (summary)

| Alternative | Issue |
|-------------|--------|
| Native Polymarket OCO | **Not available** (external constraint) |
| All-in `CopyStrategy` | Mixes guru pipeline with post-trade lifecycle; violates documented thin-strategy boundary |
| All-in `ConfiguredRiskPolicy` | Risk is pre-trade; virtual exits are continuous monitors |
| Standalone `Actor` without strategy hook | Misses or duplicates `OrderFilled` delivery path tied to `Strategy` today |

**Bottom line:** **`VirtualExitManager` in runtime/execution**, **thin `CopyStrategy` hook**, **same risk + `NautilusGuruExecutionPort` submit path**, **Tier A/Tier B reconciliation** — matches your stated preference and the repo’s stabilized live model.

---

## 13. References (files)

- `src/tyrex_pm/runtime/guru_compose.py` — node, `VenueState`, `WalletSyncActor`, risk, strategy, execution port wiring
- `src/tyrex_pm/strategy/copy_strategy.py` — guru bus, Layer A, risk, `submit_intent`, `on_order_event`
- `src/tyrex_pm/execution/nautilus_guru_exec.py` — `order_factory.limit`, `submit_order`, timers
- `src/tyrex_pm/risk/configured.py` — `evaluate`, SELL inventory gate, open-cap bypass
- `src/tyrex_pm/data/guru_ingest_pipeline.py`, `guru_monitor.py`, `guru_stream_actor.py` — ingest
- `src/tyrex_pm/runtime/venue_state.py`, `wallet_sync.py` — Tier A
- `src/tyrex_pm/reporting/order_events.py` — fill / lifecycle facts
- `Docs/LIVE_ARCHITECTURE.md` — Tier A / Tier B split
