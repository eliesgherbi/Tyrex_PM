"""N7A sealed config + authorization ceremony tests."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.runtime.live_config import LiveConfig, LiveScope
from tyrex_pm.runtime.n7_abort import N7AbortCode
from tyrex_pm.runtime.n7_authorization import (
    APPROVAL_PHRASE_PREFIX,
    create_authorization_request,
    make_test_envelope,
)
from tyrex_pm.runtime.n7_sealed import (
    default_n7_sealed_config,
    load_n7_sealed_config,
    n7_sealed_from_mapping,
)
from tyrex_pm.runtime.n7_timing import PRODUCTION_TIMING_VALUES_STATUS
from tyrex_pm.runtime.scope_a_ladder import PRODUCTION_TIMING_VALUES_STATUS as LADDER_STATUS


REPO = Path(__file__).resolve().parents[1]


def test_defaults_off_and_timing_frozen():
    cfg = default_n7_sealed_config()
    assert cfg.live.enabled is False
    assert cfg.live.mutations_enabled is False
    assert cfg.live.scope is LiveScope.A
    assert cfg.max_buy_collateral == Decimal("5.00")
    assert PRODUCTION_TIMING_VALUES_STATUS == "FROZEN_FOR_N7"
    assert LADDER_STATUS == "FROZEN_FOR_N7"
    assert "OPEN" not in cfg.timing.fingerprint_payload()["status"]


def test_sealed_config_file_loads():
    cfg = load_n7_sealed_config(REPO / "config" / "n7_tiny_live.json")
    assert cfg.live.mutations_enabled is False
    assert cfg.max_buy_collateral <= Decimal("5.00")
    assert cfg.resolution_capability is False
    assert cfg.skip_if_min_exceeds_cap is True
    assert cfg.fingerprint()


def test_cap_cannot_exceed_five():
    with pytest.raises(ValueError, match="5.00"):
        n7_sealed_from_mapping(
            {
                "max_buy_collateral": "5.01",
                "live": {"enabled": False, "mutations_enabled": False, "scope": "A"},
            }
        )


def test_scope_b_and_resolution_refused():
    with pytest.raises(ValueError):
        LiveConfig(enabled=False, mutations_enabled=False, scope=LiveScope.B)
    with pytest.raises(ValueError, match="resolution_capability"):
        n7_sealed_from_mapping(
            {
                "live": {"enabled": False, "mutations_enabled": False, "scope": "A"},
                "z_gap": {"resolution_capability": True},
            }
        )


def test_authorization_phrase_and_single_use():
    sealed = default_n7_sealed_config()
    req, env = create_authorization_request(sealed=sealed, git_head="abc" * 14)
    assert req.approval_phrase_template.startswith(APPROVAL_PHRASE_PREFIX)
    assert env.approve("wrong") is N7AbortCode.AUTHORIZATION_MISMATCH
    assert env.approve(req.approval_phrase_template) is None
    assert env.approved
    env.bind_market(
        market_id="m1", window_id="w1", market_family="btc_updown_5m"
    )
    assert env.consume_for_fake() is None
    assert env.consumed
    assert env.allows_real_venue_mutation is False
    assert env.consume_for_fake() is N7AbortCode.AUTHORIZATION_CONSUMED


def test_authorization_expiry():
    sealed = default_n7_sealed_config()
    from datetime import datetime, timezone

    now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    req, env = create_authorization_request(
        sealed=sealed, git_head="b" * 40, valid_for=timedelta(seconds=1), now=now
    )
    later = now + timedelta(seconds=2)
    assert env.approve(req.approval_phrase_template, now=later) is N7AbortCode.AUTHORIZATION_EXPIRED


def test_ci_cannot_construct_real_mutation(monkeypatch):
    sealed = default_n7_sealed_config()
    env = make_test_envelope(sealed=sealed, git_head="c" * 40)
    env.bind_market(market_id="m", window_id="w", market_family="btc_updown_5m")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_ci_cannot_construct_real_mutation")
    assert env.consume_for_real() is N7AbortCode.AUTHORIZATION_MISMATCH
    assert env.allows_real_venue_mutation is False
