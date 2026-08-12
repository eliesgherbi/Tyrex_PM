"""In-memory armed protection state for one execution session."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from tyrex_pm.core.ids import InstrumentId, MarketId, StrategyId
from tyrex_pm.protection.spec import ProtectionSpec
from tyrex_pm.protection.triggers import ResolvedThresholds, resolve_thresholds


class ProtectionPhase(str, Enum):
    IDLE = "IDLE"
    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    DISARMED = "DISARMED"


@dataclass
class ArmedProtection:
    session_id: str
    strategy_id: StrategyId
    market_id: MarketId
    instrument_id: InstrumentId
    spec: ProtectionSpec
    entry_price: Decimal
    confirmed_quantity: Decimal
    armed_at: datetime
    thresholds: ResolvedThresholds
    peak_mark: Decimal
    trailing_active: bool
    phase: ProtectionPhase = ProtectionPhase.ARMED
    trigger_reason: str | None = None
    trigger_evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def arm(
        cls,
        *,
        session_id: str,
        strategy_id: StrategyId,
        market_id: MarketId,
        instrument_id: InstrumentId,
        spec: ProtectionSpec,
        entry_price: Decimal,
        confirmed_quantity: Decimal,
        armed_at: datetime,
        initial_mark: Decimal,
    ) -> "ArmedProtection":
        if confirmed_quantity <= 0:
            raise ValueError("confirmed_quantity must be > 0")
        thresholds = resolve_thresholds(spec=spec, entry_price=entry_price)
        trailing_active = bool(
            spec.trailing is not None
            and spec.trailing.enabled
            and spec.trailing.activation == "immediate"
        )
        return cls(
            session_id=session_id,
            strategy_id=strategy_id,
            market_id=market_id,
            instrument_id=instrument_id,
            spec=spec,
            entry_price=entry_price,
            confirmed_quantity=confirmed_quantity,
            armed_at=armed_at,
            thresholds=thresholds,
            peak_mark=initial_mark,
            trailing_active=trailing_active,
        )
