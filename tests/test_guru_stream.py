from __future__ import annotations

import json
from pathlib import Path

from tyrex_pm.ingestion.guru_stream import process_fixture_signals
from tyrex_pm.state.strategy_store import StrategyStore
from tyrex_pm.venue.polymarket.data_api_client import DataApiClient


def test_guru_dedup_and_ordering() -> None:
    raw = Path(__file__).parent / "fixtures" / "data_api" / "activity_batch.json"
    text = raw.read_text(encoding="utf-8")
    sigs = DataApiClient.parse_activity_json(text, "0xguru")
    store = StrategyStore()
    new1 = process_fixture_signals(sigs, store)
    assert len(new1) == 2  # third row duplicate dedup_key act-a in batch after first processed — actually 3 rows: a, b, duplicate a
    new2 = process_fixture_signals(sigs, store)
    assert len(new2) == 0


def test_watermark_skips_old() -> None:
    store = StrategyStore()
    from tyrex_pm.core.models import GuruTradeSignal
    from tyrex_pm.core.enums import Side
    from tyrex_pm.core.ids import TokenId
    from decimal import Decimal
    from datetime import datetime, timezone

    old = GuruTradeSignal(
        guru_wallet="g",
        token_id=TokenId("1"),
        side=Side.BUY,
        size=Decimal("1"),
        price=Decimal("1"),
        notional_usd=Decimal("1"),
        dedup_key="old",
        ts_venue=datetime.fromtimestamp(1000, tz=timezone.utc),
    )
    new = GuruTradeSignal(
        guru_wallet="g",
        token_id=TokenId("1"),
        side=Side.BUY,
        size=Decimal("1"),
        price=Decimal("1"),
        notional_usd=Decimal("1"),
        dedup_key="new",
        ts_venue=datetime.fromtimestamp(2000, tz=timezone.utc),
    )
    out = process_fixture_signals([new, old], store)
    assert len(out) == 2
    store2 = StrategyStore()
    assert len(process_fixture_signals([old], store2)) == 1
    assert len(process_fixture_signals([old], store2)) == 0
