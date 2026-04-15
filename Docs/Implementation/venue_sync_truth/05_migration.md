# 05 — Migration Steps

Each step is independently reviewable. No step leaves the system in a dual-truth state.

## Step 1: Add `resolve_and_activate_by_condition_and_token` to `GuruInstrumentDynamicController`

**What:** Add a convenience method that resolves an instrument when both `condition_id` and `token_id` are known (no Gamma HTTP needed). Wraps existing `resolve_binary_option_for_condition_and_token` + `force_add_instrument`.

**File:** `src/tyrex_pm/runtime/guru_instrument_dynamic.py`

**Scope:** ~15 lines. Pure addition, no existing behavior changes.

**Definition of done:** New method exists, existing tests still pass, new unit test covers the method.

**Evidence the seam exists:** `resolve_binary_option_for_condition_and_token` already does this (`guru_instrument_dynamic.py:162–172`), and `force_add_instrument` is already used by wallet warmup (`guru_instrument_dynamic.py:415`). The convenience method just combines them with proper error handling.

---

## Step 2: Add `WalletSyncConfig`, `WalletSyncResult`, and `UnresolvableEntry` dataclasses

**What:** Define the config, result, and per-instrument failure tracking types.

**File:** `src/tyrex_pm/runtime/wallet_sync.py` (new file).

**Scope:** Three frozen dataclasses, no dependencies beyond stdlib + `RuntimeSettings`.

**Definition of done:** Types importable, frozen, slotted. `WalletSyncConfig` includes `startup_deadline_seconds`, `per_instrument_max_retries`, and `shutdown_cycle_drain_seconds`. `WalletSyncResult` includes `unresolvable_retrying`, `unresolvable_terminal`, `http_positions_ok`, `http_orders_ok`, `first_sync_complete`.

---

## Step 3: Implement `WalletSyncActor`

**What:** The core Actor implementation. Depends on Step 1 (for the controller method) and Step 2 (for config/result types).

**File:** `src/tyrex_pm/runtime/wallet_sync.py`

**Key implementation details:**

- `__init__`: stores config, clob client, dynamic controller. Initializes `_known_condition_ids: set[str] = set()`, `_first_sync_complete: bool = False`, `_unresolvable_condition_ids: dict[str, UnresolvableEntry] = {}`, `_sync_count: int = 0`, `_start_mono: float = 0.0`, `_last_successful_cycle_utc: datetime | None = None`, `_consecutive_failure_count: int = 0`.
- `on_start`: records `_start_mono = time.monotonic()`, calls `self.clock.set_timer("wallet_sync", interval_ns=…, callback=self.on_timer)`. Immediately dispatches `self.run_in_executor(self._sync_cycle)` for the first sync. Note: `on_start` is synchronous (`cpdef void on_start`, `actor.pxd:93`); async work is dispatched via `run_in_executor` (`actor.pxd:143`), not `create_task` (which is only on `LiveExecutionClient`, not `Actor`).
- `on_stop`: calls `self.clock.cancel_timer("wallet_sync")`, then `self.cancel_all_tasks()` (`actor.pxd:150`). See `04_lifecycle.md` "Shutdown and mid-cycle interruption" for rationale.
- `on_timer`: calls `self.run_in_executor(self._sync_cycle)`.
- `_sync_cycle`: synchronous method (runs in executor thread). See `02_components.md` for the full 12-step pseudocode.
- `_fetch_wallet_positions`: reuses `fetch_wallet_position_rows` from `guru_cache_warmup.py` with `_follower_positions_api_user` for the address.
- `_fetch_wallet_orders`: calls `self._clob.get_orders()` (py-clob API, returns open orders for the authenticated wallet — wallet-scoped, not cache-scoped).

**Error handling (three-state model):**

- **Transient HTTP failure:** If either `_fetch_wallet_positions` or `_fetch_wallet_orders` raises, the error is caught and logged. If **both** calls fail, `_first_sync_complete` stays False, `_consecutive_failure_count` increments, and the timer retries on its normal cadence. If only one fails, the successful source is still processed (partial data is better than none). `_first_sync_complete` can only become True when at least one HTTP source returned successfully AND completeness is satisfied.
- **Per-instrument resolution failure:** When a specific `condition_id` fails to resolve (e.g. archived market, Gamma/CLOB error), it is tracked in `_unresolvable_condition_ids` with a retry count. Each subsequent cycle that still sees the condition_id on the wallet increments the retry count. After `per_instrument_max_retries` (default 3) cycles, the condition_id is marked **terminally unresolvable** and excluded from the completeness check. A `wallet_sync_unresolvable` fact is emitted. The health source degrades to `DEGRADED_OMS` (see `02_components.md`).
- **Startup deadline:** If `_first_sync_complete` is still False after `startup_deadline_seconds` (default 120s) since `on_start`, the actor emits a `wallet_sync_startup_timeout` fact. The readiness gate surfaces this as `"startup_wallet_sync_timeout"` (distinct from `"startup_wallet_sync_pending"`), enabling operator alerting on startup hangs.

