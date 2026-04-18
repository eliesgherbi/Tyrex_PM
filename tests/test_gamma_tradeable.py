from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.venue.polymarket.gamma_client import GammaClient


@pytest.mark.asyncio
async def test_gamma_rejects_closed_market() -> None:
    client = MagicMock()
    client.get = AsyncMock(
        return_value=MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(
                return_value=[
                    {
                        "closed": True,
                        "archived": False,
                        "active": True,
                        "acceptingOrders": False,
                    }
                ]
            ),
        )
    )
    g = GammaClient()
    ok, reason = await g.is_token_tradeable(client, "123", now_s=0.0)
    assert not ok
    assert reason == rc.MARKET_UNTRADEABLE


@pytest.mark.asyncio
async def test_gamma_empty_response_is_unavailable() -> None:
    client = MagicMock()
    client.get = AsyncMock(
        return_value=MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(return_value=[]),
        )
    )
    g = GammaClient()
    ok, reason = await g.is_token_tradeable(client, "123", now_s=0.0)
    assert not ok
    assert reason == rc.MARKET_METADATA_UNAVAILABLE
