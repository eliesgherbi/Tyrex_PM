"""Sell-test strategy: validates the V2 SELL / exit path end-to-end without guru polling.

See ``Docs/Implementation/sell_feature/`` and ``strategy.py`` for behavior. This
module exists so ``app.raw["strategy"]["kind"] == "sell_test"`` runs activate a
standalone state machine (one BUY → wait for sellable inventory → one SELL),
keeping the guru-follow code path unaffected.
"""

from tyrex_pm.strategies.sell_test.strategy import (
    SELL_TEST_FACT_SOURCE,
    SellTestState,
    SellTestStrategy,
)

__all__ = ["SELL_TEST_FACT_SOURCE", "SellTestState", "SellTestStrategy"]
