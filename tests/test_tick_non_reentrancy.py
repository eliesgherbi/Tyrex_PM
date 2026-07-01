"""Tick non-reentrancy guard tests."""

from __future__ import annotations

import asyncio

import pytest

from tyrex_pm.core.ids import TokenId
from tyrex_pm.runtime.market_update_coordinator import MarketUpdateCoordinator


@pytest.mark.asyncio
async def test_concurrent_tick_blocked_by_lock() -> None:
    coord = MarketUpdateCoordinator(debounce_ms=10)
    entered = 0
    concurrent = False

    async def tick_once() -> None:
        nonlocal entered, concurrent
        async with coord.tick_lock:
            if entered:
                concurrent = True
            entered += 1
            await asyncio.sleep(0.05)
            entered -= 1

    await asyncio.gather(tick_once(), tick_once())
    assert concurrent is False
