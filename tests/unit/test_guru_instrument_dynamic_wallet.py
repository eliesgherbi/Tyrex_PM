"""Step 1 DoD: resolve_and_activate_by_condition_and_token on GuruInstrumentDynamicController."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.model.identifiers import Venue

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.runtime.guru_instrument_dynamic import (
    GuruInstrumentDynamicController,
    GuruInstrumentResolveError,
)


def _make_runtime(**overrides) -> RuntimeSettings:
    defaults = dict(
        trader_id="T-001",
        execution_mode="live",
        guru_poll_interval_seconds=30.0,
        data_api_base_url="https://data-api.polymarket.com",
        guru_dedup_state_path="var/dedup.json",
        guru_state_path="var/state.json",
        guru_activity_limit=200,
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
        polymarket_startup_token_warmup_max=32,
    )
    defaults.update(overrides)
    return RuntimeSettings(**defaults)


def _make_instrument_mock(condition_id: str, token_id: str):
    """Create a mock instrument with id that returns proper condition/token ids."""
    from nautilus_trader.model.identifiers import InstrumentId, Symbol

    inst = MagicMock()
    inst.id = InstrumentId(Symbol(f"{condition_id}-{token_id}"), Venue(POLYMARKET))
    inst.quote_currency = MagicMock()
    return inst


class TestResolveAndActivateByConditionAndToken:
    def _make_ctrl(self, cache=None, clob=None, runtime=None):
        if cache is None:
            cache = MagicMock()
            cache.instruments.return_value = []
            cache.instrument.return_value = None
        if clob is None:
            clob = MagicMock()
        if runtime is None:
            runtime = _make_runtime()
        return GuruInstrumentDynamicController(cache, clob, runtime)

    def test_already_cached(self) -> None:
        inst = _make_instrument_mock("cond1", "tok1")
        cache = MagicMock()
        cache.instruments.return_value = [inst]
        ctrl = self._make_ctrl(cache=cache)

        outcome = ctrl.resolve_and_activate_by_condition_and_token("cond1", "tok1")
        assert outcome.instrument is inst
        assert outcome.detail == ""

    def test_success_resolves_and_adds(self) -> None:
        cache = MagicMock()
        cache.instruments.return_value = []

        parsed_inst = _make_instrument_mock("cond1", "tok1")
        cache.instrument.return_value = parsed_inst

        ctrl = self._make_ctrl(cache=cache)

        with patch(
            "tyrex_pm.runtime.guru_instrument_dynamic.resolve_binary_option_for_condition_and_token",
            return_value=parsed_inst,
        ):
            outcome = ctrl.resolve_and_activate_by_condition_and_token("cond1", "tok1")

        assert outcome.instrument is parsed_inst
        assert outcome.detail == ""

    def test_clob_error_string(self) -> None:
        cache = MagicMock()
        cache.instruments.return_value = []

        ctrl = self._make_ctrl(cache=cache)

        with patch(
            "tyrex_pm.runtime.guru_instrument_dynamic.resolve_binary_option_for_condition_and_token",
            side_effect=GuruInstrumentResolveError("bad", detail="clob_error_string"),
        ):
            outcome = ctrl.resolve_and_activate_by_condition_and_token("cond1", "tok1")

        assert outcome.instrument is None
        assert outcome.detail == "clob_error_string"

    def test_parse_failed(self) -> None:
        cache = MagicMock()
        cache.instruments.return_value = []

        ctrl = self._make_ctrl(cache=cache)

        with patch(
            "tyrex_pm.runtime.guru_instrument_dynamic.resolve_binary_option_for_condition_and_token",
            side_effect=GuruInstrumentResolveError("parse err", detail="parse_failed"),
        ):
            outcome = ctrl.resolve_and_activate_by_condition_and_token("cond1", "tok1")

        assert outcome.instrument is None
        assert outcome.detail == "parse_failed"
