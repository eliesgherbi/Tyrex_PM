# 05 — Migration Steps

Each step is independently reviewable. No step leaves the system in a dual-truth state.

## Step 1: Add `resolve_and_activate_by_condition_and_token` to `GuruInstrumentDynamicController`

**What:** Add a convenience method that resolves an instrument when both `condition_id` and `token_id` are known (no Gamma HTTP needed). Wraps existing `resolve_binary_option_for_condition_and_token` + `force_add_instrument`.

**File:** `src/tyrex_pm/runtime/guru_instrument_dynamic.py`

**Scope:** ~15 lines. Pure addition, no existing behavior changes.

**Definition of done:** New method exists, existing tests still pass, new unit test covers the method.

**Evidence the seam exists:** `resolve_binary_option_for_condition_and_token` already does this (`guru_instrument_dynamic.py:162–172`), and `force_add_instrument` is already used by wallet warmup (`guru_instrument_dynamic.py:415`). The convenience method just combines them with proper error handling.

---

## Step 2: Add `WalletSyncConfig` and `WalletSyncResult` dataclasses

**What:** Define the config and result types.

**File:** `src/tyrex_pm/runtime/wallet_sync.py` (new file).

**Scope:** Two frozen dataclasses, no dependencies beyond stdlib + `RuntimeSettings`.

**Definition of done:** Types importable, frozen, slotted.

---

## Step 3: Implement `WalletSyncActor`

**What:** The core Actor implementation. Depends on Step 1 (for the controller method) and Step 2 (for config/result types).

**File:** `src/tyrex_pm/runtime/wallet_sync.py`

**Key implementation details:**

- `__init__`: stores config, clob client, dynamic controller, initializes `_known_condition_ids: set[str] = set()`, `_first_sync_complete: bool = False`, `_sync_count: int = 0`.
- `on_start`: calls `self.clock.set_timer("wallet_sync", interval_ns=…, callback=self.on_timer)`. Then immediately dispatches `self.create_task(self._sync_cycle())` for the first sync.
- `on_stop`: calls `self.clock.cancel_timer("wallet_sync")`.
- `on_timer`: calls `self.create_task(self._sync_cycle())`.
- `_sync_cycle`: async method.
  1. Refresh `_known_condition_ids` from `self.cache.instruments(venue=POLYMARKET)`.
  2. Call `asyncio.to_thread(self._fetch_wallet_positions)` to get Data API positions.
  3. Call `asyncio.to_thread(self._fetch_wallet_orders)` to get py-clob open orders.
  4. Extract `{condition_id → [token_id]}` from both.
  5. For each condition_id not in `_known_condition_ids`: resolve each token_id via `self._dynamic_ctrl.resolve_and_activate_by_condition_and_token(condition_id, token_id)`.
  6. Update `_known_condition_ids`.
  7. Set `_first_sync_complete = True`.
  8. Increment `_sync_count`.
  9. Emit wallet_sync fact if `_fact_emit` is set.
  10. Return `WalletSyncResult`.
- `_fetch_wallet_positions`: reuses `fetch_wallet_position_rows` from `guru_cache_warmup.py` with `_follower_positions_api_user` for the address.
- `_fetch_wallet_orders`: calls `self._clob.get_orders()` (py-clob API, returns open orders for the authenticated wallet).

**Error handling:** HTTP failures in steps 2–3 are caught and logged; the cycle completes with `resolution_failures` counted. `_first_sync_complete` is set to `True` even if some instruments fail to resolve (to avoid blocking startup indefinitely). The failed instruments will be retried on the next cycle.

**Scope:** ~150–200 lines.

**Definition of done:** Actor is instantiable, first sync cycle completes, instruments added to a mock cache match expected set. Unit tests cover happy path + HTTP failure + already-cached instruments.

---

## Step 4: Add `wallet_sync_enabled` and `wallet_sync_poll_interval_seconds` to `RuntimeSettings`

**What:** Add two new fields to the config loader with defaults and validation.

**File:** `src/tyrex_pm/config/loaders.py`

**Changes:**
- Add fields to `RuntimeSettings` dataclass.
- Add validation: `wallet_sync_enabled` requires `live`; `poll_interval >= 5.0`.
- Default: `wallet_sync_enabled=True` when `execution_mode=live` (explicitly in the live YAML template).

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
3. Pass `wallet_sync_ready=lambda: wallet_sync_actor.first_sync_complete` to `StartupReadinessGate` and `NautilusLiveExecutionHealthSource` constructors (when wired).
4. When `wallet_sync_enabled` and `runtime.live_exec_open_check_open_only is None`: pass `open_check_open_only=False` to the engine config.
5. When `wallet_sync_enabled` and not explicitly overridden: set `use_data_api=True` on `PolymarketExecClientConfig`.

**Definition of done:** With `wallet_sync_enabled: true` in YAML, the actor is registered on the node. Assembly exposes it. Gate uses it. Existing shadow-mode compose path unaffected.

---

## Step 6: Enhance `StartupReadinessGate` with wallet sync clause

**What:** Add wallet sync readiness check.

**File:** `src/tyrex_pm/runtime/lifecycle/gate.py`

**Changes:**
1. Constructor gains `wallet_sync_ready: Callable[[], bool] | None = None`.
2. In `evaluate()`, after the exec_connected check (line 64) and before the capital gate check (line 72), add:
   ```python
   if self._wallet_sync_ready is not None and not self._wallet_sync_ready():
       reasons.append("startup_wallet_sync_pending")
       return StartupReadinessResult(
           status=LifecycleReadiness.NOT_READY,
           reasons=tuple(reasons),
           evaluated_at_utc=now,
       )
   ```

**Definition of done:** Gate returns NOT_READY with `"startup_wallet_sync_pending"` when wallet sync has not completed first cycle. Returns READY when it has (and all other clauses pass).

---

## Step 7: Enhance `NautilusLiveExecutionHealthSource` with wallet sync awareness

**What:** Replace weak startup-reconciliation-only signal with one that also waits for wallet sync.

**File:** `src/tyrex_pm/runtime/tradable_state/nautilus_live_health.py`

**Changes:**
1. Constructor gains `wallet_sync_ready: Callable[[], bool] | None = None`.
2. In `snapshot()`, after the existing `_startup_reconciliation_event.is_set()` check succeeds, add:
   ```python
   if self._wallet_sync_ready is not None and not self._wallet_sync_ready():
       return TradableStateHealthSnapshot(
           level=TradableStateHealth.UNKNOWN_BOOTSTRAP,
           reason_code="wallet_sync_pending",
           observed_at_utc=now,
           framework_detail="startup reconciliation complete; wallet sync pending",
       )
   ```
3. This makes the health signal genuinely meaningful: HEALTHY means "startup reconciliation ran AND all wallet instruments are loaded."

**Definition of done:** Health source reports UNKNOWN_BOOTSTRAP until both conditions met.

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
2     —           WalletSyncConfig + WalletSyncResult types
3     1, 2        WalletSyncActor implementation
4     —           Config loaders
5     3, 4        Compose wiring
6     5           Readiness gate enhancement
7     5           Health source enhancement
8     5, 6, 7     Integration test
9     3           Observability
10    all         Documentation
```

Steps 1, 2, and 4 can be done in parallel. Step 3 depends on 1 and 2. Steps 5–7 depend on 3 and 4. Step 8 depends on 5–7. Steps 9 and 10 can proceed alongside 5–8.
