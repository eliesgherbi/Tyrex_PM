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
    )


def load_observe_config(path: Path | str) -> ObserveConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return observe_config_from_mapping(raw)
