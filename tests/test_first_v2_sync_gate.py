"""Phase 6: ``bootstrap_not_complete`` gate (V2 cutover hygiene).

In live mode, ``check_aggressive_readiness`` must deny new-order intents until
``HealthRuntime.first_v2_sync_complete`` flips. This catches the failure mode
where an empty / stale in-memory wallet state (no V2 sync has happened yet)
would otherwise pass the existing readiness gates and let an order through.

Also verifies:

- shadow mode is unaffected (the gate is live-only),
- once the flag flips, the gate stops triggering and we fall through to the
  existing readiness checks,
- ``RuntimeCoordinator.build_risk_context`` plumbs the flag from
  ``HealthRuntime`` into ``RiskContext`` (LIVE) and pins True for SHADOW.

See Docs/Implementation/V2_migration_plan.md §6.3 + §7 Phase 6.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import ExecutionMode
from tyrex_pm.core.models import RiskContext
from tyrex_pm.risk.health import check_aggressive_readiness
from tyrex_pm.runtime.config import (
    AppConfig,
    CapitalConfig,
    ConcurrencyConfig,
    DeploymentConfig,
    InventoryConfig,
    KillSwitchConfig,
    NotionalConfig,
    ReadinessConfig,
    ReportingConfig,
    RiskConfig,
    RuntimeConfig,
    StrategyConfig,
    VenueMinSizeConfig,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore


def _runtime(execution_mode: ExecutionMode) -> RuntimeConfig:
    return RuntimeConfig(
        execution_mode=execution_mode,
        reporting=ReportingConfig(enabled=False, runs_dir="var/reporting/runs"),
        reconcile_interval_s=30,
        submit_grace_s=15.0,
        provisional_unknown_terminal_timeout_s=60.0,
        venue_confirm_provisional_timeout_s=60.0,
        adoption_grace_s=5.0,
        log_level="INFO",
        shadow_bootstrap=None,
    )


def _readiness() -> ReadinessConfig:
    return ReadinessConfig(
        require_wallet_sync=False,
        max_wallet_age_s_live=60,
        require_heartbeat_live=False,
        require_user_ws_live=False,
    )


def _ctx(*, mode: ExecutionMode, first_v2_sync_complete: bool) -> RiskContext:
    return RiskContext(
        execution_mode=mode,
        wallet_positions=(),
        open_orders=(),
        usdc_balance=Decimal("100"),
        usdc_allowance=Decimal("100"),
        last_wallet_sync_ts=datetime.now(timezone.utc),
        mark_prices={},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
        reconcile_drift=False,
        venue_truth_stale=False,
        in_flight_buy_reservations=(),
        first_v2_sync_complete=first_v2_sync_complete,
    )


# ---------------------------------------------------------------------------
# Direct gate semantics
# ---------------------------------------------------------------------------


def test_live_denies_with_bootstrap_not_complete_when_flag_false() -> None:
    """The gate must trigger BEFORE other readiness checks so the reason is unambiguous."""
    ctx = _ctx(mode=ExecutionMode.LIVE, first_v2_sync_complete=False)

    ok, reason = check_aggressive_readiness(
        ctx, runtime=_runtime(ExecutionMode.LIVE), readiness=_readiness()
    )

    assert ok is False
    assert reason == rc.BOOTSTRAP_NOT_COMPLETE


def test_live_passes_when_flag_true_and_other_gates_clear() -> None:
    ctx = _ctx(mode=ExecutionMode.LIVE, first_v2_sync_complete=True)

    ok, reason = check_aggressive_readiness(
        ctx, runtime=_runtime(ExecutionMode.LIVE), readiness=_readiness()
    )

    assert ok is True
    assert reason is None


def test_shadow_mode_ignores_the_bootstrap_gate() -> None:
    """Shadow runs do not touch the V2 venue, so the gate must be a live-only check."""
    ctx = _ctx(mode=ExecutionMode.SHADOW, first_v2_sync_complete=False)

    ok, reason = check_aggressive_readiness(
        ctx, runtime=_runtime(ExecutionMode.SHADOW), readiness=_readiness()
    )

    assert ok is True
    assert reason is None


def test_bootstrap_gate_is_evaluated_before_health_failures() -> None:
    """Even if heartbeat / clob_session would also deny, bootstrap_not_complete wins.

    This ordering means operators see the *root* cause first instead of a misleading
    ``not_ready`` reason that hides the real "we never bootstrapped" state.
    """
    ctx = RiskContext(
        execution_mode=ExecutionMode.LIVE,
        wallet_positions=(),
        open_orders=(),
        usdc_balance=None,
        usdc_allowance=None,
        last_wallet_sync_ts=None,
        mark_prices={},
        kill_switch=False,
        health_ok=False,           # would otherwise force NOT_READY / RECONCILE_DRIFT
        heartbeat_ok=False,
        clob_session_ok=False,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
        reconcile_drift=False,
        venue_truth_stale=False,
        in_flight_buy_reservations=(),
        first_v2_sync_complete=False,
    )

    ok, reason = check_aggressive_readiness(
        ctx, runtime=_runtime(ExecutionMode.LIVE), readiness=_readiness()
    )

    assert ok is False
    assert reason == rc.BOOTSTRAP_NOT_COMPLETE


# ---------------------------------------------------------------------------
# HealthRuntime + RuntimeCoordinator wiring
# ---------------------------------------------------------------------------


def test_health_runtime_default_first_v2_sync_complete_is_false() -> None:
    """A freshly constructed live HealthRuntime must NOT pre-grant trade access."""
    health = HealthRuntime()
    assert health.first_v2_sync_complete is False


def test_mark_first_v2_sync_complete_is_idempotent() -> None:
    health = HealthRuntime()
    health.mark_first_v2_sync_complete()
    health.mark_first_v2_sync_complete()
    assert health.first_v2_sync_complete is True


def _app(execution_mode: ExecutionMode) -> AppConfig:
    risk = RiskConfig(
        notional=NotionalConfig(
            min_usd=Decimal("0.01"), max_usd=Decimal("100"), max_policy="cap"
        ),
        deployment=DeploymentConfig(
            token_cap_usd=Decimal("100"), portfolio_cap_usd=Decimal("100")
        ),
        capital=CapitalConfig(enabled=True, max_wallet_age_s=120),
        inventory=InventoryConfig(sell_requires_venue_position=True),
        kill_switch=KillSwitchConfig(enabled=False),
        concurrency=ConcurrencyConfig(max_orders_in_flight=8),
        readiness=_readiness(),
        venue_min_size=VenueMinSizeConfig(
            enabled=True, policy="deny", default_min_size=Decimal("5")
        ),
    )
    strategy = StrategyConfig.__new__(StrategyConfig)
    object.__setattr__(strategy, "guru", None)
    object.__setattr__(strategy, "filters", None)
    object.__setattr__(strategy, "sizing", None)
    object.__setattr__(strategy, "exits", None)
    return AppConfig(strategy=strategy, risk=risk, runtime=_runtime(execution_mode), raw={})


def test_coordinator_threads_flag_into_risk_context_for_live() -> None:
    health = HealthRuntime()
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=health)
    app = _app(ExecutionMode.LIVE)

    ctx_before = coord.build_risk_context(app)
    assert ctx_before.first_v2_sync_complete is False

    health.mark_first_v2_sync_complete()
    ctx_after = coord.build_risk_context(app)
    assert ctx_after.first_v2_sync_complete is True


def test_coordinator_pins_flag_true_for_shadow_regardless_of_health() -> None:
    """Shadow mode never depends on the live V2 sync; the context flag must be True."""
    health = HealthRuntime()
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=health)
    app = _app(ExecutionMode.SHADOW)

    ctx = coord.build_risk_context(app)
    assert ctx.first_v2_sync_complete is True
