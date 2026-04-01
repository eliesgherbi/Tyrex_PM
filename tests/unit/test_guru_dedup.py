"""Guru parse + dedup (v1.04)."""

from __future__ import annotations

from pathlib import Path

from tyrex_pm.data.guru_dedup import GuruDedupStore
from tyrex_pm.data.guru_parse import stable_source_trade_id, trade_row_to_signal


def test_stable_id_uses_transaction_hash() -> None:
    row = {
        "transactionHash": "0xabc",
        "timestamp": 1,
        "asset": "tok",
        "side": "BUY",
        "size": 2,
        "price": 0.5,
    }
    assert stable_source_trade_id(row).startswith("0xabc")


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
