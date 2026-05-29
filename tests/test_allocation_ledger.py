"""Unit tests for P4 allocation ledger."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.state.allocation_ledger import (
    AllocationLedger,
    load_allocation_ledger,
    save_allocation_ledger,
)


def test_new_ledger_starts_empty(tmp_path: Path) -> None:
    ledger = AllocationLedger(path=tmp_path / "allocation_ledger.json")
    assert ledger.get_allocated("sell_test", "tok-a") == Decimal("0")
    assert ledger.get_available_allocated("sell_test", "tok-a") == Decimal("0")


def test_apply_buy_increments_allocation(tmp_path: Path) -> None:
    ledger = AllocationLedger(path=tmp_path / "allocation_ledger.json")
    mut = ledger.apply_buy("sell_test", "tok-a", Decimal("10"))
    assert mut.allocated_before == Decimal("0")
    assert mut.allocated_after == Decimal("10")
    assert ledger.get_allocated("sell_test", "tok-a") == Decimal("10")


def test_apply_sell_decrements_not_below_zero(tmp_path: Path) -> None:
    ledger = AllocationLedger(path=tmp_path / "allocation_ledger.json")
    ledger.apply_buy("sell_test", "tok-a", Decimal("5"))
    mut = ledger.apply_sell("sell_test", "tok-a", Decimal("3"))
    assert mut.allocated_after == Decimal("2")
    mut2 = ledger.apply_sell("sell_test", "tok-a", Decimal("99"))
    assert mut2.allocated_after == Decimal("0")


def test_reserve_exit_reduces_available(tmp_path: Path) -> None:
    ledger = AllocationLedger(path=tmp_path / "allocation_ledger.json")
    ledger.apply_buy("sell_test", "tok-a", Decimal("10"))
    ledger.reserve_exit("sell_test", "tok-a", Decimal("4"), "res-1")
    assert ledger.get_allocated("sell_test", "tok-a") == Decimal("10")
    assert ledger.get_available_allocated("sell_test", "tok-a") == Decimal("6")


def test_release_reservation_restores_availability(tmp_path: Path) -> None:
    ledger = AllocationLedger(path=tmp_path / "allocation_ledger.json")
    ledger.apply_buy("sell_test", "tok-a", Decimal("10"))
    ledger.reserve_exit("sell_test", "tok-a", Decimal("4"), "res-1")
    released = ledger.release_reservation("res-1")
    assert released is not None
    assert ledger.get_available_allocated("sell_test", "tok-a") == Decimal("10")


def test_clamp_to_venue_positions_reduces_over_allocation(tmp_path: Path) -> None:
    ledger = AllocationLedger(path=tmp_path / "allocation_ledger.json")
    tid = TokenId("tok-a")
    ledger.apply_buy("sell_test", tid, Decimal("10"))
    clamps = ledger.clamp_to_venue_positions(
        {tid: WalletPosition(token_id=tid, qty=Decimal("6"), avg_price_usd=Decimal("0.5"))}
    )
    assert len(clamps) == 1
    assert clamps[0].allocated_after == Decimal("6")
    assert ledger.get_allocated("sell_test", tid) == Decimal("6")


def test_persistence_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "allocation_ledger.json"
    ledger = AllocationLedger(path=path)
    ledger.apply_buy("sell_test", "tok-a", Decimal("7"))
    ledger.reserve_exit("sell_test", "tok-a", Decimal("2"), "res-x")
    loaded = load_allocation_ledger(path)
    assert loaded.get_allocated("sell_test", "tok-a") == Decimal("7")
    assert loaded.get_available_allocated("sell_test", "tok-a") == Decimal("5")


def test_load_tolerates_missing_file(tmp_path: Path) -> None:
    loaded = load_allocation_ledger(tmp_path / "missing.json")
    assert loaded.get_allocated("x", "y") == Decimal("0")


def test_load_tolerates_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "allocation_ledger.json"
    path.write_text("{not json", encoding="utf-8")
    loaded = load_allocation_ledger(path)
    assert loaded.get_allocated("x", "y") == Decimal("0")


def test_snapshot_decimal_serialization(tmp_path: Path) -> None:
    ledger = AllocationLedger(path=tmp_path / "allocation_ledger.json")
    ledger.apply_buy("sell_test", "tok-a", Decimal("1.5"))
    snap = ledger.snapshot()
    assert snap["entries"][0]["allocated_qty"] == "1.5"
    save_allocation_ledger(tmp_path / "allocation_ledger.json", ledger)
    raw = json.loads((tmp_path / "allocation_ledger.json").read_text(encoding="utf-8"))
    assert raw["version"] == 1
