"""Market data runtime wiring tests (P4.5 architecture_enhance)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.runtime.config import parse_app_config, ShadowBootstrapConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.market_data_runtime import ensure_market_state_store
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore

_RISK = {
    "notional": {"min_usd": "0.01", "max_usd": "100", "max_policy": "cap"},
    "deployment": {"token_cap_usd": "500", "portfolio_cap_usd": "5000"},
    "venue_min_size": {"enabled": False},
    "capital": {"enabled": False, "max_wallet_age_s": 120},
    "concurrency": {"max_orders_in_flight": 8},
    "readiness": {
        "require_wallet_sync": False,
        "max_wallet_age_s_live": 120,
        "require_heartbeat_live": False,
        "require_user_ws_live": False,
    },
}


def _coord() -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    return coord


def test_market_data_enabled_starts_market_store_supervisor() -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy={"kind": "validation_harness", "enabled": True, "token_id": "t1", "validation": {"token_id": "t1"}},
        runtime={
            "execution_mode": "shadow",
            "shadow_bootstrap": {"usdc_balance": "100", "usdc_allowance": "100"},
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
            "supervisors": {"reconcile_interval_s": 30, "submit_grace_s": 15},
            "logging": {"level": "WARNING"},
        },
    )
    coord = _coord()
    assert coord.market_state is None
    store = ensure_market_state_store(coord, app)
    assert store is not None
    assert coord.market_state is store
    assert getattr(store, "_default_max_age_s", None) == 5.0


def test_market_data_disabled_does_not_start_supervisor() -> None:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy={"kind": "validation_harness", "enabled": True, "token_id": "t1", "validation": {"token_id": "t1"}},
        runtime={
            "execution_mode": "shadow",
            "shadow_bootstrap": {"usdc_balance": "100", "usdc_allowance": "100"},
            "market_data": {"enabled": False},
            "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
            "supervisors": {"reconcile_interval_s": 30, "submit_grace_s": 15},
            "logging": {"level": "WARNING"},
        },
    )
    coord = _coord()
    assert not app.runtime.market_data.enabled
    assert coord.market_state is None
