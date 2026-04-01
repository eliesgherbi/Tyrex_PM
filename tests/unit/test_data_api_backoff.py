"""Polymarket Data API client backoff (v1.04)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from tyrex_pm.data.data_api_client import PolymarketDataApiClient


@patch("tyrex_pm.data.data_api_client.time.sleep")
@patch("tyrex_pm.data.data_api_client.httpx.Client")
def test_429_retries_then_ok(mock_client_cls, _sleep) -> None:
    logs: list[dict] = []

    def log_backoff(**kwargs):
        logs.append(kwargs)

    mock_inst = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_inst

    r429 = MagicMock()
    r429.status_code = 429
    r429.headers = MagicMock()
    r429.headers.get = lambda k, d=None: "0" if k == "Retry-After" else d

    r200 = MagicMock()
    r200.status_code = 200
    r200.json.return_value = [
        {"transactionHash": "0xab", "timestamp": 1, "side": "BUY", "asset": "1"},
    ]

    mock_inst.get.side_effect = [r429, r200]

    client = PolymarketDataApiClient(log_backoff=log_backoff)
    out = client.get_trades(user="0x56687bf447db6ffa42ffe2204a05edaa20f55839")

    assert len(out) == 1
    assert mock_inst.get.call_count == 2
    assert any(x.get("event") == "poller_backoff" for x in logs)
