from __future__ import annotations

import pytest

from tyrex_pm.core.bus import EventBus
from tyrex_pm.core.enums import EventSource
from tyrex_pm.core.models import GuruTradeSignal
from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from decimal import Decimal
from datetime import datetime, timezone


@pytest.mark.asyncio
async def test_bus_delivers() -> None:
    bus = EventBus()
    seen: list = []

    async def h(env):
        seen.append(env.payload)

    sig = GuruTradeSignal(
        guru_wallet="0xw",
        token_id=TokenId("1"),
        side=Side.BUY,
        size=Decimal("1"),
        price=Decimal("0.5"),
        notional_usd=Decimal("0.5"),
        dedup_key="k",
        ts_venue=datetime.now(timezone.utc),
    )
    bus.subscribe(GuruTradeSignal, h)
    await bus.publish(EventBus.wrap(sig, source=EventSource.REST))
    assert len(seen) == 1
    assert seen[0].dedup_key == "k"
