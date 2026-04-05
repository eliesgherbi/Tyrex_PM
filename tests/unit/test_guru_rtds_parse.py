"""RTDS payload parsing + ingest id (C1)."""

from __future__ import annotations

from tyrex_pm.data.guru_parse import api_timestamp_to_ms, ingest_source_trade_id
from tyrex_pm.data.guru_rtds_parse import (
    normalize_wallet,
    rtds_payload_to_activity_row,
    rtds_trade_payload_to_signal,
)


def test_ingest_source_trade_id_tx_and_asset() -> None:
    row = {
        "transactionHash": "0xabc",
        "timestamp": 1,
        "asset": "t",
        "side": "BUY",
        "size": 1,
        "price": 0.5,
    }
    assert ingest_source_trade_id(row) == "0xabc:t"


def test_ingest_source_trade_id_composite_fallback() -> None:
    row = {
        "timestamp": 100,
        "asset": "t",
        "side": "SELL",
        "size": 2,
        "price": 0.4,
    }
    assert ingest_source_trade_id(row) == "100:t:SELL:2:0.4"


def test_normalize_wallet() -> None:
    assert normalize_wallet(" 0xABC ") == "0xabc"


def test_rtds_payload_to_activity_row() -> None:
    payload = {
        "timestamp": 1_700_000_000_000,
        "asset": "tok1",
        "side": "BUY",
        "size": 1.5,
        "price": 0.55,
        "transactionHash": "0xdead",
        "slug": "mkt",
    }
    row = rtds_payload_to_activity_row(payload)
    assert row["asset"] == "tok1"
    assert row["transactionHash"] == "0xdead"


def test_rtds_trade_payload_to_signal() -> None:
    payload = {
        "timestamp": 1_700_000_000_000,
        "asset": "999",
        "side": "BUY",
        "size": 10,
        "price": 0.45,
        "transactionHash": "0xsig",
        "eventSlug": "evt",
    }
    sig = rtds_trade_payload_to_signal(payload)
    assert sig is not None
    assert sig.source_trade_id == "0xsig:999"
    assert sig.token_id == "999"
    assert sig.side == "BUY"


def test_poll_and_rtds_rows_share_ingest_id() -> None:
    """Synthetic poll row and RTDS-normalized row with same economics → same dedup id."""
    poll = {
        "transactionHash": "0x1",
        "timestamp": 1_700_000_000,
        "asset": "tokA",
        "side": "BUY",
        "size": 2.0,
        "price": 0.5,
    }
    rtds = rtds_payload_to_activity_row(
        {
            "timestamp": 1_700_000_000_000,
            "asset": "tokA",
            "side": "BUY",
            "size": 2.0,
            "price": 0.5,
            "transactionHash": "0x1",
        },
    )
    rtds_norm = dict(rtds)
    rtds_norm["timestamp"] = api_timestamp_to_ms(rtds.get("timestamp"))
    assert ingest_source_trade_id(poll) == ingest_source_trade_id(rtds_norm) == "0x1:tokA"