**Scope:** ~200–250 lines.

**Definition of done:**
1. Actor is instantiable; first sync cycle with both HTTP calls succeeding and all instruments resolvable sets `first_sync_complete == True`.
2. First cycle with induced Data API failure leaves `first_sync_complete == False` and the readiness gate reports NOT_READY with reason `"startup_wallet_sync_pending"`.
3. After `startup_deadline_seconds` elapses with `first_sync_complete` still False, the gate reports `"startup_wallet_sync_timeout"`.
4. Resolution failure on a condition_id across 3 cycles marks it terminal; subsequent cycle with remaining instruments resolved sets `first_sync_complete == True`.
5. Unit tests cover happy path, HTTP failure (one source, both sources), per-instrument retry exhaustion, and startup deadline.

---

## Step 4: Add wallet sync config keys to `RuntimeSettings`

**What:** Add new fields to the config loader with defaults and validation.

**File:** `src/tyrex_pm/config/loaders.py`

**New fields:**
- `wallet_sync_enabled: bool` — default `True` for live, `False` for shadow.
- `wallet_sync_poll_interval_seconds: float` — default `15.0`, floor `5.0`.
- `wallet_sync_startup_deadline_seconds: float` — default `120.0`, floor `30.0`.
- `wallet_sync_per_instrument_max_retries: int` — default `3`, floor `1`.
- `wallet_sync_shutdown_cycle_drain_seconds: float` — default `5.0`.

**Validation:**
- `wallet_sync_enabled` requires `execution_mode == "live"`.
- `poll_interval >= 5.0`.
- `startup_deadline >= 30.0`.
- `per_instrument_max_retries >= 1`.

**Definition of done:** Config loads correctly with and without the new keys. Existing configs continue to work (defaults apply).

---

## Step 5: Wire `WalletSyncActor` into `build_guru_trading_node`

**What:** Integrate the actor into the compose root.

**File:** `src/tyrex_pm/runtime/guru_compose.py`

**Changes:**
1. When `live and runtime.wallet_sync_enabled`:
   a. Always construct `clob_dynamic` and `dynamic_ctrl` (remove the `need_dynamic or want_wallet_warm` conditional — `guru_compose.py:529`). The controller is now needed regardless.
   b. Construct `WalletSyncConfig` from runtime settings.
   c. Construct `WalletSyncActor(config, clob_dynamic, dynamic_ctrl, fact_emit=emit)`.
   d. Call `node.trader.add_actor(wallet_sync_actor)`.
2. Add `wallet_sync: WalletSyncActor | None` to `GuruTradingAssembly`.
3. Pass to `StartupReadinessGate`: `wallet_sync_ready=lambda: wallet_sync_actor.first_sync_complete` and `wallet_sync_deadline_exceeded=lambda: wallet_sync_actor.startup_deadline_exceeded`.
   Pass to `NautilusLiveExecutionHealthSource`: `wallet_sync_status=wallet_sync_actor` (the actor satisfies `WalletSyncHealthAdapter` protocol).
4. When `wallet_sync_enabled` and `runtime.live_exec_open_check_open_only is None`: pass `open_check_open_only=False` to the engine config.
5. When `wallet_sync_enabled` and not explicitly overridden: set `use_data_api=True` on `PolymarketExecClientConfig`.

**Definition of done:** With `wallet_sync_enabled: true` in YAML, the actor is registered on the node. Assembly exposes it. Gate uses it. Existing shadow-mode compose path unaffected.

---

## Step 6: Enhance `StartupReadinessGate` with wallet sync clause

**What:** Add wallet sync readiness check with two distinct reason codes.

**File:** `src/tyrex_pm/runtime/lifecycle/gate.py`

**Changes:**
1. Constructor gains two optional callables:
   - `wallet_sync_ready: Callable[[], bool] | None = None` — returns `first_sync_complete`.
   - `wallet_sync_deadline_exceeded: Callable[[], bool] | None = None` — returns `startup_deadline_exceeded`.
2. In `evaluate()`, after the exec_connected check (`gate.py:64`) and before the capital gate check (`gate.py:72`), add:
   ```python
   if self._wallet_sync_ready is not None and not self._wallet_sync_ready():
       if (
           self._wallet_sync_deadline_exceeded is not None
           and self._wallet_sync_deadline_exceeded()
       ):
           reasons.append("startup_wallet_sync_timeout")
       else:
           reasons.append("startup_wallet_sync_pending")
       return StartupReadinessResult(
           status=LifecycleReadiness.NOT_READY,
           reasons=tuple(reasons),
           evaluated_at_utc=now,
       )
   ```

**Definition of done:**
1. Gate returns NOT_READY with `"startup_wallet_sync_pending"` when wallet sync has not completed first cycle and deadline has not elapsed.
2. Gate returns NOT_READY with `"startup_wallet_sync_timeout"` when wallet sync has not completed first cycle and deadline **has** elapsed.
3. Returns READY when `first_sync_complete` is True (and all other clauses pass).

