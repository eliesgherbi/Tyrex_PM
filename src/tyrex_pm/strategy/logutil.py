"""Structured log text helpers (testable without a full TradingNode)."""


def strategy_started_line(*, trader_id: str, strategy_id: str) -> str:
    """Key=value line required by milestone v1.03 review evidence."""
    return (
        f"component=strategy event=strategy_started "
        f"trader_id={trader_id} strategy_id={strategy_id}"
    )
