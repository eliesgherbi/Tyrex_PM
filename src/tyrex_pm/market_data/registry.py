"""Instrument / market registry."""

from __future__ import annotations

from tyrex_pm.core.ids import InstrumentId, MarketId
from tyrex_pm.core.instruments import Instrument
from tyrex_pm.domain.polymarket.market import BinaryMarket


class InstrumentRegistry:
    def __init__(self) -> None:
        self._market: BinaryMarket | None = None
        self._by_instrument: dict[InstrumentId, Instrument] = {}

    def set_market(self, market: BinaryMarket) -> None:
        self._market = market
        self._by_instrument = {
            market.yes.instrument_id: market.yes,
            market.no.instrument_id: market.no,
        }

    @property
    def market(self) -> BinaryMarket | None:
        return self._market

    def get_instrument(self, instrument_id: InstrumentId) -> Instrument | None:
        return self._by_instrument.get(instrument_id)

    def require_market(self) -> BinaryMarket:
        if self._market is None:
            raise RuntimeError("market not resolved")
        return self._market

    def market_id(self) -> MarketId | None:
        return None if self._market is None else self._market.market_id
