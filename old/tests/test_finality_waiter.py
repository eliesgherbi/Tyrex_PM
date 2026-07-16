"""FinalityWaiter tests (P4.5 live wiring)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import TradeFillRecord, WalletPosition
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.finality_waiter import wait_for_allocation_final
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore

TOKEN = TokenId("tok-finality")


@pytest.mark.asyncio
async def test_finality_waiter_returns_on_confirmed() -> None:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.wallet.trade_fill_records.append(
        TradeFillRecord(
            token_id=TOKEN,
            side=Side.BUY,
            size=Decimal("10"),
            price=Decimal("0.5"),
            status="CONFIRMED",
            ts_utc=datetime.now(timezone.utc),
        )
    )
    result = await wait_for_allocation_final(
        coord,
        token_id=TOKEN,
        timeout_s=1.0,
        poll_interval_s=0.1,
        apply_local_shadow_fill=False,
    )
    assert result.final is True
    assert result.status == "CONFIRMED"


@pytest.mark.asyncio
async def test_finality_waiter_times_out() -> None:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.wallet.trade_fill_records.append(
        TradeFillRecord(
            token_id=TOKEN,
            side=Side.BUY,
            size=Decimal("10"),
            price=Decimal("0.5"),
            status="MATCHED",
            ts_utc=datetime.now(timezone.utc),
        )
    )
    result = await wait_for_allocation_final(
        coord,
        token_id=TOKEN,
        timeout_s=0.3,
        poll_interval_s=0.1,
        apply_local_shadow_fill=False,
    )
    assert result.final is False
