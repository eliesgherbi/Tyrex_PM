"""Typed observe + R4 risk/planning configuration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.indicators.momentum import MomentumConfig
from tyrex_pm.market_data.freshness import FreshnessConfig, TimestampBasis
from tyrex_pm.runtime.shadow_config import ShadowConfig, shadow_config_from_mapping


class SourceMode(str, Enum):
    FIXTURE = "fixture"
    LIVE = "live"


@dataclass(frozen=True, kw_only=True)
class RiskPlanConfig:
    """R4 risk + dry planning knobs (no OMS)."""

    runtime_mode: RuntimeMode
    target_notional: Decimal
    max_notional: Decimal
    min_price: Decimal
    max_price: Decimal
    max_spread: Decimal
    min_liquidity_notional: Decimal
    no_entry_before_close: timedelta
    duplicate_lifetime: timedelta
    kill_switch_active: bool = False

    def __post_init__(self) -> None:
        if self.target_notional <= 0:
            raise ValueError("target_notional must be > 0")
        if self.max_notional <= 0:
            raise ValueError("max_notional must be > 0")
        if self.target_notional > self.max_notional:
            raise ValueError("target_notional must be <= max_notional")
        if self.min_price < 0 or self.max_price > 1 or self.min_price > self.max_price:
            raise ValueError("price bounds must satisfy 0 <= min_price <= max_price <= 1")
        if self.max_spread <= 0:
            raise ValueError("max_spread must be > 0")
        if self.min_liquidity_notional < 0:
            raise ValueError("min_liquidity_notional must be >= 0")
        if self.no_entry_before_close < timedelta(0):
            raise ValueError("no_entry_before_close must be >= 0")
        if self.duplicate_lifetime <= timedelta(0):
            raise ValueError("duplicate_lifetime must be > 0")
        if self.runtime_mode is RuntimeMode.LIVE_TINY:
            # Allowed in config for fail-closed testing, but host will deny.
            pass

    def fingerprint(self) -> str:
        payload = {
            "runtime_mode": self.runtime_mode.value,
            "target_notional": str(self.target_notional),
            "max_notional": str(self.max_notional),
            "min_price": str(self.min_price),
            "max_price": str(self.max_price),
            "max_spread": str(self.max_spread),
            "min_liquidity_notional": str(self.min_liquidity_notional),
            "no_entry_before_close_s": self.no_entry_before_close.total_seconds(),
            "duplicate_lifetime_s": self.duplicate_lifetime.total_seconds(),
            "kill_switch_active": self.kill_switch_active,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, kw_only=True)
class ZGapObserveRuntimeConfig:
    """Fixture OBSERVE wiring for Z-Gap (no live/network provider settings)."""

    window_id: str
    ptb_k: Decimal
    fee_rate: Decimal = Decimal("0.07")
    fee_exponent: Decimal = Decimal("1")
    target_notional: Decimal = Decimal("5")
    evaluate_interval_s: float = 1.0
    timer_eval_count: int = 2
    # Provisional threshold overrides for deterministic fixture evidence
    half_life_s: float = 5.0
    min_samples_s: float = 3.0
    sample_interval_s: float = 1.0
    tau_floor_s: float = 1.0
    jump_threshold_sigma: float = 4.0
    theta_take: Decimal = Decimal("0.01")
    z_min: Decimal = Decimal("0.0")
    z_max: Decimal = Decimal("20.0")
    tau_min_s: float = 1.0
    tau_max_s: float = 600.0
    basis_max_bps: Decimal = Decimal("10000")
    expected_slippage_buy: Decimal = Decimal("0")
    expected_slippage_sell: Decimal = Decimal("0")
    reject_both_legs_edge: bool = False
    # Exit-family knobs (provisional; bind actual F2 policy behavior)
    theta_rich: Decimal = Decimal("0.02")
    p_stop: Decimal = Decimal("0.4013")
    stop_confirm_s: float = 1.0
    flatten_before_event_end_s: float = 20.0

    def __post_init__(self) -> None:
        if not self.window_id.strip():
            raise ValueError("z_gap.window_id required")
        if self.ptb_k <= 0:
            raise ValueError("z_gap.ptb_k must be > 0")
        if self.target_notional <= 0:
            raise ValueError("z_gap.target_notional must be > 0")
        if self.evaluate_interval_s <= 0:
            raise ValueError("z_gap.evaluate_interval_s must be > 0")
        if self.timer_eval_count < 0:
            raise ValueError("z_gap.timer_eval_count must be >= 0")


@dataclass(frozen=True, kw_only=True)
class ObserveConfig:
    mode: SourceMode
    output_path: Path
    binance_symbol: str
    momentum_lookback: timedelta
    momentum_threshold: Decimal
    max_book_spread: Decimal
    freshness: FreshnessConfig
    runtime_duration: timedelta | None
    fixture_path: Path | None = None
    event_slug: str | None = None
    event_url: str | None = None
    condition_id: str | None = None
    evaluate_on_reference: bool = True
    momentum_min_samples: int = 2
    risk: RiskPlanConfig | None = None
    shadow: ShadowConfig | None = None
    strategy_kind: str = "reference_momentum"
    z_gap: ZGapObserveRuntimeConfig | None = None

    def __post_init__(self) -> None:
        if self.mode is SourceMode.FIXTURE and self.fixture_path is None:
            raise ValueError("fixture mode requires fixture_path")
        if self.mode is SourceMode.LIVE and not any(
            (self.event_slug, self.event_url, self.condition_id)
        ):
            raise ValueError("live mode requires event_slug, event_url, or condition_id")
        if not self.binance_symbol.strip():
            raise ValueError("binance_symbol required")
        if self.momentum_lookback <= timedelta(0):
            raise ValueError("momentum_lookback must be > 0")
        if self.momentum_threshold <= 0:
            raise ValueError("momentum_threshold must be > 0 (no hidden default)")
        if self.max_book_spread <= 0:
            raise ValueError("max_book_spread must be > 0")
        kind = self.strategy_kind.strip().lower().replace("-", "_")
        object.__setattr__(self, "strategy_kind", kind)
        if kind in {"z_gap", "zgap"} and self.z_gap is None:
            raise ValueError("strategy_kind=z_gap requires z_gap runtime config")
        object.__setattr__(self, "binance_symbol", self.binance_symbol.upper())

    @property
    def momentum(self) -> MomentumConfig:
        return MomentumConfig(
            lookback=self.momentum_lookback,
            min_samples=self.momentum_min_samples,
        )

    def fingerprint(self) -> str:
        payload = {
            "mode": self.mode.value,
            "binance_symbol": self.binance_symbol,
            "momentum_lookback_ms": int(self.momentum_lookback.total_seconds() * 1000),
            "momentum_threshold": str(self.momentum_threshold),
            "max_book_spread": str(self.max_book_spread),
            "book_threshold_ms": self.freshness.book_threshold_ms,
            "reference_threshold_ms": self.freshness.reference_threshold_ms,
            "future_tolerance_ms": self.freshness.future_tolerance_ms,
            "timestamp_basis": self.freshness.timestamp_basis.value,
            "runtime_duration_s": None
            if self.runtime_duration is None
            else self.runtime_duration.total_seconds(),
            "fixture_path": None if self.fixture_path is None else str(self.fixture_path),
            "event_slug": self.event_slug,
            "condition_id": self.condition_id,
            "risk_fingerprint": None if self.risk is None else self.risk.fingerprint(),
            "shadow_fingerprint": None if self.shadow is None else self.shadow.fingerprint(),
            "strategy_kind": self.strategy_kind,
            "z_gap_window": None if self.z_gap is None else self.z_gap.window_id,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _risk_from_mapping(data: Mapping[str, Any]) -> RiskPlanConfig:
    required = (
        "runtime_mode",
        "target_notional",
        "max_notional",
        "min_price",
        "max_price",
        "max_spread",
        "min_liquidity_notional",
        "no_entry_before_close_s",
        "duplicate_lifetime_s",
    )
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"risk config missing required fields: {missing}")
    return RiskPlanConfig(
        runtime_mode=RuntimeMode(str(data["runtime_mode"])),
        target_notional=Decimal(str(data["target_notional"])),
        max_notional=Decimal(str(data["max_notional"])),
        min_price=Decimal(str(data["min_price"])),
        max_price=Decimal(str(data["max_price"])),
        max_spread=Decimal(str(data["max_spread"])),
        min_liquidity_notional=Decimal(str(data["min_liquidity_notional"])),
        no_entry_before_close=timedelta(seconds=float(data["no_entry_before_close_s"])),
        duplicate_lifetime=timedelta(seconds=float(data["duplicate_lifetime_s"])),
        kill_switch_active=bool(data.get("kill_switch_active", False)),
    )


def observe_config_from_mapping(data: Mapping[str, Any]) -> ObserveConfig:
    freshness_raw = data.get("freshness") or {}
    if "book_threshold_ms" not in freshness_raw or "reference_threshold_ms" not in freshness_raw:
        raise ValueError("freshness.book_threshold_ms and reference_threshold_ms are required")
    basis = TimestampBasis(str(freshness_raw.get("timestamp_basis", "EVENT_TIME")))
    freshness = FreshnessConfig(
        book_threshold_ms=int(freshness_raw["book_threshold_ms"]),
        reference_threshold_ms=int(freshness_raw["reference_threshold_ms"]),
        future_tolerance_ms=int(freshness_raw.get("future_tolerance_ms", 500)),
        timestamp_basis=basis,
    )
    lookback_ms = data.get("momentum_lookback_ms")
    if lookback_ms is None:
        raise ValueError("momentum_lookback_ms is required")
    threshold = data.get("momentum_threshold")
    if threshold is None:
        raise ValueError("momentum_threshold is required")
    max_spread = data.get("max_book_spread")
    if max_spread is None:
        raise ValueError("max_book_spread is required")
    duration_s = data.get("runtime_duration_s")
    mode = SourceMode(str(data.get("mode", "fixture")))
    fixture = data.get("fixture_path")
    risk_raw = data.get("risk")
    shadow_raw = data.get("shadow")
    zgap_raw = data.get("z_gap")
    return ObserveConfig(
        mode=mode,
        output_path=Path(str(data["output_path"])),
        binance_symbol=str(data.get("binance_symbol", "BTCUSDT")),
        momentum_lookback=timedelta(milliseconds=int(lookback_ms)),
        momentum_threshold=Decimal(str(threshold)),
        max_book_spread=Decimal(str(max_spread)),
        freshness=freshness,
        runtime_duration=None if duration_s is None else timedelta(seconds=float(duration_s)),
        fixture_path=None if fixture is None else Path(str(fixture)),
        event_slug=data.get("event_slug"),
        event_url=data.get("event_url"),
        condition_id=data.get("condition_id"),
        evaluate_on_reference=bool(data.get("evaluate_on_reference", True)),
        momentum_min_samples=int(data.get("momentum_min_samples", 2)),
        risk=None if risk_raw is None else _risk_from_mapping(risk_raw),
        shadow=None if shadow_raw is None else shadow_config_from_mapping(shadow_raw),
        strategy_kind=str(data.get("strategy_kind", "reference_momentum")),
        z_gap=None if zgap_raw is None else _zgap_from_mapping(zgap_raw),
    )


def _zgap_from_mapping(data: Mapping[str, Any]) -> ZGapObserveRuntimeConfig:
    required = ("window_id", "ptb_k")
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"z_gap config missing required fields: {missing}")
    return ZGapObserveRuntimeConfig(
        window_id=str(data["window_id"]),
        ptb_k=Decimal(str(data["ptb_k"])),
        fee_rate=Decimal(str(data.get("fee_rate", "0.07"))),
        fee_exponent=Decimal(str(data.get("fee_exponent", "1"))),
        target_notional=Decimal(str(data.get("target_notional", "5"))),
        evaluate_interval_s=float(data.get("evaluate_interval_s", 1.0)),
        timer_eval_count=int(data.get("timer_eval_count", 2)),
        half_life_s=float(data.get("half_life_s", 5.0)),
        min_samples_s=float(data.get("min_samples_s", 3.0)),
        sample_interval_s=float(data.get("sample_interval_s", 1.0)),
        tau_floor_s=float(data.get("tau_floor_s", 1.0)),
        jump_threshold_sigma=float(data.get("jump_threshold_sigma", 4.0)),
        theta_take=Decimal(str(data.get("theta_take", "0.01"))),
        z_min=Decimal(str(data.get("z_min", "0"))),
        z_max=Decimal(str(data.get("z_max", "20"))),
        tau_min_s=float(data.get("tau_min_s", 1.0)),
        tau_max_s=float(data.get("tau_max_s", 600.0)),
        basis_max_bps=Decimal(str(data.get("basis_max_bps", "10000"))),
        expected_slippage_buy=Decimal(str(data.get("expected_slippage_buy", "0"))),
        expected_slippage_sell=Decimal(str(data.get("expected_slippage_sell", "0"))),
        reject_both_legs_edge=bool(data.get("reject_both_legs_edge", False)),
        theta_rich=Decimal(str(data.get("theta_rich", "0.02"))),
        p_stop=Decimal(str(data.get("p_stop", "0.4013"))),
        stop_confirm_s=float(data.get("stop_confirm_s", 1.0)),
        flatten_before_event_end_s=float(data.get("flatten_before_event_end_s", 20.0)),
    )


def load_observe_config(path: Path | str) -> ObserveConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return observe_config_from_mapping(raw)
