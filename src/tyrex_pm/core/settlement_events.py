"""Framework settlement events (simulated SHADOW resolution — not venue fills)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.core.events import Event, EventSource, _validate_event_times
from tyrex_pm.core.ids import CorrelationId, EventId, InstrumentId, MarketId, new_event_id
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.numerics import as_decimal, require_non_negative


@dataclass(frozen=True, kw_only=True)
class SimulatedResolutionSettled(Event):
    """Apply simulated binary payout to confirmed internal inventory.

    Labels are always simulated/shadow — never confirmed venue economics.
    """

    market_id: MarketId
    window_id: str
    instrument_id: InstrumentId
    held_side: OutcomeSide
    resolved_side: OutcomeSide
    quantity: Decimal
    payout_per_share: Decimal
    payout_total: Decimal
    entry_cost_total: Decimal
    simulated_realized_pnl: Decimal
    evidence_id: str
    economics_label: str = "simulated_shadow"
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_event_times(self)
        object.__setattr__(
            self,
            "quantity",
            require_non_negative(
                as_decimal(self.quantity, field_name="quantity"),
                field_name="quantity",
            ),
        )
        object.__setattr__(
            self,
            "payout_per_share",
            as_decimal(self.payout_per_share, field_name="payout_per_share"),
        )
        object.__setattr__(
            self, "payout_total", as_decimal(self.payout_total, field_name="payout_total")
        )
        object.__setattr__(
            self,
            "entry_cost_total",
            as_decimal(self.entry_cost_total, field_name="entry_cost_total"),
        )
        object.__setattr__(
            self,
            "simulated_realized_pnl",
            as_decimal(self.simulated_realized_pnl, field_name="simulated_realized_pnl"),
        )
        if self.economics_label != "simulated_shadow":
            raise ValueError("SimulatedResolutionSettled.economics_label must be simulated_shadow")
        object.__setattr__(self, "evidence", dict(self.evidence))


def new_simulated_settlement(
    *,
    correlation_id: CorrelationId,
    causation_id: EventId | None,
    when: datetime,
    market_id: MarketId,
    window_id: str,
    instrument_id: InstrumentId,
    held_side: OutcomeSide,
    resolved_side: OutcomeSide,
    quantity: Decimal,
    payout_per_share: Decimal,
    entry_cost_total: Decimal,
    evidence_id: str,
    evidence: Mapping[str, Any] | None = None,
) -> SimulatedResolutionSettled:
    qty = as_decimal(quantity, field_name="quantity")
    pps = as_decimal(payout_per_share, field_name="payout_per_share")
    payout = qty * pps
    cost = as_decimal(entry_cost_total, field_name="entry_cost_total")
    pnl = payout - cost
    return SimulatedResolutionSettled(
        event_id=new_event_id(),
        correlation_id=correlation_id,
        causation_id=causation_id,
        ts_event=when,
        ts_received=when,
        source=EventSource.SYSTEM,
        market_id=market_id,
        window_id=window_id,
        instrument_id=instrument_id,
        held_side=held_side,
        resolved_side=resolved_side,
        quantity=qty,
        payout_per_share=pps,
        payout_total=payout,
        entry_cost_total=cost,
        simulated_realized_pnl=pnl,
        evidence_id=evidence_id,
        evidence=dict(evidence or {}),
    )
