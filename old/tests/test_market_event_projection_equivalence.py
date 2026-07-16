"""Legacy apply_market_message vs event-backbone projection equivalence (M2B.0-B)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.events import EventType
from tyrex_pm.core.ids import TokenId
from tyrex_pm.ingestion.event_factory import build_market_event_from_ws
from tyrex_pm.ingestion.market_stream import apply_market_message, project_event
from tyrex_pm.ingestion.market_ws_ingest import _handle_sequence
from tyrex_pm.ingestion.sequencer import MarketSequencer
from tyrex_pm.market_data.models import BookSource
from tyrex_pm.state.market_store import BookLevel, MarketStateSnapshot, MarketStateStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ws"
YES = TokenId("93600127953453226164027182960250028230847035010973306659500433874577010899527")
NO = TokenId("98261242917329382720414284166477944193653462411741333161666117894652193129742")
FIXED_RECV = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


def _assert_capture_equiv(left: MarketStateSnapshot, right: MarketStateSnapshot) -> None:
    assert left.best_bid == right.best_bid
    assert left.best_ask == right.best_ask
    assert left.bids == right.bids
    assert left.asks == right.asks
    assert left.book_hash == right.book_hash
    assert left.reconnect_gap == right.reconnect_gap
    assert left.source == right.source
    assert left.source_quality == right.source_quality
    assert (left.snapshot_id is not None) == (right.snapshot_id is not None)


def _token_from_msg(msg: dict) -> TokenId | None:
    asset = msg.get("asset_id")
    if asset is None and msg.get("price_changes"):
        pcs = msg.get("price_changes") or []
        if pcs and isinstance(pcs[0], dict):
            asset = pcs[0].get("asset_id")
    return TokenId(str(asset)) if asset else None


def _legacy_apply_stream(
    store: MarketStateStore,
    messages: list[dict],
    *,
    primary_mode: bool = True,
) -> None:
    for msg in messages:
        tid = _token_from_msg(msg)
        book_hash = msg.get("hash")
        if tid is not None and not _handle_sequence(
            store,
            tid,
            book_hash=str(book_hash) if book_hash is not None else None,
            sink=None,
            run_id=None,
            primary_mode=primary_mode,
        ):
            continue
        apply_market_message(store, msg, source=BookSource.WEBSOCKET)


def _event_apply_stream(
    store: MarketStateStore,
    messages: list[dict],
    *,
    market_id: str = "equiv-test",
    primary_mode: bool = True,
) -> None:
    seq = MarketSequencer(market_id, reorder_buffer_ms=0.0)
    for idx, msg in enumerate(messages, start=1):
        event = build_market_event_from_ws(
            msg,
            received_ts=FIXED_RECV,
            connection_id="test-conn",
            local_counter=idx,
            market_id=market_id,
        )
        if event is None:
            continue
        for out_event in seq.ingest(event):
            if out_event.event_type == EventType.WS_SEQ_GAP:
                project_event(store, out_event, source=BookSource.WEBSOCKET)
                continue
            if out_event.is_book_event():
                project_event(store, out_event, source=BookSource.WEBSOCKET)


def test_fixture_book_projection_equivalent() -> None:
    book = json.loads((FIXTURES / "market_book.json").read_text(encoding="utf-8"))
    legacy = MarketStateStore()
    evented = MarketStateStore()
    _legacy_apply_stream(legacy, [book])
    _event_apply_stream(evented, [book])
    left = legacy.capture(YES)
    right = evented.capture(YES)
    assert left is not None and right is not None
    _assert_capture_equiv(left, right)


def test_fixture_book_then_delta_projection_equivalent() -> None:
    book = json.loads((FIXTURES / "market_book.json").read_text(encoding="utf-8"))
    delta = json.loads((FIXTURES / "market_price_change.json").read_text(encoding="utf-8"))
    messages = [book, delta]
    legacy = MarketStateStore()
    evented = MarketStateStore()
    _legacy_apply_stream(legacy, messages)
    _event_apply_stream(evented, messages)
    for tid in (YES, NO):
        left = legacy.capture(tid)
        right = evented.capture(tid)
        if left is None and right is None:
            continue
        assert left is not None and right is not None
        _assert_capture_equiv(left, right)


def test_reconnect_gap_equivalent() -> None:
    seed = {
        "event_type": "book",
        "asset_id": str(YES),
        "bids": [{"price": "0.50", "size": "1"}],
        "asks": [{"price": "0.55", "size": "1"}],
        "hash": "0xbbb",
    }
    stale = {
        "event_type": "book",
        "asset_id": str(YES),
        "bids": [{"price": "0.49", "size": "1"}],
        "asks": [{"price": "0.56", "size": "1"}],
        "hash": "0xaaa",
    }
    messages = [seed, stale]
    legacy = MarketStateStore()
    evented = MarketStateStore()
    _legacy_apply_stream(legacy, messages, primary_mode=True)
    _event_apply_stream(evented, messages, primary_mode=True)
    assert legacy.reconnect_gap(YES)
    assert evented.reconnect_gap(YES)
    left = legacy.capture(YES)
    right = evented.capture(YES)
    assert left is not None and right is not None
    assert left.book_hash == right.book_hash == "0xbbb"
    assert left.best_bid == right.best_bid == Decimal("0.50")


def test_event_factory_preserves_raw_payload() -> None:
    book = json.loads((FIXTURES / "market_book.json").read_text(encoding="utf-8"))
    event = build_market_event_from_ws(
        book,
        received_ts=FIXED_RECV,
        connection_id="c1",
        local_counter=1,
        market_id="m1",
    )
    assert event is not None
    assert event.payload["raw"] == book
    assert len(event.payload["raw"]["bids"]) == 3


@pytest.mark.asyncio
async def test_event_backbone_flag_off_no_allocation(monkeypatch) -> None:
    from tyrex_pm.ingestion import market_ws_ingest as mwi

    book = json.loads((FIXTURES / "market_book.json").read_text(encoding="utf-8"))
    stop = asyncio.Event()
    shadow = MarketStateStore()
    auth = MarketStateStore()

    def _fail_factory(*args, **kwargs):
        raise AssertionError("build_market_event_from_ws must not run when flag is off")

    def _fail_sequencer(*args, **kwargs):
        raise AssertionError("MarketSequencer must not be constructed when flag is off")

    monkeypatch.setattr(mwi, "build_market_event_from_ws", _fail_factory)
    monkeypatch.setattr(mwi, "MarketSequencer", _fail_sequencer)
    monkeypatch.delenv("TYREX_EVENT_BACKBONE", raising=False)

    class _Ws:
        async def send(self, _payload):
            return None

        async def recv(self):
            stop.set()
            return json.dumps(book)

    class _CM:
        def __init__(self, ws):
            self._ws = ws

        async def __aenter__(self):
            return self._ws

        async def __aexit__(self, *args):
            return False

    def _fake_connect(url, ping_interval=None, open_timeout=15):
        return _CM(_Ws())

    import sys

    monkeypatch.setitem(sys.modules, "websockets", type("W", (), {"connect": _fake_connect}))

    await mwi.run_market_ws_ingest(
        object(),
        [str(YES)],
        stop=stop,
        shadow_store=shadow,
        authoritative_store=auth,
        primary_mode=False,
        event_backbone_config_flag=False,
    )
    assert shadow.capture(YES) is not None
