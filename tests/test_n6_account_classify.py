"""N6 account classification tests."""

from __future__ import annotations

from tyrex_pm.runtime.n6_account_classify import (
    AccountClassification,
    AcknowledgedExternalPosition,
    classify_account,
)


def test_acknowledged_positions_prevent_global_flat() -> None:
    result = classify_account(
        open_orders=[],
        positions=[],
        selected_market_token_ids={"tok-a"},
        acknowledged=(
            AcknowledgedExternalPosition(label="hist", token_id="old-tok"),
        ),
    )
    assert result.selected_market_flat is True
    assert result.globally_flat is False
    assert (
        AccountClassification.KNOWN_ACKNOWLEDGED_EXTERNAL_POSITIONS
        in result.classifications
    )
    assert result.acknowledged_external[0]["untouched"] is True


def test_open_orders_classification() -> None:
    result = classify_account(
        open_orders=[{"venue_order_id": "x"}],
        positions=[],
        selected_market_token_ids=set(),
    )
    assert AccountClassification.OPEN_ORDERS_PRESENT in result.classifications
