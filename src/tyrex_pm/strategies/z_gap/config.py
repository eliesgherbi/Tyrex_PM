"""Typed Z-Gap configuration for pure F2 components.

Defaults marked **provisional** retain legacy-aligned Phase A values or
spectrum-mapped placeholders (θ_rich, p_stop) pending evidence calibration.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.domain.polymarket.fees import FeeCurveParams, PROVISIONAL_SAMPLE_FEE
from tyrex_pm.core.numerics import as_decimal


@dataclass(frozen=True, kw_only=True)
class ZGapVolatilityConfig:
    half_life_s: float = 30.0  # provisional
    min_samples_s: float = 20.0  # provisional
    jump_threshold_sigma: float = 4.0  # provisional
    sample_interval_s: float = 1.0  # provisional
    tau_floor_s: float = 1.0  # provisional


@dataclass(frozen=True, kw_only=True)
class ZGapEntryConfig:
    theta_take: Decimal = Decimal("0.05")  # provisional
    theta_fill_floor: Decimal = Decimal("0.03")  # provisional
    z_min: Decimal = Decimal("0.8")  # provisional |z| band
    z_max: Decimal = Decimal("2.2")  # provisional
    block_abs_z: Decimal = Decimal("20")  # provisional
    tau_min_s: float = 60.0  # provisional
    tau_max_s: float = 210.0  # provisional
    expected_slippage_buy: Decimal = Decimal("0.01")  # provisional (1 tick @ 0.01)
    exit_friction_reserve: Decimal = Decimal("0.0")  # optional F_exit add-on
    require_repricing_edge: bool = True  # Correction D default
    tie_epsilon: Decimal = Decimal("0.001")  # near-tie → no entry
    reject_both_legs_edge: bool = True


@dataclass(frozen=True, kw_only=True)
class ZGapRealizationConfig:
    theta_rich: Decimal = Decimal("0.02")  # provisional (P5)
    min_exit_depth: Decimal = Decimal("0")  # depth check; 0 disables
    expected_slippage_sell: Decimal = Decimal("0.01")  # provisional
    # When True, executable bid already embeds depth-walk slippage.
    slippage_included_in_executable_bid: bool = True


@dataclass(frozen=True, kw_only=True)
class ZGapThesisConfig:
    # Mapped from legacy z_stop=0.25 → Φ(-0.25) ≈ 0.40129367 (provisional P5)
    p_stop: Decimal = Decimal("0.4013")
    stop_confirm_s: float = 1.0  # provisional (monotonic seconds)
    reset_on_stale_model: bool = True


@dataclass(frozen=True, kw_only=True)
class ZGapTimeResolutionConfig:
    flatten_before_event_end_s: float = 20.0  # provisional operational default
    # Sell preferred when V_sell exceeds V_resolve,adj by this margin.
    sell_vs_resolve_margin: Decimal = Decimal("0.0")
    resolution_capability_default: bool = False  # unavailable until F5


@dataclass(frozen=True, kw_only=True)
class ZGapPtbTimeQualityConfig:
    basis_max_bps: Decimal = Decimal("3")  # provisional
    max_ptb_lag_ms: int = 5000  # provisional
    max_clock_uncertainty_ms: int = 250  # provisional


@dataclass(frozen=True, kw_only=True)
class ZGapFrictionConfig:
    fee_curve: FeeCurveParams = PROVISIONAL_SAMPLE_FEE  # provisional until live fd
    provisional_resolve_penalty_per_share: Decimal = Decimal("0.0")


@dataclass(frozen=True, kw_only=True)
class ZGapConfig:
    volatility: ZGapVolatilityConfig = ZGapVolatilityConfig()
    entry: ZGapEntryConfig = ZGapEntryConfig()
    realization: ZGapRealizationConfig = ZGapRealizationConfig()
    thesis: ZGapThesisConfig = ZGapThesisConfig()
    time_resolution: ZGapTimeResolutionConfig = ZGapTimeResolutionConfig()
    ptb_time_quality: ZGapPtbTimeQualityConfig = ZGapPtbTimeQualityConfig()
    friction: ZGapFrictionConfig = ZGapFrictionConfig()

    def __post_init__(self) -> None:
        validate_zgap_config(self)


def validate_zgap_config(cfg: ZGapConfig) -> None:
    e = cfg.entry
    if as_decimal(e.theta_take, field_name="theta_take") < 0:
        raise ValueError("theta_take must be >= 0")
    if e.z_min < 0 or e.z_max < e.z_min:
        raise ValueError("invalid z band")
    if e.tau_min_s < 0 or e.tau_max_s < e.tau_min_s:
        raise ValueError("invalid tau band")
    if e.tie_epsilon < 0:
        raise ValueError("tie_epsilon must be >= 0")
    r = cfg.realization
    if r.theta_rich < 0:
        raise ValueError("theta_rich must be >= 0")
    t = cfg.thesis
    if t.p_stop < 0 or t.p_stop > 1:
        raise ValueError("p_stop must be in [0, 1]")
    if t.stop_confirm_s < 0:
        raise ValueError("stop_confirm_s must be >= 0")
    tr = cfg.time_resolution
    if tr.flatten_before_event_end_s < 0:
        raise ValueError("flatten_before_event_end_s must be >= 0")
    q = cfg.ptb_time_quality
    if q.basis_max_bps < 0:
        raise ValueError("basis_max_bps must be >= 0")
    if q.max_ptb_lag_ms < 0 or q.max_clock_uncertainty_ms < 0:
        raise ValueError("quality lag/uncertainty must be >= 0")
    v = cfg.volatility
    if v.half_life_s <= 0 or v.sample_interval_s <= 0 or v.tau_floor_s <= 0:
        raise ValueError("volatility timing parameters must be positive")
