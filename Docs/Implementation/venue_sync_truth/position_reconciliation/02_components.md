# Position Reconciliation — Component Changes

## Changes to `WalletSyncActor` (`runtime/wallet_sync.py`)

### New state fields

```python
# In __init__:
self._recently_reconciled: dict[InstrumentId, float] = {}
# Maps instrument → monotonic timestamp of last successful reconciliation.
# Used to debounce rapid re-reconciliation and defend against Race E.

self._deferred_reconciliations: dict[InstrumentId, int] = {}
# Maps instrument → number of consecutive cycles the reconciliation was deferred
# (due to in-flight orders matching the delta). Capped by config.

self._reconciliation_count: int = 0
# Total reconciliation reports sent across all cycles.

self._event_loop: asyncio.AbstractEventLoop | None = None
# Captured in on_start() via asyncio.get_running_loop(). Used by the executor
# thread to schedule _apply_reconciliation_actions on the event-loop thread
# via call_soon_threadsafe. See OQ-1.
```

### New config fields (`WalletSyncConfig`)

```python
@dataclass(frozen=True, slots=True)
class WalletSyncConfig:
    # ... existing fields ...
    position_reconciliation_enabled: bool = False
    data_api_lag_tolerance_seconds: float = 60.0
    position_reconciliation_deferral_max: int = 5
    recently_reconciled_ttl_seconds: float = 60.0
    reconcile_venue_has_more: bool = False
    position_reconciliation_shadow_mode: bool = True
```

See `04_config.md` for full descriptions.

### New method: `_reconciliation_pass`

Runs **after** the discovery pass in `_sync_cycle`, inside the executor thread.
Builds the venue-truth map and cache-position map, computes diffs, applies race
defenses, and collects `PositionStatusReport` objects to inject.

Returns a list of reports rather than sending them directly — the actual
`msgbus.send` calls must happen on the event-loop thread (see §Lifecycle).

```python
def _reconciliation_pass(
    self,
    position_rows: list[dict[str, Any]],
) -> list[ReconciliationAction]:
    """Build diffs and return actions; does NOT send to msgbus (thread safety)."""
```

### New dataclass: `ReconciliationAction`

```python
@dataclass(frozen=True, slots=True)
class ReconciliationAction:
    instrument_id: InstrumentId
    venue_qty: Decimal
    cache_qty: Decimal
    diff_direction: str    # "close" | "partial_reduce"
    deferred: bool
    defer_count: int
    report: PositionStatusReport | None  # None when deferred
```

### Modified method: `_sync_cycle`

After the existing discovery pass, if `position_reconciliation_enabled`:

1. Call `_reconciliation_pass(positions)` to compute diffs.
2. Return the actions as part of `WalletSyncResult` (new field).

### New method: `_apply_reconciliation_actions`

Called on the event-loop thread via `call_soon_threadsafe` from `_sync_cycle_wrapper`.
For each non-deferred action:

```python
def _apply_reconciliation_actions(self, actions: list[ReconciliationAction]) -> None:
    for action in actions:
        if action.report is None:
            continue
        if self._wsconfig.position_reconciliation_shadow_mode:
            # Shadow mode: emit fact with reconciliation_sent=False, skip msgbus
            self._emit_reconciliation_fact(action, self._cycle_count, sent=False)
            continue
        self.msgbus.send(
            "ExecEngine.reconcile_execution_report",
            action.report,
        )
        self._recently_reconciled[action.instrument_id] = time.monotonic()
        self._reconciliation_count += 1
        self._deferred_reconciliations.pop(action.instrument_id, None)
```

### Modified method: `on_start`

Captures the event loop reference for cross-thread dispatch:

```python
def on_start(self) -> None:
    self._event_loop = asyncio.get_running_loop()
    # ... existing timer setup ...
```

`asyncio.get_running_loop()` is correct here: `on_start()` executes during
`kernel.start_async()` → `trader.start()` → `actor.start()` while the asyncio
event loop is running in the current thread (`system/kernel.py:1036`). See OQ-1
for the full call-chain verification.

### Modified method: `on_timer`

Guards against concurrent cycles:

