"""Typed settlement_value configuration.

Every numeric default is a **research seed**, not a tuned value. Units are stated
next to each field. Live trial profiles must set operational limits explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import isfinite

SUPPORTED_BACKENDS = frozenset({"endpoint_diffusion", "twap_proxy_diffusion"})
SPOT_INPUTS = frozenset({"binance_spot", "aligned_reference"})
KNOWN_BACKENDS = SUPPORTED_BACKENDS | {"average_diffusion"}
PROVENANCE_STATUSES = frozenset({"VERIFIED", "ASSUMED", "PROXY"})
COMPARISONS = frozenset({"AT_OR_ABOVE_K", "STRICTLY_ABOVE_K"})
TARGET_MODES = frozenset({"cap_only", "require_target"})
K_QUALITIES = frozenset(
    {"CONFIRMED_CANONICAL", "PROVISIONAL", "INFERRED", "LATE", "MISMATCHED", "MISSING"}
)


@dataclass(frozen=True, kw_only=True)
class SettlementSpecConfig:
    """Which payoff the probability model claims to describe, and how well that is known."""

    functional_id: str = "reference_at_close_vs_open"
    comparison: str = "AT_OR_ABOVE_K"
    reference_series: str = "chainlink:btcusd:twap:60"
    reference_units: str = "USD"
    timestamp_meaning: str = "provider_source_time"
    provenance_status: str = "ASSUMED"
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class SettlementModelConfig:
    # endpoint_diffusion: Phi(ln(S/K) / (sigma * sqrt(tau))) on the spot input.
    # twap_proxy_diffusion: TWAP-at-close moments with Binance constituents (PROXY; see
    # twap_proxy.py). average_diffusion (official constituents) is rejected: its inputs
    # are not recorded. Both supported backends are computed and recorded every time;
    # this field selects the one that drives decisions.
    backend: str = "endpoint_diffusion"
    # Averaging window of the assumed TWAP functional [seconds] (ASSUMED from provider
    # metadata twapLookbackSeconds = 60; boundary and weights unverified).
    averaging_window_s: float = 60.0
    # S fed to the diffusion. binance_spot: latest raw Binance BTCUSDT print (no
    # USD/USDT basis correction). aligned_reference: Binance mapped into the settlement
    # reference's level through the host's basis estimate; for a lagged TWAP series that
    # mapping also absorbs the lag, so S equals K right after the seal.
    spot_input: str = "binance_spot"
    # Conservative interval: sigma scaled by (1 - band, 1 + band) [dimensionless].
    sigma_band: float = 0.25
    # Conservative interval: S shifted by +/- this many basis points of S.
    reference_band_bps: float = 1.0
    # Minimum tau used in the diffusion denominator [seconds].
    tau_floor_s: float = 1.0


@dataclass(frozen=True, kw_only=True)
class SettlementVolatilityConfig:
    """ewma_vol indicator parameters on Binance spot trades (log-return sigma / sqrt(s))."""

    half_life_s: float = 30.0
    min_samples_s: float = 20.0
    jump_threshold_sigma: float = 4.0
    sample_interval_s: float = 1.0
    tau_floor_s: float = 1.0
    # Research seed. The shared estimator's 1e-4 default bound every estimate in the
    # recorded data (realized 1 s BTC log-return sd about 2.4e-5, unfloored EWMA about
    # 3.7e-5 on 6 Oct 2026), pushing probabilities towards 0.5. See candidate ranges note.
    sigma_floor: float = 1e-5
    # Scale applied to the EWMA sigma before pricing (1.0 = as estimated).
    sigma_scale: float = 1.0
    # Versioned elapsed-time handling of the variance (indicators.ewma_volatility):
    # per_sample_v0 = original estimator; elapsed_variance_v1 = research, not qualified.
    variance_time_policy: str = "per_sample_v0"


@dataclass(frozen=True, kw_only=True)
class SettlementDisagreementVetoConfig:
    """Wait instead of entering while the model and the market disagree too much.

    ``gap_pp = 100 * |P_point(UP) - mid(UP)|`` in percentage points of probability, with
    ``mid(UP) = (best bid + best ask) / 2`` of the UP leg in the same book snapshot as
    the decision. Checked once per entry decision, after the side is chosen and before an
    attempt starts: a veto is a strategy WAIT (no attempt, POST or reservation). Missing,
    non-finite, crossed or one-sided inputs also wait. Entry only: positions, holds and
    exits are never affected. ``recheck_at_dispatch`` repeats the check at the pre-send
    gate with the freshest book; a refusal there is a NOT_SENT outcome of a started
    attempt. Off by default; any threshold is a research hypothesis until qualified.
    """

    enabled: bool = False
    # Maximum absolute gap [percentage points of probability, (0, 100]]; required when on.
    max_gap_pp: float = 0.0
    probability: str = "point"
    market_price: str = "up_two_sided_mid"
    recheck_at_dispatch: bool = False


@dataclass(frozen=True, kw_only=True)
class SettlementEntryConfig:
    # Entry band on remaining time to window end [seconds].
    tau_min_s: float = 30.0
    tau_max_s: float = 240.0
    # Minimum conservative edge per gross share at the marginal level [USDC/share].
    min_edge_per_share: Decimal = Decimal("0.02")
    target_mode: str = "cap_only"
    # Expected-profit research target for require_target [USDC].
    target_profit_usdc: Decimal = Decimal("3")
    # Never pay more than this per share, whatever the edge [USDC/share].
    max_entry_price: Decimal = Decimal("0.97")
    # Order limit above the worst walked level, in venue ticks. Room for the book to move
    # between decision and arrival; never past max_entry_price, never to a price whose
    # marginal conservative value is negative. Size and EV stay computed at walk prices.
    limit_buffer_ticks: int = 0


@dataclass(frozen=True, kw_only=True)
class SettlementSizingConfig:
    # Maximum total debit per episode [USDC].
    max_debit_usdc: Decimal = Decimal("5")
    # Maximum full loss per episode [USDC]; for a bought binary this equals debit.
    max_loss_usdc: Decimal = Decimal("5")
    # Fraction of displayed depth the size search may plan against [0, 1].
    depth_participation: Decimal = Decimal("1")
    # Bound on book levels walked per side.
    max_levels: int = 32
    # Share quantity lattice [shares].
    share_increment: Decimal = Decimal("0.01")
    # Minimum order size used when the book carries none [shares]; research assumption.
    default_min_order_shares: Decimal = Decimal("5")


@dataclass(frozen=True, kw_only=True)
class SettlementQualityConfig:
    accepted_k_qualities: tuple[str, ...] = ("CONFIRMED_CANONICAL",)
    # Block entries unless the settlement spec provenance is VERIFIED.
    require_verified_settlement_spec: bool = False
    max_clock_uncertainty_ms: int = 250


@dataclass(frozen=True, kw_only=True)
class SettlementFeeConfig:
    """Taker fee curve phi(p) = rate * (p (1-p))^exponent, charged in shares on buys."""

    rate: Decimal = Decimal("0.07")
    exponent: Decimal = Decimal("1")
    provenance: str = "SCENARIO_ASSUMPTION_NOT_VERIFIED"


@dataclass(frozen=True, kw_only=True)
class SettlementRetryConfig:
    """Safe entry recovery inside one episode (objective 1). Off by default.

    An *attempt* is one newly authorized entry intent. Attempts are re-armed only after an
    authoritative not-sent or zero-fill outcome; an uncertain outcome blocks. Every
    attempt recomputes signal, size and price and passes every gate again.
    """

    enabled: bool = False
    # Total attempts per episode INCLUDING the first. Provisional default (user decision
    # 2026-10-16) pending research; not a tuned value.
    max_attempts_per_episode: int = 3
    # Window-wide ceiling across episodes, so re-entry cannot multiply requests.
    max_attempts_per_window: int = 3
    # Attempts that failed during local preparation (no POST) [count].
    max_prepare_failures_per_episode: int = 3
    # Venue mutation attempts (POSTs) per episode and per window [count].
    max_posts_per_episode: int = 3
    max_posts_per_window: int = 3
    # Hard elapsed budget measured from the episode's first attempt [seconds].
    max_retry_duration_s: float = 30.0
    # Minimum delay before a re-attempt [milliseconds]. Provisional.
    min_retry_interval_ms: int = 500
    # Extra delay after preparation/service failures, doubled per consecutive failure
    # and capped [milliseconds]. Provisional.
    service_backoff_ms: int = 1000
    service_backoff_max_ms: int = 8000


EXIT_POLICIES = frozenset({"hold_to_resolution", "managed_exit"})
EXIT_VALUE_BASES = frozenset({"upper", "point"})
RESIDUAL_POLICIES = frozenset({"hold_to_resolution"})


@dataclass(frozen=True, kw_only=True)
class SettlementExitConfig:
    """Lifecycle policy selected before entry (objective 2).

    ``hold_to_resolution`` (default, a valid final choice): a confirmed position is
    committed to resolution at once. ``managed_exit``: the position stays open and
    exitable; strategy exits below and the run's protection block (live) / protection
    spec (replay) may sell it. At ``exit_cutoff_tau_s`` any remaining inventory follows
    ``residual_policy``. Numeric defaults are hypotheses to research, not tuned values.
    """

    policy: str = "hold_to_resolution"
    # Economic exit: sell when the fee-net executable bid per share exceeds the model's
    # hold value of the held side by at least this margin [USDC/share].
    value_exit_enabled: bool = False
    value_exit_margin_per_share: Decimal = Decimal("0.02")
    # Hold value used by the economic exit: upper interval bound (conservative for a
    # sale: only sell when the bid beats even the optimistic value) or the point value.
    value_exit_basis: str = "upper"
    # Experimental evaluated-bid confirmation; off preserves every existing policy.
    value_exit_trend_enabled: bool = False
    value_exit_trend_policy: str = "evaluated_bid_trend_v1"
    value_exit_trend_lookback_s: float = 5.0
    value_exit_trend_min_drop: Decimal = Decimal("0.02")
    value_exit_trend_confirmation_s: float = 2.0
    value_exit_trend_max_gap_s: float = 2.0
    # Declared risk rule (not an expected-value claim): exit when the point probability
    # of the held side falls below this [0, 1].
    thesis_stop_enabled: bool = False
    thesis_stop_probability: float = 0.30
    # No new strategy exit is submitted when tau is below this [seconds].
    exit_cutoff_tau_s: float = 15.0
    residual_policy: str = "hold_to_resolution"
    # Never sell below this price per share [USDC/share].
    min_exit_price: Decimal = Decimal("0.01")
    # Strategy exit submissions per episode [count].
    max_exit_attempts_per_episode: int = 3


REARM_MODES = frozenset({"signal_reset", "cooldown"})


@dataclass(frozen=True, kw_only=True)
class SettlementReentryConfig:
    """New episodes after a confirmed complete exit, same window (objective 3).

    ``max_trades_per_window`` counts episodes that acquired confirmed exposure; zero-fill
    attempts are governed by the retry budgets. One position at a time; no top-up.
    """

    max_trades_per_window: int = 1
    # signal_reset: after a close the traded side must first become ineligible again;
    # cooldown: only the delay applies. The cooldown applies in both modes [seconds].
    rearm: str = "signal_reset"
    cooldown_s: float = 10.0
    lock_after_protective_stop: bool = True
    lock_after_thesis_stop: bool = True
    same_side_allowed: bool = True
    opposite_side_allowed: bool = False
    # Additional conservative edge a re-entry must clear [USDC/share].
    min_extra_edge_per_share: Decimal = Decimal("0")
    # No re-entry episode starts when tau is below this [seconds].
    last_entry_tau_s: float = 60.0
    # Total debit across the window's episodes [USDC].
    max_window_debit_usdc: Decimal = Decimal("5")


@dataclass(frozen=True, kw_only=True)
class SettlementValueConfig:
    settlement: SettlementSpecConfig = SettlementSpecConfig()
    model: SettlementModelConfig = SettlementModelConfig()
    volatility: SettlementVolatilityConfig = SettlementVolatilityConfig()
    entry: SettlementEntryConfig = SettlementEntryConfig()
    sizing: SettlementSizingConfig = SettlementSizingConfig()
    quality: SettlementQualityConfig = SettlementQualityConfig()
    fees: SettlementFeeConfig = SettlementFeeConfig()
    retry: SettlementRetryConfig = SettlementRetryConfig()
    exit: SettlementExitConfig = SettlementExitConfig()
    reentry: SettlementReentryConfig = SettlementReentryConfig()
    disagreement_veto: SettlementDisagreementVetoConfig = SettlementDisagreementVetoConfig()

    def __post_init__(self) -> None:
        validate_settlement_value_config(self)


VARIANCE_TIME_POLICIES = frozenset({"per_sample_v0", "elapsed_variance_v1"})
VETO_PROBABILITIES = frozenset({"point"})
VETO_MARKET_PRICES = frozenset({"up_two_sided_mid"})


def validate_settlement_value_config(cfg: SettlementValueConfig) -> None:
    if cfg.volatility.variance_time_policy not in VARIANCE_TIME_POLICIES:
        raise ValueError(
            "volatility.variance_time_policy must be one of "
            f"{sorted(VARIANCE_TIME_POLICIES)}")
    veto = cfg.disagreement_veto
    if veto.probability not in VETO_PROBABILITIES:
        raise ValueError(
            f"disagreement_veto.probability must be one of {sorted(VETO_PROBABILITIES)}")
    if veto.market_price not in VETO_MARKET_PRICES:
        raise ValueError(
            f"disagreement_veto.market_price must be one of {sorted(VETO_MARKET_PRICES)}")
    if veto.enabled and not 0 < veto.max_gap_pp <= 100:
        raise ValueError("disagreement_veto.max_gap_pp must be in (0, 100] when enabled")
    if veto.recheck_at_dispatch and not veto.enabled:
        raise ValueError("disagreement_veto.recheck_at_dispatch requires enabled")
    s = cfg.settlement
    if s.provenance_status not in PROVENANCE_STATUSES:
        raise ValueError(
            f"settlement.provenance_status must be one of {sorted(PROVENANCE_STATUSES)}"
        )
    if s.comparison not in COMPARISONS:
        raise ValueError(f"settlement.comparison must be one of {sorted(COMPARISONS)}")
    if not s.functional_id.strip() or not s.reference_series.strip():
        raise ValueError("settlement.functional_id and reference_series are required")
    m = cfg.model
    if m.backend not in KNOWN_BACKENDS:
        raise ValueError(f"unknown model.backend {m.backend!r}")
    if m.backend not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"model.backend {m.backend!r} is disabled: it requires the averaging window, "
            "weights and constituent history, which are not established inputs"
        )
    if m.spot_input not in SPOT_INPUTS:
        raise ValueError(f"model.spot_input must be one of {sorted(SPOT_INPUTS)}")
    if not 0 <= m.sigma_band < 1:
        raise ValueError("model.sigma_band must be in [0, 1)")
    if not 0 < m.averaging_window_s <= 300:
        raise ValueError("model.averaging_window_s must be in (0, 300]")
    if m.reference_band_bps < 0 or m.tau_floor_s <= 0:
        raise ValueError("model.reference_band_bps must be >= 0 and tau_floor_s > 0")
    v = cfg.volatility
    if v.sigma_scale <= 0 or v.half_life_s <= 0 or v.sample_interval_s <= 0:
        raise ValueError("volatility sigma_scale, half_life_s and sample_interval_s must be > 0")
    e = cfg.entry
    if e.tau_min_s < 0 or e.tau_max_s < e.tau_min_s:
        raise ValueError("invalid entry tau band")
    if e.min_edge_per_share < 0:
        raise ValueError("entry.min_edge_per_share must be >= 0")
    if e.target_mode not in TARGET_MODES:
        raise ValueError(f"entry.target_mode must be one of {sorted(TARGET_MODES)}")
    if e.target_profit_usdc <= 0:
        raise ValueError("entry.target_profit_usdc must be > 0")
    if e.limit_buffer_ticks < 0:
        raise ValueError("entry.limit_buffer_ticks must be >= 0")
    if not Decimal(0) < e.max_entry_price < Decimal(1):
        raise ValueError("entry.max_entry_price must be in (0, 1)")
    z = cfg.sizing
    if z.max_debit_usdc <= 0 or z.max_loss_usdc <= 0:
        raise ValueError("sizing.max_debit_usdc and max_loss_usdc must be > 0")
    if not Decimal(0) < z.depth_participation <= Decimal(1):
        raise ValueError("sizing.depth_participation must be in (0, 1]")
    if z.max_levels < 1 or z.share_increment <= 0 or z.default_min_order_shares < 0:
        raise ValueError("invalid sizing lattice or level bound")
    q = cfg.quality
    if not q.accepted_k_qualities or any(k not in K_QUALITIES for k in q.accepted_k_qualities):
        raise ValueError(f"quality.accepted_k_qualities must be a subset of {sorted(K_QUALITIES)}")
    if q.max_clock_uncertainty_ms < 0:
        raise ValueError("quality.max_clock_uncertainty_ms must be >= 0")
    f = cfg.fees
    if f.rate < 0 or f.exponent < 0 or not f.provenance.strip():
        raise ValueError("fees.rate/exponent must be >= 0 and provenance is required")
    r = cfg.retry
    if min(
        r.max_attempts_per_episode,
        r.max_attempts_per_window,
        r.max_prepare_failures_per_episode,
        r.max_posts_per_episode,
        r.max_posts_per_window,
    ) < 1:
        raise ValueError("retry ceilings must be >= 1 (1 means a single attempt)")
    if r.max_retry_duration_s <= 0 or r.min_retry_interval_ms < 0:
        raise ValueError("retry.max_retry_duration_s must be > 0 and intervals >= 0")
    if r.service_backoff_ms < 0 or r.service_backoff_max_ms < r.service_backoff_ms:
        raise ValueError("retry.service_backoff_max_ms must be >= service_backoff_ms >= 0")
    x = cfg.exit
    if (not all(isfinite(v) for v in (x.value_exit_trend_lookback_s,
                                     x.value_exit_trend_max_gap_s,
                                     x.value_exit_trend_confirmation_s))
            or not x.value_exit_trend_min_drop.is_finite()):
        raise ValueError("value exit trend parameters must be finite")
    if x.value_exit_trend_policy != "evaluated_bid_trend_v1":
        raise ValueError("unknown value exit trend policy")
    if (x.value_exit_trend_lookback_s <= 0 or x.value_exit_trend_max_gap_s <= 0
            or x.value_exit_trend_confirmation_s < 0 or x.value_exit_trend_min_drop <= 0):
        raise ValueError("invalid value exit trend durations or minimum drop")
    if x.value_exit_trend_enabled and (not x.value_exit_enabled or x.policy != "managed_exit"):
        raise ValueError("exit trend requires enabled value exit and managed_exit policy")
    if x.policy not in EXIT_POLICIES:
        raise ValueError(f"exit.policy must be one of {sorted(EXIT_POLICIES)}")
    if x.value_exit_basis not in EXIT_VALUE_BASES:
        raise ValueError(f"exit.value_exit_basis must be one of {sorted(EXIT_VALUE_BASES)}")
    if x.residual_policy not in RESIDUAL_POLICIES:
        raise ValueError(f"exit.residual_policy must be one of {sorted(RESIDUAL_POLICIES)}")
    if x.value_exit_margin_per_share < 0 or not 0 <= x.thesis_stop_probability <= 1:
        raise ValueError("exit margin must be >= 0 and thesis_stop_probability in [0, 1]")
    if x.exit_cutoff_tau_s < 0 or not Decimal(0) < x.min_exit_price < Decimal(1):
        raise ValueError("exit.exit_cutoff_tau_s must be >= 0 and min_exit_price in (0, 1)")
    if x.max_exit_attempts_per_episode < 1:
        raise ValueError("exit.max_exit_attempts_per_episode must be >= 1")
    if x.policy == "hold_to_resolution" and (x.value_exit_enabled or x.thesis_stop_enabled):
        raise ValueError(
            "strategy exits require exit.policy: managed_exit (hold_to_resolution never sells)"
        )
    n = cfg.reentry
    if n.max_trades_per_window < 1 or n.rearm not in REARM_MODES:
        raise ValueError(
            f"reentry.max_trades_per_window must be >= 1 and rearm one of {sorted(REARM_MODES)}"
        )
    if n.cooldown_s < 0 or n.last_entry_tau_s < 0 or n.min_extra_edge_per_share < 0:
        raise ValueError("reentry cooldown, last_entry_tau_s and extra edge must be >= 0")
    if n.max_window_debit_usdc <= 0:
        raise ValueError("reentry.max_window_debit_usdc must be > 0")
    if n.max_trades_per_window > 1 and x.policy != "managed_exit":
        raise ValueError(
            "reentry.max_trades_per_window > 1 requires exit.policy: managed_exit "
            "(a held-to-resolution position never closes inside the window)"
        )
    if n.max_trades_per_window > 1 and not (n.same_side_allowed or n.opposite_side_allowed):
        raise ValueError("re-entry needs same_side_allowed or opposite_side_allowed")
