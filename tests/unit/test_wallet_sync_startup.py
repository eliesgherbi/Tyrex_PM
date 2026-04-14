"""
Step 8: Integration test — wallet sync actor + readiness gate + health source interaction.

Verifies compose → startup → wallet sync → readiness gate flow without
real HTTP or real TradingNode. Uses real Cache and real BinaryOption instruments
to validate end-to-end instrument discovery and readiness gate behavior.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from nautilus_trader.adapters.polymarket.common.parsing import (
    parse_polymarket_instrument,
)
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.portfolio.portfolio import Portfolio

from tyrex_pm.config.loaders import RiskSettings, RuntimeSettings
from tyrex_pm.runtime.guru_instrument_dynamic import (
    WalletPositionResolveOutcome,
)
from tyrex_pm.runtime.lifecycle import (
    LifecycleReadiness,
    StartupReadinessGate,
)
from tyrex_pm.runtime.tradable_state import (
    NautilusLiveExecutionHealthSource,
    StaticTradableStateHealthSource,
)
from tyrex_pm.runtime.tradable_state.types import (
    TradableStateHealth,
    TradableStateHealthSnapshot,
)
from tyrex_pm.runtime.wallet_sync import (
    WalletSyncActor,
    WalletSyncConfig,
)


def _make_instrument(condition_id: str, token_id: str) -> BinaryOption:
    market_info: dict[str, Any] = {
        "condition_id": condition_id,
        "question_id": f"q_{condition_id}",
        "question": "Test market?",
        "tokens": [{"token_id": token_id, "outcome": "Yes"}],
        "active": True,
        "closed": False,
        "market_slug": f"test-{condition_id}",
        "end_date_iso": "2030-01-01",
        "description": "test",
        "minimum_tick_size": "0.01",
        "minimum_order_size": "1",
        "maker_base_fee": "0",
        "taker_base_fee": "0",
    }
    return parse_polymarket_instrument(
        market_info, token_id, "Yes", ts_init=time.time_ns(),
    )


def _risk(**over: object) -> RiskSettings:
    base: dict[str, object] = {
        "max_notional_usd_per_order": 10.0,
        "max_token_notional_usd_open": float("inf"),
        "kill_switch": False,
        "fail_on_missing_price_for_notional": True,
        "capital_gate_enabled": False,
    }
    base.update(over)
    return RiskSettings(**base)  # type: ignore[arg-type]


def _runtime(**kwargs: object) -> RuntimeSettings:
    base: dict[str, object] = {
        "trader_id": "T-TEST-001",
        "execution_mode": "live",
        "guru_poll_interval_seconds": 30.0,
        "data_api_base_url": "https://data-api.polymarket.com",
        "guru_dedup_state_path": "var/d.json",
        "guru_state_path": "var/w.json",
        "guru_activity_limit": 200,
        "guru_startup_backfill_seconds": 0.0,
        "guru_max_activity_pages_per_poll": 4,
        "logging_level": "INFO",
        "clob_host": "https://clob.polymarket.com",
        "chain_id": 137,
        "polymarket_instrument_ids": (),
        "polymarket_token_to_instrument": (),
        "polymarket_dynamic_instruments": True,
        "polymarket_dynamic_max_activations": 32,
        "polymarket_gamma_base_url": "https://gamma-api.polymarket.com",
        "polymarket_gamma_http_timeout_seconds": 15.0,
        "polymarket_startup_token_warmup_max": 0,
    }
    base.update(kwargs)
    return RuntimeSettings(**base)  # type: ignore[arg-type]


def _h(lev: TradableStateHealth) -> StaticTradableStateHealthSource:
    return StaticTradableStateHealthSource(
        TradableStateHealthSnapshot(
            level=lev,
            reason_code="test",
            observed_at_utc=datetime.now(tz=UTC),
        ),
    )


def _build_actor_and_gate(
    *,
    positions: list[dict[str, Any]],
    resolve_map: dict[tuple[str, str], BinaryOption | None],
) -> tuple[WalletSyncActor, StartupReadinessGate, Cache]:
    """Wire actor + gate with real Cache, mocked HTTP and resolve layer."""
    config = WalletSyncConfig(startup_deadline_seconds=0.5)
    clob = MagicMock()
    clob.get_orders.return_value = []
    ctrl = MagicMock()

    actor = WalletSyncActor(
        config=config,
        clob_client=clob,
        dynamic_controller=ctrl,
        positions_fetcher=lambda: positions,
    )

    clock = LiveClock()
    cache = Cache(database=None)
    msgbus = MessageBus(trader_id=TraderId("TEST-001"), clock=clock)
    portfolio = Portfolio(msgbus=msgbus, clock=clock, cache=cache)
    actor.register_base(portfolio=portfolio, msgbus=msgbus, cache=cache, clock=clock)
    actor._start_mono = time.monotonic()

    def _resolve(cid: str, tid: str) -> WalletPositionResolveOutcome:
        inst = resolve_map.get((cid, tid))
        if inst is not None:
            cache.add_currency(inst.quote_currency)
            cache.add_instrument(inst)
            return WalletPositionResolveOutcome(inst, "", None)
        return WalletPositionResolveOutcome(None, "clob_error_string", "err")

    ctrl.resolve_and_activate_by_condition_and_token.side_effect = _resolve

    gate = StartupReadinessGate(
        runtime=_runtime(),
        risk=_risk(),
        capital_provider=None,
        health_source=_h(TradableStateHealth.HEALTHY),
        cache=cache,
        exec_connected=lambda: True,
        wallet_sync_ready=lambda: actor.first_sync_complete,
        wallet_sync_deadline_exceeded=lambda: actor.startup_deadline_exceeded,
    )

    return actor, gate, cache


class TestStartupWithWalletState:
    def test_gate_blocks_until_first_sync(self) -> None:
        inst_a = _make_instrument("cond_a", "tok_a")
        actor, gate, cache = _build_actor_and_gate(
            positions=[{"conditionId": "cond_a", "asset": "tok_a"}],
            resolve_map={("cond_a", "tok_a"): inst_a},
        )

        r = gate.evaluate()
        assert r.status == LifecycleReadiness.NOT_READY
        assert "startup_wallet_sync_pending" in r.reasons

        actor._sync_cycle()
        assert actor.first_sync_complete is True

        r2 = gate.evaluate()
        assert r2.status == LifecycleReadiness.READY

    def test_discovered_instruments_in_cache(self) -> None:
        inst_a = _make_instrument("cond_a", "tok_a")
        inst_b = _make_instrument("cond_b", "tok_b")
        actor, gate, cache = _build_actor_and_gate(
            positions=[
                {"conditionId": "cond_a", "asset": "tok_a"},
                {"conditionId": "cond_b", "asset": "tok_b"},
            ],
            resolve_map={
                ("cond_a", "tok_a"): inst_a,
                ("cond_b", "tok_b"): inst_b,
            },
        )

        actor._sync_cycle()

        from nautilus_trader.adapters.polymarket import POLYMARKET
        from nautilus_trader.model.identifiers import Venue

        cached = cache.instruments(venue=Venue(POLYMARKET))
        cached_ids = {str(i.id) for i in cached}
        assert "cond_a-tok_a.POLYMARKET" in cached_ids
        assert "cond_b-tok_b.POLYMARKET" in cached_ids

    def test_gate_reports_timeout_after_deadline(self) -> None:
        actor, gate, _ = _build_actor_and_gate(
            positions=[{"conditionId": "cond_a", "asset": "tok_a"}],
            resolve_map={},
        )

        actor._start_mono = time.monotonic() - 10.0

        actor._sync_cycle()

        r = gate.evaluate()
        assert r.status == LifecycleReadiness.NOT_READY
        assert "startup_wallet_sync_timeout" in r.reasons

    def test_retry_exhaustion_unblocks_gate(self) -> None:
        """Scenario 5: bounded retry with terminal marking unblocks readiness."""
        inst_a = _make_instrument("cond_a", "tok_a")

        config = WalletSyncConfig(
            per_instrument_max_retries=2, startup_deadline_seconds=120.0,
        )
        clob = MagicMock()
        clob.get_orders.return_value = []
        ctrl = MagicMock()

        actor = WalletSyncActor(
            config=config,
            clob_client=clob,
            dynamic_controller=ctrl,
            positions_fetcher=lambda: [
                {"conditionId": "cond_a", "asset": "tok_a"},
                {"conditionId": "cond_bad", "asset": "tok_bad"},
            ],
        )

        clock = LiveClock()
        cache = Cache(database=None)
        msgbus = MessageBus(trader_id=TraderId("TEST-001"), clock=clock)
        portfolio = Portfolio(msgbus=msgbus, clock=clock, cache=cache)
        actor.register_base(portfolio=portfolio, msgbus=msgbus, cache=cache, clock=clock)
        actor._start_mono = time.monotonic()

        def _resolve(cid: str, tid: str) -> WalletPositionResolveOutcome:
            if (cid, tid) == ("cond_a", "tok_a"):
                cache.add_currency(inst_a.quote_currency)
                cache.add_instrument(inst_a)
                return WalletPositionResolveOutcome(inst_a, "", None)
            return WalletPositionResolveOutcome(None, "clob_error_string", "err")

        ctrl.resolve_and_activate_by_condition_and_token.side_effect = _resolve

        gate = StartupReadinessGate(
            runtime=_runtime(),
            risk=_risk(),
            capital_provider=None,
            health_source=_h(TradableStateHealth.HEALTHY),
            cache=cache,
            exec_connected=lambda: True,
            wallet_sync_ready=lambda: actor.first_sync_complete,
            wallet_sync_deadline_exceeded=lambda: actor.startup_deadline_exceeded,
        )

        # Cycle 1: non-terminal failure blocks
        actor._sync_cycle()
        assert actor.first_sync_complete is False
        r1 = gate.evaluate()
        assert r1.status == LifecycleReadiness.NOT_READY

        # Cycle 2: terminal — unblocks
        actor._sync_cycle()
        assert actor.terminally_unresolvable_count == 1
        assert actor.first_sync_complete is True
        r2 = gate.evaluate()
        assert r2.status == LifecycleReadiness.READY
