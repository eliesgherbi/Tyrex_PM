"""Immutable risk context assembled by the host."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketStatus
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import ExecutableQuote
from tyrex_pm.risk.dedup import DedupRecord, IntentDedupRegistry

_ = MarketStatus  # re-exported usage in helpers below


@dataclass(frozen=True, kw_only=True)
class RiskConfigView:
    max_notional: Decimal
    min_price: Decimal
    max_price: Decimal
    max_spread: Decimal
    min_liquidity_notional: Decimal
    no_entry_before_close: timedelta
    kill_switch_active: bool
    config_fingerprint: str


@dataclass(frozen=True, kw_only=True)
class BookReadiness:
    initialized: bool
    recovery_required: bool
    tick_size: Decimal | None


@dataclass(frozen=True, kw_only=True)
class PortfolioRiskView:
    """Immutable portfolio/lifecycle view for risk. Missing view fails closed."""

    available: bool
    net_quantity: Decimal
    total_cost_notional: Decimal
    lifecycle_state: str
    has_pending_order: bool
    max_position_notional: Decimal
    max_total_exposure: Decimal


@dataclass(frozen=True, kw_only=True)
class RiskContext:
    mode: RuntimeMode
    now: datetime
    market: BinaryMarket
    snapshot: DecisionSnapshot
    yes_quote: ExecutableQuote
    no_quote: ExecutableQuote
    yes_book: BookReadiness
    no_book: BookReadiness
    risk_config: RiskConfigView
    dedup: IntentDedupRegistry
    # Never interpret missing exposure as zero.
    exposure_available: bool = False
    portfolio: PortfolioRiskView | None = None
    # Composition-supplied: may the framework accept HoldToResolutionIntent?
    resolution_capability_available: bool = False

    def quote_for_instrument(self, instrument_id: str) -> ExecutableQuote:
        if instrument_id == self.market.yes.instrument_id.value:
            return self.yes_quote
        if instrument_id == self.market.no.instrument_id.value:
            return self.no_quote
        raise KeyError(instrument_id)

    def readiness_for_instrument(self, instrument_id: str) -> BookReadiness:
        if instrument_id == self.market.yes.instrument_id.value:
            return self.yes_book
        if instrument_id == self.market.no.instrument_id.value:
            return self.no_book
        raise KeyError(instrument_id)

    def market_is_active(self) -> bool:
        return self.market.status in (MarketStatus.ACTIVE, MarketStatus.UNKNOWN)

    def lookup_duplicate(self, semantic_key: str) -> DedupRecord | None:
        return self.dedup.lookup(semantic_key, now=self.now)
