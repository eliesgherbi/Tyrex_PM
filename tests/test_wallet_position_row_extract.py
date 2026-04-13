"""Data API ``/positions`` row extraction (Nautilus-aligned schema)."""

from __future__ import annotations

from tyrex_pm.runtime.guru_cache_warmup import (
    extract_wallet_position_row_fields,
)


def test_positions_row_canonical_nautilus_shape() -> None:
    """Matches ``PolymarketExecutionClient._fetch_quantities_from_gamma_api`` field usage."""
    row = {
        "conditionId": "0xabc123",
        "asset": "9876543210",
        "size": 12.5,
    }
    ex = extract_wallet_position_row_fields(row)
    assert ex.skip_reason is None
    assert ex.failure_reason is None
    assert ex.token_id == "9876543210"
    assert ex.condition_id == "0xabc123"
    assert ex.size == 12.5


def test_positions_row_token_alias_token_id() -> None:
    row = {"conditionId": "0xm", "tokenId": "111", "size": 1.0}
    ex = extract_wallet_position_row_fields(row)
    assert ex.token_id == "111"
    assert ex.condition_id == "0xm"


def test_positions_row_token_alias_clob_token_id() -> None:
    row = {"condition_id": "0xn", "clobTokenId": "222", "size": 2.0}
    ex = extract_wallet_position_row_fields(row)
    assert ex.token_id == "222"
    assert ex.condition_id == "0xn"


def test_positions_row_asset_wins_over_alias() -> None:
    row = {
        "asset": "primary",
        "tokenId": "other",
        "size": 1.0,
    }
    ex = extract_wallet_position_row_fields(row)
    assert ex.token_id == "primary"


def test_positions_row_missing_token_with_nonzero_size() -> None:
    row = {"conditionId": "0xx", "size": 5.0}
    ex = extract_wallet_position_row_fields(row)
    assert ex.failure_reason == "missing_outcome_token_field"
    assert ex.condition_id == "0xx"


def test_positions_row_flat_skip() -> None:
    ex = extract_wallet_position_row_fields({"asset": "1", "size": 0})
    assert ex.skip_reason == "flat_size"


def test_positions_row_invalid_size() -> None:
    ex = extract_wallet_position_row_fields({"asset": "1", "size": "x"})
    assert ex.skip_reason == "invalid_size"


def test_positions_row_not_mapping() -> None:
    ex = extract_wallet_position_row_fields(["nope"])
    assert ex.skip_reason == "invalid_row_type"


def test_positions_row_missing_size_key_malformed() -> None:
    ex = extract_wallet_position_row_fields({"asset": "1", "conditionId": "0xx"})
    assert ex.skip_reason == "missing_size_key"


def test_positions_row_null_size_malformed() -> None:
    ex = extract_wallet_position_row_fields({"asset": "1", "size": None})
    assert ex.skip_reason == "null_size"
