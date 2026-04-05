"""C2 capital allocation: conviction sizing + min-follow-notional gate."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tyrex_pm.config.loaders import load_strategy_settings
from tyrex_pm.core.types import GuruTradeSignal
from tyrex_pm.signal.follow_worthiness import FollowWorthinessGate
from tyrex_pm.signal.sizing import (
    ConvictionProportionalSizingPolicy,
    ProportionalSizingPolicy,
    build_sizing_policy,
)


def _sig(*, size: float, price: float | None = 0.5, side: str = "BUY") -> GuruTradeSignal:
    return GuruTradeSignal(
        source_trade_id="t",
        ts_event_ms=1,
        side=side,
        token_id="99",
        size_raw=size,
        price_raw=price,
        raw_payload_ref="m",
    )


def test_proportional_sizing_matches_pre_c2_with_branch() -> None:
    p = ProportionalSizingPolicy(0.5)
    s = _sig(size=10.0)
    assert p.size(s, branch="entry") == 5.0
    assert p.size(s, branch="exit") == 5.0


def test_build_sizing_policy_disabled_is_proportional() -> None:
    pol = build_sizing_policy(
        copy_scale=0.5,
        conviction_sizing_enabled=False,
        conviction_sizing_cap=2.0,
        conviction_sizing_lookback_trades=5,
    )
    assert isinstance(pol, ProportionalSizingPolicy)
    assert pol.size(_sig(size=10.0), branch="entry") == 5.0


def test_conviction_cold_start_ratio_one() -> None:
    c = ConvictionProportionalSizingPolicy(
        base_scale=1.0,
        conviction_cap=2.0,
        lookback_trades=10,
    )
    s = _sig(size=100.0)
    assert c.size(s, branch="entry") == 100.0
    m = c.entry_metrics_after_last_size()
    assert m["rolling_avg_guru_size"] is None
    assert m["effective_scale"] == 1.0
    c.record_accepted_entry_size(s)


def test_conviction_second_trade_uses_avg() -> None:
    c = ConvictionProportionalSizingPolicy(
        base_scale=1.0,
        conviction_cap=2.0,
        lookback_trades=10,
    )
    c.record_accepted_entry_size(_sig(size=10.0))
    # avg=10, trade=20 -> ratio min(2,2)=2, eff=2, qty=40
    assert c.size(_sig(size=20.0), branch="entry") == 40.0
    m = c.entry_metrics_after_last_size()
    assert m["rolling_avg_guru_size"] == 10.0
    assert m["effective_scale"] == 2.0


def test_conviction_cap_binds() -> None:
    c = ConvictionProportionalSizingPolicy(
        base_scale=1.0,
        conviction_cap=1.2,
        lookback_trades=10,
    )
    c.record_accepted_entry_size(_sig(size=10.0))
    # trade 30 vs avg 10 -> raw ratio 3, capped 1.2 -> eff=1.2, qty=36
    assert c.size(_sig(size=30.0), branch="entry") == pytest.approx(36.0)


def test_conviction_exit_uses_base_only() -> None:
    c = ConvictionProportionalSizingPolicy(
        base_scale=0.5,
        conviction_cap=2.0,
        lookback_trades=5,
    )
    c.record_accepted_entry_size(_sig(size=10.0))
    q = c.size(_sig(size=100.0, side="SELL"), branch="exit")
    assert q == 50.0


def test_conviction_buffer_only_records_positive_raw() -> None:
    c = ConvictionProportionalSizingPolicy(
        base_scale=1.0,
        conviction_cap=2.0,
        lookback_trades=5,
    )
    c.record_accepted_entry_size(_sig(size=0.0))
    c.record_accepted_entry_size(
        GuruTradeSignal("x", 1, "BUY", "99", None, 0.5, None),
    )
    assert c.size(_sig(size=5.0), branch="entry") == 5.0


def test_follow_worthiness_disabled_always_ok() -> None:
    g = FollowWorthinessGate(0.0)
    assert g.evaluate(price_ref=None, qty=99.0) == (True, None)


def test_follow_worthiness_missing_price() -> None:
    g = FollowWorthinessGate(1.0)
    ok, rc = g.evaluate(price_ref=None, qty=10.0)
    assert ok is False
    assert rc == "min_follow_notional_price_missing"


def test_follow_worthiness_below_min() -> None:
    g = FollowWorthinessGate(10.0)
    ok, rc = g.evaluate(price_ref=0.1, qty=50.0)
    assert ok is False
    assert rc == "min_follow_notional"


def test_load_strategy_c2_defaults(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "guru_wallet_address": "0x1234567890123456789012345678901234567890",
                "token_filter": {"enabled": False, "allowlisted_token_ids": []},
                "copy_scale": 1.0,
            }
        ),
        encoding="utf-8",
    )
    s = load_strategy_settings(p)
    assert not s.conviction_sizing_enabled
    assert s.min_follow_notional_usd == 0.0


def test_load_strategy_c2_conviction_validation(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    doc = {
        "guru_wallet_address": "0x1234567890123456789012345678901234567890",
        "token_filter": {"enabled": False, "allowlisted_token_ids": []},
        "conviction_sizing_enabled": True,
        "conviction_sizing_lookback_trades": 0,
    }
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="lookback"):
        load_strategy_settings(p)


def test_load_strategy_rejects_negative_min_follow(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    doc = {
        "guru_wallet_address": "0x1234567890123456789012345678901234567890",
        "token_filter": {"enabled": False, "allowlisted_token_ids": []},
        "min_follow_notional_usd": -1,
    }
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="min_follow"):
        load_strategy_settings(p)
