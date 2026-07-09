from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.errors import ConfigError


_PAIRED_BINARY_DEPRECATED_KEYS = frozenset(
    {
        "loser_stop_loss",
        "winner_take_profit",
        "stop_reference",
        "min_stop_after_activation_s",
    }
)
_PAIRED_BINARY_DEPRECATED_MSG = (
    "paired_binary now uses pair_stop_loss_pct / pair_take_profit_pct; "
    "absolute price params are not supported."
)


def _reject_paired_binary_deprecated_keys(pb: dict[str, Any]) -> None:
    found = sorted(k for k in _PAIRED_BINARY_DEPRECATED_KEYS if k in pb)
    if found:
        raise ConfigError(f"{_PAIRED_BINARY_DEPRECATED_MSG} (found: {', '.join(found)})")


STRATEGY_KIND_GURU_FOLLOW = "guru_follow"
STRATEGY_KIND_SELL_TEST = "sell_test"
STRATEGY_KIND_ALLOCATION_TEST = "allocation_test"
STRATEGY_KIND_TP_SL_TEST = "tp_sl_test"
STRATEGY_KIND_SIMPLE_SIGNAL_TEST = "simple_signal_test"
STRATEGY_KIND_VALIDATION_HARNESS = "validation_harness"
STRATEGY_KIND_PAIRED_BINARY = "paired_binary"
_VALID_STRATEGY_KINDS = (
    STRATEGY_KIND_GURU_FOLLOW,
    STRATEGY_KIND_SELL_TEST,
    STRATEGY_KIND_ALLOCATION_TEST,
    STRATEGY_KIND_TP_SL_TEST,
    STRATEGY_KIND_SIMPLE_SIGNAL_TEST,
    STRATEGY_KIND_VALIDATION_HARNESS,
    STRATEGY_KIND_PAIRED_BINARY,
)

VALIDATION_MODE_NORMAL_ENTRY = "normal_entry"
VALIDATION_MODE_URGENT_EXIT = "urgent_exit"
VALIDATION_MODE_STALE_BOOK_DENY = "stale_book_deny"
VALIDATION_MODE_PROTECTION_REGISTER = "protection_register_only"
VALIDATION_MODE_PROTECTION_TP = "protection_trigger_tp"
VALIDATION_MODE_PROTECTION_SL = "protection_trigger_sl"
VALIDATION_MODE_PROTECTION_TRIGGER_LIVE = "protection_trigger_live"
VALIDATION_MODE_MARKET_DATA_READONLY = "market_data_readonly"
_VALID_VALIDATION_MODES = (
    VALIDATION_MODE_NORMAL_ENTRY,
    VALIDATION_MODE_URGENT_EXIT,
    VALIDATION_MODE_STALE_BOOK_DENY,
    VALIDATION_MODE_PROTECTION_REGISTER,
    VALIDATION_MODE_PROTECTION_TP,
    VALIDATION_MODE_PROTECTION_SL,
    VALIDATION_MODE_PROTECTION_TRIGGER_LIVE,
    VALIDATION_MODE_MARKET_DATA_READONLY,
)


SELL_TEST_PRICING_FIXED = "fixed"
SELL_TEST_PRICING_AUTO = "auto"
_VALID_SELL_TEST_PRICING_MODES = (SELL_TEST_PRICING_FIXED, SELL_TEST_PRICING_AUTO)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _load_yaml(p: Path) -> dict[str, Any]:
    if not p.is_file():
        raise ConfigError(f"missing config: {p}")
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class GuruConfig:
    wallet: str
    data_api_poll_interval_s: float
    data_api_limit: int
    data_api_max_pages_per_poll: int


@dataclass(frozen=True)
class FiltersConfig:
    token_allowlist: frozenset[str]
    min_notional_usd: Decimal
    significance_min_notional_usd: Decimal
    min_conviction_score: Decimal
    exclude_untradeable_markets: bool


@dataclass(frozen=True)
class ConvictionConfig:
    enabled: bool
    score_min: Decimal
    score_max: Decimal
    min_multiplier: Decimal
    max_multiplier: Decimal


@dataclass(frozen=True)
class SizingConfig:
    copy_scale: Decimal
    conviction: ConvictionConfig
    #: BUY entry: fixed USD notional (ignores copy_scale and conviction when True).
    static_enabled: bool
    static_amount_usd: Decimal


@dataclass(frozen=True)
class ExitsConfig:
    dust_notional_usd: Decimal
    sell_mode: str  # proportional_to_guru | full_bot_position
    #: Validation: after a copied guru BUY, schedule a demo SELL (see ``scheduled_exit_demo``).
    demo_forced_exit_enabled: bool = False
    demo_forced_exit_delay_s: float = 3.0


@dataclass(frozen=True)
class StrategyConfig:
    guru: GuruConfig
    filters: FiltersConfig
    sizing: SizingConfig
    exits: ExitsConfig


@dataclass(frozen=True)
class SellTestBuyConfig:
    """One BUY leg the sell_test strategy should emit at startup.

    ``pricing_mode``:

    * ``fixed`` (default, backward compatible) — submit at ``limit_price`` exactly.
      The order rests on the book unless ``limit_price`` already crosses the ask.
    * ``auto`` — at run time, fetch the venue order book for ``token_id`` and pick
      ``best_ask + aggression_ticks * tick_size`` so the BUY is marketable. When
      ``limit_price`` is set under ``auto`` it is used as a *fallback* if the book
      lookup fails (e.g. transient venue error). ``max_price`` is an optional
      upper guardrail: if the resolved aggressive price would exceed it, the
      strategy falls back instead of paying through the cap.
    """

    enabled: bool
    notional_usd: Decimal
    #: ``fixed``: required and used verbatim. ``auto``: optional fallback.
    limit_price: Decimal | None
    order_style: OrderStyle
    pricing_mode: str = SELL_TEST_PRICING_FIXED
    aggression_ticks: int = 1
    max_price: Decimal | None = None


@dataclass(frozen=True)
class SellTestSellConfig:
    """SELL leg fired ``delay_s`` after the BUY becomes sellable inventory.

    ``pricing_mode``:

    * ``fixed`` (default) — submit at ``limit_price`` if set, otherwise re-use the
      BUY's limit price (mirrors the guru-follow demo exit; deliberately simple
      and observable).
    * ``auto`` — at SELL emission time, fetch the venue book and pick
      ``best_bid - aggression_ticks * tick_size`` so the SELL is marketable.
      ``limit_price`` is the fallback when the lookup fails. ``min_price`` is an
      optional lower guardrail: the strategy refuses to dump below it and falls
      back to ``limit_price`` instead.
    """

    enabled: bool
    delay_s: float
    order_style: OrderStyle
    limit_price: Decimal | None
    pricing_mode: str = SELL_TEST_PRICING_FIXED
    aggression_ticks: int = 1
    min_price: Decimal | None = None


@dataclass(frozen=True)
class SellTestStrategyConfig:
    """Standalone strategy used to validate the V2 SELL / exit path end-to-end.

    See ``Docs/Implementation/sell_feature/`` for design context. This strategy
    does not poll guru activity; it emits one BUY for ``token_id`` then schedules
    one SELL after sellable inventory is observed (live) or after the synthetic
    fill (shadow). Intended for debugging — not production alpha.
    """

    enabled: bool
    token_id: str
    buy: SellTestBuyConfig
    sell: SellTestSellConfig
    run_once: bool


@dataclass(frozen=True)
class AllocationTestBuyConfig:
    enabled: bool
    notional_usd: Decimal
    limit_price: Decimal | None
    order_style: OrderStyle


@dataclass(frozen=True)
class AllocationTestOwnerBConfig:
    enabled: bool
    size_mode: str  # match_owner_a_buy | fixed
    fixed_size: Decimal


@dataclass(frozen=True)
class AllocationTestOwnerASellConfig:
    enabled: bool
    delay_s: float
    order_style: OrderStyle
    #: ``fixed``: submit at ``limit_price`` (or buy limit if unset). ``auto``: live
    #: run resolves ``best_bid - aggression_ticks * tick`` before SELL submit.
    limit_price: Decimal | None
    pricing_mode: str = SELL_TEST_PRICING_AUTO
    aggression_ticks: int = 0
    min_price: Decimal | None = None


@dataclass(frozen=True)
class AllocationTestTimeoutsConfig:
    allocation_visible_s: float
    position_visible_s: float
    unauthorized_sell_timeout_s: float
    owner_a_exit_timeout_s: float


@dataclass(frozen=True)
class AllocationTestStrategyConfig:
    """Toy strategy validating P4 allocation ownership (A buy / B block / A sell)."""

    enabled: bool
    token_id: str
    owner_a_id: str
    owner_b_id: str
    buy: AllocationTestBuyConfig
    owner_b_unauthorized_sell: AllocationTestOwnerBConfig
    owner_a_sell: AllocationTestOwnerASellConfig
    run_once: bool
    timeouts: AllocationTestTimeoutsConfig


TP_SL_PRICE_SOURCE_FIXTURE = "fixture"
TP_SL_PRICE_SOURCE_BEST_BID = "best_bid"
TP_SL_PRICE_SOURCE_MARK = "mark"
_VALID_TP_SL_PRICE_SOURCES = (
    TP_SL_PRICE_SOURCE_FIXTURE,
    TP_SL_PRICE_SOURCE_BEST_BID,
    TP_SL_PRICE_SOURCE_MARK,
)

TP_SL_TRIGGER_MODE_TP_OR_SL = "take_profit_or_stop_loss"
_VALID_TP_SL_TRIGGER_MODES = (TP_SL_TRIGGER_MODE_TP_OR_SL,)

TP_SL_TRIGGER_REFERENCE_ENTRY = "entry_price"
_VALID_TP_SL_TRIGGER_REFERENCES = (TP_SL_TRIGGER_REFERENCE_ENTRY,)

TP_SL_SIZE_MODE_FULL = "full_allocated_position"
TP_SL_SIZE_MODE_PERCENT = "percent_allocated_position"
TP_SL_SIZE_MODE_FIXED = "fixed_size"
_VALID_TP_SL_SIZE_MODES = (
    TP_SL_SIZE_MODE_FULL,
    TP_SL_SIZE_MODE_PERCENT,
    TP_SL_SIZE_MODE_FIXED,
)


@dataclass(frozen=True)
class TpSlTestBuyConfig:
    enabled: bool
    notional_usd: Decimal
    limit_price: Decimal | None
    order_style: OrderStyle
    pricing_mode: str = SELL_TEST_PRICING_FIXED
    aggression_ticks: int = 1
    max_price: Decimal | None = None


@dataclass(frozen=True)
class TpSlTestMonitorConfig:
    enabled: bool
    price_source: str
    poll_interval_s: float
    trigger_mode: str
    take_profit_price: Decimal | None
    stop_loss_price: Decimal | None
    take_profit_pct: Decimal | None
    stop_loss_pct: Decimal | None
    trigger_reference: str | None
    fixture_prices: tuple[Decimal, ...]


@dataclass(frozen=True)
class TpSlTestExitConfig:
    enabled: bool
    size_mode: str
    percent: Decimal
    fixed_size: Decimal | None
    order_style: OrderStyle
    pricing_mode: str = SELL_TEST_PRICING_FIXED
    aggression_ticks: int = 1
    min_price: Decimal | None = None
    limit_price: Decimal | None = None


@dataclass(frozen=True)
class TpSlTestTimeoutsConfig:
    inventory_timeout_s: float
    trigger_timeout_s: float
    completion_timeout_s: float


@dataclass(frozen=True)
class TpSlTestStrategyConfig:
    """Validation harness for P6 TP/SL overlay (deterministic fixture triggers)."""

    enabled: bool
    token_id: str
    owner_id: str
    buy: TpSlTestBuyConfig
    monitor: TpSlTestMonitorConfig
    exit: TpSlTestExitConfig
    timeouts: TpSlTestTimeoutsConfig
    run_once: bool


@dataclass(frozen=True)
class SimpleSignalTestStrategyConfig:
    """Non-guru architecture harness (P1 architecture_enhance).

    CLI-safe reference harness for the generic ``Signal → Strategy → Intent`` path.
    Driven by :func:`tyrex_pm.runtime.fixture_signal_run.run_fixture_signals_once`
    (no guru polling).
    """

    enabled: bool
    token_id: str
    owner_id: str
    side: Side
    notional_usd: Decimal
    limit_price: Decimal | None
    order_style: OrderStyle
    run_once: bool
    #: Live-only: ``auto`` resolves a marketable limit from the venue book (same helper as sell_test).
    pricing_mode: str = SELL_TEST_PRICING_FIXED
    aggression_ticks: int = 2
    max_price: Decimal | None = None


@dataclass(frozen=True)
class ProtectionRuntimeConfig:
    """Runtime protection overlay config (P4 / P4.5)."""

    enabled: bool
    take_profit_pct: Decimal | None = None
    stop_loss_pct: Decimal | None = None
    take_profit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    size_mode: str = "full"
    fixed_size: Decimal | None = None
    percent: Decimal | None = None
    exit_order_style: OrderStyle = OrderStyle.FAK
    exit_limit_price: Decimal | None = None
    max_book_age_s: float = 5.0
    register_on_buy: bool = True
    tick_interval_s: float = 1.0
    max_runtime_s: float = 180.0
    stop_after_trigger: bool = True
    stop_after_exit_submit: bool = True
    fail_if_no_trigger: bool = False


