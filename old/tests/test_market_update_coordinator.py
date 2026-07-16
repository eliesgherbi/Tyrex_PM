"""MarketUpdateCoordinator debounce and wake tests."""

from __future__ import annotations

import asyncio

import pytest

from tyrex_pm.core.ids import TokenId
from tyrex_pm.runtime.market_update_coordinator import MarketUpdateCoordinator
from tyrex_pm.state.market_store import MarketStateStore, BookLevel
from tyrex_pm.market_data.models import BookSource
from decimal import Decimal

TOKEN = TokenId("tok-muc")


@pytest.mark.asyncio
async def test_burst_updates_coalesce_to_one_wake() -> None:
    coord = MarketUpdateCoordinator(debounce_ms=50)
    store = MarketStateStore()
    coord.register_authoritative_store(store)
    wakes = 0

    async def waiter() -> None:
        nonlocal wakes
        source, count = await coord.wait_for_update([TOKEN], timeout_s=1.0)
        wakes += 1
        assert source == "event_wake"
        assert count >= 10

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    for _ in range(10):
        coord.notify_token_update(TOKEN, store=store)
    await asyncio.wait_for(task, timeout=2.0)
    assert wakes == 1


@pytest.mark.asyncio
async def test_timer_wake_without_events() -> None:
    coord = MarketUpdateCoordinator(debounce_ms=50)
    source, count = await coord.wait_for_update([TOKEN], timeout_s=0.05)
    assert source == "timer"
    assert count == 0


def test_shadow_store_updates_ignored() -> None:
    coord = MarketUpdateCoordinator(debounce_ms=10)
    auth = MarketStateStore()
    shadow = MarketStateStore()
    coord.register_authoritative_store(auth)
    coord.notify_token_update(TOKEN, store=shadow)
    assert coord._coalesce_count == 0
