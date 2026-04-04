"""Step 5: self-bootstrap cache warmup from guru activity."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.runtime.guru_cache_warmup import warm_polymarket_cache_from_guru_activity


def _rt(*, warmup: int = 4) -> RuntimeSettings:
    return RuntimeSettings(
        trader_id="T-001",
        execution_mode="live",
        guru_poll_interval_seconds=30.0,
        data_api_base_url="https://data-api.polymarket.com",
        guru_dedup_state_path="var/d.json",
        guru_state_path="var/w.json",
        guru_activity_limit=50,
        guru_startup_backfill_seconds=0.0,
        guru_max_activity_pages_per_poll=4,
        logging_level="INFO",
        clob_host="https://clob.polymarket.com",
        chain_id=137,
        polymarket_nautilus_live=True,
        polymarket_instrument_ids=(),
        polymarket_framework_submit=True,
        polymarket_token_to_instrument=(),
        polymarket_dynamic_instruments=True,
        polymarket_dynamic_max_activations=32,
        polymarket_gamma_base_url="https://gamma-api.polymarket.com",
        polymarket_gamma_http_timeout_seconds=15.0,
        polymarket_startup_token_warmup_max=warmup,
    )


def test_warm_cache_calls_resolve_per_distinct_asset() -> None:
    rt = _rt(warmup=10)
    ctrl = MagicMock()
    inst = MagicMock()
    ctrl.resolve_and_activate.return_value = (inst, "")

    client_instance = MagicMock()
    client_instance.get_user_trade_activity.return_value = [
        {"asset": "111", "timestamp": 1},
        {"asset": "111", "timestamp": 2},
        {"asset": "222", "timestamp": 3},
    ]

    with patch("tyrex_pm.runtime.guru_cache_warmup.PolymarketDataApiClient") as mcli:
        mcli.return_value = client_instance
        n = warm_polymarket_cache_from_guru_activity(
            ctrl,
            guru_wallet_address="0x1234567890123456789012345678901234567890",
            runtime=rt,
        )

    assert n == 2
    assert ctrl.resolve_and_activate.call_count == 2


def test_warm_cache_skips_when_cap_zero() -> None:
    rt = _rt(warmup=0)
    ctrl = MagicMock()
    n = warm_polymarket_cache_from_guru_activity(
        ctrl,
        guru_wallet_address="0x1234567890123456789012345678901234567890",
        runtime=rt,
    )
    assert n == 0
    ctrl.resolve_and_activate.assert_not_called()