```python
def on_timer(self, event: Event) -> None:
    if self._cycle_in_progress:
        return
    self._cycle_in_progress = True
    self.run_in_executor(self._sync_cycle_wrapper)
```

### New method: `_sync_cycle_wrapper`

Wraps `_sync_cycle` to handle cross-thread dispatch and error safety:

```python
def _sync_cycle_wrapper(self) -> None:
    try:
        result = self._sync_cycle()
        if result.reconciliation_actions:
            self._event_loop.call_soon_threadsafe(
                self._apply_reconciliation_actions,
                result.reconciliation_actions,
            )
    except Exception:
        _LOG.exception("event=wallet_sync_cycle_error component=wallet_sync")
    finally:
        self._cycle_in_progress = False
```

`call_soon_threadsafe` schedules `_apply_reconciliation_actions` on the event-loop
thread for the next loop iteration — no queue, no latency penalty. See OQ-1 for
rationale and rejected alternatives.

## Changes to `WalletSyncResult`

### New fields

```python
@dataclass(frozen=True, slots=True)
class WalletSyncResult:
    # ... existing fields ...
    reconciliation_actions: list[ReconciliationAction] = field(default_factory=list)
    reconciliation_sent_count: int = 0
    reconciliation_deferred_count: int = 0
    reconciliation_skipped_recently_reconciled: int = 0
```

## Changes to `WalletSyncHealthAdapter` protocol

### New property

```python
@property
def stuck_deferral_count(self) -> int: ...
```

Returns the number of instruments whose deferral counter has reached
`position_reconciliation_deferral_max`. Used by the health source for
a new degradation rule.

## Changes to `NautilusLiveExecutionHealthSource` (`tradable_state/nautilus_live_health.py`)

### New rule (inserted between existing rules 5 and 7)

**Rule 6.5: Stuck reconciliation deferrals**

```python
# After checking stale/unresolvable (existing rules 4-6):
if ws.stuck_deferral_count > 0:
    return TradableStateHealthSnapshot(
        level=TradableStateHealth.DEGRADED_OMS,
        reason_code="position_reconciliation_stuck",
        observed_at_utc=now,
        framework_detail=(
            f"{ws.stuck_deferral_count} position reconciliation(s) "
            "deferred past maximum; cache may be stale"
        ),
    )
```

Priority: stale/unresolvable wins over stuck-deferral when both apply
(stale implies the sync cycle itself is failing, which subsumes deferral concerns).

## Changes to fact schema (`reporting/schema/facts_v1.py`)

### New fact type: `position_reconciliation`

```python
"position_reconciliation": frozenset({
    "cycle",
    "instrument_id",
    "venue_qty",
    "cache_qty",
    "diff_direction",      # "close" | "partial_reduce" | "deferred" | "skipped_ttl"
    "deferred",
    "defer_count",
    "reconciliation_sent",  # True if report was sent to engine
}),
```

Emitted per-instrument per-cycle when a diff is detected (including deferrals and
TTL skips). This provides full audit trail for every reconciliation decision.

## Components that do NOT change

The following components are explicitly verified to require **no** changes:

- **`NautilusDeploymentBudget`** (`runtime/deployment_budget.py`): Reads
  `Cache.positions_open()` only. Reconciliation updates the cache through the
  engine's normal event path; the budget sees the updated positions automatically.
  No code changes needed.

- **`NautilusExecutionStateReader`** (`runtime/state_readers.py`): Reads
  `Cache.orders_open()` and `Cache.order()`. Reconciliation adds EXTERNAL orders
  to cache via the engine, but the reader doesn't filter by strategy_id. No changes.

- **`ConfiguredRiskPolicy`** (`risk/configured.py`): Reads deployment budget
  totals. Budget totals change because cache changes. No risk policy code changes.

- **`GuruInstrumentDynamicController`** (`runtime/guru_instrument_dynamic.py`):
  Handles instrument discovery only. Position reconciliation is orthogonal.

- **`StartupReadinessGate`**: Existing wallet-sync readiness rules are sufficient.
  Position reconciliation is not a startup gate — it runs continuously.

- **`PolymarketExecutionClient`**: Not modified. The engine's built-in reconciliation
  may query it for fill reports as part of its own pipeline; that's the engine's
  concern, not the actor's.
