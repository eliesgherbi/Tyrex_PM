"""Strategy observability line (v1.03)."""

from tyrex_pm.strategy.logutil import strategy_started_line


def test_strategy_started_line_shape():
    line = strategy_started_line(trader_id="TRADER-1", strategy_id="STRAT-1")
    assert "component=strategy" in line
    assert "event=strategy_started" in line
    assert "trader_id=TRADER-1" in line
    assert "strategy_id=STRAT-1" in line
