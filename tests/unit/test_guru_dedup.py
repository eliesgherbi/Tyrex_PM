"""Guru parse + dedup (v1.04)."""

from __future__ import annotations

from pathlib import Path

from tyrex_pm.data.guru_dedup import GuruDedupStore
from tyrex_pm.data.guru_parse import ingest_source_trade_id, stable_source_trade_id, trade_row_to_signal


def test_stable_id_uses_transaction_hash_and_asset() -> None:
    row = {
        "transactionHash": "0xabc",
        "timestamp": 1,
        "asset": "tok",
        "side": "BUY",
        "size": 2,
        "price": 0.5,
    }
    assert stable_source_trade_id(row) == "0xabc:tok"


def test_ingest_id_tx_with_missing_asset_trailing_colon() -> None:
    row = {
        "transactionHash": "0xabc",
        "timestamp": 1,
        "side": "BUY",
        "size": 1,
        "price": 0.5,
    }
    assert ingest_source_trade_id(row) == "0xabc:"


def test_same_tx_different_asset_distinct_ids() -> None:
    base = {"transactionHash": "0xsame", "timestamp": 1, "side": "BUY", "size": 1, "price": 0.5}
    a = {**base, "asset": "111"}
    b = {**base, "asset": "222"}
    assert ingest_source_trade_id(a) != ingest_source_trade_id(b)
    assert ingest_source_trade_id(a) == "0xsame:111"
    assert ingest_source_trade_id(b) == "0xsame:222"


def test_same_tx_same_asset_second_ingest_not_new(tmp_path: Path) -> None:
    row = {"transactionHash": "0x1", "asset": "tok", "timestamp": 1, "side": "BUY", "size": 1, "price": 0.5}
    tid = ingest_source_trade_id(row)
    store = GuruDedupStore(tmp_path / "x.json")
    store.load()
    assert store.is_new(tid)
    store.remember(tid)
    assert not store.is_new(tid)


def test_trade_row_to_signal() -> None:
    row = {
        "transactionHash": "0x1",
        "timestamp": 1700000000000,
        "asset": "123",
        "side": "SELL",
        "size": 10.0,
        "price": 0.45,
        "slug": "some-market",
    }
    sig = trade_row_to_signal(row)
    assert sig.source_trade_id == "0x1:123"
    assert sig.side == "SELL"
    assert sig.token_id == "123"
    assert sig.size_raw == 10.0
    assert sig.price_raw == 0.45
    assert sig.raw_payload_ref == "some-market"


def test_dedup_persists_restart(tmp_path: Path) -> None:
    p = tmp_path / "dedup.json"
    tid = "trade-A"
    s1 = GuruDedupStore(p)
    s1.load()
    assert s1.is_new(tid)
    s1.remember(tid)

    s2 = GuruDedupStore(p)
    s2.load()
    assert not s2.is_new(tid)


def test_dedup_lru_evicts_oldest(tmp_path: Path) -> None:
    p = tmp_path / "dedup.json"
    s = GuruDedupStore(p, max_ids=3)
    for i in range(4):
        s.remember(f"id{i}")
    s2 = GuruDedupStore(p, max_ids=3)
    s2.load()
    assert not s2.is_new("id3")
    assert s2.is_new("id0")
