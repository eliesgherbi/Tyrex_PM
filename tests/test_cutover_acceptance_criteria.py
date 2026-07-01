"""M8 cutover acceptance criteria — automatable checks (D6)."""

from __future__ import annotations

from tyrex_pm.market_data.decision_gate import rest_poll_should_run, ws_primary_enabled
from tyrex_pm.market_data.quality import EnforcementMode
from tyrex_pm.runtime.config import parse_app_config


def _ws_primary_runtime(**over):
    base = {
        "execution_mode": "shadow",
        "market_data": {
            "enabled": True,
            "websocket": {"primary_enabled": True, "shadow_enabled": False},
            "rest": {"poll_enabled": False, "bootstrap_on_startup": True, "recovery_on_reconnect": True},
            "quality": {
                "enforcement_mode": "enforce",
                "require_ws_primary_for_entry": True,
                "allow_rest_recovery_for_exit": True,
                "allow_rest_recovery_for_entry": False,
            },
        },
        "execution": {"planner": {"enabled": True}},
        "observability": {"emit_decision_snapshot": True},
    }
    base.update(over)
    return base


def test_criterion_7_rest_poll_disabled_in_ws_primary_config() -> None:
    app = parse_app_config(
        risk={"notional": {"min_usd": "0.01", "max_usd": "100", "max_policy": "cap"}, "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"}, "venue_min_size": {"enabled": False}, "capital": {"enabled": False}},
        strategy={"kind": "paired_binary", "enabled": True, "paired_binary": {"owner_id": "pb", "market_id": "m", "yes_token_id": "1", "no_token_id": "2", "position_size": "5", "max_pair_entry_cost": "1.02", "max_spread_yes": "0.02", "max_spread_no": "0.02", "pair_stop_loss_pct": "0.04", "pair_take_profit_pct": "0.10", "slippage_buffer": "0.005"}},
        runtime=_ws_primary_runtime(),
    )
    assert ws_primary_enabled(app)
    assert not app.runtime.market_data.rest.poll_enabled
    assert not rest_poll_should_run(app, ws_connected=True)


def test_criterion_4_rest_sources_blocked_for_entry_enforce() -> None:
    app = parse_app_config(
        risk={"notional": {"min_usd": "0.01", "max_usd": "100", "max_policy": "cap"}, "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"}, "venue_min_size": {"enabled": False}, "capital": {"enabled": False}},
        strategy={"kind": "paired_binary", "enabled": True, "paired_binary": {"owner_id": "pb", "market_id": "m", "yes_token_id": "1", "no_token_id": "2", "position_size": "5", "max_pair_entry_cost": "1.02", "max_spread_yes": "0.02", "max_spread_no": "0.02", "pair_stop_loss_pct": "0.04", "pair_take_profit_pct": "0.10", "slippage_buffer": "0.005"}},
        runtime=_ws_primary_runtime(),
    )
    q = app.runtime.market_data.quality
    assert q.enforcement_mode == EnforcementMode.ENFORCE.value
    assert q.require_ws_primary_for_entry
    assert not q.allow_rest_recovery_for_entry


def test_criterion_10_rollback_config_restores_rest_authoritative() -> None:
    app = parse_app_config(
        risk={"notional": {"min_usd": "0.01", "max_usd": "100", "max_policy": "cap"}, "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"}, "venue_min_size": {"enabled": False}, "capital": {"enabled": False}},
        strategy={"kind": "paired_binary", "enabled": True, "paired_binary": {"owner_id": "pb", "market_id": "m", "yes_token_id": "1", "no_token_id": "2", "position_size": "5", "max_pair_entry_cost": "1.02", "max_spread_yes": "0.02", "max_spread_no": "0.02", "pair_stop_loss_pct": "0.04", "pair_take_profit_pct": "0.10", "slippage_buffer": "0.005"}},
        runtime={
            "execution_mode": "shadow",
            "market_data": {
                "enabled": True,
                "websocket": {"primary_enabled": False, "shadow_enabled": True},
                "rest": {"poll_enabled": True},
                "quality": {"enforcement_mode": "observe_only"},
            },
            "execution": {"planner": {"enabled": True}},
        },
    )
    assert not ws_primary_enabled(app)
    assert app.runtime.market_data.websocket.shadow_enabled
    assert app.runtime.market_data.rest.poll_enabled
