"""Typed settlement_value configuration.

Every numeric default is a **research seed**, not a tuned value. Units are stated
next to each field. Live trial profiles must set operational limits explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

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
class SettlementValueConfig:
    settlement: SettlementSpecConfig = SettlementSpecConfig()
    model: SettlementModelConfig = SettlementModelConfig()
    volatility: SettlementVolatilityConfig = SettlementVolatilityConfig()
    entry: SettlementEntryConfig = SettlementEntryConfig()
    sizing: SettlementSizingConfig = SettlementSizingConfig()
    quality: SettlementQualityConfig = SettlementQualityConfig()
    fees: SettlementFeeConfig = SettlementFeeConfig()

    def __post_init__(self) -> None:
        validate_settlement_value_config(self)


def validate_settlement_value_config(cfg: SettlementValueConfig) -> None:
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
