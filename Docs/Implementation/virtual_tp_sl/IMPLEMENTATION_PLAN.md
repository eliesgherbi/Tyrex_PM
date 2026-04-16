# Virtual TP/SL — detailed implementation plan (Tyrex_PM)

| Field | Value |
|--------|--------|
| **Status** | Pre-implementation — review only |
| **Authoritative for** | Virtual take-profit / stop-loss (Polymarket, Tyrex-owned contingency) |
| **Not implementation yet** | yes |
| **Depends on review approval** | yes |

**Related docs (do not duplicate):** audit + architecture rationale in [`PROPOSAL.md`](PROPOSAL.md); live truth model in [`../../LIVE_ARCHITECTURE.md`](../../LIVE_ARCHITECTURE.md); ops in [`../../OPERATIONS.md`](../../OPERATIONS.md); module map in [`../../Architecture.md`](../../Architecture.md); config rules in [`../../CONFIG_MODEL.md`](../../CONFIG_MODEL.md); dev workflow in [`../../developer_guide.md`](../../developer_guide.md).

---

## 1. Executive decision

| Option | Summary |
|--------|---------|
| **A** | Thin `CopyStrategy` hook + **`VirtualExitManager`** (runtime/execution scope) |
| **B** | Dedicated **`VirtualExitActor`** (`nautilus_trader.common.actor.Actor`) + store + minimal strategy involvement |

### Recommendation for **this branch**

**Choose Option A (thin `CopyStrategy` hook + `VirtualExitManager`).**

**Evidence — why the strategy path is already the canonical order-event ingress**

- `CopyStrategy.on_order_event` handles the full `OrderEvent` union, reporting (`emit_order_event_facts`), position snapshots, and `NautilusGuruExecutionPort.notify_order_event` for limit-timeout cancellation | `src/tyrex_pm/strategy/copy_strategy.py`.
- Guru live submission is **`order_factory.limit`** + `submit_order` only today | `src/tyrex_pm/execution/nautilus_guru_exec.py` (`submit_intent`).

**Evidence — Option B is *possible* in pinned Nautilus but incomplete without extra wiring**

- Installed **nautilus-trader 1.225.0** (see §2). In package source `nautilus_trader/common/actor.pyx`:
  - `subscribe_order_fills(InstrumentId)` subscribes to topic `events.fills.{instrument_id}` and forwards to `on_order_filled`.
  - `subscribe_order_cancels(InstrumentId)` subscribes to `events.cancels.{instrument_id}` and forwards to `on_order_canceled`.
- **Gaps for TP/SL:** there is **no** matching documented `subscribe_order_*` for **OrderRejected** / **OrderDenied** / all lifecycle types on a plain `Actor` in the same file (only fills + cancels). `Strategy` exposes `on_order_event` and granular `on_order_*` handlers | `nautilus_trader/trading/strategy.pyx` (`cpdef void on_order_event`).
- A `VirtualExitActor` would need **per-instrument** subscribe/unsubscribe whenever markets activate/deactivate (dynamic instruments — `GuruInstrumentDynamicController` in `guru_compose.py`), or would miss events. That duplicates bookkeeping already unnecessary if Option A forwards from `CopyStrategy`.

**Conclusion:** Option A minimizes new moving parts, preserves one order-event ordering context with guru copy logic, and matches the existing **thin strategy** pattern (delegate to services). Option B is a **future refactor** only if the manager must outlive strategy or multiple strategies share exits.

---

## 2. Verified constraints

### 2.1 Confirmed (repository)

