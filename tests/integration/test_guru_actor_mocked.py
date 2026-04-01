"""GuruMonitorActor with mocked Data API client (v1.04)."""

from __future__ import annotations

from unittest.mock import MagicMock

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.portfolio.portfolio import Portfolio

from tyrex_pm.core.types import GuruTradeSignal
from tyrex_pm.data.guru_monitor import GuruMonitorActor, GuruMonitorActorConfig

_ROW = {
    "transactionHash": "0xdup",
    "timestamp": 1,
    "side": "BUY",
    "asset": "99",
    "size": 1,
    "price": 0.5,
}


class RecordingGuruMonitorActor(GuruMonitorActor):
    """Captures signals without relying on msgbus spy/patch (Cython read-only methods)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.recorded: list[GuruTradeSignal] = []

    def _publish_signal(self, sig: GuruTradeSignal) -> None:
        self.recorded.append(sig)


def _wire_actor(actor: GuruMonitorActor) -> None:
    clock = LiveClock()
    cache = Cache(database=None)
    msgbus = MessageBus(trader_id=TraderId("TEST-001"), clock=clock)
    portfolio = Portfolio(msgbus=msgbus, clock=clock, cache=cache)
    actor.register_base(portfolio=portfolio, msgbus=msgbus, cache=cache, clock=clock)


def test_actor_emits_once_for_duplicate_rows() -> None:
    cfg = GuruMonitorActorConfig(
        guru_wallet_address="0x56687bf447db6ffa42ffe2204a05edaa20f55839",
    )
    fake = MagicMock()
    fake.get_trades.return_value = [_ROW, _ROW]

    actor = RecordingGuruMonitorActor(cfg, data_client=fake)
    _wire_actor(actor)

    actor.on_start()
    assert len(actor.recorded) == 1


def test_actor_dedup_across_pagination() -> None:
    cfg = GuruMonitorActorConfig(
        guru_wallet_address="0x56687bf447db6ffa42ffe2204a05edaa20f55839",
    )
    fake = MagicMock()
    # First page must be full (100 rows) so the actor requests the next offset.
    full_page = [dict(_ROW) for _ in range(100)]
    duplicate_tail = [_ROW]
    fake.get_trades.side_effect = [full_page, duplicate_tail]

    actor = RecordingGuruMonitorActor(cfg, data_client=fake)
    _wire_actor(actor)

    actor.on_start()
    assert len(actor.recorded) == 1
    assert fake.get_trades.call_count == 2