@dataclass(frozen=True)
class ValidationHarnessStrategyConfig:
    """Architecture validation harness (P4.5). Operator tool — not production."""

    enabled: bool
    mode: str
    owner_id: str
    token_id: str
    side: Side
    notional_usd: Decimal | None
    limit_price: Decimal | None
    size: Decimal | None
    urgency: str
    order_style: OrderStyle
    run_once: bool
    pricing_mode: str = SELL_TEST_PRICING_FIXED
    aggression_ticks: int = 2
    max_price: Decimal | None = None
    entry_price: Decimal | None = None
    seed_allocation_qty: Decimal | None = None
    fixture_book_bid: Decimal | None = None
    fixture_book_ask: Decimal | None = None
    market_data_readonly_seconds: float = 3.0
    use_fixture_book: bool = False
    allow_seed_allocation: bool = False
    wait_for_confirmed: bool = False
    confirmed_timeout_s: float = 90.0
    fail_if_not_confirmed: bool = True


@dataclass(frozen=True)
class PairedBinaryStrategyConfig:
    """Production paired binary strategy (Phase 4.6)."""

    enabled: bool
    owner_id: str
    market_id: str
    yes_token_id: str
    no_token_id: str
    position_size: Decimal
    max_pair_entry_cost: Decimal
    max_spread_yes: Decimal
    max_spread_no: Decimal
    pair_stop_loss_pct: Decimal
    pair_take_profit_pct: Decimal
    slippage_buffer: Decimal
    reject_if_spread_exceeds_loss_budget: bool
    max_holding_time_s: float
    entry_order_style: OrderStyle
    exit_order_style: OrderStyle
    entry_fill_timeout_s: float
    abort_unpaired_entry: bool
    unwind_partial_entry: bool
    min_effective_pair_qty: Decimal | None
    run_once: bool
    max_markets: int
    tick_interval_s: float
    max_book_age_s: float
    entry_dry_run: bool = False
    stop_after_entry: bool = False
    max_runtime_s: float = 600.0
    use_fixture_book: bool = False
    allow_seed_allocation: bool = False
    seed_allocation_qty: Decimal | None = None
    fixture_yes_bid: Decimal | None = None
    fixture_yes_ask: Decimal | None = None
    fixture_no_bid: Decimal | None = None
    fixture_no_ask: Decimal | None = None
    activation_gap_retry_s: float = 0.0
    activation_gap_retry_interval_s: float = 0.5
    activation_gap_max_retries: int = 6
    activation_unwind_retry_s: float = 15.0
    activation_unwind_retry_interval_s: float = 0.5
    entry_price_mismatch_tolerance: Decimal = Decimal("0.005")
    entry_price_mismatch_abort_threshold: Decimal | None = Decimal("0.02")
    allow_entry_style_downgrade: bool = False
    allow_resting_entry_orders: bool = False
    pair_entry_submit_timeout_s: float = 5.0
    pair_entry_fill_timeout_s: float = 10.0
    pair_entry_resting_timeout_s: float = 2.0
    condition_id: str | None = None
    event_start_ts: float | None = None
    event_end_ts: float | None = None


@dataclass(frozen=True)
class NotionalConfig:
    min_usd: Decimal
    max_usd: Decimal
    #: When order notional exceeds max_usd: clip size to max (cap) or reject (deny).
    max_policy: str


@dataclass(frozen=True)
class DeploymentConfig:
    token_cap_usd: Decimal
    portfolio_cap_usd: Decimal


@dataclass(frozen=True)
class CapitalConfig:
    enabled: bool
    max_wallet_age_s: int


@dataclass(frozen=True)
class VenueMinSizeConfig:
    """Pre-submit guard: clip-side and notional-cap math can produce a final ``size`` below
    the venue's hard minimum (Polymarket commonly = 5 shares). Without this gate the order
    reaches the venue and is rejected with ``Size (X) lower than the minimum: 5``.

    ``policy=deny``  → block locally with reason ``below_venue_min_size`` (no submit).
    ``policy=bump``  → raise ``size`` to ``default_min_size`` then re-validate deployment +
    capital with the bumped size; only submit if both still pass.
    """

    enabled: bool
    policy: str  # "deny" | "bump"
    default_min_size: Decimal


@dataclass(frozen=True)
class InventoryConfig:
    sell_requires_venue_position: bool


@dataclass(frozen=True)
class KillSwitchConfig:
    enabled: bool


@dataclass(frozen=True)
class ConcurrencyConfig:
    max_orders_in_flight: int


@dataclass(frozen=True)
class ReadinessConfig:
    require_wallet_sync: bool
    max_wallet_age_s_live: int
    require_heartbeat_live: bool
    require_user_ws_live: bool


@dataclass(frozen=True)
class RiskExitsConfig:
    """Reduce-only urgent exit policy (Phase 4.6 architecture fix)."""

    allow_reduce_only_mark_fallback: bool = True
    require_fresh_book_for_mark_fallback: bool = True
    urgent_exit_max_book_age_s: float = 0.5


@dataclass(frozen=True)
class RiskConfig:
    notional: NotionalConfig
    deployment: DeploymentConfig
    capital: CapitalConfig
    inventory: InventoryConfig
    kill_switch: KillSwitchConfig
    concurrency: ConcurrencyConfig
    readiness: ReadinessConfig
    venue_min_size: VenueMinSizeConfig
    exits: RiskExitsConfig = RiskExitsConfig()


@dataclass(frozen=True)
class ReportingConfig:
    enabled: bool
    runs_dir: str


@dataclass(frozen=True)
class ShadowBootstrapConfig:
    """Seed WalletStore for shadow runs when venue sync is not wired (not secrets)."""

    usdc_balance: Decimal
    usdc_allowance: Decimal


@dataclass(frozen=True)
class AllocationLedgerConfig:
    """Allocation ledger is always active; persisted to ``var/state/allocation_ledger.json``."""

    #: Do not clamp owner allocation to ``venue_qty=0`` within this window after a BUY
    #: credit when REST positions lag behind instant live fills.
    clamp_grace_s_after_buy: float = 90.0


@dataclass(frozen=True)
class ExecutionPlannerConfig:
    """ExecutionPlanner settings (P3 architecture_enhance).

    Dark-launched: ``enabled`` defaults to ``False`` so the pipeline keeps using
    the strategy/intent order style. When enabled, a market data provider is
    required (cross-checked against ``market_data.enabled``). Urgent/protection
    exits require a fresh book unless ``allow_urgent_exit_fallback`` is set.
    """

    enabled: bool = False
    require_fresh_book_for_urgent: bool = True
    max_book_age_s: float = 5.0
    allow_urgent_exit_fallback: bool = False
    use_executable_depth: bool = True
    max_slippage_vs_touch: float = 0.05


@dataclass(frozen=True)
class FillReconciliationConfig:
    price_tolerance: float = 0.001
    cash_tolerance_usd: float = 0.01
    require_final_for_realized_pnl: bool = True


@dataclass(frozen=True)
class ExecutionConfig:
    planner: ExecutionPlannerConfig = ExecutionPlannerConfig()
    fill_reconciliation: FillReconciliationConfig = FillReconciliationConfig()


@dataclass(frozen=True)
class MarketDataWebSocketConfig:
    primary_enabled: bool = False
    shadow_enabled: bool = False
    url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    reconnect_backoff_s: float = 3.0
    compare_interval_s: float = 5.0


@dataclass(frozen=True)
class MarketDataRestConfig:
    poll_enabled: bool = True
    bootstrap_on_startup: bool = True
    recovery_on_reconnect: bool = True


@dataclass(frozen=True)
class MarketDataFeaturesConfig:
    v0_enabled: bool = True


@dataclass(frozen=True)
class MarketDataQualityConfig:
    enforcement_mode: str = "observe_only"
    market_profile: str = "crypto_5m"
    require_ws_primary_for_entry: bool = True
    allow_rest_recovery_for_exit: bool = True
    allow_rest_recovery_for_entry: bool = False


@dataclass(frozen=True)
class MarketDataHealthConfig:
    max_ws_disconnect_s: float = 30.0
    max_reconnects_per_hour: int = 10
    max_p95_book_age_ms: int = 2000


@dataclass(frozen=True)
class MarketDataEventBackboneConfig:
    """Phase 2B M2B.0-B — WS MarketEvent path behind default-off flag."""

    enabled: bool = False
    reorder_buffer_ms: float = 75.0
    emit_rest_recovery: bool = True


@dataclass(frozen=True)
class ObservabilityConfig:
    emit_decision_snapshot: bool = True
    sample_raw_events: int = 0
    emit_event_correlation: bool = False


@dataclass(frozen=True)
class RecordingConfig:
    """Phase 2B M2B.1-A/B — record-only MarketEvent persistence."""

    enabled: bool = False
    output_dir: str = "var/recordings"
    segment_max_mb: int = 64
    segment_max_s: int = 300
    queue_maxsize: int = 10_000
    batch_size: int = 100
    batch_flush_ms: int = 50
    market_id: str | None = None
    yes_token_id: str | None = None
    no_token_id: str | None = None
    discovery_enabled: bool = False
    discovery_poll_s: float = 25.0
    heartbeat_stall_s: float = 120.0
    heartbeat_interval_s: float = 30.0
    compress: bool = False
    tap_in_live: bool = False
    post_close_grace_s: float = 60.0
    skip_expired_markets: bool = True
    pre_open_recording_lead_s: float = 60.0


@dataclass(frozen=True)
class ExternalBtcConfig:
    """M2B.2 — read-only external BTC feed (record mode only)."""

    enabled: bool = False
    symbol: str = "BTCUSDT"
    streams: tuple[str, ...] = ("bookTicker", "aggTrade")
    venue: str = "binance"
    reconnect_backoff_s: float = 3.0
    clock_sync_interval_s: float = 60.0


@dataclass(frozen=True)
class ReferencePricesConfig:
    """M2B.3-A — read-only Polymarket RTDS reference prices (record mode only)."""

    enabled: bool = False
    venue: str = "polymarket_rtds"
    feeds: tuple[str, ...] = ("chainlink",)
    symbols: tuple[str, ...] = ("btc/usd",)
    emit_price_to_beat: bool = True
    price_to_beat_max_lag_ms: float = 5000.0
    reconnect_backoff_s: float = 3.0


@dataclass(frozen=True)
class MarketDataConfig:
    """Shared market-data / order-book state (Phase 2 architecture_enhance).

    Dark-launched: ``enabled`` defaults to ``False`` so merging Phase 2 changes
    no live behavior. When the planner (Phase 3) is enabled it requires a market
    data provider; ``max_book_age_s`` is the freshness window beyond which a book
    is treated as stale (fail-closed for urgent/protection exits).
    """

    enabled: bool = False
    max_book_age_s: float = 5.0
    token_ids: tuple[str, ...] = ()
    store_top_n_levels: int = 5
    websocket: MarketDataWebSocketConfig = MarketDataWebSocketConfig()
    rest: MarketDataRestConfig = MarketDataRestConfig()
    features: MarketDataFeaturesConfig = MarketDataFeaturesConfig()
    quality: MarketDataQualityConfig = MarketDataQualityConfig()
    health: MarketDataHealthConfig = MarketDataHealthConfig()
    event_backbone: MarketDataEventBackboneConfig = MarketDataEventBackboneConfig()
    market_profiles: dict[str, dict[str, object]] | None = None


@dataclass(frozen=True)
class MarketTimingDiagnosticsConfig:
    enabled: bool = True
    near_close_window_s: float = 45.0


@dataclass(frozen=True)
class StrategyLifecycleConfig:
    """Phase 1 M0 — market-aware loop clock. ``enabled=False`` when YAML block absent."""

    enabled: bool = False
    mode: str = "market_aware"
    exit_clock_source: str = "event_end_ts"
    max_runtime_s: float | None = None
    fallback_max_runtime_s: float = 900.0
    flatten_before_event_end_s: float = 20.0
    block_new_entry_phases: frozenset[str] = frozenset({"near_close", "closed"})
    emit_unknown_market_end_warning: bool = True
    min_survival_window_s: float = 45.0


@dataclass(frozen=True)
class SurvivalTargetPolicyConfig:
    mode: str = "dynamic"
    small_loss_max_usd_per_pair: Decimal = Decimal("0.05")
    small_profit_min_usd_per_pair: Decimal = Decimal("0.03")
    max_reasonable_exit_price: Decimal = Decimal("0.98")
    slippage_buffer: Decimal | None = None
    estimated_fee_bps: Decimal = Decimal("0")


@dataclass(frozen=True)
class SurvivalExitPlanningConfig:
    require_executable_evidence: bool = True
    allow_partial_survival_exit: bool = True
    min_depth_fraction: Decimal = Decimal("0.8")
    max_book_age_s: float | None = None


@dataclass(frozen=True)
class ReachabilityWeightsConfig:
    distance_to_target: Decimal = Decimal("0.25")
    time_remaining: Decimal = Decimal("0.25")
    spread: Decimal = Decimal("0.15")
    depth_at_size: Decimal = Decimal("0.20")
    book_velocity: Decimal = Decimal("0.10")
    quality_stability: Decimal = Decimal("0.05")


@dataclass(frozen=True)
class ReachabilityConfig:
    enforcement_mode: str = "advisory"
    reachable_min_score: Decimal = Decimal("0.65")
    weak_min_score: Decimal = Decimal("0.35")
    weights: ReachabilityWeightsConfig = ReachabilityWeightsConfig()


@dataclass(frozen=True)
class StallExitConfig:
    enabled: bool = False
    enforcement_mode: str = "advisory"
    min_progress_ratio: Decimal = Decimal("0.35")
    stall_threshold_fraction: Decimal = Decimal("0.40")
    check_after_s: float = 20.0
    max_stall_s: float = 60.0
    action_when_stalled: str = "downgrade"


