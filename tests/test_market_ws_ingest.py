"""WS fixture parsing and shadow ingest tests (Phase 2 M1)."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.ids import TokenId
from tyrex_pm.ingestion.market_stream import apply_market_message
from tyrex_pm.ingestion.market_ws_ingest import _compare_books, _parse_messages
from tyrex_pm.market_data.models import BookSource
from tyrex_pm.state.market_store import BookLevel, MarketStateStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ws"
YES = TokenId("93600127953453226164027182960250028230847035010973306659500433874577010899527")


def test_fixture_book_parses_to_shadow_store() -> None:
    msg = json.loads((FIXTURES / "market_book.json").read_text(encoding="utf-8"))
    store = MarketStateStore()
    assert apply_market_message(store, msg, source=BookSource.WEBSOCKET) is True
    assert store.best_bid(YES) == Decimal("0.54")
    assert store.best_ask(YES) == Decimal("0.55")
    cap = store.capture(YES)
    assert cap is not None
    assert cap.source == BookSource.WEBSOCKET
    assert cap.book_hash == "0xabc123def4567890abcdef1234567890abcdef12"


def test_fixture_price_change_polymarket_price_changes_array() -> None:
    book = json.loads((FIXTURES / "market_book.json").read_text(encoding="utf-8"))
    delta = json.loads((FIXTURES / "market_price_change.json").read_text(encoding="utf-8"))
    store = MarketStateStore()
    apply_market_message(store, book, source=BookSource.WEBSOCKET)
    assert apply_market_message(store, delta, source=BookSource.WEBSOCKET) is True
    assert store.best_bid(YES) == Decimal("0.54")


def test_ws_updates_shadow_only_not_authoritative() -> None:
    auth = MarketStateStore()
    shadow = MarketStateStore()
    auth.apply_book(YES, [], [BookLevel(Decimal("0.99"), Decimal("1"))], source=BookSource.REST_POLL)
    msg = json.loads((FIXTURES / "market_book.json").read_text(encoding="utf-8"))
    apply_market_message(shadow, msg, source=BookSource.WEBSOCKET)
    assert shadow.best_bid(YES) == Decimal("0.54")
    assert auth.best_bid(YES) is None or auth.best_bid(YES) == Decimal("0.99")


def test_compare_books_payload_fields() -> None:
    auth = MarketStateStore()
    shadow = MarketStateStore()
    msg = json.loads((FIXTURES / "market_book.json").read_text(encoding="utf-8"))
    apply_market_message(shadow, msg, source=BookSource.WEBSOCKET)
    auth.apply_book(YES, [BookLevel(Decimal("0.50"), Decimal("1"))], [BookLevel(Decimal("0.56"), Decimal("1"))], source=BookSource.REST_POLL)
    facts: list[dict] = []

    class _Sink:
        def write(self, fact) -> None:
            facts.append(fact)

    _compare_books(token_id=YES, authoritative=auth, shadow=shadow, sink=_Sink(), run_id="run1")
    assert facts
    payload = facts[0]["payload"]
    assert payload["token_id"] == str(YES)
    assert payload["rest_best_bid"] == "0.50"
    assert payload["ws_best_bid"] == "0.54"
    assert "bid_delta" in payload


def test_parse_messages_handles_list_and_pong() -> None:
    assert _parse_messages("PONG") == []
    msgs = _parse_messages(json.dumps([{"event_type": "book", "asset_id": "1"}]))
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_run_market_ws_ingest_applies_fixture_via_handler(monkeypatch) -> None:
    from tyrex_pm.ingestion import market_ws_ingest as mwi

    msg = json.loads((FIXTURES / "market_book.json").read_text(encoding="utf-8"))
    stop = asyncio.Event()
    shadow = MarketStateStore()
    auth = MarketStateStore()

    class _Ws:
        async def send(self, _payload):
            return None

        async def recv(self):
            stop.set()
            return json.dumps(msg)

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

    task = asyncio.create_task(
        mwi.run_market_ws_ingest(
            object(),
            [str(YES)],
            stop=stop,
            shadow_store=shadow,
            authoritative_store=auth,
            compare_interval_s=0.0,
        )
    )
    await asyncio.wait_for(task, timeout=2.0)
    assert shadow.best_bid(YES) == Decimal("0.54")
    assert auth.snapshot(YES) is None
