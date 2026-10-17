"""Order metadata is refreshed before it expires, also while its market is active.

Regression for run 60b239b8 (2026-10-16): metadata warmed when the next market was prepared expired
8 minutes later, about 3 minutes into that market's active window, and nothing refreshed it, so
ORDER_METADATA_NOT_READY blocked most of the 150 s entry window of every market.
"""

from __future__ import annotations

import asyncio
import time as real_time
import types
from decimal import Decimal
from types import SimpleNamespace

import pytest

import tyrex_pm.runtime.trading_runtime as tr
from tyrex_pm.execution.polymarket.gateway import (
    OrderMetadataWarmResult,
    OrderMetadataWarmTokenResult,
)
from tyrex_pm.runtime.trading_runtime import TradingRuntime

TOKENS = ("tok-up", "tok-down")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name: str):  # everything else stays the real time module
        return getattr(real_time, name)


class Gateway:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, ...]] = []
        self.release: asyncio.Event | None = None

    async def warm_order_metadata(self, token_ids, **_kwargs):  # noqa: ANN001
        self.calls.append(tuple(token_ids))
        if self.release is not None:
            await self.release.wait()
        return OrderMetadataWarmResult(
            tokens=tuple(
                OrderMetadataWarmTokenResult(
                    token_id=t, ok=self.ok, elapsed_ms=1.0, error=None if self.ok else "boom"
                )
                for t in token_ids
            )
        )


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]


_METHODS = (
    "_market_token_ids",
    "_token_metadata_is_warm",
    "_token_metadata_refresh_due",
    "_refresh_order_metadata_capability",
    "_schedule_order_metadata_warm",
    "_note_order_metadata_failure",
    "_note_order_metadata_success",
    "_maybe_refresh_active_order_metadata",
    "_warm_order_metadata",
)


def _market() -> SimpleNamespace:
    side = lambda t: SimpleNamespace(token_id=SimpleNamespace(value=t))  # noqa: E731
    return SimpleNamespace(market=SimpleNamespace(yes=side(TOKENS[0]), no=side(TOKENS[1])))


def _runtime(gateway: Gateway) -> SimpleNamespace:
    rt = SimpleNamespace(
        active_market=None,
        gateway=gateway,
        run_recorder=Recorder(),
        capabilities=SimpleNamespace(order_metadata_ready=False),
        config=SimpleNamespace(risk=SimpleNamespace(maximum_total_debit=Decimal("5"))),
        _order_metadata_warmed_at={},
        _order_metadata_warm_tasks=set(),
        _order_metadata_inflight=set(),
        _order_metadata_failures={},
        _order_metadata_retry_after={},
        _record_readiness_if_changed=lambda: None,
        _record_runtime_error=lambda *a, **k: None,
    )
    for name in _METHODS:
        setattr(rt, name, types.MethodType(getattr(TradingRuntime, name), rt))
    return rt


@pytest.fixture()
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(tr, "time", c)
    return c


async def _settle(rt) -> None:
    for _ in range(5):
        if rt._order_metadata_warm_tasks:
            await asyncio.gather(*list(rt._order_metadata_warm_tasks))
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_prepared_market_stays_ready_through_its_active_window(clock) -> None:
    gw = Gateway()
    rt = _runtime(gw)
    market = _market()
    rt._schedule_order_metadata_warm(market)  # prepared 5 minutes before activation
    await _settle(rt)
    clock.now += 300  # activation
    rt.active_market = market
    for _ in range(300):  # the whole 5-minute active window at 1 Hz
        clock.now += 1
        rt._maybe_refresh_active_order_metadata()
        await _settle(rt)
        assert rt.capabilities.order_metadata_ready, f"not ready at +{clock.now - 1000:.0f}s"
    assert len(gw.calls) == 2  # prepare + one refresh before expiry
    assert "ORDER_METADATA_REFRESH_SCHEDULED" in rt.run_recorder.kinds()


@pytest.mark.asyncio
async def test_without_refresh_metadata_would_expire_mid_window(clock) -> None:
    """Documents the incident timeline the refresh prevents (TTL 8 min, prepared 5 min early)."""
    rt = _runtime(Gateway())
    market = _market()
    rt._schedule_order_metadata_warm(market)
    await _settle(rt)
    rt.active_market = market
    clock.now += 300 + 181  # 3 min into the active window
    rt._refresh_order_metadata_capability()  # what the old tick did
    assert not rt.capabilities.order_metadata_ready


@pytest.mark.asyncio
async def test_single_refresh_in_flight_per_token(clock) -> None:
    gw = Gateway()
    rt = _runtime(gw)
    market = _market()
    rt.active_market = market
    gw.release = asyncio.Event()
    for _ in range(10):  # ten ticks while the first request hangs
        clock.now += 1
        rt._maybe_refresh_active_order_metadata()
        await asyncio.sleep(0)
    assert len(gw.calls) == 1
    gw.release.set()
    await _settle(rt)
    assert rt.capabilities.order_metadata_ready
    assert not rt._order_metadata_inflight


@pytest.mark.asyncio
async def test_failed_refresh_blocks_entries_and_retries_are_bounded(clock) -> None:
    gw = Gateway(ok=False)
    rt = _runtime(gw)
    rt.active_market = _market()
    for _ in range(600):  # 10 minutes of ticks
        clock.now += 1
        rt._maybe_refresh_active_order_metadata()
        await _settle(rt)
        assert not rt.capabilities.order_metadata_ready
    assert len(gw.calls) == tr._ORDER_METADATA_REFRESH_MAX_ATTEMPTS
    exhausted = [p for k, p in rt.run_recorder.events if k == "ORDER_METADATA_REFRESH_EXHAUSTED"]
    assert {p["token_id"] for p in exhausted} == set(TOKENS)


@pytest.mark.asyncio
async def test_failure_then_recovery_resets_retry_budget(clock) -> None:
    gw = Gateway(ok=False)
    rt = _runtime(gw)
    rt.active_market = _market()
    clock.now += 1
    rt._maybe_refresh_active_order_metadata()
    await _settle(rt)
    assert rt._order_metadata_failures
    gw.ok = True
    clock.now += tr._ORDER_METADATA_RETRY_BASE_S
    rt._maybe_refresh_active_order_metadata()
    await _settle(rt)
    assert rt.capabilities.order_metadata_ready
    assert not rt._order_metadata_failures and not rt._order_metadata_retry_after