@dataclass(frozen=True)
class SurvivorFloorConfig:
    enabled: bool = True
    enforcement_mode: str = "advisory"
    mode: str = "winner_entry_price"
    floor_buffer: Decimal = Decimal("0.00")
    require_fresh_book: bool = True
    max_spread: Decimal = Decimal("0.04")
    min_depth_fraction: Decimal = Decimal("0.8")


@dataclass(frozen=True)
class RecoveryLevelConfig:
    desired_buffer: Decimal = Decimal("0")
    slippage_buffer: Decimal | None = None
    activation_buffer: Decimal = Decimal("0")


@dataclass(frozen=True)
class TrailingStopConfig:
    enabled: bool = True
    enforcement_mode: str = "advisory"
    activation_mode: str = "loss_recovered"
    recovery_buffer: Decimal = Decimal("0.00")
    arm_after_executable_gain: Decimal = Decimal("0.02")
    trail_distance: Decimal = Decimal("0.03")
    trail_distance_mode: str = "absolute"
    min_profit_lock: Decimal = Decimal("0.01")
    arm_delay_s: float = 5.0
    require_fresh_book: bool = True
    max_spread: Decimal = Decimal("0.04")
    min_depth_fraction: Decimal = Decimal("0.8")
    disable_near_close_s: float = 30.0
    max_book_age_s: float | None = None


@dataclass(frozen=True)
class EconomicsConfig:
    enabled: bool = True
    enforcement_mode: str = "advisory"
    min_acceptable_net_usd_per_pair: Decimal = Decimal("-0.02")
    estimated_fee_bps: Decimal = Decimal("0")
    reject_entry_if_expected_net_below: Decimal | None = None
    exit_survivor_if_expected_net_below: Decimal | None = Decimal("-0.05")


@dataclass(frozen=True)
class SurvivalObservabilityConfig:
    min_emit_interval_s: float = 5.0
    emit_on_verdict_change: bool = True
    emit_on_target_mode_change: bool = True
    emit_on_material_price_change_ticks: int = 1


@dataclass(frozen=True)
class SurvivalKillSwitchConfig:
    enabled: bool = False
    per_pair_max_loss_usd: Decimal = Decimal("0.50")
    daily_max_loss_usd: Decimal = Decimal("5.00")
    max_failed_lifecycle_count: int = 3
    max_consecutive_no_entry: int = 10
    max_bad_market_quality_streak: int = 5
    max_manual_intervention_count: int = 2
    persist_daily: bool = True


@dataclass(frozen=True)
class SurvivalExitOrderPolicyConfig:
    mode: str = "fak_retry"
    initial_order_type: str = "FAK"
    retry_order_type: str = "FAK"
    max_fak_retries: int = 3
    reprice_on_retry: bool = True
    sell_reprice_ticks: tuple[int, ...] = (0, 1, 3)
    buy_reprice_ticks: tuple[int, ...] = (0, 1, 3)
    allow_partial_retry: bool = True
    managed_rest_enabled: bool = False
    managed_rest_order_type: str = "GTC"
    managed_rest_local_ttl_s: float = 2.0
    managed_rest_max_attempts: int = 2
    managed_rest_cancel_replace: bool = True
    disable_managed_rest_when_seconds_to_close_lt: float = 30.0
    urgent_when_seconds_to_close_lt: float = 20.0
    post_only_for_survival_exit: bool = False


@dataclass(frozen=True)
class SurvivalEnforcementConfig:
    order_policy: SurvivalExitOrderPolicyConfig = SurvivalExitOrderPolicyConfig()
    retry_quality_rejects: bool = False
    max_quality_reject_retries: int = 10
    quality_reject_retry_backoff_s: float = 0.25
    abandon_quality_reject_after_s: float = 10.0


@dataclass(frozen=True)
class SurvivalConfig:
    enabled: bool = False
    monitor_mode: str = "hybrid"
    target_policy: SurvivalTargetPolicyConfig = SurvivalTargetPolicyConfig()
    exit_planning: SurvivalExitPlanningConfig = SurvivalExitPlanningConfig()
    survivor_floor: SurvivorFloorConfig = SurvivorFloorConfig()
    recovery_level: RecoveryLevelConfig = RecoveryLevelConfig()
    reachability: ReachabilityConfig = ReachabilityConfig()
    stall_exit: StallExitConfig = StallExitConfig()
    trailing_stop: TrailingStopConfig = TrailingStopConfig()
    economics: EconomicsConfig = EconomicsConfig()
    kill_switches: SurvivalKillSwitchConfig = SurvivalKillSwitchConfig()
    observability: SurvivalObservabilityConfig = SurvivalObservabilityConfig()
    enforcement: SurvivalEnforcementConfig = SurvivalEnforcementConfig()


def _parse_tick_tuple(raw: object, default: tuple[int, ...]) -> tuple[int, ...]:
    if raw is None:
        return default
    if isinstance(raw, (list, tuple)):
        return tuple(int(x) for x in raw)
    return default


def _parse_survival_order_policy(raw: dict[str, Any] | None) -> SurvivalExitOrderPolicyConfig:
    r = raw or {}
    return SurvivalExitOrderPolicyConfig(
        mode=str(r.get("mode", "fak_retry")),
        initial_order_type=str(r.get("initial_order_type", "FAK")),
        retry_order_type=str(r.get("retry_order_type", "FAK")),
        max_fak_retries=int(r.get("max_fak_retries", 3)),
        reprice_on_retry=bool(r.get("reprice_on_retry", True)),
        sell_reprice_ticks=_parse_tick_tuple(r.get("sell_reprice_ticks"), (0, 1, 3)),
        buy_reprice_ticks=_parse_tick_tuple(r.get("buy_reprice_ticks"), (0, 1, 3)),
        allow_partial_retry=bool(r.get("allow_partial_retry", True)),
        managed_rest_enabled=bool(r.get("managed_rest_enabled", False)),
        managed_rest_order_type=str(r.get("managed_rest_order_type", "GTC")),
        managed_rest_local_ttl_s=float(r.get("managed_rest_local_ttl_s", 2.0)),
        managed_rest_max_attempts=int(r.get("managed_rest_max_attempts", 2)),
        managed_rest_cancel_replace=bool(r.get("managed_rest_cancel_replace", True)),
        disable_managed_rest_when_seconds_to_close_lt=float(
            r.get("disable_managed_rest_when_seconds_to_close_lt", 30)
        ),
        urgent_when_seconds_to_close_lt=float(r.get("urgent_when_seconds_to_close_lt", 20)),
        post_only_for_survival_exit=bool(r.get("post_only_for_survival_exit", False)),
    )


def _parse_survival_enforcement(raw: dict[str, Any] | None) -> SurvivalEnforcementConfig:
    r = raw or {}
    op = r.get("order_policy") or {}
    return SurvivalEnforcementConfig(
        order_policy=_parse_survival_order_policy(op),
        retry_quality_rejects=bool(r.get("retry_quality_rejects", False)),
        max_quality_reject_retries=int(r.get("max_quality_reject_retries", 10)),
        quality_reject_retry_backoff_s=float(r.get("quality_reject_retry_backoff_s", 0.25)),
        abandon_quality_reject_after_s=float(r.get("abandon_quality_reject_after_s", 10.0)),
    )


def _parse_reachability_weights(raw: dict[str, Any] | None) -> ReachabilityWeightsConfig:
    w = raw or {}
    return ReachabilityWeightsConfig(
        distance_to_target=_dec(w, "distance_to_target", "0.25"),
        time_remaining=_dec(w, "time_remaining", "0.25"),
        spread=_dec(w, "spread", "0.15"),
        depth_at_size=_dec(w, "depth_at_size", "0.20"),
        book_velocity=_dec(w, "book_velocity", "0.10"),
        quality_stability=_dec(w, "quality_stability", "0.05"),
    )


def _parse_survival_config(raw: dict[str, Any] | None) -> SurvivalConfig:
    if not raw:
        return SurvivalConfig()
    tp_raw = raw.get("target_policy") or {}
    ep_raw = raw.get("exit_planning") or {}
    reach_raw = raw.get("reachability") or {}
    stall_raw = raw.get("stall_exit") or {}
    trail_raw = raw.get("trailing_stop") or {}
    floor_raw = raw.get("survivor_floor") or {}
    recovery_raw = raw.get("recovery_level") or {}
    econ_raw = raw.get("economics") or {}
    slippage_raw = tp_raw.get("slippage_buffer")
    slippage = None if slippage_raw in (None, "") else _dec(tp_raw, "slippage_buffer", "0")
    max_book_raw = ep_raw.get("max_book_age_s")
    max_book = None if max_book_raw in (None, "") else float(max_book_raw)
    trail_max_book_raw = trail_raw.get("max_book_age_s")
    trail_max_book = None if trail_max_book_raw in (None, "") else float(trail_max_book_raw)
    reject_entry_raw = econ_raw.get("reject_entry_if_expected_net_below")
    reject_entry = (
        None if reject_entry_raw in (None, "") else _dec(econ_raw, "reject_entry_if_expected_net_below", "0")
    )
    exit_survivor_raw = econ_raw.get("exit_survivor_if_expected_net_below")
    exit_survivor = (
        None if exit_survivor_raw in (None, "")
        else _dec(econ_raw, "exit_survivor_if_expected_net_below", "-0.05")
    )
    ks_raw = raw.get("kill_switches") or {}
    obs_raw = raw.get("observability") or {}
    enf_raw = raw.get("enforcement") or {}
    recovery_slippage_raw = recovery_raw.get("slippage_buffer")
    recovery_slippage = None if recovery_slippage_raw in (None, "") else _dec(recovery_raw, "slippage_buffer", "0")
    return SurvivalConfig(
        enabled=bool(raw.get("enabled", False)),
        monitor_mode=str(raw.get("monitor_mode", "hybrid")),
        target_policy=SurvivalTargetPolicyConfig(
            mode=str(tp_raw.get("mode", "dynamic")),
            small_loss_max_usd_per_pair=_dec(tp_raw, "small_loss_max_usd_per_pair", "0.05"),
            small_profit_min_usd_per_pair=_dec(tp_raw, "small_profit_min_usd_per_pair", "0.03"),
            max_reasonable_exit_price=_dec(tp_raw, "max_reasonable_exit_price", "0.98"),
            slippage_buffer=slippage,
            estimated_fee_bps=_dec(tp_raw, "estimated_fee_bps", "0"),
        ),
        exit_planning=SurvivalExitPlanningConfig(
            require_executable_evidence=bool(ep_raw.get("require_executable_evidence", True)),
            allow_partial_survival_exit=bool(ep_raw.get("allow_partial_survival_exit", True)),
            min_depth_fraction=_dec(ep_raw, "min_depth_fraction", "0.8"),
            max_book_age_s=max_book,
        ),
        survivor_floor=SurvivorFloorConfig(
            enabled=bool(floor_raw.get("enabled", True)),
            enforcement_mode=str(floor_raw.get("enforcement_mode", "advisory")),
            mode=str(floor_raw.get("mode", "winner_entry_price")),
            floor_buffer=_dec(floor_raw, "floor_buffer", "0"),
            require_fresh_book=bool(floor_raw.get("require_fresh_book", True)),
            max_spread=_dec(floor_raw, "max_spread", "0.04"),
            min_depth_fraction=_dec(floor_raw, "min_depth_fraction", "0.8"),
        ),
        recovery_level=RecoveryLevelConfig(
            desired_buffer=_dec(recovery_raw, "desired_buffer", "0"),
            slippage_buffer=recovery_slippage,
            activation_buffer=_dec(recovery_raw, "activation_buffer", "0"),
        ),
        reachability=ReachabilityConfig(
            enforcement_mode=str(reach_raw.get("enforcement_mode", "advisory")),
            reachable_min_score=_dec(reach_raw, "reachable_min_score", "0.65"),
            weak_min_score=_dec(reach_raw, "weak_min_score", "0.35"),
            weights=_parse_reachability_weights(reach_raw.get("weights")),
        ),
        stall_exit=StallExitConfig(
            enabled=bool(stall_raw.get("enabled", False)),
            enforcement_mode=str(stall_raw.get("enforcement_mode", "advisory")),
            min_progress_ratio=_dec(stall_raw, "min_progress_ratio", "0.35"),
            stall_threshold_fraction=_dec(stall_raw, "stall_threshold_fraction", "0.40"),
            check_after_s=float(stall_raw.get("check_after_s", 20)),
            max_stall_s=float(stall_raw.get("max_stall_s", 60)),
            action_when_stalled=str(stall_raw.get("action_when_stalled", "downgrade")),
        ),
        trailing_stop=TrailingStopConfig(
            enabled=bool(trail_raw.get("enabled", True)),
            enforcement_mode=str(trail_raw.get("enforcement_mode", "advisory")),
            activation_mode=str(trail_raw.get("activation_mode", "loss_recovered")),
            recovery_buffer=_dec(trail_raw, "recovery_buffer", "0"),
            arm_after_executable_gain=_dec(trail_raw, "arm_after_executable_gain", "0.02"),
            trail_distance=_dec(trail_raw, "trail_distance", "0.03"),
            trail_distance_mode=str(trail_raw.get("trail_distance_mode", "absolute")),
            min_profit_lock=_dec(trail_raw, "min_profit_lock", "0.01"),
            arm_delay_s=float(trail_raw.get("arm_delay_s", 5)),
            require_fresh_book=bool(trail_raw.get("require_fresh_book", True)),
            max_spread=_dec(trail_raw, "max_spread", "0.04"),
            min_depth_fraction=_dec(trail_raw, "min_depth_fraction", "0.8"),
            disable_near_close_s=float(trail_raw.get("disable_near_close_s", 30)),
            max_book_age_s=trail_max_book,
        ),
        economics=EconomicsConfig(
            enabled=bool(econ_raw.get("enabled", True)),
            enforcement_mode=str(econ_raw.get("enforcement_mode", "advisory")),
            min_acceptable_net_usd_per_pair=_dec(econ_raw, "min_acceptable_net_usd_per_pair", "-0.02"),
            estimated_fee_bps=_dec(econ_raw, "estimated_fee_bps", "0"),
            reject_entry_if_expected_net_below=reject_entry,
            exit_survivor_if_expected_net_below=exit_survivor,
        ),
        kill_switches=SurvivalKillSwitchConfig(
            enabled=bool(ks_raw.get("enabled", False)),
            per_pair_max_loss_usd=_dec(ks_raw, "per_pair_max_loss_usd", "0.50"),
            daily_max_loss_usd=_dec(ks_raw, "daily_max_loss_usd", "5.00"),
            max_failed_lifecycle_count=int(ks_raw.get("max_failed_lifecycle_count", 3)),
            max_consecutive_no_entry=int(ks_raw.get("max_consecutive_no_entry", 10)),
            max_bad_market_quality_streak=int(ks_raw.get("max_bad_market_quality_streak", 5)),
            max_manual_intervention_count=int(ks_raw.get("max_manual_intervention_count", 2)),
            persist_daily=bool(ks_raw.get("persist_daily", True)),
        ),
        observability=SurvivalObservabilityConfig(
            min_emit_interval_s=float(obs_raw.get("min_emit_interval_s", 5)),
            emit_on_verdict_change=bool(obs_raw.get("emit_on_verdict_change", True)),
            emit_on_target_mode_change=bool(obs_raw.get("emit_on_target_mode_change", True)),
            emit_on_material_price_change_ticks=int(obs_raw.get("emit_on_material_price_change_ticks", 1)),
        ),
        enforcement=_parse_survival_enforcement(enf_raw),
    )


