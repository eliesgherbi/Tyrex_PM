"""feature_probe configuration — explicit emit_entry plus host tau/clock hooks."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FeatureProbeConfig:
    emit_entry: bool
    tau_min_s: float
    tau_max_s: float
    max_clock_uncertainty_ms: float
    entry_ask_threshold: Decimal = Decimal("0.70")
    max_price_pad: Decimal = Decimal("0.08")

    def __post_init__(self) -> None:
        if self.tau_min_s < 0 or self.tau_max_s <= self.tau_min_s:
            raise ValueError("tau_max_s must be > tau_min_s and tau_min_s >= 0")
        if self.max_clock_uncertainty_ms <= 0:
            raise ValueError("max_clock_uncertainty_ms must be > 0")
        if not (Decimal("0") < self.entry_ask_threshold < Decimal("1")):
            raise ValueError("entry_ask_threshold must be in (0, 1)")
        if self.max_price_pad < 0:
            raise ValueError("max_price_pad cannot be negative")
