"""V1-d: a market family defined only in configuration (BTC 15-minute) is accepted by the
runtime family registry, run configs, discovery binding and the warehouse profile check."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tyrex_pm.adapters.polymarket.discovery import bind_btc_5m_gamma_event
from tyrex_pm.data_warehouse.config import Profile, Selector
from tyrex_pm.data_warehouse.families.btc_5m import RecurringWindow, normalized_families
from tyrex_pm.domain.polymarket.discovery_binding import DiscoverySessionRole
from tyrex_pm.domain.polymarket.window_families import window_families, window_family
from tyrex_pm.runtime.market_family import get_market_family
from tyrex_pm.runtime.run_config import RunConfigError, market_window_family

REPO = Path(__file__).resolve().parents[3]
START = 1_784_582_100 // 900 * 900  # a 15-minute boundary


def test_families_come_from_configuration() -> None:
    families = window_families()
    assert families["btc_updown_5m"].window_duration_s == 300
    fifteen = families["btc_updown_15m"]
    assert (fifteen.window_duration_s, fifteen.slug_prefix, fifteen.enabled) == (
        900, "btc-updown-15m", False
    )
    assert fifteen.slug(START) == f"btc-updown-15m-{START}"
    now = datetime.fromtimestamp(START + 100, tz=timezone.utc)
    assert fifteen.current_slug(now) == f"btc-updown-15m-{START}"
    assert fifteen.next_slug(now) == f"btc-updown-15m-{START + 900}"
    assert fifteen.seconds_to_next_window(now) == 800.0


def test_runtime_registry_and_run_config_accept_the_family() -> None:
    assert get_market_family("btc_updown_15m").window_duration_s == 900
    family = market_window_family({"family": "btc_updown_15m"})
    assert family.binance_symbol == "BTCUSDT"
    with pytest.raises(RunConfigError, match="disagrees"):
        market_window_family({"family": "btc_updown_15m", "binance_symbol": "ETHUSDT"})
    with pytest.raises(RunConfigError, match="unknown market family"):
        market_window_family({"family": "btc_updown_7m"})


def _fifteen_minute_event(end_offset_s: int = 900) -> dict:
    event = json.loads((REPO / "tests/fixtures/n2/gamma_btc_5m_event.json").read_text("utf-8"))
    start = datetime.fromtimestamp(START, tz=timezone.utc)
    event["slug"] = f"btc-updown-15m-{START}"
    market = event["markets"][0]
    market["eventStartTime"] = start.isoformat().replace("+00:00", "Z")
    market["endDate"] = (start + timedelta(seconds=end_offset_s)).isoformat().replace(
        "+00:00", "Z"
    )
    return event


def test_discovery_binds_a_fifteen_minute_window() -> None:
    binding = bind_btc_5m_gamma_event(
        _fifteen_minute_event(),
        expected_slug=f"btc-updown-15m-{START}",
        session_role=DiscoverySessionRole.ACTIVE,
    )
    window = binding.market.event_end - binding.market.event_start
    assert window == timedelta(seconds=900)
    with pytest.raises(ValueError, match="MARKET_WINDOW_END_MISMATCH"):
        bind_btc_5m_gamma_event(
            _fifteen_minute_event(end_offset_s=300),
            expected_slug=f"btc-updown-15m-{START}",
        )


def test_warehouse_profile_and_family_accept_it() -> None:
    profile = replace(
        Profile(),
        selector=Selector(duration_s=900, slug_pattern=r"^btc-updown-15m-[0-9]+$"),
    )
    profile.validate()
    assert profile.window_family().family_id == "btc_updown_15m"
    with pytest.raises(ValueError, match="slug_pattern"):
        replace(Profile(), selector=Selector(duration_s=900)).validate()
    family = RecurringWindow(window_family("btc_updown_15m"))
    entity = family.identify({
        "id": "fixture", "slug": f"btc-updown-15m-{START}", "conditionId": "c",
        "outcomes": ["Up", "Down"], "clobTokenIds": ["1", "2"],
    })
    assert entity["duration_s"] == 900
    assert entity["window_end"] - entity["window_start"] == 900 * 10**9
    assert [s for s, _ in family.expected_windows(START, 0)][-1] == f"btc-updown-15m-{START}"


def test_a_disabled_family_is_not_normalized_until_enabled() -> None:
    # Enabling a family changes normalizer output for the same raw records: that needs a
    # parser version bump, so disabled families stay out of the normalizer.
    prefixes = {family.spec.slug_prefix for family in normalized_families()}
    assert prefixes == {"btc-updown-5m"}
