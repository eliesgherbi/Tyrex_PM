"""Runtime implementation of :class:`~tyrex_pm.signal.layer_a.types.LayerAContext`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nautilus_trader.cache.cache import Cache
from nautilus_trader.portfolio.portfolio import Portfolio

from tyrex_pm.runtime.state_readers import instrument_id_for_outcome_token


def _float_venue_sz(sz: Any) -> float:
    if sz is None:
        return 0.0
    try:
        return max(0.0, float(sz))
    except (TypeError, ValueError):
        return 0.0


def _float_portfolio_net(net: Any) -> float:
    if net is None:
        return 0.0
    try:
        if hasattr(net, "as_double"):
            return float(net.as_double())
        if hasattr(net, "as_decimal"):
            return float(net.as_decimal())  # type: ignore[arg-type]
        return float(net)
    except (TypeError, ValueError):
        return 0.0


class NautilusLayerAContext:
    """
    Follower long quantity from :class:`~nautilus_trader.portfolio.portfolio.Portfolio`
    or :class:`~tyrex_pm.runtime.venue_state.VenueState` (Tier A) for ``full_exit`` interpretation.
    """

    __slots__ = ("_portfolio", "_cache", "_static", "_venue_state", "_venue_state_reads_enabled")

    def __init__(
        self,
        portfolio: Portfolio,
        cache: Cache,
        static_token_to_instrument: Mapping[str, str],
        *,
        venue_state: Any | None = None,
        venue_state_reads_enabled: bool = False,
    ) -> None:
        self._portfolio = portfolio
        self._cache = cache
        self._static = static_token_to_instrument
        self._venue_state = venue_state
        self._venue_state_reads_enabled = bool(venue_state_reads_enabled)

    def follower_long_qty_for_outcome_token(self, token_id: str) -> float | None:
        iid = instrument_id_for_outcome_token(
            self._cache,
            token_id,
            static_token_to_instrument=self._static,
        )
        if iid is None:
            return None
        if self._cache.instrument(iid) is None:
            return None
        if self._venue_state_reads_enabled and self._venue_state is not None:
            sz = self._venue_state.position_size(iid)
            return _float_venue_sz(sz)
        net = self._portfolio.net_position(iid)
        return max(0.0, _float_portfolio_net(net))
