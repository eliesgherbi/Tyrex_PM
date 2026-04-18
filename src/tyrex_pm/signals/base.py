from __future__ import annotations

from dataclasses import dataclass

from tyrex_pm.core.models import GuruTradeSignal


@dataclass(frozen=True)
class GuruCopySignal:
    """Enriched guru signal for strategy (parity: same as row)."""

    trade: GuruTradeSignal
