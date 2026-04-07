"""Nautilus CashAccount-shaped dict → USDC free."""

from __future__ import annotations

from tyrex_pm.runtime.nautilus_cash_extract import extract_nautilus_cash_free_usd


def test_extract_nested_polymarket_shape() -> None:
    balances = {
        "events": [
            {
                "account_id": "POLYMARKET-001",
                "balances": [
                    {
                        "currency": "USDC.e",
                        "free": "0.423789",
                        "locked": "0.000000",
                    },
                ],
            },
        ],
    }
    got, note = extract_nautilus_cash_free_usd(balances)
    assert abs(got - 0.423789) < 1e-9
    assert note == "single_usdc_free"