| Constraint | Evidence |
|------------|----------|
| Tier A = `VenueState` + `WalletSyncActor`; Tier B = Nautilus cache/portfolio/exec | `Docs/LIVE_ARCHITECTURE.md`; wiring in `build_guru_trading_node` | `src/tyrex_pm/runtime/guru_compose.py` |
| `CopyStrategy`: guru bus → Layer A → sizing → `ConfiguredRiskPolicy.evaluate` → `ExecutionPort.submit_intent` | `src/tyrex_pm/strategy/copy_strategy.py` |
| Live guru uses **limit** orders, GTC, `OrderFactory.limit`, Polymarket client id | `src/tyrex_pm/execution/nautilus_guru_exec.py` |
| SELL inventory gate + additive open-cap bypass for eligible SELL | `ConfiguredRiskPolicy._sell_exit_inventory_gate`, `_evaluate_impl` | `src/tyrex_pm/risk/configured.py` |
| Guru concurrent resting cap counts **guru-tagged** (or `TX`+26hex) open orders only | `is_guru_resting_order`, `count_guru_resting_orders_open` | `src/tyrex_pm/runtime/state_readers.py`; tags from `_guru_tag` in `nautilus_guru_exec.py` |
| No framework persistence for node local state by default | `load_state=False`, `save_state=False` | `src/tyrex_pm/runtime/guru_compose.py` |
| Reporting: `OrderFilled` → `fill` + `order_lifecycle` | `emit_order_event_facts` | `src/tyrex_pm/reporting/order_events.py` |
| Startup / SELL under degraded OMS | `ExecutionLifecycleStatus.block_reason_for_side` | `src/tyrex_pm/runtime/lifecycle/status.py` |

### 2.2 Confirmed (installed Nautilus **1.225.0**)

| Item | Evidence |
|------|----------|
| **Pinned version** | `pyproject.toml`: `nautilus_trader[polymarket]==1.225.0`; `uv.lock` package `nautilus-trader` **1.225.0** |
| **`Actor.subscribe_order_fills` / `subscribe_order_cancels`** | `nautilus_trader/common/actor.pyx` (`cpdef void subscribe_order_fills`, `subscribe_order_cancels`; topics `events.fills.{instrument_id}`, `events.cancels.{instrument_id}`) |
| **`Strategy.on_order_event`** | `nautilus_trader/trading/strategy.pyx` |
| **`OrderFactory.market` and `OrderFactory.limit`** | `nautilus_trader/common/factories.pyx` |

**Verification method:** `python -c "import nautilus_trader; print(nautilus_trader.__version__)"` → **1.225.0** (workspace interpreter); `.pyx` read from site-packages.

### 2.3 Confirmed (Polymarket adapter in **1.225.0** — market SELL)

| Item | Evidence |
|------|----------|
| **MARKET orders supported** | `PolymarketExecutionClient._submit_order`: branch `OrderType.MARKET` → `_submit_market_order` | `nautilus_trader/adapters/polymarket/execution.py` |
| **Market SELL quantity** | For `OrderSide` not BUY: if `order.is_quote_quantity` → **deny**; else base-denominated `amount = float(order.quantity)` | same file `_submit_market_order` |
| **Venue mapping** | `MarketOrderArgs` with `order_type=PolyOrderType.FOK` (Polymarket **FOK** semantics for market) | same file |
| **Tyrex today** | Does **not** call `order_factory.market`; only `limit` | `nautilus_guru_exec.py` |

**Inferred (adapter-level):** “Market” on Polymarket is implemented as **FOK-style** submission (`PolyOrderType.FOK`), not a resting order — behavior on partial liquidity is **venue-defined**; treat as **aggressive immediate** with possible full reject if book insufficient.

### 2.4 Inferred (needs live test proof)

| Item | Why |
|------|-----|
| Exact fill/reject ratio for FOK market SELL on illiquid outcome tokens | Venue + book state dependent |
| Whether `PriceType.LAST` is always present for dynamically activated instruments | Data client / subscription timing |
| Restart ordering: whether any `OrderFilled` can arrive before manager finishes loading persisted state | Race — needs tests |

### 2.5 Unknown / must not assume

| Item | Proof needed |
|------|----------------|
| Whether operators want **market SELL** as default for SL vs limit | Product/ops; implement both behind config after review |
| Optimal debounce interval for trigger evaluation | Tune in paper/shadow |

---

## 3. Final target architecture

### 3.1 Components

| Piece | Location (proposed) | Role |
|-------|---------------------|------|
| **`VirtualExitManager`** | `src/tyrex_pm/runtime/virtual_exit/manager.py` (package) | State machine, trigger evaluation, reconciliation, calls risk+exec |
| **`VirtualExitStore`** | `src/tyrex_pm/runtime/virtual_exit/store.py` | Atomic JSON persistence |
| **Types / config** | `src/tyrex_pm/core/types.py` (extend `OrderIntent` or adjacent dataclass); `src/tyrex_pm/config/loaders.py` | Typed YAML |
| **Execution** | Extend `src/tyrex_pm/execution/nautilus_guru_exec.py` **or** parallel helper used by manager | Market vs aggressive limit for virtual exits; **non-guru** `ClientOrderId` + tags |
| **Compose** | `src/tyrex_pm/runtime/guru_compose.py` | Construct manager, inject refs (`VenueState`, `ConfiguredRiskPolicy`, execution port, strategy, clock, emit) |
| **Strategy hook** | `src/tyrex_pm/strategy/copy_strategy.py` | After existing `on_order_event` work: forward to manager (and register pending entry client order ids when guru BUY submits — **or** manager listens via callback from execution port on submit) |

