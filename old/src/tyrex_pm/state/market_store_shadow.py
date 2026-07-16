"""Shadow market store — separate instance for WS comparison (Phase 2 M1).

Live paired-binary decisions must never receive this handle.
"""

from __future__ import annotations

from tyrex_pm.state.market_store import MarketStateStore

MarketStateStoreShadow = MarketStateStore

__all__ = ["MarketStateStoreShadow"]
