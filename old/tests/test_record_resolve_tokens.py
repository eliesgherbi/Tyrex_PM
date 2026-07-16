"""Record mode token resolution (--event-url precedence)."""

from __future__ import annotations

import pytest

from tyrex_pm.core.errors import ConfigError
from tyrex_pm.runtime.config import RecordingConfig, parse_app_config
from tyrex_pm.runtime.record_run import _resolve_record_tokens, _validate_record_token_source
from tyrex_pm.venue.polymarket.event_metadata import PairedBinaryEventMetadata

URL_META = PairedBinaryEventMetadata(
    market_id="btc_5m_20260703_1445",
    condition_id="0xabc",
    yes_token_id="96206845860005414098215218762900890699563998201528924855754931333698106492944",
    no_token_id="94642669085769196752261559800019698787765660477050217264451640410131389089540",
    event_start_ts=1783089600.0,
    event_end_ts=1783089900.0,
    event_slug="btc-updown-5m-1783089600",
    event_title="Bitcoin Up or Down",
    yes_outcome_label="Up",
    no_outcome_label="Down",
    market_slug="btc-updown-5m-1783089600",
)

SCENARIO_REC = RecordingConfig(
    enabled=True,
    market_id="btc_5m_record_single",
    yes_token_id="60623465405255973232915789757360817490886260321202505048709097730361810974033",
    no_token_id="39973642943404485458661593289415231607414175313101347321562244976405845796515",
)


def _app_with_recording(rec: RecordingConfig):
    from paired_binary_shutdown_helpers import app_cfg, risk_cfg, strategy_cfg

    strategy = strategy_cfg()
    strategy["enabled"] = False
    return parse_app_config(
        risk=risk_cfg(),
        strategy=strategy,
        runtime={
            "execution_mode": "shadow",
            "recording": {
                "enabled": rec.enabled,
                "output_dir": rec.output_dir,
                "market_id": rec.market_id,
                "yes_token_id": rec.yes_token_id,
                "no_token_id": rec.no_token_id,
            },
            "market_data": {"enabled": True},
        },
    )


def test_event_url_meta_overrides_scenario_recording_tokens() -> None:
    app = _app_with_recording(SCENARIO_REC)
    market_id, yes, no = _resolve_record_tokens(app, SCENARIO_REC, event_meta=URL_META)
    assert market_id == URL_META.market_id
    assert yes == URL_META.yes_token_id
    assert no == URL_META.no_token_id


def test_recording_block_used_without_event_url() -> None:
    app = _app_with_recording(SCENARIO_REC)
    market_id, yes, no = _resolve_record_tokens(app, SCENARIO_REC)
    assert market_id == SCENARIO_REC.market_id
    assert yes == SCENARIO_REC.yes_token_id
    assert no == SCENARIO_REC.no_token_id


def test_missing_recording_tokens_requires_event_url() -> None:
    rec = RecordingConfig(enabled=True)
    with pytest.raises(ConfigError, match="--event-url"):
        _validate_record_token_source(rec, event_url=None, discovery_enabled=False)


def test_discovery_enabled_bypasses_event_url_requirement() -> None:
    rec = RecordingConfig(enabled=True)
    _validate_record_token_source(rec, event_url=None, discovery_enabled=True)