### 3.2 Information flows

| Data | Source |
|------|--------|
| **Fills / order lifecycle** | `CopyStrategy.on_order_event` → `VirtualExitManager.on_order_event(event)` |
| **Price / book** | `Strategy.cache` (`PriceType.LAST`), optional REST book (`build_clob_client_from_env`, `resolve_book_top` pattern from `nautilus_guru_exec.py`) |
| **Tier A** | `VenueState.position_size`, `is_stale`, `last_success_utc` | `src/tyrex_pm/runtime/venue_state.py` |
| **Tier B** | `Cache.order`, `Portfolio` via existing readers if needed |
| **Exit submit** | Build `OrderIntent` → `ConfiguredRiskPolicy.evaluate` → same `NautilusGuruExecutionPort` instance (or extracted submit function) |

### 3.3 Persistence

- New path in **runtime** YAML (e.g. `virtual_exit_state_path`), JSON file, atomic writes — pattern analogous to `GuruDedupStore` paths | see `guru_compose.py` for guru paths.

### 3.4 Reporting

- New fact types (§12) via existing `run_context.emit` / `FactEmitFn` passed into manager.

### 3.5 Explicitly **outside**

| Area | Reason |
|------|--------|
| **Layer A** (`LayerAOrchestrator`, filters) | Signal gating only — TP/SL is post-fill |
| **`GuruSignalPipeline` / ingest** | No guru event required for virtual exits |
| **`ConfiguredRiskPolicy` core** | Continuous monitoring does not belong inside `evaluate`; optional **small** hook only if `intent_origin` needs a branch (e.g. concurrent cap) |

---

## 4. Detailed state model (persisted + derived)

### 4.1 `ProtectedLot` (one row per logical position slice)

**Persisted fields (JSON-serializable):**

| Field | Type | Notes |
|-------|------|--------|
| `schema_version` | `int` | Start at **1** |
| `lot_id` | `str` | UUID4 stable |
| `instrument_id` | `str` | Full `InstrumentId` string |
| `token_id` | `str` | Outcome token |
| `entry_guru_correlation_id` | `str \| null` | From `OrderCorrelationRegistry` when BUY is guru-driven |
| `entry_client_order_id` | `str` | Tyrex submit id for the **entry** order |
| `entry_qty_filled` | `float` | Cumulative **BUY** fill qty for this lot |
| `entry_vwap` | `float` | Running VWAP (updated on each entry fill) |
| `tp_pct` | `float` | Frozen at first arm |
| `sl_pct` | `float` | Frozen at first arm |
| `tp_trigger_price` | `float \| null` | Derived |
| `sl_trigger_price` | `float \| null` | Derived |
| `state` | `str` | Enum string — see §5 |
| `armed_sibling` | `str` | `"both"` \| `"tp_only"` \| `"sl_only"` \| `"none"` |
| `exit_client_order_id` | `str \| null` | In-flight **virtual** exit |
| `exit_kind` | `str \| null` | `"tp"` \| `"sl"` |
| `last_trigger_ts_ms` | `int \| null` | Idempotency / debounce |
| `created_ts_ms` | `int` | |
| `updated_ts_ms` | `int` | |

**Recomputed on restart (not stored as source of truth):**

- Trigger prices from `entry_vwap` + pct (if policy says recompute from config on restart, document operator impact — default: **use stored thresholds**).

**Discarded on restart:**

- Ephemeral “evaluating” flags; in-memory debounce counters.

**Keying:**

- Primary: `lot_id`.
- Secondary lookup: `(instrument_id, entry_client_order_id)` for idempotent arm from same entry order.

**Link to guru:**

