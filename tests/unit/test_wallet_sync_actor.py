"""Step 3 DoD: WalletSyncActor sync cycle logic — all test cases from 06_tests.md §1."""

from __future__ import annotations

import time
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

from tyrex_pm.runtime.guru_instrument_dynamic import (
    WalletPositionResolveOutcome,
)
from tyrex_pm.runtime.wallet_sync import (
    WalletSyncActor,
    WalletSyncConfig,
)


def _make_instrument(condition_id: str, token_id: str) -> BinaryOption:
    """Build a real ``BinaryOption`` that can be added to a real ``Cache``."""
    market_info: dict[str, Any] = {
        "condition_id": condition_id,
        "question_id": f"q_{condition_id}",
        "question": "Test market?",
        "tokens": [
            {"token_id": token_id, "outcome": "Yes"},
        ],
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


def _register_actor(actor: WalletSyncActor) -> Cache:
    clock = LiveClock()
    cache = Cache(database=None)
    msgbus = MessageBus(trader_id=TraderId("TEST-001"), clock=clock)
    portfolio = Portfolio(msgbus=msgbus, clock=clock, cache=cache)
    actor.register_base(portfolio=portfolio, msgbus=msgbus, cache=cache, clock=clock)
    return cache


def _make_actor(
    *,
    positions: list[dict[str, Any]] | Exception | None = None,
    orders: list[dict[str, Any]] | Exception | None = None,
    resolve_map: dict[tuple[str, str], BinaryOption | None] | None = None,
    resolve_fail_details: dict[tuple[str, str], str] | None = None,
    config: WalletSyncConfig | None = None,
    fact_log: list[tuple[str, dict]] | None = None,
) -> tuple[WalletSyncActor, Cache]:
    """
    Build and register a WalletSyncActor with mocked HTTP and resolve layer.

    ``resolve_map``:  ``(condition_id, token_id) -> BinaryOption | None``.
    When the BinaryOption is not None the mock controller adds it to the real Cache
    and returns success. When None, the controller returns a failure with the detail
    from ``resolve_fail_details`` (default ``"clob_error_string"``).
    """
    if config is None:
        config = WalletSyncConfig()
    if resolve_map is None:
        resolve_map = {}
    if resolve_fail_details is None:
        resolve_fail_details = {}

    clob = MagicMock()
    if isinstance(orders, Exception):
        clob.get_orders.side_effect = orders
    else:
        clob.get_orders.return_value = orders if orders is not None else []

    _pos = positions if positions is not None else []

    def _positions_fetcher() -> list[dict[str, Any]]:
        if isinstance(_pos, Exception):
            raise _pos
        return _pos

    collected_facts = fact_log if fact_log is not None else []

    def _fact_emit(name: str, payload: dict) -> None:
        collected_facts.append((name, payload))

    ctrl = MagicMock()

    actor = WalletSyncActor(
        config=config,
        clob_client=clob,
        dynamic_controller=ctrl,
        fact_emit=_fact_emit,
        positions_fetcher=_positions_fetcher,
    )
    cache = _register_actor(actor)
    actor._start_mono = time.monotonic()

    def _resolve(cid: str, tid: str) -> WalletPositionResolveOutcome:
        key = (cid, tid)
        inst = resolve_map.get(key)
        if inst is not None:
            cache.add_currency(inst.quote_currency)
            cache.add_instrument(inst)
            return WalletPositionResolveOutcome(inst, "", None)
        detail = resolve_fail_details.get(key, "clob_error_string")
        return WalletPositionResolveOutcome(None, detail, "test error")

    ctrl.resolve_and_activate_by_condition_and_token.side_effect = _resolve

    return actor, cache


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestSyncCycleHappyPath:
    def test_discovers_3_new_instruments(self) -> None:
        inst_a = _make_instrument("cond_a", "tok_a")
        inst_b = _make_instrument("cond_b", "tok_b")
        inst_c = _make_instrument("cond_c", "tok_c")

        actor, _ = _make_actor(
            positions=[
                {"conditionId": "cond_a", "asset": "tok_a"},
                {"conditionId": "cond_b", "asset": "tok_b"},
            ],
            orders=[
                {"asset_id": "tok_c", "condition_id": "cond_c"},
            ],
            resolve_map={
                ("cond_a", "tok_a"): inst_a,
                ("cond_b", "tok_b"): inst_b,
                ("cond_c", "tok_c"): inst_c,
            },
        )

        result = actor._sync_cycle()
        assert result.instruments_newly_added == 3
        assert result.first_sync_complete is True
        assert result.http_positions_ok is True
        assert result.http_orders_ok is True

    def test_no_new_when_all_cached(self) -> None:
        inst_a = _make_instrument("cond_a", "tok_a")
        inst_b = _make_instrument("cond_b", "tok_b")

        actor, _ = _make_actor(
            positions=[
                {"conditionId": "cond_a", "asset": "tok_a"},
                {"conditionId": "cond_b", "asset": "tok_b"},
            ],
            resolve_map={
                ("cond_a", "tok_a"): inst_a,
                ("cond_b", "tok_b"): inst_b,
            },
        )

        r1 = actor._sync_cycle()
        assert r1.instruments_newly_added == 2
        assert r1.first_sync_complete is True

        r2 = actor._sync_cycle()
        assert r2.instruments_newly_added == 0
        assert r2.first_sync_complete is True


# ---------------------------------------------------------------------------
# Resolution failures
# ---------------------------------------------------------------------------

class TestResolutionFailures:
    def test_resolution_failure_blocks_completeness(self) -> None:
        inst_a = _make_instrument("cond_a", "tok_a")

        actor, _ = _make_actor(
            positions=[
                {"conditionId": "cond_a", "asset": "tok_a"},
                {"conditionId": "cond_b", "asset": "tok_b"},
            ],
            resolve_map={("cond_a", "tok_a"): inst_a},
            resolve_fail_details={("cond_b", "tok_b"): "clob_error_string"},
        )

        result = actor._sync_cycle()
        assert result.instruments_newly_added == 1
        assert result.resolution_failures == 1
        assert result.unresolvable_retrying == 1
        assert result.first_sync_complete is False

    def test_resolution_failure_exhausts_retries(self) -> None:
        inst_a = _make_instrument("cond_a", "tok_a")

        config = WalletSyncConfig(per_instrument_max_retries=3)
        facts: list[tuple[str, dict]] = []
        actor, _ = _make_actor(
            positions=[
                {"conditionId": "cond_a", "asset": "tok_a"},
                {"conditionId": "cond_b", "asset": "tok_b"},
            ],
            resolve_map={("cond_a", "tok_a"): inst_a},
            resolve_fail_details={("cond_b", "tok_b"): "clob_error_string"},
            config=config,
            fact_log=facts,
        )

        for _ in range(3):
            result = actor._sync_cycle()

        assert result.unresolvable_terminal == 1
        assert result.first_sync_complete is True

        unresolvable_facts = [f for f in facts if f[0] == "wallet_sync_unresolvable"]
        assert len(unresolvable_facts) == 1


# ---------------------------------------------------------------------------
# HTTP failures
# ---------------------------------------------------------------------------

class TestHTTPFailures:
    def test_data_api_failure_only(self) -> None:
        inst_c = _make_instrument("cond_c", "tok_c")

        actor, _ = _make_actor(
            positions=RuntimeError("Data API down"),
            orders=[{"asset_id": "tok_c", "condition_id": "cond_c"}],
            resolve_map={("cond_c", "tok_c"): inst_c},
        )

        result = actor._sync_cycle()
        assert result.http_positions_ok is False
        assert result.http_orders_ok is True
        assert result.instruments_newly_added == 1
        assert result.first_sync_complete is True

    def test_both_http_fail(self) -> None:
        actor, _ = _make_actor(
            positions=RuntimeError("Data API down"),
            orders=RuntimeError("CLOB down"),
        )

        result = actor._sync_cycle()
        assert result.http_positions_ok is False
        assert result.http_orders_ok is False
        assert result.first_sync_complete is False
        assert actor.consecutive_failure_count == 1

    def test_pyclob_failure_only(self) -> None:
        inst_a = _make_instrument("cond_a", "tok_a")

        actor, _ = _make_actor(
            positions=[{"conditionId": "cond_a", "asset": "tok_a"}],
            orders=RuntimeError("CLOB down"),
            resolve_map={("cond_a", "tok_a"): inst_a},
        )

        result = actor._sync_cycle()
        assert result.http_positions_ok is True
        assert result.http_orders_ok is False
        assert result.first_sync_complete is True


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_dedup_across_cycles(self) -> None:
        inst_a = _make_instrument("cond_a", "tok_a")

        actor, _ = _make_actor(
            positions=[{"conditionId": "cond_a", "asset": "tok_a"}],
            resolve_map={("cond_a", "tok_a"): inst_a},
        )

        r1 = actor._sync_cycle()
        assert r1.instruments_newly_added == 1

        r2 = actor._sync_cycle()
        assert r2.instruments_newly_added == 0

    def test_new_instrument_between_cycles(self) -> None:
        inst_a = _make_instrument("cond_a", "tok_a")
        inst_b = _make_instrument("cond_b", "tok_b")

        positions_data: list[list[dict]] = [
            [{"conditionId": "cond_a", "asset": "tok_a"}],
            [
                {"conditionId": "cond_a", "asset": "tok_a"},
                {"conditionId": "cond_b", "asset": "tok_b"},
            ],
        ]
        call_count = [0]

        def _positions_fetcher():
            idx = min(call_count[0], len(positions_data) - 1)
            call_count[0] += 1
            return positions_data[idx]

        clob = MagicMock()
        clob.get_orders.return_value = []
        ctrl = MagicMock()

        actor = WalletSyncActor(
            config=WalletSyncConfig(),
            clob_client=clob,
            dynamic_controller=ctrl,
            positions_fetcher=_positions_fetcher,
        )
        cache = _register_actor(actor)
        actor._start_mono = time.monotonic()

        resolve_map = {
            ("cond_a", "tok_a"): inst_a,
            ("cond_b", "tok_b"): inst_b,
        }

        def _resolve(cid: str, tid: str) -> WalletPositionResolveOutcome:
            inst = resolve_map.get((cid, tid))
            if inst is not None:
                cache.add_currency(inst.quote_currency)
                cache.add_instrument(inst)
                return WalletPositionResolveOutcome(inst, "", None)
            return WalletPositionResolveOutcome(None, "not_configured", "test")

        ctrl.resolve_and_activate_by_condition_and_token.side_effect = _resolve

        r1 = actor._sync_cycle()
        assert r1.instruments_newly_added == 1

        r2 = actor._sync_cycle()
        assert r2.instruments_newly_added == 1


# ---------------------------------------------------------------------------
# Startup deadline
# ---------------------------------------------------------------------------

class TestStartupDeadline:
    def test_startup_deadline_exceeded(self) -> None:
        facts: list[tuple[str, dict]] = []
        config = WalletSyncConfig(startup_deadline_seconds=0.01)

        actor, _ = _make_actor(
            positions=RuntimeError("always fail"),
            orders=RuntimeError("always fail"),
            config=config,
            fact_log=facts,
        )
        actor._start_mono = time.monotonic() - 1.0

        result = actor._sync_cycle()
        assert result.first_sync_complete is False
        assert actor.startup_deadline_exceeded is True

        timeout_facts = [f for f in facts if f[0] == "wallet_sync_startup_timeout"]
        assert len(timeout_facts) >= 1


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class TestProperties:
    def test_poll_interval_seconds(self) -> None:
        actor, _ = _make_actor(config=WalletSyncConfig(poll_interval_seconds=25.0))
        assert actor.poll_interval_seconds == 25.0

    def test_terminally_unresolvable_count(self) -> None:
        actor, _ = _make_actor()
        assert actor.terminally_unresolvable_count == 0

    def test_sync_count_increments(self) -> None:
        actor, _ = _make_actor()
        assert actor.sync_count == 0
        actor._sync_cycle()
        assert actor.sync_count == 1
