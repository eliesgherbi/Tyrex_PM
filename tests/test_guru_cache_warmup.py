"""Self-bootstrap cache warmup from guru activity."""

from __future__ import annotations

import logging
from dataclasses import replace
from unittest.mock import MagicMock, patch

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.runtime.guru_cache_warmup import (
    WARMUP_OUTCOME_EMPTY_POSITIONS_API,
    WARMUP_OUTCOME_FAILURE_ALL,
    WARMUP_OUTCOME_NO_ELIGIBLE_ROWS,
    WARMUP_OUTCOME_SUCCESS,
    fetch_wallet_position_rows,
    warm_polymarket_cache_from_guru_activity,
    warm_polymarket_cache_from_wallet_positions,
)
from tyrex_pm.runtime.guru_instrument_dynamic import WalletPositionResolveOutcome


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
        polymarket_instrument_ids=(),
        polymarket_token_to_instrument=(),
        polymarket_dynamic_instruments=True,
        polymarket_dynamic_max_activations=32,
        polymarket_gamma_base_url="https://gamma-api.polymarket.com",
        polymarket_gamma_http_timeout_seconds=15.0,
        polymarket_startup_token_warmup_max=warmup,
        polymarket_wallet_position_warmup_max=0,
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


def test_wallet_warmup_calls_resolve_wallet_per_nonzero_position() -> None:
    rt = replace(_rt(warmup=0), polymarket_wallet_position_warmup_max=10)
    ctrl = MagicMock()
    inst = MagicMock()
    ctrl.resolve_and_activate_wallet_position.return_value = WalletPositionResolveOutcome(
        inst,
        "",
        None,
    )

    rows = [
        {"asset": "111", "size": 5},
        {"asset": "111", "size": 5},
        {"asset": "222", "size": 0.0},
        {"asset": "333", "size": 1.2},
    ]

    with patch(
        "tyrex_pm.runtime.guru_cache_warmup.fetch_wallet_position_rows",
        return_value=rows,
    ):
        with patch(
            "tyrex_pm.runtime.guru_cache_warmup._follower_positions_api_user",
            return_value="0xabc",
        ):
            n = warm_polymarket_cache_from_wallet_positions(ctrl, runtime=rt)

    assert n == 2
    assert ctrl.resolve_and_activate_wallet_position.call_count == 2


def test_wallet_warmup_passes_row_condition_id_from_api_row() -> None:
    rt = replace(_rt(warmup=0), polymarket_wallet_position_warmup_max=10)
    ctrl = MagicMock()
    inst = MagicMock()
    ctrl.resolve_and_activate_wallet_position.return_value = WalletPositionResolveOutcome(
        inst,
        "",
        None,
    )

    rows = [
        {
            "conditionId": "0xcond1",
            "asset": "999",
            "size": 1.0,
        },
    ]

    with patch(
        "tyrex_pm.runtime.guru_cache_warmup.fetch_wallet_position_rows",
        return_value=rows,
    ):
        with patch(
            "tyrex_pm.runtime.guru_cache_warmup._follower_positions_api_user",
            return_value="0xabc",
        ):
            warm_polymarket_cache_from_wallet_positions(ctrl, runtime=rt)

    ctrl.resolve_and_activate_wallet_position.assert_called_once()
    _args, kwargs = ctrl.resolve_and_activate_wallet_position.call_args
    assert _args[0] == "999"
    assert kwargs.get("row_condition_id") == "0xcond1"


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


def test_wallet_warmup_logs_data_api_empty_and_full_positions_user(caplog) -> None:
    """``rows=0`` is a successful fetch with no positions — must not look like a silent bug."""
    caplog.set_level(logging.INFO, logger="tyrex_pm.runtime.guru_cache_warmup")
    rt = replace(_rt(warmup=0), polymarket_wallet_position_warmup_max=10)
    ctrl = MagicMock()
    addr = "0x64367d66bf08d3c8623530bc3113e60908c82bb0"

    with patch(
        "tyrex_pm.runtime.guru_cache_warmup.fetch_wallet_position_rows",
        return_value=[],
    ):
        with patch(
            "tyrex_pm.runtime.guru_cache_warmup._follower_positions_api_user",
            return_value=addr,
        ):
            n = warm_polymarket_cache_from_wallet_positions(ctrl, runtime=rt)

    assert n == 0
    ctrl.resolve_and_activate_wallet_position.assert_not_called()
    messages = [r.message for r in caplog.records]
    assert any(
        "event=wallet_position_warmup_data_api_empty" in m and addr in m for m in messages
    )
    assert any(
        "event=wallet_position_warmup_done" in m and f"positions_user={addr}" in m
        for m in messages
    )
    assert any(
        WARMUP_OUTCOME_EMPTY_POSITIONS_API in m for m in messages
    )


