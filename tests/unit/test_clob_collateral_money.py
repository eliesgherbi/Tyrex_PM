"""Polymarket CLOB collateral string → USD normalization."""

from __future__ import annotations

from tyrex_pm.runtime.clob_collateral_money import parse_clob_collateral_usd


def test_integer_string_is_atomic_6decimals() -> None:
    p = parse_clob_collateral_usd({"balance": "423789", "allowance": None})
    assert abs(p.balance_usd - 0.423789) < 1e-9
    assert p.balance_parse_note == "polymarket_atomic_usdc_6"
    assert p.allowance_usd is None


def test_decimal_string_unchanged() -> None:
    p = parse_clob_collateral_usd({"balance": "100.0", "allowance": "0.5"})
    assert p.balance_usd == 100.0
    assert p.allowance_usd == 0.5
    assert p.balance_parse_note == "decimal_string"


def test_zero_atom() -> None:
    p = parse_clob_collateral_usd({"balance": "0", "allowance": "0"})
    assert p.balance_usd == 0.0
    assert p.allowance_usd == 0.0


def test_preserves_raw() -> None:
    p = parse_clob_collateral_usd({"balance": "42"})
    assert p.balance_raw == "42"

