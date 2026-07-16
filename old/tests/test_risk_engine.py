from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import EnterIntent, ExitIntent, RiskContext
from datetime import datetime, timezone

from tyrex_pm.risk.engine import evaluate_intent
from tyrex_pm.runtime.config import load_app_config
from pathlib import Path


def test_kill_switch_denies() -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root)
    # force kill switch via raw manipulation — reload risk file is heavy; construct AppConfig manually skipped
    from tyrex_pm.runtime import config as cfgmod

    risk = app.risk
    # dataclass frozen - use replace not available; load yaml with overlay
    app2 = cfgmod.parse_app_config(
        risk={**app.raw["risk"], "kill_switch": {"enabled": True}},
        strategy=app.raw["strategy"],
        runtime=app.raw["runtime"],
    )
    intent = EnterIntent(
        token_id=TokenId("1234567890"),
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ctx = RiskContext(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=(),
        open_orders=(),
        usdc_balance=Decimal("1000"),
        usdc_allowance=Decimal("1000"),
        last_wallet_sync_ts=datetime.now(timezone.utc),
        mark_prices={TokenId("1234567890"): Decimal("0.5")},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
        reconcile_drift=False,
    )
    d = evaluate_intent(intent, ctx, app=app2, run_id=RunId("r"))
    assert not d.approved


def test_concurrency_denies() -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root)
    from tyrex_pm.runtime import config as cfgmod

    app2 = cfgmod.parse_app_config(
        risk={**app.raw["risk"], "concurrency": {"max_orders_in_flight": 2}},
        strategy=app.raw["strategy"],
        runtime=app.raw["runtime"],
    )
    intent = EnterIntent(
        token_id=TokenId("1234567890"),
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ctx = RiskContext(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=(),
        open_orders=(),
        usdc_balance=Decimal("1000"),
        usdc_allowance=Decimal("1000"),
        last_wallet_sync_ts=datetime.now(timezone.utc),
        mark_prices={TokenId("1234567890"): Decimal("0.5")},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=2,
        orders_in_flight_by_token={},
        reconcile_drift=False,
    )
    d = evaluate_intent(intent, ctx, app=app2, run_id=RunId("r"))
    assert not d.approved
    from tyrex_pm.core import reason_codes as rc

    assert rc.CONCURRENCY_LIMIT in d.reason_codes


def test_stale_wallet_denies() -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root)
    intent = EnterIntent(
        token_id=TokenId("1234567890"),
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    ctx = RiskContext(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=(),
        open_orders=(),
        usdc_balance=Decimal("1000"),
        usdc_allowance=Decimal("1000"),
        last_wallet_sync_ts=old,
        mark_prices={TokenId("1234567890"): Decimal("0.5")},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
        reconcile_drift=False,
    )
    d = evaluate_intent(intent, ctx, app=app, run_id=RunId("r"))
    assert not d.approved
    from tyrex_pm.core import reason_codes as rc

    assert rc.STALE_WALLET_SNAPSHOT in d.reason_codes


def test_reconcile_drift_denies() -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root)
    intent = EnterIntent(
        token_id=TokenId("1234567890"),
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ctx = RiskContext(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=(),
        open_orders=(),
        usdc_balance=Decimal("1000"),
        usdc_allowance=Decimal("1000"),
        last_wallet_sync_ts=datetime.now(timezone.utc),
        mark_prices={TokenId("1234567890"): Decimal("0.5")},
        kill_switch=False,
        health_ok=False,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
        reconcile_drift=True,
    )
    d = evaluate_intent(intent, ctx, app=app, run_id=RunId("r"))
    assert not d.approved
    from tyrex_pm.core import reason_codes as rc

    assert rc.RECONCILE_DRIFT in d.reason_codes


def test_naked_sell_denies() -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root)
    intent = ExitIntent(
        token_id=TokenId("1234567890"),
        side=Side.SELL,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ctx = RiskContext(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=(),
        open_orders=(),
        usdc_balance=Decimal("1000"),
        usdc_allowance=Decimal("1000"),
        last_wallet_sync_ts=datetime.now(timezone.utc),
        mark_prices={TokenId("1234567890"): Decimal("0.5")},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
        reconcile_drift=False,
    )
    d = evaluate_intent(intent, ctx, app=app, run_id=RunId("r"))
    assert not d.approved
    from tyrex_pm.core import reason_codes as rc

    assert rc.NAKED_SELL in d.reason_codes