- `OrderCorrelationRegistry.correlation_for(client_order_id)` | `src/tyrex_pm/reporting/correlation_registry.py` — used when emitting facts, not required for trigger math.

---

## 5. State machine (v1)

**Lot-level states** (`state` field):

| State | Meaning |
|-------|---------|
| `PENDING_ENTRY` | Guru BUY submitted; waiting for fills |
| `ARMED` | Entry fill > 0; TP+SL active (or sibling already disarmed) |
| `TRIGGERED_TP` / `TRIGGERED_SL` | Condition met; exit submission in progress |
| `EXIT_SUBMITTED` | Virtual exit order accepted path (Tier B) |
| `EXIT_PARTIAL` | Partial exit fill; qty updated |
| `COMPLETED` | Position flat for this lot |
| `DISARMED_DRIFT` | Tier A qty < protected; policy disarmed |
| `DISARMED_EXTERNAL_FLAT` | Venue flat; cleanup |
| `FAILED` | Unrecoverable (operator intervention) |

**Transitions (high level):**

1. `PENDING_ENTRY` → `ARMED` on first `OrderFilled` for tracked entry `client_order_id`, side BUY, instrument match; accumulate qty + VWAP.
2. `ARMED` → `TRIGGERED_*` when trigger semantics (§6) fire; **set `armed_sibling` to single-leg** (disarm sibling).
3. `TRIGGERED_*` → `EXIT_SUBMITTED` after successful `submit_order` path (or stay in `TRIGGERED_*` until accept — implementation choice: merge `TRIGGERED` + `EXIT_SUBMITTED` if simpler).
4. `EXIT_SUBMITTED` → `EXIT_PARTIAL` on partial SELL fill; → `COMPLETED` when protected qty → 0.
5. `ARMED` / `EXIT_PARTIAL` → `DISARMED_DRIFT` if Tier A sellable < remaining (policy).
6. `*` → `DISARMED_EXTERNAL_FLAT` if Tier A position 0 for instrument.
7. Reject/deny on exit → `ARMED` or `EXIT_SUBMITTED` with retry counter → `FAILED` if max retries.

**Idempotency:**

- **One active virtual exit order per lot** (`exit_client_order_id` non-null blocks second submit).
- **Trigger:** `last_trigger_ts_ms` + debounce window prevents double-fire on same tick.
- **Entry fill accumulation:** use `(client_order_id, venue_order_id?, ts_event_ns, last_qty)` hash already emitted in `fill` facts | `order_events.py`.

**Stale market hold:** do not transition `ARMED` → `TRIGGERED_*` if quote stale (§6).

---

## 6. Trigger semantics

### 6.1 Price series

| Basis | Use |
|-------|-----|
| **Best bid** (executable sell) | **Preferred** for both TP and SL when selling long — mirrors what a seller can hit |
| **Last trade** | Fallback when book missing |
| **Midpoint** | **Not** preferred for v1 (not executable) |

**Implementation note:** Reuse patterns from `resolve_book_top` / `cache.price(..., PriceType.LAST)` | `copy_strategy.py`, `nautilus_guru_exec.py`.

### 6.2 TP vs SL

- **Same trigger basis** in v1 (configurable single `trigger_price_source`: `book_bid` | `last`).
- **TP:** fire when `executable_bid >= tp_trigger_price` (long profit).
- **SL:** fire when `executable_bid <= sl_trigger_price`.

### 6.3 Stale / missing data

- If `VenueState.is_stale()` beyond `max_venue_staleness_seconds` (config): **hold** triggers; emit `virtual_exit_hold` (§12).
- If book unavailable and `last` missing: **hold**.

### 6.4 Debounce

- Minimum interval between trigger evaluations (timer) + **cooldown after a failed exit submit** before re-evaluating trigger (avoid hot loop).

### 6.5 Repeated trigger protection

- After `TRIGGERED_*`, ignore further price crosses until exit terminal or explicit reset.

---

## 7. Exit execution policy

### 7.1 Verified capability: **market SELL**

- **Available** in Nautilus Polymarket adapter: `OrderType.MARKET`, `quote_quantity=False`, base quantity | `execution.py` `_submit_market_order`.
- **Caveat:** maps to **FOK** at venue layer — may **fully fail** if liquidity insufficient; no resting remainder.

### 7.2 Recommended v1 policy

