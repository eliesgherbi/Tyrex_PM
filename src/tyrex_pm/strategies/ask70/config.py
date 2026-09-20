"""ask70 harness configuration — all thresholds explicit."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

LegPreference = Literal["first_hit", "up_then_down", "down_then_up"]
Ask70LiquidityRole = Literal["taker", "maker"]


@dataclass(frozen=True)
class Ask70Config:
    entry_ask_threshold: Decimal
    leg_preference: LegPreference
    max_price_pad: Decimal
    tau_min_s: float
    tau_max_s: float
    max_clock_uncertainty_ms: float
    liquidity_role: Ask70LiquidityRole = "taker"
    maker_limit_price: Decimal | None = None

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
        if self.liquidity_role not in {"taker", "maker"}:
            raise ValueError("liquidity_role must be taker or maker")
        if self.maker_limit_price is not None:
            if not (Decimal("0") < self.maker_limit_price < Decimal("1")):
                raise ValueError("maker_limit_price must be in (0, 1)")
            if self.liquidity_role != "maker":
                raise ValueError("maker_limit_price is only valid when liquidity_role=maker")