@dataclass(frozen=True)
class PairedBinaryRuntimeConfig:
    poll_interval_s: float = 1.0
    max_decision_rate_per_market_ms: int = 75
    #: Policy when tick budget / ``max_runtime_s`` elapses with open paired-binary exposure.
    open_exposure_on_max_runtime: str = "force_reduce_only_exit"
    #: Extra monitor window when ``open_exposure_on_max_runtime=continue_until_open_exposure_timeout``.
    open_exposure_timeout_s: float = 300.0
    #: Legacy alias — if set in YAML without ``open_exposure_on_max_runtime``, used as fallback.
    survivor_on_max_runtime: str = "force_reduce_only_exit"
    #: Legacy alias for ``open_exposure_timeout_s``.
    survivor_timeout_s: float = 300.0
    #: When false (default), terminal persisted lifecycle (DONE/FAILED) resets to IDLE on same token pair.
    allow_terminal_state_resume: bool = False
    market_timing_diagnostics: MarketTimingDiagnosticsConfig = MarketTimingDiagnosticsConfig()
    stop_background_tasks_after_strategy_done: bool = False


OPEN_EXPOSURE_ON_MAX_RUNTIME_FORCE = "force_reduce_only_exit"
OPEN_EXPOSURE_ON_MAX_RUNTIME_MANUAL = "manual_intervention"
OPEN_EXPOSURE_ON_MAX_RUNTIME_CONTINUE = "continue_until_open_exposure_timeout"
VALID_OPEN_EXPOSURE_ON_MAX_RUNTIME = frozenset(
    {
        OPEN_EXPOSURE_ON_MAX_RUNTIME_FORCE,
        OPEN_EXPOSURE_ON_MAX_RUNTIME_MANUAL,
        OPEN_EXPOSURE_ON_MAX_RUNTIME_CONTINUE,
    }
)

# Back-compat aliases (survivor-specific names delegate to open-exposure policy).
SURVIVOR_ON_MAX_RUNTIME_FORCE = OPEN_EXPOSURE_ON_MAX_RUNTIME_FORCE
SURVIVOR_ON_MAX_RUNTIME_MANUAL = OPEN_EXPOSURE_ON_MAX_RUNTIME_MANUAL
SURVIVOR_ON_MAX_RUNTIME_CONTINUE = "continue_until_survivor_timeout"
VALID_SURVIVOR_ON_MAX_RUNTIME = VALID_OPEN_EXPOSURE_ON_MAX_RUNTIME | {SURVIVOR_ON_MAX_RUNTIME_CONTINUE}


def _normalize_open_exposure_policy(raw: object) -> str:
    key = str(raw or OPEN_EXPOSURE_ON_MAX_RUNTIME_FORCE).strip()
    if key == SURVIVOR_ON_MAX_RUNTIME_CONTINUE:
        return OPEN_EXPOSURE_ON_MAX_RUNTIME_CONTINUE
    return key


def _parse_strategy_lifecycle_config(runtime: dict[str, Any]) -> StrategyLifecycleConfig:
    sl_raw = runtime.get("strategy_lifecycle")
    if not isinstance(sl_raw, dict):
        return StrategyLifecycleConfig(enabled=False)

    phases_raw = sl_raw.get("block_new_entry_phases")
    if isinstance(phases_raw, (list, tuple)):
        phases = frozenset(str(p) for p in phases_raw)
    else:
        phases = frozenset({"near_close", "closed"})

    max_rt = sl_raw.get("max_runtime_s")
    max_runtime_s: float | None
    if max_rt in (None, "", "null"):
        max_runtime_s = None
    else:
        max_runtime_s = float(max_rt)

    fallback_raw = sl_raw.get("fallback_max_runtime_s", 900)
    fallback = float(fallback_raw) if fallback_raw not in (None, "", "null") else 900.0

    return StrategyLifecycleConfig(
        enabled=True,
        mode=str(sl_raw.get("mode", "market_aware")),
        exit_clock_source=str(sl_raw.get("exit_clock_source", "event_end_ts")),
        max_runtime_s=max_runtime_s,
        fallback_max_runtime_s=fallback,
        flatten_before_event_end_s=float(sl_raw.get("flatten_before_event_end_s", 20)),
        block_new_entry_phases=phases,
        emit_unknown_market_end_warning=bool(sl_raw.get("emit_unknown_market_end_warning", True)),
        min_survival_window_s=float(sl_raw.get("min_survival_window_s", 45)),
    )


@dataclass(frozen=True)
class RuntimeConfig:
    execution_mode: ExecutionMode
    reporting: ReportingConfig
    reconcile_interval_s: int
    #: Provisional repair window (s): age below this is non-blocking ``provisional_pending_venue``.
    submit_grace_s: float
    #: Provisional age (s) past which absent rows auto-resolve to ``UNKNOWN_TERMINAL`` (non-blocking)
    #: when WS is fresh and no venue restart suspected.
    provisional_unknown_terminal_timeout_s: float
    #: Back-compat alias kept for old configs / scripts; mirrors ``provisional_unknown_terminal_timeout_s``.
    venue_confirm_provisional_timeout_s: float
    #: Adoption window (s) for venue-truth mirror race: a venue order id that we don't yet track
    #: locally is matched against recent no-vid provisional rows submitted within this window.
    adoption_grace_s: float
    log_level: str
    shadow_bootstrap: ShadowBootstrapConfig | None
    allocation_ledger: AllocationLedgerConfig
    market_data: MarketDataConfig = MarketDataConfig()
    observability: ObservabilityConfig = ObservabilityConfig()
    recording: RecordingConfig = RecordingConfig()
    external_btc: ExternalBtcConfig = ExternalBtcConfig()
    reference_prices: ReferencePricesConfig = ReferencePricesConfig()
    paired_binary: PairedBinaryRuntimeConfig = PairedBinaryRuntimeConfig()
    strategy_lifecycle: StrategyLifecycleConfig = StrategyLifecycleConfig()


@dataclass(frozen=True)
class AppConfig:
    strategy: StrategyConfig
    risk: RiskConfig
    runtime: RuntimeConfig
    raw: dict[str, Any]
    #: Populated only when the loaded strategy YAML declares ``kind: sell_test``.
    #: Mutually exclusive with normal guru-follow operation; ``strategy`` then
    #: holds default placeholder values so risk gates and other code paths that
    #: read ``app.strategy.*`` remain stable.
    sell_test: SellTestStrategyConfig | None = None
    #: Populated only when the loaded strategy YAML declares ``kind: allocation_test``.
    allocation_test: AllocationTestStrategyConfig | None = None
    #: Populated only when the loaded strategy YAML declares ``kind: tp_sl_test``.
    tp_sl_test: TpSlTestStrategyConfig | None = None
    #: Populated only when the loaded strategy YAML declares ``kind: simple_signal_test``.
    simple_signal_test: SimpleSignalTestStrategyConfig | None = None
    validation_harness: ValidationHarnessStrategyConfig | None = None
    paired_binary: PairedBinaryStrategyConfig | None = None
    protection: ProtectionRuntimeConfig | None = None
    #: Execution layer config (P3 architecture_enhance): planner enable + book policy.
    execution: ExecutionConfig = ExecutionConfig()
    #: Phase 1 survival primitives — default disabled; no behavior change when false.
    survival: SurvivalConfig = SurvivalConfig()
    #: Loaded strategy YAML ``kind`` (selects the runtime loop in ``runtime/app.py``).
    strategy_kind: str = STRATEGY_KIND_GURU_FOLLOW


def _dec(d: dict[str, Any], key: str, default: str = "0") -> Decimal:
    v = d.get(key, default)
    return Decimal(str(v))


def _parse_venue_min_size(d: dict[str, Any]) -> VenueMinSizeConfig:
    """Parse the ``risk.venue_min_size`` block (defaults: enabled, deny, 5 shares).

    Polymarket's hard floor is 5 shares regardless of token. ``5`` is therefore the safe
    default when the operator omits the block entirely. ``policy`` accepts ``deny|bump``
    and falls back to ``deny`` for any other string (fail-closed).
    """
    raw_policy = str(d.get("policy", "deny") or "deny").lower().strip()
    policy = raw_policy if raw_policy in ("deny", "bump") else "deny"
    return VenueMinSizeConfig(
        enabled=bool(d.get("enabled", True)),
        policy=policy,
        default_min_size=_dec(d, "default_min_size", "5"),
    )


def _parse_conviction(d: dict[str, Any]) -> ConvictionConfig:
    if not d:
        return ConvictionConfig(
            enabled=False,
            score_min=Decimal("0"),
            score_max=Decimal("1"),
            min_multiplier=Decimal("1"),
            max_multiplier=Decimal("1"),
        )
    return ConvictionConfig(
        enabled=bool(d.get("enabled", False)),
        score_min=_dec(d, "score_min", "0"),
        score_max=_dec(d, "score_max", "1"),
        min_multiplier=_dec(d, "min_multiplier", "0.5"),
        max_multiplier=_dec(d, "max_multiplier", "2.0"),
    )


def _parse_order_style(raw: Any, default: OrderStyle = OrderStyle.GTC) -> OrderStyle:
    """Coerce a YAML ``order_style`` field to the enum (case-insensitive, fail-closed to default)."""
    if isinstance(raw, OrderStyle):
        return raw
    s = str(raw or "").strip().upper()
    if s in OrderStyle.__members__:
        return OrderStyle[s]
    return default


def _parse_pricing_mode(
    raw: Any,
    *,
    where: str,
    default: str = SELL_TEST_PRICING_FIXED,
) -> str:
    """Validate a ``pricing_mode`` field; default when missing/blank."""
    if raw in (None, ""):
        return default
    s = str(raw).strip().lower()
    if s not in _VALID_SELL_TEST_PRICING_MODES:
        raise ConfigError(
            f"sell_test {where}.pricing_mode '{raw}' is not supported "
            f"(valid: {', '.join(_VALID_SELL_TEST_PRICING_MODES)})"
        )
    return s


def _parse_sell_test_strategy(strategy: dict[str, Any]) -> SellTestStrategyConfig:
    """Parse a ``kind: sell_test`` strategy YAML.

    Required: ``token_id`` (canonical CLOB outcome token id). When
    ``buy.pricing_mode == "fixed"`` (the default) ``buy.limit_price`` is also
    required. Under ``buy.pricing_mode == "auto"`` ``limit_price`` is optional
    and used as a fallback if the venue book lookup fails.
    """
    enabled = bool(strategy.get("enabled", True))
    token_id = str(strategy.get("token_id", "")).strip()
    if not token_id:
        raise ConfigError("sell_test strategy requires non-empty top-level 'token_id'")
    buy_raw = strategy.get("buy") or {}
    sell_raw = strategy.get("sell") or {}

    buy_enabled = bool(buy_raw.get("enabled", True))
    buy_pricing_mode = _parse_pricing_mode(buy_raw.get("pricing_mode"), where="buy")
    buy_aggression = int(buy_raw.get("aggression_ticks", 1))
    if buy_aggression < 0:
        raise ConfigError("sell_test buy.aggression_ticks must be >= 0")
    buy_price_raw = buy_raw.get("limit_price")
    buy_max_price_raw = buy_raw.get("max_price")
    if buy_enabled and buy_pricing_mode == SELL_TEST_PRICING_FIXED and buy_price_raw in (None, ""):
        raise ConfigError(
            "sell_test buy.enabled with pricing_mode=fixed requires buy.limit_price"
        )
    buy_cfg = SellTestBuyConfig(
        enabled=buy_enabled,
        notional_usd=_dec(buy_raw, "notional_usd", "5"),
        limit_price=Decimal(str(buy_price_raw)) if buy_price_raw not in (None, "") else None,
        order_style=_parse_order_style(buy_raw.get("order_style"), OrderStyle.GTC),
        pricing_mode=buy_pricing_mode,
        aggression_ticks=buy_aggression,
        max_price=Decimal(str(buy_max_price_raw)) if buy_max_price_raw not in (None, "") else None,
    )

    sell_pricing_mode = _parse_pricing_mode(sell_raw.get("pricing_mode"), where="sell")
    sell_aggression = int(sell_raw.get("aggression_ticks", 1))
    if sell_aggression < 0:
        raise ConfigError("sell_test sell.aggression_ticks must be >= 0")
    sell_price_raw = sell_raw.get("limit_price")
    sell_min_price_raw = sell_raw.get("min_price")
    sell_cfg = SellTestSellConfig(
        enabled=bool(sell_raw.get("enabled", True)),
        delay_s=float(sell_raw.get("delay_s", 3)),
        order_style=_parse_order_style(sell_raw.get("order_style"), OrderStyle.GTC),
        limit_price=Decimal(str(sell_price_raw)) if sell_price_raw not in (None, "") else None,
        pricing_mode=sell_pricing_mode,
        aggression_ticks=sell_aggression,
        min_price=Decimal(str(sell_min_price_raw)) if sell_min_price_raw not in (None, "") else None,
    )
    return SellTestStrategyConfig(
        enabled=enabled,
        token_id=token_id,
        buy=buy_cfg,
        sell=sell_cfg,
        run_once=bool(strategy.get("run_once", True)),
    )