| Leg | Preferred | Fallback |
|-----|-----------|----------|
| **TP** | **Aggressive limit** (cross spread, GTC or IOC per adapter support) — operator-visible resting behavior matches guru; easier to debug | Market FOK if limit repeatedly fails and ops enable |
| **SL** | **Market FOK** (when config `exit_order_style.sl == market`) for urgency | Aggressive limit if market denied / policy off |

**Tyrex code change:** implement **`submit_virtual_exit`** in execution layer using either `order_factory.market(..., quote_quantity=False)` or `order_factory.limit` with price from book + aggression ticks (mirror guru C3 book logic where safe).

### 7.3 Retry / cooldown

- On `OrderDenied` / `OrderRejected`: exponential backoff, max `exit_retry_max`, emit `virtual_exit_retry`.
- Refresh quote before retry.

### 7.4 Partial exit fill

- Update `entry_qty_filled` remaining; if > min lot size, re-arm TP/SL on **remaining** VWAP or **frozen** thresholds (product choice: default **recompute** from remaining cost basis).

### 7.5 Cancel/replace

- v1: **no** cancel/replace loop unless exit order is **resting** GTC and stale; prefer submit once + retry on failure.

### 7.6 Wallet inventory < protected qty

- **Clamp** sell qty to `min(remaining_lot_qty, tier_a_sellable_qty)` before `OrderIntent`.
- If Tier A sellable == 0: `DISARMED_EXTERNAL_FLAT` or hold + fact (config).

---

## 8. Risk-path integration

**Path:** `OrderIntent` → `ConfiguredRiskPolicy.evaluate` | `configured.py`.

| Gate | Virtual exit behavior |
|------|----------------------|
| **SELL inventory gate** | **Must pass** — `_sell_exit_inventory_gate`; use **clamped** qty vs `deployment_budget.filled_usd_for_token` |
| **Additive open-cap bypass** | Applies to eligible SELL like guru exits |
| **Kill switch / health** | Unchanged — virtual exits are still **orders** |
| **`intent_origin`** | **Required** in v1 for facts and optional policy branches |
| **Concurrent guru resting** | Virtual exits **must not** use `guru_cid=` tags or `TX`+26hex `ClientOrderId` pattern so `count_guru_resting_orders_open` | `state_readers.py` |

**Fail-closed:** never skip `_sell_exit_inventory_gate` or allow `order_deploy` above verified sellable inventory.

**Optional code change:** if market exits interact oddly with per-order max notional, clip qty — same as today for limits.

---

## 9. Tier A / Tier B reconciliation rules

| Question | Rule |
|----------|------|
| Sellable size | **min** of (lot remaining qty, Tier A long qty for instrument) when Tier A wired |
| Entry fill truth | **Tier B** `OrderFilled` drives `entry_qty_filled` / VWAP |
| Drift (manual sell) | Tier A smaller → **clamp** or disarm |
| Pending exit | Tier B `Cache.order` for `exit_client_order_id` |
| **Precedence** | **Tier A for max sell qty**; **Tier B for “did our exit submit fill”** |

Emit `virtual_exit_reconcile` when a correction is applied.

---

## 10. Restart and recovery plan

1. **Process start:** `build_guru_trading_node` constructs manager; **load** JSON store **before** trading (or lazy-load in manager `on_start` equivalent).
2. **Do not arm new virtual logic** until `StartupReadinessGate` / wallet sync conditions satisfied — mirror `CopyStrategy._startup_block_reason` | `copy_strategy.py` + `guru_compose.py` gate.
3. **After `wallet_sync_first_sync_complete` + venue cash ready:** for each persisted lot in `ARMED` / `EXIT_*`:
   - Resolve instrument in cache.
   - Compare Tier A position to `entry_qty_filled` remaining; clamp.
   - Reconcile `exit_client_order_id` with `Cache.order`; if missing, clear in-flight and allow retry policy.
4. **Crash mid-entry:** `PENDING_ENTRY` lots may be dropped or recovered if entry order id was persisted when submit succeeded — persist **on `execution_outcome` submit** path.
5. **Crash mid-exit:** same — exit coid persisted; on restart verify open vs closed.
6. **Stale abandoned:** lots older than **N** days or with `instrument_id` not in cache → `FAILED` or prune (config).

---

## 11. Config design (typed YAML v1)