def test_wallet_warmup_done_classifies_failure_all(caplog) -> None:
    """Non-empty /positions with resolvable tokens but resolve fails → failure_all_resolvable."""
    caplog.set_level(logging.INFO, logger="tyrex_pm.runtime.guru_cache_warmup")
    rt = replace(_rt(warmup=0), polymarket_wallet_position_warmup_max=10)
    ctrl = MagicMock()
    ctrl.resolve_and_activate_wallet_position.return_value = WalletPositionResolveOutcome(
        None,
        "gamma_empty",
        "Gamma returned no market",
    )
    rows = [{"asset": "111", "size": 1.0, "conditionId": "0xc1"}]

    with patch(
        "tyrex_pm.runtime.guru_cache_warmup.fetch_wallet_position_rows",
        return_value=rows,
    ):
        with patch(
            "tyrex_pm.runtime.guru_cache_warmup._follower_positions_api_user",
            return_value="0xabc",
        ):
            n = warm_polymarket_cache_from_wallet_positions(ctrl, runtime=rt)

    assert n == 0
    messages = " ".join(r.message for r in caplog.records)
    assert WARMUP_OUTCOME_FAILURE_ALL in messages
    assert "failure_details=gamma_empty:1" in messages


def test_wallet_warmup_done_success_outcome(caplog) -> None:
    caplog.set_level(logging.INFO, logger="tyrex_pm.runtime.guru_cache_warmup")
    rt = replace(_rt(warmup=0), polymarket_wallet_position_warmup_max=10)
    ctrl = MagicMock()
    inst = MagicMock()
    ctrl.resolve_and_activate_wallet_position.return_value = WalletPositionResolveOutcome(
        inst,
        "",
        None,
    )
    rows = [{"asset": "999", "size": 2.0}]

    with patch(
        "tyrex_pm.runtime.guru_cache_warmup.fetch_wallet_position_rows",
        return_value=rows,
    ):
        with patch(
            "tyrex_pm.runtime.guru_cache_warmup._follower_positions_api_user",
            return_value="0xabc",
        ):
            n = warm_polymarket_cache_from_wallet_positions(ctrl, runtime=rt)
    assert n == 1
    assert any(WARMUP_OUTCOME_SUCCESS in r.message for r in caplog.records)


def test_wallet_warmup_no_eligible_rows_all_flat(caplog) -> None:
    caplog.set_level(logging.INFO, logger="tyrex_pm.runtime.guru_cache_warmup")
    rt = replace(_rt(warmup=0), polymarket_wallet_position_warmup_max=10)
    ctrl = MagicMock()
    rows = [
        {"asset": "1", "size": 0},
        {"asset": "2", "size": 0.0},
    ]
    with patch(
        "tyrex_pm.runtime.guru_cache_warmup.fetch_wallet_position_rows",
        return_value=rows,
    ):
        with patch(
            "tyrex_pm.runtime.guru_cache_warmup._follower_positions_api_user",
            return_value="0xabc",
        ):
            n = warm_polymarket_cache_from_wallet_positions(ctrl, runtime=rt)
    assert n == 0
    ctrl.resolve_and_activate_wallet_position.assert_not_called()
    assert any(WARMUP_OUTCOME_NO_ELIGIBLE_ROWS in r.message for r in caplog.records)


def test_fetch_wallet_position_rows_stops_on_non_list_payload() -> None:
    with patch("tyrex_pm.runtime.guru_cache_warmup.httpx.Client") as mcls:
        mclient = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"not": "a list"}
        mclient.__enter__.return_value = mclient
        mclient.__exit__.return_value = False
        mclient.get.return_value = resp
        mcls.return_value = mclient
        out = fetch_wallet_position_rows(
            user_address="0xa",
            data_api_base_url="https://data-api.polymarket.com",
            timeout=5.0,
        )
    assert out == []
