"""Nautilus-backed state reader contracts (mocked cache / portfolio / clob)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from nautilus_trader.model.enums import OrderSide, OrderStatus
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.runtime.state_readers import (
    ClobAllowanceStateProvider,
    NautilusAccountSnapshotProvider,
    NautilusExecutionStateReader,
)


def test_execution_reader_lists_open_orders() -> None:
    mo = MagicMock()
    mo.client_order_id = ClientOrderId("c1")
    mo.venue_order_id = MagicMock()
    mo.venue_order_id.__str__ = lambda _: "v1"  # noqa: ARG005
    mo.status = OrderStatus.ACCEPTED
    mo.side = OrderSide.BUY
    mo.quantity = MagicMock()
    mo.quantity.as_decimal.return_value = __import__("decimal").Decimal("2")
    mo.leaves_qty = MagicMock()
    mo.leaves_qty.as_decimal.return_value = __import__("decimal").Decimal("2")
    mo.price = MagicMock()
    mo.price.as_decimal.return_value = __import__("decimal").Decimal("0.5")
    mo.instrument_id = InstrumentId.from_str("0xdead-0xbeef.POLYMARKET")

    cache = MagicMock()
    cache.orders_open.return_value = [mo]
    cache.order.return_value = mo

    reader = NautilusExecutionStateReader(cache)
    open_os = reader.list_open_orders()
    assert len(open_os) == 1
    assert open_os[0].client_order_id == "c1"
    assert open_os[0].status == "ACCEPTED"
    assert open_os[0].leaves_quantity == "2"
    got = reader.get_order("c1")
    assert got is not None
    assert got.instrument_id.endswith(".POLYMARKET")


def test_execution_reader_get_order_missing() -> None:
    cache = MagicMock()
    cache.order.return_value = None
    reader = NautilusExecutionStateReader(cache)
    assert reader.get_order("missing") is None


def test_account_snapshot_absent() -> None:
    portfolio = MagicMock()
    portfolio.account.return_value = None
    prov = NautilusAccountSnapshotProvider(portfolio)
    snap = prov.snapshot()
    assert snap.account_present is False
    assert snap.balances is None
    assert snap.captured_at_utc.tzinfo is not None


def test_account_snapshot_with_to_dict() -> None:
    portfolio = MagicMock()

    class _Acc:
        @staticmethod
        def to_dict(obj: object) -> dict:
            _ = obj
            return {"type": "CashAccount", "events": []}

    portfolio.account.return_value = _Acc()
    prov = NautilusAccountSnapshotProvider(portfolio)
    s2 = prov.snapshot()
    assert s2.account_present is True
    assert isinstance(s2.captured_at_utc, datetime)
    assert s2.captured_at_utc.tzinfo == UTC
    assert s2.balances is not None
    assert s2.balances.get("type") == "CashAccount"


def test_allowance_snapshot_from_clob() -> None:
    client = MagicMock()
    client.get_balance_allowance.return_value = {"balance": "1", "allowance": "2"}
    prov = ClobAllowanceStateProvider(client, signature_type=0)
    snap = prov.snapshot()
    assert snap.raw["balance"] == "1"
    assert "captured_at" not in snap.raw


def test_allowance_rejects_non_dict() -> None:
    client = MagicMock()
    client.get_balance_allowance.return_value = []
    prov = ClobAllowanceStateProvider(client)
    with pytest.raises(TypeError, match="dict"):
        prov.snapshot()


def test_allowance_from_runtime_uses_factory(tmp_path: object) -> None:
    rt = RuntimeSettings(
        trader_id="T-001",
        execution_mode="live",
        guru_poll_interval_seconds=30.0,
        data_api_base_url="https://data-api.polymarket.com",
        guru_dedup_state_path="var/dedup.json",
        guru_state_path="var/wm.json",
        guru_activity_limit=100,
        guru_startup_backfill_seconds=0.0,
        guru_max_activity_pages_per_poll=4,
        logging_level="INFO",
        clob_host="https://clob.polymarket.com",
        chain_id=137,
        polymarket_instrument_ids=(),
        polymarket_token_to_instrument=(),
        polymarket_dynamic_instruments=False,
        polymarket_dynamic_max_activations=32,
        polymarket_gamma_base_url="https://gamma-api.polymarket.com",
        polymarket_gamma_http_timeout_seconds=15.0,
        polymarket_startup_token_warmup_max=32,
    )
    with patch.dict(
        "os.environ",
        {"POLYMARKET_PK": "0x" + "1" * 64},
        clear=False,
    ):
        with patch(
            "tyrex_pm.runtime.state_readers.build_clob_client_from_env",
            return_value=MagicMock(get_balance_allowance=MagicMock(return_value={"balance": "3"})),
        ):
            prov = ClobAllowanceStateProvider.from_runtime(rt)
            assert prov.snapshot().raw["balance"] == "3"
