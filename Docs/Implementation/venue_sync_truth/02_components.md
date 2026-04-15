# 02 — Components

## New components

### `WalletSyncActor`

**Location:** `src/tyrex_pm/runtime/wallet_sync.py`

**Base class:** `nautilus_trader.trading.strategy.Actor` (Nautilus Actor, registered on the node via `node.trader.add_actor`).

**Purpose:** Continuously discovers all markets the wallet has exposure on (positions or resting orders), ensures they are in `Cache`, and triggers targeted reconciliation for newly discovered instruments.

**Threading/async model:** Runs on the Nautilus event loop. Uses `self.clock.set_timer` for periodic polling. The sync cycle runs in an executor thread via `self.run_in_executor(self._sync_cycle)` (`actor.pxd:143`) — `_sync_cycle` is a synchronous method. This is the correct pattern for `Actor` (which does not have `create_task` — that's on `LiveExecutionClient` only). HTTP calls to py-clob / Data API / Gamma are blocking within the executor thread. Instrument resolution reuses `GuruInstrumentDynamicController` which is already thread-safe (uses `threading.Lock` in `CacheInstrumentActivator`, `guru_instrument_dynamic.py:249`).

```python
@dataclass(frozen=True, slots=True)
class UnresolvableEntry:
    """Tracks a condition_id that has failed resolution across multiple cycles."""
    condition_id: str
    token_ids: tuple[str, ...]
    last_detail: str
    retry_count: int
    terminal: bool  # True when retry_count >= per_instrument_max_retries


@dataclass(frozen=True, slots=True)
class WalletSyncConfig:
    poll_interval_seconds: float = 15.0
    startup_deadline_seconds: float = 120.0
    per_instrument_max_retries: int = 3
    shutdown_cycle_drain_seconds: float = 5.0
    data_api_base_url: str = "https://data-api.polymarket.com"
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    gamma_http_timeout_seconds: float = 15.0
    clob_host: str = "https://clob.polymarket.com"


class WalletSyncActor(Actor):
    """
    Continuously discovers all markets the wallet has exposure on and ensures
    they are in Cache.

    Manages three categories of sync state:
    - ``_first_sync_complete``: flipped to True only when both HTTP calls succeed
      and every wallet condition_id is either cached or terminally unresolvable.
    - ``_unresolvable_condition_ids``: tracks per-instrument resolution failures
      with retry counts. After ``per_instrument_max_retries`` cycles, a condition_id
      is marked terminal and excluded from the completeness check.
    - ``_start_mono``: monotonic timestamp from on_start, used to enforce
      ``startup_deadline_seconds``.

    The actor has no persistent state of its own and no cleanup obligation on restart.
    """

    __slots__ = (
        "_config",
        "_clob",
        "_dynamic_ctrl",
        "_known_condition_ids",
        "_first_sync_complete",
        "_unresolvable_condition_ids",
        "_start_mono",
        "_inflight_task_id",
        "_fact_emit",
        "_sync_count",
        "_instruments_discovered",
        "_last_successful_cycle_utc",
        "_consecutive_failure_count",
    )

    def __init__(
        self,
        config: WalletSyncConfig,
        clob_client: ClobClient,
        dynamic_controller: GuruInstrumentDynamicController,
        *,
        fact_emit: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None: ...

    def on_start(self) -> None:
        """
        Record ``_start_mono = time.monotonic()``, run first sync immediately
        via ``run_in_executor``, then schedule periodic timer.
        """

    def on_stop(self) -> None:
        """
        Cancel the timer, then cancel the in-flight sync task if one exists.

        ``on_stop`` is synchronous (``cpdef void on_stop``, ``actor.pxd:94``).
        The framework does not await it. We call ``self.cancel_all_tasks()``
        (``actor.pxd:150``) to request cancellation of any executor task
        dispatched via ``run_in_executor``. The HTTP call inside
        ``asyncio.to_thread`` is not itself cancellable, but the executor
        wrapper will not deliver its result after cancellation, and the
        node shutdown proceeds regardless. There is no transactional state
        to roll back — each ``Cache.add_instrument`` call is atomic per
        instrument (``guru_instrument_dynamic.py:266–268``).
        """

    def on_timer(self, event: TimeEvent) -> None:
        """Dispatch sync cycle via ``run_in_executor``."""

    def _sync_cycle(self) -> WalletSyncResult:
        """
        Core polling logic (runs in executor thread):

        1. Refresh ``_known_condition_ids`` from
           ``self.cache.instruments(venue=POLYMARKET)``.
        2. Fetch wallet positions (Data API /positions, same as warmup).
           If HTTP fails: log, set ``http_failed = True``, skip positions.
        3. Fetch wallet open orders (py-clob ``get_orders()``).
           If HTTP fails: log, set ``http_failed = True``, skip orders.
        4. If **both** HTTP calls failed: increment
           ``_consecutive_failure_count``, return early.
           ``_first_sync_complete`` stays False.
        5. Extract ``{condition_id → [token_id]}`` from fetched data.
        6. For each condition_id not in ``_known_condition_ids`` and not
           terminally unresolvable:
           a. Resolve each token_id via
              ``GuruInstrumentDynamicController.resolve_and_activate_by_condition_and_token``.
           b. On success: update ``_known_condition_ids``, increment counter.
           c. On failure: update ``_unresolvable_condition_ids`` — increment
              retry_count; if ``retry_count >= per_instrument_max_retries``,
              mark terminal, emit ``wallet_sync_unresolvable`` fact.
        7. Evaluate completeness: ``_first_sync_complete = True`` iff:
           - No HTTP call failed this cycle (at least one source returned), AND
           - Every condition_id on the wallet is either in Cache or
             terminally unresolvable.
        8. If ``_first_sync_complete`` became True or was already True:
           reset ``_consecutive_failure_count`` to 0, record
           ``_last_successful_cycle_utc = datetime.now(UTC)``.
        9. Increment ``_sync_count``.
        10. If ``_first_sync_complete`` is still False and
            ``time.monotonic() - _start_mono > startup_deadline_seconds``:
            emit ``wallet_sync_startup_timeout`` fact.
        11. Emit ``wallet_sync`` fact.
        12. Return ``WalletSyncResult``.
        """

    @property
    def first_sync_complete(self) -> bool:
        """
        True only when at least one sync cycle has completed with both HTTP
        calls succeeding and every wallet condition_id either cached or
        terminally unresolvable. Used by the readiness gate.
        """

    @property
    def startup_deadline_exceeded(self) -> bool:
        """
        True when ``first_sync_complete`` is False and
        ``time.monotonic() - _start_mono > startup_deadline_seconds``.
        Used by the readiness gate to distinguish timeout from pending.
        """

    @property
    def last_successful_cycle_utc(self) -> datetime | None:
        """UTC timestamp of the last cycle that completed with full success
        (both HTTP calls OK + completeness check passed). None before first
        successful cycle."""

    @property
    def consecutive_failure_count(self) -> int:
        """Number of consecutive cycles where both HTTP calls failed or
        completeness check did not pass after first_sync_complete was True."""

    @property
    def terminally_unresolvable_count(self) -> int:
        """Number of condition_ids that exhausted per_instrument_max_retries."""

    @property
    def sync_count(self) -> int:
        """Number of completed sync cycles."""

    @property
    def instruments_discovered(self) -> int:
        """Total instruments added to Cache by this actor."""
```

**Position reconciliation extension:** `WalletSyncActor` was extended with a position-reconciliation pass that runs after instrument discovery on each poll cycle. It compares venue-truth positions against `Cache.positions_open()` and emits synthetic `PositionStatusReport` events to bring the cache into line with venue reality when external closes or reductions are detected. See `docs/implementation/venue_sync_truth/position_reconciliation/` for the full plan, including diff algorithm, race defenses, shadow-mode rollout, and config surface.

**Invariants:**
- Never submits orders directly — position reconciliation operates by sending `PositionStatusReport` to the engine via `msgbus.send`, which triggers Nautilus's own netting reconciliation pipeline.
- Uses `CacheInstrumentActivator.force_add_instrument` (bypasses `polymarket_dynamic_max_activations` cap), same as wallet warmup (`guru_instrument_dynamic.py:271–281`).
- `_known_condition_ids` is maintained as a `set[str]` to avoid re-resolving instruments already in cache. Refreshed from `Cache.instruments(venue=POLYMARKET)` at the start of each cycle.
- `first_sync_complete` gates startup readiness (see `04_lifecycle.md`). Only flips to True on a fully successful cycle — HTTP failures keep it False and the readiness gate blocks until success or timeout.
- `_unresolvable_condition_ids` tracks per-instrument failures with bounded retries. Terminal entries are excluded from the completeness check so a single archived/delisted market does not block startup indefinitely.
- The actor has **no persistent state** of its own. On restart, compose-time warmup reseeds from wallet positions and the actor's first cycle re-diffs from scratch.

### `WalletSyncResult`

**Location:** same file.

```python
@dataclass(frozen=True, slots=True)
class WalletSyncResult:
    cycle_number: int
    positions_fetched: int
    orders_fetched: int
    condition_ids_on_wallet: int
    condition_ids_in_cache: int
    instruments_newly_added: int
    resolution_failures: int
    unresolvable_retrying: int
    unresolvable_terminal: int
    http_positions_ok: bool
    http_orders_ok: bool
    first_sync_complete: bool
    elapsed_seconds: float
    failure_details: dict[str, int]
```

## Modified components

### `build_guru_trading_node` (`guru_compose.py`)

**Changes:**

1. **Create `WalletSyncActor` when `live` and `runtime.wallet_sync_enabled`.** Construct a `WalletSyncConfig` from runtime settings. Reuse the existing `clob_dynamic` client and `dynamic_ctrl` (`GuruInstrumentDynamicController`) — these are already built in compose for `need_dynamic or want_wallet_warm` (`guru_compose.py:529–535`). Register via `node.trader.add_actor(wallet_sync_actor)`.

2. **Expose `wallet_sync_actor` on `GuruTradingAssembly`.** Add field `wallet_sync: WalletSyncActor | None` (default `None` for shadow).

3. **Existing `warm_polymarket_cache_from_wallet_positions` continues to run at compose time.** It is not replaced — the actor is additive. The warmup seeds `Cache` before `node.build()`, so the adapter's `_connect` picks up those instruments for initial WS subscriptions. The actor handles continuous discovery after startup.

4. **`dynamic_ctrl` construction becomes mandatory for live mode.** Currently it is conditional on `need_dynamic or want_wallet_warm` (`guru_compose.py:529`). With wallet sync, the controller is always needed for live mode.

**What is removed:** Nothing yet. The compose-time warmup remains. The `polymarket_dynamic_max_activations` cap continues to apply to **guru-signal-driven** dynamic instrument resolution (the actor uses `force_add_instrument` which bypasses it).

### `GuruTradingAssembly` (`guru_compose.py`)

Add field:

```python
wallet_sync: WalletSyncActor | None = None
```

### `StartupReadinessGate` (`runtime/lifecycle/gate.py`)

**Changes:**

Add a new clause after the existing exec-connected check (`gate.py:63–70`): if `wallet_sync_actor` is wired and `first_sync_complete` is `False`, return `NOT_READY` with one of two distinct reasons:

- `"startup_wallet_sync_pending"` — first cycle has not completed yet but the startup deadline has not been exceeded.
- `"startup_wallet_sync_timeout"` — first cycle has not completed and `startup_deadline_seconds` has elapsed since `on_start`. This enables operator alerting on startup hangs.

This ensures the strategy does not trade until the wallet sync actor has completed at least one full poll cycle, meaning all wallet-held instruments are in `Cache` and targeted reconciliation has been triggered.

**Injection:** The gate constructor gains two optional callables:
- `wallet_sync_ready: Callable[[], bool] | None = None` — returns `first_sync_complete`.
- `wallet_sync_deadline_exceeded: Callable[[], bool] | None = None` — returns `startup_deadline_exceeded`.

Compose passes lambdas wrapping the actor's properties when the actor is wired.

### `NautilusDeploymentBudget` (`deployment_budget.py`)

**No changes to code.** The deployment budget already reads from `Cache.orders_open` and `Cache.positions_open` (`deployment_budget.py:94`, `149`). Once `WalletSyncActor` ensures complete cache coverage, the budget automatically includes all wallet exposure. This is the core architectural insight that makes Layer 2 (`WalletTruthProvider`) unnecessary.

### `NautilusExecutionStateReader` (`state_readers.py`)

**No changes.** Same reasoning: reads from `Cache.orders_open` which is now correctly populated.

### `NautilusAccountSnapshotProvider` (`state_readers.py`)

**No changes.** Reads from `Portfolio.account(venue)`, updated by adapter's `_update_account_state`.

### `ClobAllowanceStateProvider` (`state_readers.py`)

**No changes.** Reads from py-clob `get_balance_allowance` directly.

### `ConfiguredRiskPolicy` (`risk/configured.py`)

**No changes to code.** Already consumes deployment budget and capital provider. With complete cache coverage, its inputs become accurate.

### `DefaultCapitalStateProvider` (`runtime/capital/provider.py`)

**No changes.** Merges Nautilus account snapshot with optional CLOB allowance.

### `NautilusLiveExecutionHealthSource` (`tradable_state/nautilus_live_health.py`)

**Enhanced (non-breaking).** Currently maps only `_startup_reconciliation_event` to `HEALTHY` / `UNKNOWN_BOOTSTRAP` (`nautilus_live_health.py:62–80`). The known limitation is documented: "HEALTHY means startup reconciliation pass finished, not zero discrepancies" (`nautilus_live_health.py:19–24`).

The existing `TradableStateHealth` enum (`tradable_state/types.py:10–20`) has four levels:
- `HEALTHY = "healthy"`
- `UNKNOWN_BOOTSTRAP = "unknown_bootstrap"`
- `DEGRADED_OMS = "degraded_oms"`
- `DIVERGENT_PERSISTENT = "divergent_persistent"`

**Change:** The health source gains an optional `wallet_sync` parameter that exposes read-only properties from the `WalletSyncActor`. The constructor signature becomes:

```python
def __init__(
    self,
    exec_engine: Any,
    *,
    wallet_sync_status: WalletSyncHealthAdapter | None = None,
) -> None: ...
```

Where `WalletSyncHealthAdapter` is a frozen protocol / dataclass bridge:

```python
@runtime_checkable
class WalletSyncHealthAdapter(Protocol):
    @property
    def first_sync_complete(self) -> bool: ...
    @property
    def startup_deadline_exceeded(self) -> bool: ...
    @property
    def last_successful_cycle_utc(self) -> datetime | None: ...
    @property
    def consecutive_failure_count(self) -> int: ...
    @property
    def terminally_unresolvable_count(self) -> int: ...
    @property
    def poll_interval_seconds(self) -> float: ...
```

**Snapshot rules (evaluated in order, first match wins):**

1. `_startup_reconciliation_event` not set → `UNKNOWN_BOOTSTRAP`, reason `"nautilus_exec_startup_reconciliation_pending"` (existing behavior, `nautilus_live_health.py:65–74`).

2. `wallet_sync_status` is not None and `first_sync_complete` is False and `startup_deadline_exceeded` is False → `UNKNOWN_BOOTSTRAP`, reason `"wallet_sync_pending"`.

3. `wallet_sync_status` is not None and `first_sync_complete` is False and `startup_deadline_exceeded` is True → `DEGRADED_OMS`, reason `"wallet_sync_startup_timeout"`.
   - **Taxonomy note:** `DEGRADED_OMS` is the correct level. The existing enum docstring (`types.py:14`) references the risk matrix in `tradable_state_health.md §10` and `DEGRADED_OMS` maps to "entries blocked, exits permitted" in the readiness gate (`gate.py:153–167`). This matches the desired behavior: if wallet sync cannot complete, the system should not accept new entries but may still exit existing positions. `DIVERGENT_PERSISTENT` is reserved for persistent position divergence between cache and venue, which is a different failure mode.

4. `wallet_sync_status` is not None and `first_sync_complete` is True and `terminally_unresolvable_count > 0` → `DEGRADED_OMS`, reason `"wallet_sync_unresolvable_instruments"`.
   - The system is operational but has a known blind spot for specific markets. Blocking new entries is conservative-correct until an operator investigates.

5. `wallet_sync_status` is not None and `first_sync_complete` is True and (`last_successful_cycle_utc` is None or age > `2 × poll_interval_seconds` or `consecutive_failure_count >= 3`) → `DEGRADED_OMS`, reason `"wallet_sync_stale"`.
   - Stale wallet sync means the system may be blind to new venue-side activity. Same risk posture as unresolvable instruments.

6. If rules 4 and 5 both apply (unresolvable AND stale), prefer `"wallet_sync_stale"` as the reason — staleness is the more operationally urgent signal. The `framework_detail` string includes both conditions. This follows the existing pattern of single `reason_code` per snapshot (`TradableStateHealthSnapshot` has one `reason_code: str` field, not a list — `types.py:27`).

7. Otherwise → `HEALTHY`, reason `"nautilus_exec_startup_reconciliation_complete"` (existing behavior).

**When `wallet_sync_status` is None** (wallet sync not wired, e.g. shadow mode): rules 2–6 are skipped entirely. Behavior is identical to the existing implementation.

### `GuruInstrumentDynamicController` (`guru_instrument_dynamic.py`)

**Minor addition.** Add a convenience method:

```python
def resolve_and_activate_by_condition_and_token(
    self,
    condition_id: str,
    token_id: str,
) -> WalletPositionResolveOutcome:
    """
    Resolve using condition_id directly (no Gamma lookup).
    Used by WalletSyncActor when condition_id is already known
    from the positions/orders API response.
    """
```

This wraps the existing `resolve_binary_option_for_condition_and_token` + `force_add_instrument` pattern already used by wallet warmup (`guru_instrument_dynamic.py:229`), avoiding Gamma HTTP when the condition_id is already available.

### `_live_exec_engine_config` (`guru_compose.py`)

**Change:** When `wallet_sync_enabled`, pass `open_check_open_only=False` if the runtime YAML does not explicitly set it. Rationale: with wallet sync ensuring cache coverage, the full open-order history check becomes safe and valuable. The incremental API cost is acceptable.

Also set `use_data_api=True` on the `PolymarketExecClientConfig` when wallet sync is enabled, unless explicitly overridden. The Data API path is more robust for position reporting.
