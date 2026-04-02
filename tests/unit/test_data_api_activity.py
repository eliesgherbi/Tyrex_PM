"""Data API /activity client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tyrex_pm.data.data_api_client import PolymarketDataApiClient


@patch("tyrex_pm.data.data_api_client.httpx.Client")
def test_get_user_trade_activity_builds_query(mock_client_cls) -> None:
    mock_inst = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_inst
    r200 = MagicMock()
    r200.status_code = 200
    r200.json.return_value = [{"type": "TRADE", "timestamp": 1, "asset": "1"}]
    mock_inst.get.return_value = r200

    client = PolymarketDataApiClient()
    out = client.get_user_trade_activity(
        user="0x56687bf447db6ffa42ffe2204a05edaa20f55839",
        limit=50,
        offset=100,
        start_ts_sec=1_700_000,
    )

    assert len(out) == 1
    call_kw = mock_inst.get.call_args
    assert "/activity" in str(call_kw[0][0])
    params = call_kw[1]["params"]
    assert ("type", "TRADE") in params
    assert ("start", 1_700_000) in params
    assert ("sortDirection", "ASC") in params


def test_activity_limit_validation() -> None:
    client = PolymarketDataApiClient()
    with pytest.raises(ValueError, match="limit"):
        client.get_user_trade_activity(user="0x56687bf447db6ffa42ffe2204a05edaa20f55839", limit=501)
