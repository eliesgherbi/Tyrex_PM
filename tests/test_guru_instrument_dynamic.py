"""Dynamic guru instrument resolution and cache activation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from nautilus_trader.adapters.polymarket.common.parsing import parse_polymarket_instrument
from nautilus_trader.cache.cache import Cache

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.runtime.guru_instrument_dynamic import (
    CacheInstrumentActivator,
    GuruInstrumentDynamicController,
    GuruInstrumentResolveError,
    _fetch_gamma_market_row,
    resolve_binary_option_for_clob_token,
)


def _sample_market_info(*, condition_id: str = "0xcondstep5", token_id: str = "88881") -> dict:
    return {
        "condition_id": condition_id,
        "question": "Step 5 test market?",
        "minimum_tick_size": "0.01",
        "minimum_order_size": 1,
        "end_date_iso": "2027-06-01T00:00:00Z",
        "maker_base_fee": 0,
        "taker_base_fee": 0,
        "tokens": [
            {"token_id": token_id, "outcome": "Yes"},
            {"token_id": "88882", "outcome": "No"},
        ],
    }


def _runtime_dynamic(**kwargs: object) -> RuntimeSettings:
    base = dict(
        trader_id="T-001",
        execution_mode="live",
        guru_poll_interval_seconds=30.0,
        data_api_base_url="https://data-api.polymarket.com",
        guru_dedup_state_path="var/dedup.json",
        guru_state_path="var/wm.json",
        guru_activity_limit=100,
        guru_startup_backfill_seconds=0.0,
        guru_max_activity_pages_per_poll=4,
        logging_level="INFO",
        clob_host="https://clob.polymarket.com",
        chain_id=137,
        polymarket_instrument_ids=("0xabc-1.POLYMARKET",),
        polymarket_token_to_instrument=(("1", "0xabc-1.POLYMARKET"),),
        polymarket_dynamic_instruments=True,
        polymarket_dynamic_max_activations=32,
        polymarket_gamma_base_url="https://gamma-api.polymarket.com",
        polymarket_gamma_http_timeout_seconds=15.0,
        polymarket_startup_token_warmup_max=32,
    )
    base.update(kwargs)
    return RuntimeSettings(**base)  # type: ignore[arg-type]


def test_resolve_binary_option_happy_path_uses_gamma_then_clob() -> None:
    token_id = "777701"
    mi = _sample_market_info(token_id=token_id)
    gamma_row = {"conditionId": mi["condition_id"]}
    clob = MagicMock()
    clob.get_market.return_value = mi

    with patch("tyrex_pm.runtime.guru_instrument_dynamic.httpx.Client") as mock_client_cls:
        mock_resp = MagicMock()
        mock_resp.json.return_value = [gamma_row]
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        inst = resolve_binary_option_for_clob_token(
            token_id,
            clob,
            gamma_base_url="https://gamma-api.polymarket.com",
            http_timeout=10.0,
        )

    assert str(inst.id).startswith("0xcondstep5")
    clob.get_market.assert_called_once_with("0xcondstep5")


def test_resolve_binary_option_gamma_empty_raises() -> None:
    clob = MagicMock()

    with patch("tyrex_pm.runtime.guru_instrument_dynamic.httpx.Client") as mock_client_cls:
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        with pytest.raises(GuruInstrumentResolveError, match="no market"):
            resolve_binary_option_for_clob_token(
                "nope",
                clob,
                gamma_base_url="https://gamma-api.polymarket.com",
                http_timeout=10.0,
            )


def test_fetch_gamma_http_error_wraps() -> None:
    with patch("tyrex_pm.runtime.guru_instrument_dynamic.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = httpx.HTTPError("boom")
        mock_client_cls.return_value = mock_client

        with pytest.raises(httpx.HTTPError):
            _fetch_gamma_market_row(
                "1",
                base_url="https://gamma-api.polymarket.com",
                timeout=1.0,
            )


def test_cache_activator_cap_blocks_new_adds() -> None:
    cache = Cache()
    mi = _sample_market_info(token_id="cap_a")
    a = parse_polymarket_instrument(mi, "cap_a", "Yes", ts_init=1)
    activator = CacheInstrumentActivator(cache, max_new_activations=1)

    ok1, r1 = activator.try_add_instrument(a)
    assert ok1 and r1 == "activated"

    mi2 = _sample_market_info(condition_id="0xother", token_id="cap_b")
    b = parse_polymarket_instrument(mi2, "cap_b", "Yes", ts_init=2)
    ok2, r2 = activator.try_add_instrument(b)
    assert not ok2 and r2 == "activation_cap"


def test_cache_activator_already_cached_no_budget_use() -> None:
    cache = Cache()
    mi = _sample_market_info()
    inst = parse_polymarket_instrument(mi, "88881", "Yes", ts_init=1)
    cache.add_currency(inst.quote_currency)
    cache.add_instrument(inst)

    activator = CacheInstrumentActivator(cache, max_new_activations=0)
    ok, r = activator.try_add_instrument(inst)
    assert ok and r == "already_cached"


def test_resolve_and_activate_uses_cache_scan_before_http() -> None:
    """Cache scan matches guru ``asset`` via ``get_polymarket_token_id`` (**Package-source**)."""
    cache = Cache()
    mi = _sample_market_info(token_id="88881")
    bo = parse_polymarket_instrument(mi, "88881", "Yes", ts_init=1)
    cache.add_currency(bo.quote_currency)
    cache.add_instrument(bo)
    rt = _runtime_dynamic()
    clob = MagicMock()
    ctrl = GuruInstrumentDynamicController(cache, clob, rt)
    out, err = ctrl.resolve_and_activate("88881")
    assert err == ""
    assert out is not None
    clob.get_market.assert_not_called()


def test_guru_instrument_dynamic_controller_integration() -> None:
    cache = Cache()
    rt = _runtime_dynamic(polymarket_dynamic_max_activations=4)
    clob = MagicMock()
    mi = _sample_market_info(token_id="901")
    clob.get_market.return_value = mi

    with patch("tyrex_pm.runtime.guru_instrument_dynamic.httpx.Client") as mock_client_cls:
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"conditionId": mi["condition_id"]}]
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        ctrl = GuruInstrumentDynamicController(cache, clob, rt)
        out, err = ctrl.resolve_and_activate("901")

    assert err == ""
    assert out is not None
    assert cache.instrument(out.id) is not None