**Proposed new block** (runtime or strategy — **recommend `RuntimeSettings`** for path + intervals; **strategy** for pct if per-guru-follow):

```yaml
virtual_exit:
  enabled: false
  state_path: "var/virtual_exit_state.json"
  take_profit_pct: 10.0
  stop_loss_pct: 5.0
  trigger_price_source: "book_bid"   # book_bid | last
  max_venue_staleness_seconds: 45.0
  evaluate_interval_seconds: 1.0
  exit_order_style:
    take_profit: "aggressive_limit"   # aggressive_limit | market
    stop_loss: "market"               # market | aggressive_limit
  aggressive_limit_ticks: 2
  exit_retry_max: 5
  exit_retry_cooldown_seconds: 2.0
  drift_policy: "clamp_to_venue"      # clamp_to_venue | disarm
  adopt_existing_positions: false     # default false — v1 conservative
```

**Not configurable in v1 (explicit):**

- Short selling / negative lots
- Per-instrument overrides (unless trivial extension)
- Dynamic pct by volatility
- OCO on venue (impossible)

**Loader:** extend `RuntimeSettings` + parsing in `src/tyrex_pm/config/loaders.py` | pattern in same file for other nested blocks.

---

## 12. Reporting / observability plan

| Fact | When | Minimum payload |
|------|------|-------------------|
| `virtual_exit_arm` | Lot becomes `ARMED` | `lot_id`, `instrument_id`, `token_id`, `entry_client_order_id`, `entry_guru_correlation_id`, `entry_qty_filled`, `entry_vwap`, `tp_trigger_price`, `sl_trigger_price` |
| `virtual_exit_trigger` | Condition detected | `lot_id`, `kind` (`tp`\|`sl`), `executable_price`, `trigger_basis`, `ts_ms` |
| `virtual_exit_submit` | Before/after risk+submit | `lot_id`, `intent_origin`, `correlation_id`, `order_style`, `qty`, `price` (if limit) |
| `virtual_exit_retry` | After deny/reject | `lot_id`, `reason`, `attempt`, `next_backoff_s` |
| `virtual_exit_reconcile` | Tier A/B correction | `lot_id`, `before_qty`, `after_qty`, `reason` |
| `virtual_exit_disarm` | Terminal disarm | `lot_id`, `reason` |
| `virtual_exit_recovery` | Restart action | `lot_id`, `action`, `detail` |
| `virtual_exit_hold` | Stale data | `lot_id`, `reason` |

**Join keys:** `run_id` (existing), `lot_id`, `entry_guru_correlation_id`, `client_order_id`, `instrument_id`.

---

## 13. File-by-file patch plan

| File | Action | Responsibility / scope |
|------|--------|-------------------------|
| `src/tyrex_pm/runtime/virtual_exit/__init__.py` | **Add** | Package export |
| `src/tyrex_pm/runtime/virtual_exit/manager.py` | **Add** | Core FSM, triggers, calls risk+exec (~400–700 LOC target) |
| `src/tyrex_pm/runtime/virtual_exit/store.py` | **Add** | JSON load/save, atomic write (~150–250 LOC) |
| `src/tyrex_pm/core/types.py` | **Modify** | `OrderIntent` optional `intent_origin`, `virtual_lot_id` (~10–20 LOC) |
| `src/tyrex_pm/config/loaders.py` | **Modify** | Dataclass + YAML parse for `virtual_exit` (~80–150 LOC) |
| `config/runtime/live_polymarket.yaml` (and shadow base) | **Modify** | Commented defaults / disabled block (~30 LOC) |
| `src/tyrex_pm/runtime/guru_compose.py` | **Modify** | Wire manager, inject into strategy (~40–80 LOC) |
| `src/tyrex_pm/strategy/copy_strategy.py` | **Modify** | `set_virtual_exit_manager`, forward `on_order_event`, optional `on_start` timer registration (~30–60 LOC) |
| `src/tyrex_pm/execution/nautilus_guru_exec.py` | **Modify** | `submit_virtual_exit_intent` — market vs limit, **non-guru** coid + tags (~80–150 LOC) |
| `src/tyrex_pm/risk/configured.py` | **Modify** (optional) | Only if `intent_origin` must bypass a gate; prefer **tags/coid** for guru concurrent first (~0–40 LOC) |
| `src/tyrex_pm/reporting/` | **Add** helper optional | `virtual_exit_facts.py` emit helpers |
| `Docs/Implementation/virtual_tp_sl/PROPOSAL.md` | **Optional** | Cross-link this plan |
| `Docs/OPERATIONS.md` | **Later** | Operator section after implementation |