def _parse_tp_sl_test_strategy(strategy: dict[str, Any]) -> TpSlTestStrategyConfig:
    from tyrex_pm.runtime.allocation_ids import OWNER_TP_SL_TEST

    enabled = bool(strategy.get("enabled", True))
    token_id = str(strategy.get("token_id", "")).strip()
    if not token_id:
        raise ConfigError("tp_sl_test strategy requires non-empty top-level 'token_id'")
    owner_id = str(strategy.get("owner_id", OWNER_TP_SL_TEST)).strip()
    if not owner_id:
        raise ConfigError("tp_sl_test owner_id must be non-empty")

    buy_raw = strategy.get("buy") or {}
    buy_pricing_mode = _parse_pricing_mode(
        buy_raw.get("pricing_mode"),
        where="buy",
        default=SELL_TEST_PRICING_FIXED,
    )
    buy_aggression = int(buy_raw.get("aggression_ticks", 1))
    if buy_aggression < 0:
        raise ConfigError("tp_sl_test buy.aggression_ticks must be >= 0")
    buy_price_raw = buy_raw.get("limit_price")
    buy_enabled = bool(buy_raw.get("enabled", True))
    if buy_enabled and buy_pricing_mode == SELL_TEST_PRICING_FIXED and buy_price_raw in (None, ""):
        raise ConfigError("tp_sl_test buy.enabled with pricing_mode=fixed requires buy.limit_price")
    buy_max_raw = buy_raw.get("max_price")
    buy_cfg = TpSlTestBuyConfig(
        enabled=buy_enabled,
        notional_usd=_dec(buy_raw, "notional_usd", "5"),
        limit_price=Decimal(str(buy_price_raw)) if buy_price_raw not in (None, "") else None,
        order_style=_parse_order_style(buy_raw.get("order_style"), OrderStyle.GTC),
        pricing_mode=buy_pricing_mode,
        aggression_ticks=buy_aggression,
        max_price=Decimal(str(buy_max_raw)) if buy_max_raw not in (None, "") else None,
    )

    mon_raw = strategy.get("monitor") or {}
    price_source = str(mon_raw.get("price_source", TP_SL_PRICE_SOURCE_FIXTURE)).strip().lower()
    if price_source not in _VALID_TP_SL_PRICE_SOURCES:
        raise ConfigError(
            f"tp_sl_test monitor.price_source '{price_source}' is not supported "
            f"(valid: {', '.join(_VALID_TP_SL_PRICE_SOURCES)})"
        )
    if price_source == TP_SL_PRICE_SOURCE_MARK:
        raise ConfigError(
            "tp_sl_test monitor.price_source 'mark' is not implemented yet "
            "(use fixture or best_bid)"
        )
    trigger_mode = str(mon_raw.get("trigger_mode", TP_SL_TRIGGER_MODE_TP_OR_SL)).strip().lower()
    if trigger_mode not in _VALID_TP_SL_TRIGGER_MODES:
        raise ConfigError(
            f"tp_sl_test monitor.trigger_mode '{trigger_mode}' is not supported "
            f"(valid: {', '.join(_VALID_TP_SL_TRIGGER_MODES)})"
        )
    tp_raw = mon_raw.get("take_profit_price")
    sl_raw = mon_raw.get("stop_loss_price")
    tp_pct_raw = mon_raw.get("take_profit_pct")
    sl_pct_raw = mon_raw.get("stop_loss_pct")
    if tp_raw not in (None, "") and tp_pct_raw not in (None, ""):
        raise ConfigError(
            "tp_sl_test monitor cannot define both take_profit_price and take_profit_pct"
        )
    if sl_raw not in (None, "") and sl_pct_raw not in (None, ""):
        raise ConfigError(
            "tp_sl_test monitor cannot define both stop_loss_price and stop_loss_pct"
        )
    take_profit_pct = Decimal(str(tp_pct_raw)) if tp_pct_raw not in (None, "") else None
    stop_loss_pct = Decimal(str(sl_pct_raw)) if sl_pct_raw not in (None, "") else None
    if take_profit_pct is not None and take_profit_pct < 0:
        raise ConfigError("tp_sl_test monitor.take_profit_pct must be >= 0")
    if stop_loss_pct is not None and stop_loss_pct < 0:
        raise ConfigError("tp_sl_test monitor.stop_loss_pct must be >= 0")
    if stop_loss_pct is not None and stop_loss_pct >= 1:
        raise ConfigError("tp_sl_test monitor.stop_loss_pct must be < 1")
    monitor_enabled = bool(mon_raw.get("enabled", True))
    has_tp = tp_raw not in (None, "") or take_profit_pct is not None
    has_sl = sl_raw not in (None, "") or stop_loss_pct is not None
    if monitor_enabled and not has_tp and not has_sl:
        raise ConfigError(
            "tp_sl_test monitor requires at least one of "
            "take_profit_price, take_profit_pct, stop_loss_price, stop_loss_pct "
            "(or set monitor.enabled: false)"
        )
    trigger_ref_raw = mon_raw.get("trigger_reference")
    uses_pct = take_profit_pct is not None or stop_loss_pct is not None
    if uses_pct:
        trigger_reference = str(trigger_ref_raw or TP_SL_TRIGGER_REFERENCE_ENTRY).strip().lower()
        if trigger_reference not in _VALID_TP_SL_TRIGGER_REFERENCES:
            raise ConfigError(
                f"tp_sl_test monitor.trigger_reference '{trigger_reference}' is not supported "
                f"(valid: {', '.join(_VALID_TP_SL_TRIGGER_REFERENCES)})"
            )
    else:
        trigger_reference = (
            str(trigger_ref_raw).strip().lower() if trigger_ref_raw not in (None, "") else None
        )
        if trigger_reference is not None and trigger_reference not in _VALID_TP_SL_TRIGGER_REFERENCES:
            raise ConfigError(
                f"tp_sl_test monitor.trigger_reference '{trigger_reference}' is not supported "
                f"(valid: {', '.join(_VALID_TP_SL_TRIGGER_REFERENCES)})"
            )
    fixture_raw = mon_raw.get("fixture_prices") or []
    if price_source == TP_SL_PRICE_SOURCE_FIXTURE:
        if not isinstance(fixture_raw, list) or not fixture_raw:
            raise ConfigError(
                "tp_sl_test monitor.price_source=fixture requires non-empty monitor.fixture_prices"
            )
    fixture_prices = tuple(Decimal(str(x)) for x in fixture_raw) if fixture_raw else ()
    monitor_cfg = TpSlTestMonitorConfig(
        enabled=monitor_enabled,
        price_source=price_source,
        poll_interval_s=float(mon_raw.get("poll_interval_s", 1)),
        trigger_mode=trigger_mode,
        take_profit_price=Decimal(str(tp_raw)) if tp_raw not in (None, "") else None,
        stop_loss_price=Decimal(str(sl_raw)) if sl_raw not in (None, "") else None,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        trigger_reference=trigger_reference,
        fixture_prices=fixture_prices,
    )

    exit_raw = strategy.get("exit") or {}
    size_mode = str(exit_raw.get("size_mode", TP_SL_SIZE_MODE_FULL)).strip().lower()
    if size_mode not in _VALID_TP_SL_SIZE_MODES:
        raise ConfigError(
            f"tp_sl_test exit.size_mode '{size_mode}' is not supported "
            f"(valid: {', '.join(_VALID_TP_SL_SIZE_MODES)})"
        )
    fixed_raw = exit_raw.get("fixed_size")
    if size_mode == TP_SL_SIZE_MODE_FIXED and fixed_raw in (None, ""):
        raise ConfigError("tp_sl_test exit.size_mode=fixed_size requires exit.fixed_size")
    exit_pricing_mode = _parse_pricing_mode(
        exit_raw.get("pricing_mode"),
        where="exit",
        default=SELL_TEST_PRICING_FIXED,
    )
    exit_aggression = int(exit_raw.get("aggression_ticks", 1))
    if exit_aggression < 0:
        raise ConfigError("tp_sl_test exit.aggression_ticks must be >= 0")
    exit_price_raw = exit_raw.get("limit_price")
    exit_min_raw = exit_raw.get("min_price")
    exit_cfg = TpSlTestExitConfig(
        enabled=bool(exit_raw.get("enabled", True)),
        size_mode=size_mode,
        percent=_dec(exit_raw, "percent", "1.0"),
        fixed_size=Decimal(str(fixed_raw)) if fixed_raw not in (None, "") else None,
        order_style=_parse_order_style(exit_raw.get("order_style"), OrderStyle.GTC),
        pricing_mode=exit_pricing_mode,
        aggression_ticks=exit_aggression,
        min_price=Decimal(str(exit_min_raw)) if exit_min_raw not in (None, "") else None,
        limit_price=Decimal(str(exit_price_raw)) if exit_price_raw not in (None, "") else None,
    )

    to_raw = strategy.get("timeouts") or {}
    timeouts_cfg = TpSlTestTimeoutsConfig(
        inventory_timeout_s=float(to_raw.get("inventory_timeout_s", 90)),
        trigger_timeout_s=float(to_raw.get("trigger_timeout_s", 120)),
        completion_timeout_s=float(to_raw.get("completion_timeout_s", 120)),
    )

    return TpSlTestStrategyConfig(
        enabled=enabled,
        token_id=token_id,
        owner_id=owner_id,
        buy=buy_cfg,
        monitor=monitor_cfg,
        exit=exit_cfg,
        timeouts=timeouts_cfg,
        run_once=bool(strategy.get("run_once", True)),
    )


def _parse_simple_signal_test_strategy(strategy: dict[str, Any]) -> SimpleSignalTestStrategyConfig:
    from tyrex_pm.runtime.allocation_ids import OWNER_SIMPLE_SIGNAL_TEST

    enabled = bool(strategy.get("enabled", True))
    token_id = str(strategy.get("token_id", "")).strip()
    if not token_id:
        raise ConfigError("simple_signal_test strategy requires non-empty top-level 'token_id'")
    owner_id = str(strategy.get("owner_id", OWNER_SIMPLE_SIGNAL_TEST)).strip()
    if not owner_id:
        raise ConfigError("simple_signal_test owner_id must be non-empty")
    side_raw = str(strategy.get("side", "BUY")).strip().upper()
    if side_raw not in ("BUY", "SELL"):
        raise ConfigError("simple_signal_test side must be BUY or SELL")
    side = Side.BUY if side_raw == "BUY" else Side.SELL
    price_raw = strategy.get("limit_price")
    pricing_mode = _parse_pricing_mode(
        strategy.get("pricing_mode"),
        where="simple_signal_test",
        default=SELL_TEST_PRICING_FIXED,
    )
    max_price_raw = strategy.get("max_price")
    return SimpleSignalTestStrategyConfig(
        enabled=enabled,
        token_id=token_id,
        owner_id=owner_id,
        side=side,
        notional_usd=_dec(strategy, "notional_usd", "5"),
        limit_price=Decimal(str(price_raw)) if price_raw not in (None, "") else None,
        order_style=_parse_order_style(strategy.get("order_style"), OrderStyle.GTC),
        run_once=bool(strategy.get("run_once", True)),
        pricing_mode=pricing_mode,
        aggression_ticks=int(strategy.get("aggression_ticks", 2)),
        max_price=Decimal(str(max_price_raw)) if max_price_raw not in (None, "") else None,
    )


