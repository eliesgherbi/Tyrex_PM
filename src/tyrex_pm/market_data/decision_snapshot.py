"""Immutable decision snapshot for one observe evaluation cycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tyrex_pm.core.ids import CorrelationId, EventId
from tyrex_pm.core.snapshots import BookSnapshot, ReferencePriceSnapshot
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.market_data.executable import ExecutableQuote
from tyrex_pm.market_data.freshness import FreshnessAssessment


@dataclass(frozen=True, kw_only=True)
class DecisionSnapshot:
    market: BinaryMarket
    yes_book: BookSnapshot | None
    no_book: BookSnapshot | None
    yes_quote: ExecutableQuote
    no_quote: ExecutableQuote
    reference: ReferencePriceSnapshot | None
    yes_freshness: FreshnessAssessment
    no_freshness: FreshnessAssessment
    reference_freshness: FreshnessAssessment
    observed_at: datetime
    correlation_id: CorrelationId
    causation_id: EventId | None = None