---

## Step 7: Enhance `NautilusLiveExecutionHealthSource` with wallet sync awareness

**What:** Replace weak startup-reconciliation-only signal with one that reports both startup and steady-state wallet sync health.

**File:** `src/tyrex_pm/runtime/tradable_state/nautilus_live_health.py`

**Changes:**
1. Constructor gains `wallet_sync_status: WalletSyncHealthAdapter | None = None` (protocol defined in `02_components.md`).
2. `__slots__` expanded from `("_exec_engine",)` to `("_exec_engine", "_wallet_sync_status")`.
3. In `snapshot()`, after the existing `_startup_reconciliation_event.is_set()` check succeeds (`nautilus_live_health.py:75`), apply the rule set from `02_components.md`:
   - Rule 2: `first_sync_complete` False, deadline not exceeded → `UNKNOWN_BOOTSTRAP` / `"wallet_sync_pending"`.
   - Rule 3: `first_sync_complete` False, deadline exceeded → `DEGRADED_OMS` / `"wallet_sync_startup_timeout"`.
   - Rule 4: `terminally_unresolvable_count > 0` → `DEGRADED_OMS` / `"wallet_sync_unresolvable_instruments"`.
   - Rule 5: `last_successful_cycle_utc` stale (age > `2 × poll_interval_seconds`) or `consecutive_failure_count >= 3` → `DEGRADED_OMS` / `"wallet_sync_stale"`.
   - Rule 6: both 4 and 5 apply → `DEGRADED_OMS` / `"wallet_sync_stale"`, `framework_detail` includes both conditions.
   - Rule 7: otherwise → `HEALTHY` (existing behavior).

**Definition of done:**
1. Health source reports `UNKNOWN_BOOTSTRAP` with reason `"wallet_sync_pending"` when startup reconciliation is done but wallet sync first cycle has not completed.
2. Reports `DEGRADED_OMS` with reason `"wallet_sync_startup_timeout"` when startup deadline has elapsed without completion.
3. Reports `DEGRADED_OMS` with reason `"wallet_sync_stale"` when the last successful cycle is older than `2 × poll_interval_seconds` or 3+ consecutive failures have occurred.
4. Reports `DEGRADED_OMS` with reason `"wallet_sync_unresolvable_instruments"` when there are terminally unresolvable condition_ids.
5. Reports `HEALTHY` when all conditions are satisfied.
6. Reports existing behavior unchanged when `wallet_sync_status` is None.

---

## Step 8: Integration test — full startup with pre-existing wallet state

**What:** End-to-end test that compose, startup, wallet sync, and readiness gate all interact correctly.

**File:** `tests/unit/test_wallet_sync_startup.py`

**Scope:** Mock py-clob and Data API responses. Verify:
1. WalletSyncActor discovers instruments not seeded by warmup.
2. Instruments are added to Cache.
3. Readiness gate blocks until first sync complete.
4. After first sync, gate reports READY (assuming other clauses pass).
5. Deployment budget includes newly discovered positions.

**Definition of done:** Test passes. No dual-truth, no fallback paths.

---

## Step 9: Observability — wallet_sync fact emission

**What:** Emit structured facts on each sync cycle for operational visibility.

**File:** `src/tyrex_pm/runtime/wallet_sync.py` (inside `_sync_cycle`).

**Fact schema:**
```python
{
    "cycle": int,
    "positions_fetched": int,
    "orders_fetched": int,
    "condition_ids_wallet": int,
    "condition_ids_cache": int,
    "newly_added": int,
    "resolution_failures": int,
    "unresolvable_retrying": int,
    "unresolvable_terminal": int,
    "http_positions_ok": bool,
    "http_orders_ok": bool,
    "first_sync_complete": bool,
    "elapsed_ms": float,
    "failure_details": dict[str, int],  # detail_code → count
}
```

**Definition of done:** Facts emitted, parseable, consistent with existing reporting taxonomy.

---

## Step 10: Documentation and YAML template updates

**What:** Update `Docs/CONFIG_MODEL.md`, scenario YAML templates, and operational docs.

**Definition of done:** Docs reflect new config keys, updated defaults, and operational guidance.

---

## Migration order summary

```
Step  Depends on  Description
1     —           Controller convenience method
2     —           WalletSyncConfig + WalletSyncResult + UnresolvableEntry types
3     1, 2        WalletSyncActor implementation (three-state sync model)
4     —           Config loaders (5 new keys)
5     3, 4        Compose wiring
6     5           Readiness gate enhancement (pending + timeout reasons)
7     5           Health source enhancement (startup + steady-state rules)
8     5, 6, 7     Integration test
9     3           Observability
10    all         Documentation
```

Steps 1, 2, and 4 can be done in parallel. Step 3 depends on 1 and 2. Steps 5–7 depend on 3 and 4. Step 8 depends on 5–7. Steps 9 and 10 can proceed alongside 5–8.