def _parse_paired_binary_strategy(strategy: dict[str, Any]) -> PairedBinaryStrategyConfig:
    from tyrex_pm.runtime.allocation_ids import OWNER_PAIRED_BINARY

    pb = strategy.get("paired_binary") or {}
    if not isinstance(pb, dict):
        pb = {}
    enabled = bool(strategy.get("enabled", pb.get("enabled", True)))
    owner_id = str(pb.get("owner_id", OWNER_PAIRED_BINARY)).strip()
    market_id = str(pb.get("market_id", "")).strip()
    yes_token_id = str(pb.get("yes_token_id", "")).strip()
    no_token_id = str(pb.get("no_token_id", "")).strip()
    if not market_id:
        raise ConfigError("paired_binary.market_id is required")
    if not yes_token_id or not no_token_id:
        raise ConfigError("paired_binary.yes_token_id and no_token_id are required")
    min_eff_raw = pb.get("min_effective_pair_qty")
    seed_raw = pb.get("seed_allocation_qty")
    fy_bid = pb.get("fixture_yes_bid")
    fy_ask = pb.get("fixture_yes_ask")
    fn_bid = pb.get("fixture_no_bid")
    fn_ask = pb.get("fixture_no_ask")
    _reject_paired_binary_deprecated_keys(pb)
    cond_raw = pb.get("condition_id")
    start_raw = pb.get("event_start_ts")
    end_raw = pb.get("event_end_ts")
    return PairedBinaryStrategyConfig(
        enabled=enabled,
        owner_id=owner_id,
        market_id=market_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        position_size=_dec(pb, "position_size", "100"),
        max_pair_entry_cost=_dec(pb, "max_pair_entry_cost", "1.02"),
        max_spread_yes=_dec(pb, "max_spread_yes", "0.02"),
        max_spread_no=_dec(pb, "max_spread_no", "0.02"),
        pair_stop_loss_pct=_dec(pb, "pair_stop_loss_pct", "0.02"),
        pair_take_profit_pct=_dec(pb, "pair_take_profit_pct", "0.05"),
        slippage_buffer=_dec(pb, "slippage_buffer", "0.005"),
        reject_if_spread_exceeds_loss_budget=bool(
            pb.get("reject_if_spread_exceeds_loss_budget", True)
        ),
        max_holding_time_s=float(pb.get("max_holding_time_s", 1800)),
        entry_order_style=_parse_order_style(pb.get("entry_order_style"), OrderStyle.GTC),
        exit_order_style=_parse_order_style(pb.get("exit_order_style"), OrderStyle.FAK),
        entry_fill_timeout_s=float(pb.get("entry_fill_timeout_s", 60)),
        abort_unpaired_entry=bool(pb.get("abort_unpaired_entry", True)),
        unwind_partial_entry=bool(pb.get("unwind_partial_entry", True)),
        min_effective_pair_qty=Decimal(str(min_eff_raw)) if min_eff_raw not in (None, "") else None,
        run_once=bool(pb.get("run_once", False)),
        max_markets=int(pb.get("max_markets", 1)),
        tick_interval_s=float(pb.get("tick_interval_s", 1.0)),
        max_book_age_s=float(pb.get("max_book_age_s", 5.0)),
        entry_dry_run=bool(pb.get("entry_dry_run", False)),
        stop_after_entry=bool(pb.get("stop_after_entry", False)),
        max_runtime_s=float(pb.get("max_runtime_s", 600)),
        use_fixture_book=bool(pb.get("use_fixture_book", False)),
        allow_seed_allocation=bool(pb.get("allow_seed_allocation", False)),
        seed_allocation_qty=Decimal(str(seed_raw)) if seed_raw not in (None, "") else None,
        fixture_yes_bid=Decimal(str(fy_bid)) if fy_bid not in (None, "") else None,
        fixture_yes_ask=Decimal(str(fy_ask)) if fy_ask not in (None, "") else None,
        fixture_no_bid=Decimal(str(fn_bid)) if fn_bid not in (None, "") else None,
        fixture_no_ask=Decimal(str(fn_ask)) if fn_ask not in (None, "") else None,
        activation_gap_retry_s=float(pb.get("activation_gap_retry_s", 0)),
        activation_gap_retry_interval_s=float(pb.get("activation_gap_retry_interval_s", 0.5)),
        activation_gap_max_retries=int(pb.get("activation_gap_max_retries", 6)),
        activation_unwind_retry_s=float(pb.get("activation_unwind_retry_s", 15)),
        activation_unwind_retry_interval_s=float(pb.get("activation_unwind_retry_interval_s", 0.5)),
        entry_price_mismatch_tolerance=_dec(pb, "entry_price_mismatch_tolerance", "0.005"),
        entry_price_mismatch_abort_threshold=(
            _dec(pb, "entry_price_mismatch_abort_threshold", "0.02")
            if pb.get("entry_price_mismatch_abort_threshold") not in (None, "")
            else None
        ),
        allow_entry_style_downgrade=bool(pb.get("allow_entry_style_downgrade", False)),
        allow_resting_entry_orders=bool(pb.get("allow_resting_entry_orders", False)),
        pair_entry_submit_timeout_s=float(pb.get("pair_entry_submit_timeout_s", pb.get("entry_fill_timeout_s", 5))),
        pair_entry_fill_timeout_s=float(pb.get("pair_entry_fill_timeout_s", pb.get("entry_fill_timeout_s", 10))),
        pair_entry_resting_timeout_s=float(pb.get("pair_entry_resting_timeout_s", 2)),
        condition_id=str(cond_raw).strip() if cond_raw not in (None, "") else None,
        event_start_ts=float(start_raw) if start_raw not in (None, "") else None,
        event_end_ts=float(end_raw) if end_raw not in (None, "") else None,
    )


def _parse_protection_block(raw: dict[str, Any] | None, *, where: str) -> ProtectionRuntimeConfig | None:
    if not raw or not isinstance(raw, dict):
        return None
    if not bool(raw.get("enabled", False)):
        return None
    from tyrex_pm.protection.config import SIZE_MODE_FULL

    tp_pct = raw.get("take_profit_pct")
    sl_pct = raw.get("stop_loss_pct")
    tp_price = raw.get("take_profit_price")
    sl_price = raw.get("stop_loss_price")
    fixed = raw.get("fixed_size")
    pct = raw.get("percent")
    exit_lim = raw.get("exit_limit_price")
    return ProtectionRuntimeConfig(
        enabled=True,
        take_profit_pct=Decimal(str(tp_pct)) if tp_pct not in (None, "") else None,
        stop_loss_pct=Decimal(str(sl_pct)) if sl_pct not in (None, "") else None,
        take_profit_price=Decimal(str(tp_price)) if tp_price not in (None, "") else None,
        stop_loss_price=Decimal(str(sl_price)) if sl_price not in (None, "") else None,
        size_mode=str(raw.get("size_mode", SIZE_MODE_FULL)),
        fixed_size=Decimal(str(fixed)) if fixed not in (None, "") else None,
        percent=Decimal(str(pct)) if pct not in (None, "") else None,
        exit_order_style=_parse_order_style(raw.get("exit_order_style"), OrderStyle.FAK),
        exit_limit_price=Decimal(str(exit_lim)) if exit_lim not in (None, "") else None,
        max_book_age_s=float(raw.get("max_book_age_s", 5)),
        register_on_buy=bool(raw.get("register_on_buy", True)),
        tick_interval_s=float(raw.get("tick_interval_s", 1)),
        max_runtime_s=float(raw.get("max_runtime_s", 180)),
        stop_after_trigger=bool(raw.get("stop_after_trigger", True)),
        stop_after_exit_submit=bool(raw.get("stop_after_exit_submit", True)),
        fail_if_no_trigger=bool(raw.get("fail_if_no_trigger", False)),
    )


def _parse_validation_harness_strategy(strategy: dict[str, Any]) -> ValidationHarnessStrategyConfig:
    from tyrex_pm.runtime.allocation_ids import OWNER_VALIDATION_HARNESS

    enabled = bool(strategy.get("enabled", True))
    v = strategy.get("validation") or {}
    if not isinstance(v, dict):
        v = {}
    mode = str(v.get("mode", VALIDATION_MODE_NORMAL_ENTRY)).strip().lower()
    if mode not in _VALID_VALIDATION_MODES:
        raise ConfigError(
            f"validation_harness mode '{mode}' unsupported "
            f"(valid: {', '.join(_VALID_VALIDATION_MODES)})"
        )
    token_id = str(v.get("token_id") or strategy.get("token_id", "")).strip()
    if not token_id and mode != VALIDATION_MODE_MARKET_DATA_READONLY:
        raise ConfigError("validation_harness requires validation.token_id or top-level token_id")
    owner_id = str(v.get("owner_id", OWNER_VALIDATION_HARNESS)).strip()
    side_raw = str(v.get("side", "BUY")).strip().upper()
    if side_raw not in ("BUY", "SELL"):
        raise ConfigError("validation_harness side must be BUY or SELL")
    side = Side.BUY if side_raw == "BUY" else Side.SELL
    price_raw = v.get("limit_price")
    size_raw = v.get("size")
    notional_raw = v.get("notional_usd")
    entry_raw = v.get("entry_price")
    seed_raw = v.get("seed_allocation_qty")
    bid_raw = v.get("fixture_book_bid")
    ask_raw = v.get("fixture_book_ask")
    pricing_mode = _parse_pricing_mode(
        v.get("pricing_mode"),
        where="validation_harness",
        default=SELL_TEST_PRICING_FIXED,
    )
    max_price_raw = v.get("max_price")
    return ValidationHarnessStrategyConfig(
        enabled=enabled,
        mode=mode,
        owner_id=owner_id,
        token_id=token_id,
        side=side,
        notional_usd=Decimal(str(notional_raw)) if notional_raw not in (None, "") else None,
        limit_price=Decimal(str(price_raw)) if price_raw not in (None, "") else None,
        size=Decimal(str(size_raw)) if size_raw not in (None, "") else None,
        urgency=str(v.get("urgency", "normal")),
        order_style=_parse_order_style(v.get("order_style"), OrderStyle.GTC),
        run_once=bool(v.get("run_once", True)),
        pricing_mode=pricing_mode,
        aggression_ticks=int(v.get("aggression_ticks", 2)),
        max_price=Decimal(str(max_price_raw)) if max_price_raw not in (None, "") else None,
        entry_price=Decimal(str(entry_raw)) if entry_raw not in (None, "") else None,
        seed_allocation_qty=Decimal(str(seed_raw)) if seed_raw not in (None, "") else None,
        fixture_book_bid=Decimal(str(bid_raw)) if bid_raw not in (None, "") else None,
        fixture_book_ask=Decimal(str(ask_raw)) if ask_raw not in (None, "") else None,
        market_data_readonly_seconds=float(v.get("market_data_readonly_seconds", 3.0)),
        use_fixture_book=bool(v.get("use_fixture_book", False)),
        allow_seed_allocation=bool(v.get("allow_seed_allocation", False)),
        wait_for_confirmed=bool(v.get("wait_for_confirmed", False)),
        confirmed_timeout_s=float(v.get("confirmed_timeout_s", 90)),
        fail_if_not_confirmed=bool(v.get("fail_if_not_confirmed", True)),
    )


_VALID_ALLOCATION_TEST_SIZE_MODES = ("match_owner_a_buy", "fixed")


def _parse_allocation_test_strategy(strategy: dict[str, Any]) -> AllocationTestStrategyConfig:
    from tyrex_pm.runtime.allocation_ids import (
        DEFAULT_ALLOCATION_TEST_OWNER_A,
        DEFAULT_ALLOCATION_TEST_OWNER_B,
    )

    enabled = bool(strategy.get("enabled", True))
    token_id = str(strategy.get("token_id", "")).strip()
    if not token_id:
        raise ConfigError("allocation_test strategy requires non-empty top-level 'token_id'")
    owner_a_id = str(strategy.get("owner_a_id", DEFAULT_ALLOCATION_TEST_OWNER_A)).strip()
    owner_b_id = str(strategy.get("owner_b_id", DEFAULT_ALLOCATION_TEST_OWNER_B)).strip()
    if not owner_a_id or not owner_b_id:
        raise ConfigError("allocation_test owner_a_id and owner_b_id must be non-empty")
    if owner_a_id == owner_b_id:
        raise ConfigError("allocation_test owner_a_id and owner_b_id must differ")

    buy_raw = strategy.get("buy") or {}
    buy_enabled = bool(buy_raw.get("enabled", True))
    buy_price_raw = buy_raw.get("limit_price")
    if buy_enabled and buy_price_raw in (None, ""):
        raise ConfigError("allocation_test buy.enabled requires buy.limit_price")
    buy_cfg = AllocationTestBuyConfig(
        enabled=buy_enabled,
        notional_usd=_dec(buy_raw, "notional_usd", "5"),
        limit_price=Decimal(str(buy_price_raw)) if buy_price_raw not in (None, "") else None,
        order_style=_parse_order_style(buy_raw.get("order_style"), OrderStyle.GTC),
    )

    ob_raw = strategy.get("owner_b_unauthorized_sell") or {}
    size_mode = str(ob_raw.get("size_mode", "match_owner_a_buy")).strip().lower()
    if size_mode not in _VALID_ALLOCATION_TEST_SIZE_MODES:
        raise ConfigError(
            f"allocation_test owner_b_unauthorized_sell.size_mode '{size_mode}' is not supported "
            f"(valid: {', '.join(_VALID_ALLOCATION_TEST_SIZE_MODES)})"
        )
    owner_b_cfg = AllocationTestOwnerBConfig(
        enabled=bool(ob_raw.get("enabled", True)),
        size_mode=size_mode,
        fixed_size=_dec(ob_raw, "fixed_size", "10"),
    )

    sell_raw = strategy.get("owner_a_sell") or {}
    sell_price_raw = sell_raw.get("limit_price")
    sell_pricing_mode = _parse_pricing_mode(
        sell_raw.get("pricing_mode"),
        where="owner_a_sell",
        default=SELL_TEST_PRICING_AUTO,
    )
    sell_aggression = int(sell_raw.get("aggression_ticks", 0))
    if sell_aggression < 0:
        raise ConfigError("allocation_test owner_a_sell.aggression_ticks must be >= 0")
    sell_min_price_raw = sell_raw.get("min_price")
    owner_a_sell_cfg = AllocationTestOwnerASellConfig(
        enabled=bool(sell_raw.get("enabled", True)),
        delay_s=float(sell_raw.get("delay_s", 0)),
        order_style=_parse_order_style(sell_raw.get("order_style"), OrderStyle.GTC),
        limit_price=Decimal(str(sell_price_raw)) if sell_price_raw not in (None, "") else None,
        pricing_mode=sell_pricing_mode,
        aggression_ticks=sell_aggression,
        min_price=Decimal(str(sell_min_price_raw)) if sell_min_price_raw not in (None, "") else None,
    )

    to_raw = strategy.get("timeouts") or {}
    timeouts_cfg = AllocationTestTimeoutsConfig(
        allocation_visible_s=float(to_raw.get("allocation_visible_s", 5)),
        position_visible_s=float(to_raw.get("position_visible_s", 90)),
        unauthorized_sell_timeout_s=float(to_raw.get("unauthorized_sell_timeout_s", 10)),
        owner_a_exit_timeout_s=float(to_raw.get("owner_a_exit_timeout_s", 120)),
    )

    return AllocationTestStrategyConfig(
        enabled=enabled,
        token_id=token_id,
        owner_a_id=owner_a_id,
        owner_b_id=owner_b_id,
        buy=buy_cfg,
        owner_b_unauthorized_sell=owner_b_cfg,
        owner_a_sell=owner_a_sell_cfg,
        run_once=bool(strategy.get("run_once", True)),
        timeouts=timeouts_cfg,
    )


