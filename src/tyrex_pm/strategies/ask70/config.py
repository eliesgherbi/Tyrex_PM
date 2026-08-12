"""ask70 harness configuration — all thresholds explicit."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

LegPreference = Literal["first_hit", "up_then_down", "down_then_up"]


@dataclass(frozen=True)
class Ask70Config:
    entry_ask_threshold: Decimal
    leg_preference: LegPreference
    max_price_pad: Decimal
    tau_min_s: float
    tau_max_s: float
    max_clock_uncertainty_ms: float

    def __post_init__(self) -> None:
        if not (Decimal("0") < self.entry_ask_threshold < Decimal("1")):
            raise ValueError("entry_ask_threshold must be in (0, 1)")
        if self.max_price_pad < 0:
            raise ValueError("max_price_pad cannot be negative")
        if self.tau_min_s < 0 or self.tau_max_s <= self.tau_min_s:
            raise ValueError("tau_max_s must be > tau_min_s and tau_min_s >= 0")
        if self.max_clock_uncertainty_ms <= 0:
            raise ValueError("max_clock_uncertainty_ms must be > 0")
        if self.leg_preference not in {"first_hit", "up_then_down", "down_then_up"}:
            raise ValueError("leg_preference is invalid")
