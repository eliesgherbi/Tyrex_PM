"""R5 snapshot persistence and recovery."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.ids import new_order_id
from tyrex_pm.execution.order_store import OrderStatus
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleState
from tyrex_pm.persistence.snapshot import PersistenceError, StateSnapshotStore
from helpers_r5 import T0, YES, make_book, submit_buy, wired_shadow


def test_atomic_save_load_roundtrip(tmp_path: Path) -> None:
    _, orders, ledger, portfolio, life, oms = wired_shadow()
    oms.on_book_updated(make_book(YES))
    oid = new_order_id()
    life.note_entry_submitted(oid, YES, when=T0)
    oms.submit(submit_buy(qty="5"), order_id=oid)
    assert life.state is LifecycleState.ACTIVE

    store = StateSnapshotStore(tmp_path / "snap.json")
    payload = {
        "schema_version": 1,
        "run_id": "r1",
        "market_id": "cond-fixture-1",
        "runtime_mode": "SHADOW",
        "config_fingerprint": "abc",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "orders": orders.snapshot(),
        "fills": ledger.snapshot(),
        "portfolio": portfolio.snapshot(),
        "lifecycle": life.snapshot(),
        "dedup_keys": [],
        "kill_switch": False,
    }
    store.save(payload)
    loaded = store.load(
        expected_market_id="cond-fixture-1",
        expected_config_fingerprint="abc",
        expected_runtime_mode="SHADOW",
    )
    _, orders2, ledger2, portfolio2, life2, _ = wired_shadow()
    orders2.restore(loaded["orders"])
    ledger2.restore(loaded["fills"])
    portfolio2.restore(loaded["portfolio"])
    life2.restore(loaded["lifecycle"])
    assert life2.state is LifecycleState.ACTIVE
    assert portfolio2.net_quantity(YES) == Decimal("5")
    assert orders2.get(oid).status is OrderStatus.FILLED  # type: ignore[union-attr]
    assert len(ledger2.all_fills()) == 1


def test_corrupt_and_mismatch_rejected(tmp_path: Path) -> None:
    path = tmp_path / "snap.json"
    store = StateSnapshotStore(path)
    store.save(
        {
            "schema_version": 1,
            "run_id": "r1",
            "market_id": "m1",
            "runtime_mode": "SHADOW",
            "config_fingerprint": "fp1",
            "updated_at": T0.isoformat(),
        }
    )
    with pytest.raises(PersistenceError, match="market_id"):
        store.load(
            expected_market_id="other",
            expected_config_fingerprint="fp1",
            expected_runtime_mode="SHADOW",
        )
    with pytest.raises(PersistenceError, match="config_fingerprint"):
        store.load(
            expected_market_id="m1",
            expected_config_fingerprint="wrong",
            expected_runtime_mode="SHADOW",
        )
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(PersistenceError, match="corrupted"):
        store.load(
            expected_market_id="m1",
            expected_config_fingerprint="fp1",
            expected_runtime_mode="SHADOW",
        )


def test_atomic_replace_no_partial(tmp_path: Path) -> None:
    path = tmp_path / "snap.json"
    store = StateSnapshotStore(path)
    store.save(
        {
            "schema_version": 1,
            "run_id": "r1",
            "market_id": "m1",
            "runtime_mode": "SHADOW",
            "config_fingerprint": "fp",
            "updated_at": T0.isoformat(),
            "marker": 1,
        }
    )
    store.save(
        {
            "schema_version": 1,
            "run_id": "r1",
            "market_id": "m1",
            "runtime_mode": "SHADOW",
            "config_fingerprint": "fp",
            "updated_at": T0.isoformat(),
            "marker": 2,
        }
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["marker"] == 2
    assert not path.with_suffix(path.suffix + ".tmp").exists()