def _build_risk_runtime(risk: dict[str, Any], runtime: dict[str, Any]) -> tuple[RiskConfig, RuntimeConfig]:
    n = risk.get("notional") or {}
    d = risk.get("deployment") or {}
    c = risk.get("capital") or {}
    inv = risk.get("inventory") or {}
    ks = risk.get("kill_switch") or {}
    co = risk.get("concurrency") or {}
    rd = risk.get("readiness") or {}
    vms = risk.get("venue_min_size") or {}
    ex = risk.get("exits") or {}

    mp_raw = str(n.get("max_policy", "deny") or "deny").lower().strip()
    max_policy = mp_raw if mp_raw in ("cap", "deny") else "deny"
    rsk = RiskConfig(
        notional=NotionalConfig(
            min_usd=_dec(n, "min_usd"),
            max_usd=_dec(n, "max_usd"),
            max_policy=max_policy,
        ),
        deployment=DeploymentConfig(
            token_cap_usd=_dec(d, "token_cap_usd"),
            portfolio_cap_usd=_dec(d, "portfolio_cap_usd"),
        ),
        capital=CapitalConfig(
            enabled=bool(c.get("enabled", True)),
            max_wallet_age_s=int(c.get("max_wallet_age_s", 120)),
        ),
        inventory=InventoryConfig(
            sell_requires_venue_position=bool(inv.get("sell_requires_venue_position", True)),
        ),
        kill_switch=KillSwitchConfig(enabled=bool(ks.get("enabled", False))),
        concurrency=ConcurrencyConfig(max_orders_in_flight=int(co.get("max_orders_in_flight", 8))),
        readiness=ReadinessConfig(
            require_wallet_sync=bool(rd.get("require_wallet_sync", True)),
            max_wallet_age_s_live=int(rd.get("max_wallet_age_s_live", 60)),
            require_heartbeat_live=bool(rd.get("require_heartbeat_live", True)),
            require_user_ws_live=bool(rd.get("require_user_ws_live", True)),
        ),
        venue_min_size=_parse_venue_min_size(vms),
        exits=RiskExitsConfig(
            allow_reduce_only_mark_fallback=bool(ex.get("allow_reduce_only_mark_fallback", True)),
            require_fresh_book_for_mark_fallback=bool(
                ex.get("require_fresh_book_for_mark_fallback", True)
            ),
            urgent_exit_max_book_age_s=float(ex.get("urgent_exit_max_book_age_s", 0.5)),
        ),
    )

    em = str(runtime.get("execution_mode", "shadow")).lower()
    execution_mode = ExecutionMode.LIVE if em == "live" else ExecutionMode.SHADOW
    rep = runtime.get("reporting") or {}
    sup = runtime.get("supervisors") or {}
    log = runtime.get("logging") or {}
    sb_raw = runtime.get("shadow_bootstrap")
    shadow_boot: ShadowBootstrapConfig | None = None
    if isinstance(sb_raw, dict) and sb_raw:
        shadow_boot = ShadowBootstrapConfig(
            usdc_balance=_dec(sb_raw, "usdc_balance", "0"),
            usdc_allowance=_dec(sb_raw, "usdc_allowance", "0"),
        )

    submit_grace = float(sup.get("submit_grace_s", 15))
    unknown_terminal = sup.get("provisional_unknown_terminal_timeout_s")
    if unknown_terminal is None:
        unknown_terminal = sup.get("venue_confirm_provisional_timeout_s", 60)
    unknown_terminal = float(unknown_terminal)
    adoption_grace = float(sup.get("adoption_grace_s", 5))
    al = runtime.get("allocation_ledger")
    if al is None:
        al = {}
    if isinstance(al, dict) and al.get("enabled") is False:
        raise ConfigError(
            "allocation_ledger.enabled=false is not supported; the allocation ledger is required"
        )
    md_raw = runtime.get("market_data") or {}
    md_tokens = md_raw.get("token_ids") or []
    if not isinstance(md_tokens, list):
        md_tokens = []
    ws_raw = md_raw.get("websocket") or {}
    rest_raw = md_raw.get("rest") or {}
    feat_raw = md_raw.get("features") or {}
    qual_raw = md_raw.get("quality") or {}
    health_raw = md_raw.get("health") or {}
    eb_raw = md_raw.get("event_backbone") or {}
    profiles_raw = md_raw.get("market_profiles")
    obs_raw = runtime.get("observability") or {}
    rec_raw = runtime.get("recording") or {}
    ext_btc_raw = runtime.get("external_btc") or {}
    ref_prices_raw = runtime.get("reference_prices") or {}
    pb_rt_raw = runtime.get("paired_binary") or {}
    mtd_raw = pb_rt_raw.get("market_timing_diagnostics") or {}
    if not isinstance(mtd_raw, dict):
        mtd_raw = {}
    market_timing_diagnostics = MarketTimingDiagnosticsConfig(
        enabled=bool(mtd_raw.get("enabled", True)),
        near_close_window_s=float(mtd_raw.get("near_close_window_s", 45)),
    )
    market_data = MarketDataConfig(
        enabled=bool(md_raw.get("enabled", False)),
        max_book_age_s=float(md_raw.get("max_book_age_s", 5)),
        token_ids=tuple(str(x) for x in md_tokens),
        store_top_n_levels=int(md_raw.get("store_top_n_levels", 5)),
        websocket=MarketDataWebSocketConfig(
            primary_enabled=bool(ws_raw.get("primary_enabled", False)),
            shadow_enabled=bool(ws_raw.get("shadow_enabled", False)),
            url=str(ws_raw.get("url", "wss://ws-subscriptions-clob.polymarket.com/ws/market")),
            reconnect_backoff_s=float(ws_raw.get("reconnect_backoff_s", 3.0)),
            compare_interval_s=float(ws_raw.get("compare_interval_s", 5.0)),
        ),
        rest=MarketDataRestConfig(
            poll_enabled=bool(rest_raw.get("poll_enabled", True)),
            bootstrap_on_startup=bool(rest_raw.get("bootstrap_on_startup", True)),
            recovery_on_reconnect=bool(rest_raw.get("recovery_on_reconnect", True)),
        ),
        features=MarketDataFeaturesConfig(v0_enabled=bool(feat_raw.get("v0_enabled", True))),
        quality=MarketDataQualityConfig(
            enforcement_mode=str(qual_raw.get("enforcement_mode", "observe_only")),
            market_profile=str(qual_raw.get("market_profile", "crypto_5m")),
            require_ws_primary_for_entry=bool(qual_raw.get("require_ws_primary_for_entry", True)),
            allow_rest_recovery_for_exit=bool(qual_raw.get("allow_rest_recovery_for_exit", True)),
            allow_rest_recovery_for_entry=bool(qual_raw.get("allow_rest_recovery_for_entry", False)),
        ),
        health=MarketDataHealthConfig(
            max_ws_disconnect_s=float(health_raw.get("max_ws_disconnect_s", 30.0)),
            max_reconnects_per_hour=int(health_raw.get("max_reconnects_per_hour", 10)),
            max_p95_book_age_ms=int(health_raw.get("max_p95_book_age_ms", 2000)),
        ),
        event_backbone=MarketDataEventBackboneConfig(
            enabled=bool(eb_raw.get("enabled", False)),
            reorder_buffer_ms=float(eb_raw.get("reorder_buffer_ms", 75)),
            emit_rest_recovery=bool(eb_raw.get("emit_rest_recovery", True)),
        ),
        market_profiles=dict(profiles_raw) if isinstance(profiles_raw, dict) else None,
    )
    strategy_lifecycle = _parse_strategy_lifecycle_config(runtime)
    rt = RuntimeConfig(
        execution_mode=execution_mode,
        reporting=ReportingConfig(
            enabled=bool(rep.get("enabled", True)),
            runs_dir=str(rep.get("runs_dir", "var/reporting/runs")),
        ),
        reconcile_interval_s=int(sup.get("reconcile_interval_s", 30)),
        submit_grace_s=submit_grace,
        provisional_unknown_terminal_timeout_s=unknown_terminal,
        venue_confirm_provisional_timeout_s=unknown_terminal,
        adoption_grace_s=adoption_grace,
        log_level=str(log.get("level", "INFO")),
        shadow_bootstrap=shadow_boot,
        allocation_ledger=AllocationLedgerConfig(
            clamp_grace_s_after_buy=float(al.get("clamp_grace_s_after_buy", 90)),
        ),
        market_data=market_data,
        observability=ObservabilityConfig(
            emit_decision_snapshot=bool(obs_raw.get("emit_decision_snapshot", True)),
            sample_raw_events=int(obs_raw.get("sample_raw_events", 0)),
            emit_event_correlation=bool(obs_raw.get("emit_event_correlation", False)),
        ),
        recording=RecordingConfig(
            enabled=bool(rec_raw.get("enabled", False)),
            output_dir=str(rec_raw.get("output_dir", "var/recordings")),
            segment_max_mb=int(rec_raw.get("segment_max_mb", 64)),
            segment_max_s=int(rec_raw.get("segment_max_s", 300)),
            queue_maxsize=int(rec_raw.get("queue_maxsize", 10_000)),
            batch_size=int(rec_raw.get("batch_size", 100)),
            batch_flush_ms=int(rec_raw.get("batch_flush_ms", 50)),
            market_id=str(rec_raw["market_id"]) if rec_raw.get("market_id") else None,
            yes_token_id=str(rec_raw["yes_token_id"]) if rec_raw.get("yes_token_id") else None,
            no_token_id=str(rec_raw["no_token_id"]) if rec_raw.get("no_token_id") else None,
            discovery_enabled=bool(rec_raw.get("discovery_enabled", False)),
            discovery_poll_s=float(rec_raw.get("discovery_poll_s", 25.0)),
            heartbeat_stall_s=float(rec_raw.get("heartbeat_stall_s", 120.0)),
            heartbeat_interval_s=float(rec_raw.get("heartbeat_interval_s", 30.0)),
            compress=bool(rec_raw.get("compress", False)),
            tap_in_live=bool(rec_raw.get("tap_in_live", False)),
            post_close_grace_s=float(rec_raw.get("post_close_grace_s", 60.0)),
            skip_expired_markets=bool(rec_raw.get("skip_expired_markets", True)),
            pre_open_recording_lead_s=float(rec_raw.get("pre_open_recording_lead_s", 60.0)),
        ),
        external_btc=ExternalBtcConfig(
            enabled=bool(ext_btc_raw.get("enabled", False)),
            symbol=str(ext_btc_raw.get("symbol", "BTCUSDT")),
            streams=tuple(str(s) for s in (ext_btc_raw.get("streams") or ["bookTicker", "aggTrade"])),
            venue=str(ext_btc_raw.get("venue", "binance")),
            reconnect_backoff_s=float(ext_btc_raw.get("reconnect_backoff_s", 3.0)),
            clock_sync_interval_s=float(ext_btc_raw.get("clock_sync_interval_s", 60.0)),
        ),
        reference_prices=ReferencePricesConfig(
            enabled=bool(ref_prices_raw.get("enabled", False)),
            venue=str(ref_prices_raw.get("venue", "polymarket_rtds")),
            feeds=tuple(str(f) for f in (ref_prices_raw.get("feeds") or ["chainlink"])),
            symbols=tuple(str(s) for s in (ref_prices_raw.get("symbols") or ["btc/usd"])),
            emit_price_to_beat=bool(ref_prices_raw.get("emit_price_to_beat", True)),
            price_to_beat_max_lag_ms=float(ref_prices_raw.get("price_to_beat_max_lag_ms", 5000.0)),
            reconnect_backoff_s=float(ref_prices_raw.get("reconnect_backoff_s", 3.0)),
        ),
        paired_binary=PairedBinaryRuntimeConfig(
            poll_interval_s=float(pb_rt_raw.get("poll_interval_s", 1.0)),
            max_decision_rate_per_market_ms=int(pb_rt_raw.get("max_decision_rate_per_market_ms", 75)),
            open_exposure_on_max_runtime=_normalize_open_exposure_policy(
                pb_rt_raw.get("open_exposure_on_max_runtime")
                or pb_rt_raw.get("survivor_on_max_runtime", OPEN_EXPOSURE_ON_MAX_RUNTIME_FORCE)
            ),
            open_exposure_timeout_s=float(
                pb_rt_raw.get("open_exposure_timeout_s", pb_rt_raw.get("survivor_timeout_s", 300))
            ),
            survivor_on_max_runtime=str(
                pb_rt_raw.get("survivor_on_max_runtime", OPEN_EXPOSURE_ON_MAX_RUNTIME_FORCE)
            ),
            survivor_timeout_s=float(pb_rt_raw.get("survivor_timeout_s", 300)),
            allow_terminal_state_resume=bool(pb_rt_raw.get("allow_terminal_state_resume", False)),
            market_timing_diagnostics=market_timing_diagnostics,
            stop_background_tasks_after_strategy_done=bool(
                pb_rt_raw.get("stop_background_tasks_after_strategy_done", False)
            ),
        ),
        strategy_lifecycle=strategy_lifecycle,
    )
    return rsk, rt


