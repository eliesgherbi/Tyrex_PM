# 02 — Components

## New components

### `WalletSyncActor`

**Location:** `src/tyrex_pm/runtime/wallet_sync.py`

**Base class:** `nautilus_trader.trading.strategy.Actor` (Nautilus Actor, registered on the node via `node.trader.add_actor`).

**Purpose:** Continuously discovers all markets the wallet has exposure on (positions or resting orders), ensures they are in `Cache`, and triggers targeted reconciliation for newly discovered instruments.

**Threading/async model:** Runs on the Nautilus event loop. Uses `self.clock.set_timer` for periodic polling. HTTP calls to py-clob / Data API / Gamma are dispatched via `asyncio.to_thread` (same pattern as `PolymarketExecutionClient._update_account_state`, `execution.py:301`). Instrument resolution reuses `GuruInstrumentDynamicController` which is already thread-safe (uses `threading.Lock` in `CacheInstrumentActivator`, `guru_instrument_dynamic.py:249`).

```python
@dataclass(frozen=True, slots=True)
class WalletSyncConfig:
    poll_interval_seconds: float = 15.0
    data_api_base_url: str = "https://data-api.polymarket.com"
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    gamma_http_timeout_seconds: float = 15.0
    clob_host: str = "https://clob.polymarket.com"


class WalletSyncActor(Actor):
    __slots__ = (
        "_config",
        "_clob",
        "_dynamic_ctrl",
        "_known_condition_ids",
        "_first_sync_complete",
        "_fact_emit",
        "_sync_count",
        "_instruments_discovered",
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
        """Run first sync immediately, then schedule periodic timer."""

    def on_stop(self) -> None:
        """Cancel timer."""

    def on_timer(self, event: TimeEvent) -> None:
        """Dispatch async sync cycle."""

    async def _sync_cycle(self) -> WalletSyncResult:
        """
        Core polling logic:
        1. Fetch wallet positions (Data API /positions, same as warmup).
        2. Fetch wallet open orders (py-clob get_orders()).
        3. Extract condition_ids from both.
        4. Diff against Cache instruments.
        5. For each missing: resolve via GuruInstrumentDynamicController.
        6. For each newly added: send targeted GenerateOrderStatusReport
           to the exec engine (triggers _maintain_active_market + order backfill).
        7. Emit wallet_sync fact.
        """

    async def _trigger_targeted_reconciliation(
        self, instrument_id: InstrumentId,
    ) -> None:
        """
        Send GenerateOrderStatusReport for this instrument through the message bus.
        This causes the exec client to call _maintain_active_market (opening the
        WS channel) and return the order's current status, which the engine reconciles.

        Evidence: PolymarketExecutionClient.generate_order_status_report calls
        _maintain_active_market as its first line (execution.py:554).
        """

    @property
    def first_sync_complete(self) -> bool:
        """Used by the readiness gate."""

    @property
    def sync_count(self) -> int:
        """Number of completed sync cycles."""

    @property
    def instruments_discovered(self) -> int:
        """Total instruments added to Cache by this actor."""
```

**Invariants:**
- Never submits orders, never modifies positions — read-only with respect to trading.
- Uses `CacheInstrumentActivator.force_add_instrument` (bypasses `polymarket_dynamic_max_activations` cap), same as wallet warmup (`guru_instrument_dynamic.py:271–281`).
- `_known_condition_ids` is maintained as a `set[str]` to avoid re-resolving instruments already in cache. Refreshed from `Cache.instruments(venue=POLYMARKET)` at the start of each cycle.
- `first_sync_complete` gates startup readiness (see `04_lifecycle.md`).

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

Add a new clause after the existing exec-connected check (`gate.py:63–70`): if `wallet_sync_actor` is wired and `first_sync_complete` is `False`, return `NOT_READY` with reason `"startup_wallet_sync_pending"`.

This ensures the strategy does not trade until the wallet sync actor has completed at least one full poll cycle, meaning all wallet-held instruments are in `Cache` and targeted reconciliation has been triggered.

**Injection:** The gate constructor gains an optional `wallet_sync_ready: Callable[[], bool] | None = None` parameter. Compose passes `wallet_sync_actor.first_sync_complete.__get__` (or a lambda) when the actor is wired.

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

**Enhanced (non-breaking).** Currently maps only `_startup_reconciliation_event` to HEALTHY/UNKNOWN_BOOTSTRAP. The known limitation is documented: "HEALTHY means startup reconciliation pass finished, not zero discrepancies" (`nautilus_live_health.py:19–24`).

**Change:** After `WalletSyncActor` integration, add a new health dimension: if the actor exists and `first_sync_complete` is False, the health source should report `UNKNOWN_BOOTSTRAP` with reason `"wallet_sync_pending"`. This replaces the weak signal with a meaningful one: "we have verified that all wallet instruments are loaded."

**Implementation:** The health source gains an optional `wallet_sync_ready: Callable[[], bool] | None` constructor parameter. When set and returning `False`, health reports `UNKNOWN_BOOTSTRAP`. This is simpler and more useful than trying to introspect adapter reconciliation success.

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