**API changes:**

- `CopyStrategy.set_virtual_exit_manager(manager | None)`
- `NautilusGuruExecutionPort.submit_virtual_exit_intent(...)` (name TBD)
- New runtime YAML keys under `virtual_exit:`

---

## 14. Ordered implementation phases

| Phase | Goal | Files | Acceptance criteria |
|-------|------|-------|---------------------|
| **1** | Types + config | `types.py`, `loaders.py`, sample YAML | Loader round-trip tests; `virtual_exit.enabled` gates all behavior default off |
| **2** | Store + model | `store.py`, unit tests | Crash-safe write; schema_version migration stub |
| **3** | Manager core (no live submit) | `manager.py`, tests | FSM transitions on synthetic events |
| **4** | Execution integration | `nautilus_guru_exec.py`, shadow `NoOpExecutionPort` | Shadow records virtual SELL intent with correct market/limit |
| **5** | Compose + strategy hook | `guru_compose.py`, `copy_strategy.py` | One end-to-end shadow run with fake fills |
| **6** | Reporting | reporting helper | Facts appear in `facts.jsonl` when enabled |
| **7** | Restart recovery | manager + store | Unit test: reload + reconcile |
| **8** | Tier A drift | manager | Clamp tests with mocked `VenueState` |
| **9** | Tests full | `tests/` | See §15 |
| **10** | Operator docs | `OPERATIONS.md` | Runbook for enable/disable and state file |

---

## 15. Test plan

| Category | Cases |
|----------|--------|
| **Unit** | FSM transitions; VWAP math; trigger edge at exact threshold; idempotent trigger |
| **Unit** | Store atomic write; corrupt JSON handling |
| **Unit** | `intent_origin` ensures order is not counted as guru resting |
| **Integration / shadow** | Guru BUY → synthetic fill → arm → trigger → `NoOpExecutionPort` SELL recorded |
| **Restart** | Persist armed lot; reload; clamp qty |
| **External drift** | Tier A qty drop mid-armed |
| **Partial fills** | Entry partial then arm; exit partial then complete |
| **Stale data** | `is_stale` → hold, no submit |
| **Duplicate trigger** | Debounce / single exit in-flight |
| **Market vs limit** | Mock adapter or stub port: SELL market uses base qty, `quote_quantity=False` |

**Mandatory before production-safe:**

- SELL inventory gate cannot be bypassed (regression test).
- Concurrent guru cap: virtual exit limit **does not** increase `count_guru_resting_orders_open` when guru at cap.
- Restart recovery does not double-submit exit (in-flight reconciliation).

---

## 16. Risks / open decisions

| Item | Severity |
|------|-----------|
| FOK market SELL fails often on illiquid tokens — need aggressive limit fallback | **should decide before coding** (config default) |
| Whether TP should ever use market FOK | **can defer to v2** |
| `adopt_existing_positions: true` semantics (VWAP unknown) | **can defer to v2** |
| Exact debounce / evaluate interval | **can defer** (tune live) |
| Proof of full `OrderEvent` ordering vs timer callbacks | **should decide before coding** (integration test) |

**Blockers:** none identified in repo audit — **market SELL is supported** in pinned adapter; Tyrex must implement the call path.

---

## 17. Final recommendation

- **Path:** **Option A** — `VirtualExitManager` under `src/tyrex_pm/runtime/virtual_exit/`, **thin** `CopyStrategy.on_order_event` delegation, **extend** `NautilusGuruExecutionPort` for virtual exit order construction (market + aggressive limit).
- **v1 scope:** long-only; fixed TP/SL %; arm on entry fills; Tier A clamp; persistence file; facts; **SL default market** + **TP default aggressive limit** (both behind `exit_order_style`); **no** `adopt_existing_positions` unless explicitly enabled later.
- **Not in v1:** guru strategy changes; Layer A rules; risk policy rewrite; native OCO; short positions; multi-strategy shared manager.

---

*End of implementation plan.*
