"""Phase 5 — :class:`InstrumentReadinessPolicy` (``execution_truth_alignment.md``)."""

from __future__ import annotations

from unittest.mock import MagicMock

from nautilus_trader.model.identifiers import InstrumentId

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.runtime.lifecycle.instrument_readiness_policy import InstrumentReadinessPolicy


def _rt(**kwargs: object) -> RuntimeSettings:
    base = RuntimeSettings(
        trader_id="T-001",
        execution_mode="live",
        guru_poll_interval_seconds=30.0,
        data_api_base_url="https://data-api.polymarket.com",
        guru_dedup_state_path="var/guru_dedup.json",
        guru_state_path="var/guru_watermark.json",
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
    if not kwargs:
        return base
    from dataclasses import replace

    return replace(base, **kwargs)


def test_gate_ready_empty_instruments_waived() -> None:
    rt = _rt()
    pol = InstrumentReadinessPolicy(rt)
    cache = MagicMock()
    assert pol.gate_ready(cache) == (True, None)


def test_allow_submit_dynamic_only_waives() -> None:
    rt = _rt(polymarket_instrument_ids=(), polymarket_token_to_instrument=())
    pol = InstrumentReadinessPolicy(rt)
    cache = MagicMock()
    assert pol.allow_submit("any", cache) is True


def test_allow_submit_mapped_requires_cache_instrument() -> None:
    rt = _rt(
        polymarket_instrument_ids=("0xabc-0xdef.POLYMARKET",),
        polymarket_token_to_instrument=(("tok", "0xabc-0xdef.POLYMARKET"),),
        polymarket_dynamic_instruments=False,
    )
    pol = InstrumentReadinessPolicy(rt)
    cache = MagicMock()
    cache.instrument.return_value = None
    assert pol.allow_submit("tok", cache) is False
    inst = MagicMock()
    cache.instrument.return_value = inst
    iid = InstrumentId.from_str("0xabc-0xdef.POLYMARKET")
    assert pol.allow_submit("tok", cache) is True
    cache.instrument.assert_called_with(iid)