def _parse_execution_config(
    runtime: dict[str, Any], market_data: MarketDataConfig
) -> ExecutionConfig:
    ex_raw = runtime.get("execution") or {}
    pl_raw = ex_raw.get("planner") or {}
    planner = ExecutionPlannerConfig(
        enabled=bool(pl_raw.get("enabled", False)),
        require_fresh_book_for_urgent=bool(pl_raw.get("require_fresh_book_for_urgent", True)),
        max_book_age_s=float(pl_raw.get("max_book_age_s", market_data.max_book_age_s)),
        allow_urgent_exit_fallback=bool(pl_raw.get("allow_urgent_exit_fallback", False)),
        use_executable_depth=bool(pl_raw.get("use_executable_depth", True)),
        max_slippage_vs_touch=float(pl_raw.get("max_slippage_vs_touch", 0.05)),
    )
    if planner.enabled and not market_data.enabled:
        raise ConfigError(
            "execution.planner.enabled requires market_data.enabled "
            "(the planner needs a market data provider)"
        )
    fr_raw = ex_raw.get("fill_reconciliation") or {}
    fill_reconciliation = FillReconciliationConfig(
        price_tolerance=float(fr_raw.get("price_tolerance", 0.001)),
        cash_tolerance_usd=float(fr_raw.get("cash_tolerance_usd", 0.01)),
        require_final_for_realized_pnl=bool(fr_raw.get("require_final_for_realized_pnl", True)),
    )
    return ExecutionConfig(planner=planner, fill_reconciliation=fill_reconciliation)


def _finalize_app_config(
    strat: StrategyConfig,
    risk: dict[str, Any],
    runtime: dict[str, Any],
    strategy_raw: dict[str, Any],
    sell_test: SellTestStrategyConfig | None,
    allocation_test: AllocationTestStrategyConfig | None = None,
    tp_sl_test: TpSlTestStrategyConfig | None = None,
    simple_signal_test: SimpleSignalTestStrategyConfig | None = None,
    validation_harness: ValidationHarnessStrategyConfig | None = None,
    protection: ProtectionRuntimeConfig | None = None,
    paired_binary: PairedBinaryStrategyConfig | None = None,
    strategy_kind: str = STRATEGY_KIND_GURU_FOLLOW,
) -> AppConfig:
    rsk, rt = _build_risk_runtime(risk, runtime)
    execution = _parse_execution_config(runtime, rt.market_data)
    survival = _parse_survival_config(runtime.get("survival"))
    raw = {"risk": risk, "strategy": strategy_raw, "runtime": runtime}
    if survival.enabled or runtime.get("survival"):
        raw["survival"] = runtime.get("survival") or {"enabled": survival.enabled}
    app = AppConfig(
        strategy=strat,
        risk=rsk,
        runtime=rt,
        raw=raw,
        sell_test=sell_test,
        allocation_test=allocation_test,
        tp_sl_test=tp_sl_test,
        simple_signal_test=simple_signal_test,
        validation_harness=validation_harness,
        paired_binary=paired_binary,
        protection=protection,
        execution=execution,
        survival=survival,
        strategy_kind=strategy_kind,
    )
    from tyrex_pm.runtime.paired_binary_live import (
        validate_paired_binary_live_config,
        validate_paired_binary_required_wiring,
    )
    from tyrex_pm.runtime.validation_harness_live import validate_validation_harness_live_config

    validate_validation_harness_live_config(app)
    validate_paired_binary_live_config(app)
    validate_paired_binary_required_wiring(app)
    return app


def _placeholder_guru_strategy_config() -> StrategyConfig:
    """A neutral StrategyConfig used when the loaded YAML is sell_test-only.

    Risk + pipeline code reads ``app.strategy.*`` (e.g. ``exits.demo_forced_exit_enabled``)
    unconditionally. The sell_test path does not need any of it, so we synthesize a
    no-op StrategyConfig so those code paths stay safe instead of branching on None.
    """
    return StrategyConfig(
        guru=GuruConfig(
            wallet="",
            data_api_poll_interval_s=5.0,
            data_api_limit=50,
            data_api_max_pages_per_poll=5,
        ),
        filters=FiltersConfig(
            token_allowlist=frozenset(),
            min_notional_usd=Decimal("0"),
            significance_min_notional_usd=Decimal("0"),
            min_conviction_score=Decimal("-1000000000"),
            exclude_untradeable_markets=False,
        ),
        sizing=SizingConfig(
            copy_scale=Decimal("1"),
            conviction=ConvictionConfig(
                enabled=False,
                score_min=Decimal("0"),
                score_max=Decimal("1"),
                min_multiplier=Decimal("1"),
                max_multiplier=Decimal("1"),
            ),
            static_enabled=False,
            static_amount_usd=Decimal("0"),
        ),
        exits=ExitsConfig(
            dust_notional_usd=Decimal("0.5"),
            sell_mode="proportional_to_guru",
            demo_forced_exit_enabled=False,
            demo_forced_exit_delay_s=3.0,
        ),
    )


def parse_app_config(*, risk: dict[str, Any], strategy: dict[str, Any], runtime: dict[str, Any]) -> AppConfig:
    kind_raw = str(strategy.get("kind", STRATEGY_KIND_GURU_FOLLOW) or STRATEGY_KIND_GURU_FOLLOW).strip().lower()
    if kind_raw not in _VALID_STRATEGY_KINDS:
        raise ConfigError(
            f"strategy.kind '{kind_raw}' is not supported (valid: {', '.join(_VALID_STRATEGY_KINDS)})"
        )
    sell_test_cfg: SellTestStrategyConfig | None = None
    allocation_test_cfg: AllocationTestStrategyConfig | None = None
    if kind_raw == STRATEGY_KIND_SELL_TEST:
        sell_test_cfg = _parse_sell_test_strategy(strategy)
        strat = _placeholder_guru_strategy_config()
        return _finalize_app_config(
            strat, risk, runtime, strategy, sell_test_cfg, strategy_kind=kind_raw
        )
    if kind_raw == STRATEGY_KIND_ALLOCATION_TEST:
        allocation_test_cfg = _parse_allocation_test_strategy(strategy)
        strat = _placeholder_guru_strategy_config()
        return _finalize_app_config(
            strat, risk, runtime, strategy, None, allocation_test_cfg, strategy_kind=kind_raw
        )
    if kind_raw == STRATEGY_KIND_TP_SL_TEST:
        tp_sl_test_cfg = _parse_tp_sl_test_strategy(strategy)
        strat = _placeholder_guru_strategy_config()
        return _finalize_app_config(
            strat, risk, runtime, strategy, None, None, tp_sl_test_cfg, strategy_kind=kind_raw
        )
    if kind_raw == STRATEGY_KIND_SIMPLE_SIGNAL_TEST:
        simple_cfg = _parse_simple_signal_test_strategy(strategy)
        strat = _placeholder_guru_strategy_config()
        return _finalize_app_config(
            strat,
            risk,
            runtime,
            strategy,
            None,
            None,
            None,
            simple_signal_test=simple_cfg,
            strategy_kind=kind_raw,
        )
    if kind_raw == STRATEGY_KIND_VALIDATION_HARNESS:
        vh_cfg = _parse_validation_harness_strategy(strategy)
        prot = _parse_protection_block(strategy.get("protection"), where="validation_harness")
        if prot is None:
            prot = _parse_protection_block(runtime.get("protection"), where="runtime")
        strat = _placeholder_guru_strategy_config()
        return _finalize_app_config(
            strat,
            risk,
            runtime,
            strategy,
            None,
            None,
            None,
            None,
            vh_cfg,
            prot,
            strategy_kind=kind_raw,
        )
    if kind_raw == STRATEGY_KIND_PAIRED_BINARY:
        pb_cfg = _parse_paired_binary_strategy(strategy)
        strat = _placeholder_guru_strategy_config()
        return _finalize_app_config(
            strat,
            risk,
            runtime,
            strategy,
            None,
            None,
            None,
            None,
            None,
            None,
            paired_binary=pb_cfg,
            strategy_kind=kind_raw,
        )

    g = strategy.get("guru") or {}
    f = strategy.get("filters") or {}
    sz = strategy.get("sizing") or {}
    ex = strategy.get("exits") or {}

    allow = f.get("token_allowlist") or []
    if not isinstance(allow, list):
        allow = []
    strat = StrategyConfig(
        guru=GuruConfig(
            wallet=str(g.get("wallet", "")),
            data_api_poll_interval_s=float(g.get("data_api_poll_interval_s", 5)),
            data_api_limit=int(g.get("data_api_limit", 50)),
            data_api_max_pages_per_poll=int(g.get("data_api_max_pages_per_poll", 5)),
        ),
        filters=FiltersConfig(
            token_allowlist=frozenset(str(x) for x in allow),
            min_notional_usd=_dec(f, "min_notional_usd", "0"),
            significance_min_notional_usd=_dec(f, "significance_min_notional_usd", "0"),
            min_conviction_score=_dec(f, "min_conviction_score", "-1000000000"),
            exclude_untradeable_markets=bool(f.get("exclude_untradeable_markets", False)),
        ),
        sizing=SizingConfig(
            copy_scale=_dec(sz, "copy_scale", "1"),
            conviction=_parse_conviction(sz.get("conviction") or {}),
            static_enabled=bool(sz.get("static_enabled", False)),
            static_amount_usd=_dec(sz, "static_amount_usd", "0"),
        ),
        exits=ExitsConfig(
            dust_notional_usd=_dec(ex, "dust_notional_usd", "0.5"),
            sell_mode=str(ex.get("sell_mode", "proportional_to_guru")),
            demo_forced_exit_enabled=bool(ex.get("demo_forced_exit_enabled", False)),
            demo_forced_exit_delay_s=float(ex.get("demo_forced_exit_delay_s", 3)),
        ),
    )

    rsk, rt = _build_risk_runtime(risk, runtime)
    execution = _parse_execution_config(runtime, rt.market_data)
    raw = {"risk": risk, "strategy": strategy, "runtime": runtime}
    return AppConfig(
        strategy=strat,
        risk=rsk,
        runtime=rt,
        raw=raw,
        sell_test=None,
        execution=execution,
        strategy_kind=STRATEGY_KIND_GURU_FOLLOW,
    )


def _resolve_scenario_path(repo_root: Path, scenario_file: str | None) -> str | None:
    """Bare name `shadow_guru` → `config/scenarios/shadow_guru.yaml` under repo_root."""
    if scenario_file is None:
        return None
    raw = scenario_file.strip()
    p = Path(raw)
    if p.is_absolute():
        return raw
    if "/" in raw or "\\" in raw:
        return str(repo_root / raw)
    name = raw if raw.endswith(".yaml") else f"{raw}.yaml"
    return str(repo_root / "config" / "scenarios" / name)


def load_app_config(
    *,
    repo_root: Path,
    strategy_file: str = "config/strategies/guru_follow.yaml",
    scenario_file: str | None = None,
) -> AppConfig:
    scenario_file = _resolve_scenario_path(repo_root, scenario_file)
    risk_p = repo_root / "config" / "risk" / "default.yaml"
    rt_p = repo_root / "config" / "runtime" / "default.yaml"
    st_p = repo_root / strategy_file if not Path(strategy_file).is_absolute() else Path(strategy_file)

    risk = _load_yaml(risk_p)
    runtime = _load_yaml(rt_p)
    strategy = _load_yaml(st_p)

    if scenario_file:
        sc_p = repo_root / scenario_file if not Path(scenario_file).is_absolute() else Path(scenario_file)
        sc = _load_yaml(sc_p)
        if "risk" in sc:
            risk = _deep_merge(risk, sc["risk"])
        if "runtime" in sc:
            runtime = _deep_merge(runtime, sc["runtime"])
        if "survival" in sc:
            runtime = _deep_merge(runtime, {"survival": sc["survival"]})
        if "strategy" in sc:
            strategy = _deep_merge(strategy, sc["strategy"])
        # scenario top-level keys
        rt_overlay = {
            k: sc[k]
            for k in ("execution_mode", "reporting", "supervisors", "logging", "market_data", "execution")
            if k in sc
        }
        if rt_overlay:
            runtime = _deep_merge(runtime, rt_overlay)
        st_overlay = {
            k: sc[k]
            for k in (
                "kind",
                "guru",
                "filters",
                "sizing",
                "exits",
                "buy",
                "sell",
                "token_id",
                "run_once",
                "owner_a_id",
                "owner_b_id",
                "owner_b_unauthorized_sell",
                "owner_a_sell",
                "timeouts",
            )
            if k in sc
        }
        if st_overlay:
            strategy = _deep_merge(strategy, st_overlay)

    return parse_app_config(risk=risk, strategy=strategy, runtime=runtime)
