"""Composed risk inputs — Phase 2 wires ``health`` only; deployment/capital optional later."""

from __future__ import annotations

from dataclasses import dataclass

from tyrex_pm.runtime.capital.state import CapitalState
from tyrex_pm.runtime.tradable_state.types import TradableStateHealthSnapshot


@dataclass(frozen=True, slots=True)
class RiskStateView:
    """
    Planning contract: health + (later) deployment + capital.

    Phase 2: only ``health`` is required. Optional fields support startup readiness / reporting
    composition without a second policy table.
    """

    health: TradableStateHealthSnapshot
    capital: CapitalState | None = None
