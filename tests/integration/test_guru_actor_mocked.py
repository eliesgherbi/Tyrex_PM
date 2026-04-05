"""GuruMonitorActor incremental activity polling with mocked Data API client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.portfolio.portfolio import Portfolio

from tyrex_pm.core.types import GuruTradeSignal
from tyrex_pm.data.guru_monitor import GuruMonitorActor, GuruMonitorActorConfig


def _row(*, ts_sec: int, tx: str, asset: str = "99") -> dict:
    """Activity API shape for type=TRADE (timestamp as Unix seconds in API)."""

    return {
        "type": "TRADE",
        "transactionHash": tx,
        "timestamp": ts_sec,
        "side": "BUY",
        "asset": asset,
        "size": 1,
        "price": 0.5,
    }


class RecordingGuruMonitorActor(GuruMonitorActor):
    """Captures signals without relying on msgbus spy/patch (Cython read-only methods)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.recorded: list[GuruTradeSignal] = []

    def _ingest_row(self, sig: GuruTradeSignal, *, ts_poll_recv: int = 0) -> None:
        if self._ingest_state is not None and not self._ingest_state.poll_should_publish():
            return
        if not self._dedup.is_new(sig.source_trade_id):
            return
        self.recorded.append(sig)
        super()._ingest_row(sig, ts_poll_recv=ts_poll_recv)


def _wire_actor(actor: GuruMonitorActor) -> None:
    clock = LiveClock()
    cache = Cache(database=None)
    msgbus = MessageBus(trader_id=TraderId("TEST-001"), clock=clock)
    portfolio = Portfolio(msgbus=msgbus, clock=clock, cache=cache)
    actor.register_base(portfolio=portfolio, msgbus=msgbus, cache=cache, clock=clock)


def test_actor_emits_once_for_duplicate_rows(tmp_path, monkeypatch) -> None:
    # Cold-start watermark = now; trades must be *after* that instant in ms.
    monkeypatch.setattr("tyrex_pm.data.guru_watermark.utc_now_ms", lambda: 1_000_000)
    cfg = GuruMonitorActorConfig(
        guru_wallet_address="0x56687bf447db6ffa42ffe2204a05edaa20f55839",
        watermark_state_path=str(tmp_path / "wm.json"),
        dedup_state_path=str(tmp_path / "dedup.json"),
    )
    fake = MagicMock()
    r = _row(ts_sec=5000, tx="0xdup")
    fake.get_user_trade_activity.return_value = [r, dict(r)]

    actor = RecordingGuruMonitorActor(cfg, data_client=fake)
    _wire_actor(actor)

    actor.on_start()
    assert len(actor.recorded) == 1
    fake.get_user_trade_activity.assert_called()


def test_actor_paginates_within_bounded_pages(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("tyrex_pm.data.guru_watermark.utc_now_ms", lambda: 0)
    cfg = GuruMonitorActorConfig(
        guru_wallet_address="0x56687bf447db6ffa42ffe2204a05edaa20f55839",
        watermark_state_path=str(tmp_path / "wm.json"),
        dedup_state_path=str(tmp_path / "dedup.json"),
        activity_limit=2,
        max_activity_pages_per_poll=3,
    )
    fake = MagicMock()
    fake.get_user_trade_activity.side_effect = [
        [_row(ts_sec=100, tx="0xaa"), _row(ts_sec=101, tx="0xbb")],
        [_row(ts_sec=102, tx="0xcc")],
    ]

    actor = RecordingGuruMonitorActor(cfg, data_client=fake)
    _wire_actor(actor)
    actor.on_start()
    assert len(actor.recorded) == 3
    assert fake.get_user_trade_activity.call_count == 2


@patch("tyrex_pm.data.guru_monitor.time.sleep", autospec=True)
def test_actor_on_start_survives_http_error(_sleep, tmp_path) -> None:
    cfg = GuruMonitorActorConfig(
        guru_wallet_address="0x56687bf447db6ffa42ffe2204a05edaa20f55839",
        watermark_state_path=str(tmp_path / "wm.json"),
    )
    fake = MagicMock()
    req = httpx.Request("GET", "https://data-api.polymarket.com/activity")
    resp = httpx.Response(400, request=req)
    fake.get_user_trade_activity.side_effect = httpx.HTTPStatusError(
        "bad", request=req, response=resp
    )

    actor = RecordingGuruMonitorActor(cfg, data_client=fake)
    _wire_actor(actor)
    actor.on_start()
    assert fake.get_user_trade_activity.called


def test_actor_skips_rows_trailing_the_watermark(tmp_path) -> None:
    # Watermark 5000 ms → skip rows at or before 5000 ms (1s and 5s API stamps → 1000/5000 ms)
    wm = {"last_seen_ts_ms": 5_000}
    (tmp_path / "wm.json").write_text(
        __import__("json").dumps(wm),
        encoding="utf-8",
    )
    cfg = GuruMonitorActorConfig(
        guru_wallet_address="0x56687bf447db6ffa42ffe2204a05edaa20f55839",
        watermark_state_path=str(tmp_path / "wm.json"),
        dedup_state_path=str(tmp_path / "dedup.json"),
    )
    fake = MagicMock()
    fake.get_user_trade_activity.return_value = [
        _row(ts_sec=1, tx="0xold"),
        _row(ts_sec=10, tx="0xnew"),
    ]

    actor = RecordingGuruMonitorActor(cfg, data_client=fake)
    _wire_actor(actor)
    actor.on_start()
    assert len(actor.recorded) == 1
    assert actor.recorded[0].source_trade_id == "0xnew:99"


def test_actor_emits_both_legs_same_tx_different_assets(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("tyrex_pm.data.guru_watermark.utc_now_ms", lambda: 0)
    cfg = GuruMonitorActorConfig(
        guru_wallet_address="0x56687bf447db6ffa42ffe2204a05edaa20f55839",
        watermark_state_path=str(tmp_path / "wm.json"),
        dedup_state_path=str(tmp_path / "dedup.json"),
    )
    fake = MagicMock()
    fake.get_user_trade_activity.return_value = [
        _row(ts_sec=100, tx="0xmult", asset="111"),
        _row(ts_sec=100, tx="0xmult", asset="222"),
    ]
    actor = RecordingGuruMonitorActor(cfg, data_client=fake)
    _wire_actor(actor)
    actor.on_start()
    assert len(actor.recorded) == 2
    assert {s.source_trade_id for s in actor.recorded} == {"0xmult:111", "0xmult:222"}


@pytest.mark.parametrize("now_ms", [1_800_000_000_000])
def test_backfill_sees_older_trade(tmp_path, monkeypatch, now_ms: int) -> None:
    monkeypatch.setattr("tyrex_pm.data.guru_watermark.utc_now_ms", lambda: now_ms)
    cfg = GuruMonitorActorConfig(
        guru_wallet_address="0x56687bf447db6ffa42ffe2204a05edaa20f55839",
        watermark_state_path=str(tmp_path / "wm.json"),
        dedup_state_path=str(tmp_path / "dedup.json"),
        startup_backfill_seconds=120.0,
    )
    fake = MagicMock()
    # 90s before "now" → inside backfill window (watermark = now - 120s)
    ts_sec = (now_ms // 1000) - 90
    fake.get_user_trade_activity.return_value = [_row(ts_sec=ts_sec, tx="0xbf")]

    actor = RecordingGuruMonitorActor(cfg, data_client=fake)
    _wire_actor(actor)
    actor.on_start()
    assert len(actor.recorded) == 1
