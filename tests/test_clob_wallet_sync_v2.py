"""Phase 3: V2 wallet sync unit tests.

Covers the V2 surface used by ``clob_wallet_sync._sync_wallet_from_clob`` /
``refresh_wallet_from_clob``:

- ``ClobClient.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))``
  populates ``WalletStore.usdc_balance`` and ``usdc_allowance`` with
  Polymarket USD figures.

  Two payload shapes are exercised:

  * **Real V2 shape** (``"balance"`` in raw 6-decimal token units +
    ``"allowances"`` plural dict). Balance is divided by ``10**6`` and the
    allowance dict is reduced via ``min`` to the binding per-exchange
    approval, also scaled to USD-decimals.
  * **Legacy / V1-shaped** (``"balance"`` USD-decimal + ``"allowance"``
    singular USD-decimal). Kept for tests/mocks pre-dating the V2 shape;
    used as-is, no scaling.

- ``ClobClient.get_open_orders()`` (V2's replacement for V1 ``get_orders``)
  is consumed as a flat ``list`` (V2 SDK auto-paginates) and parsed into
  :class:`OpenOrderView` rows.
- The V1 method ``client.get_orders`` is no longer called.
- A missing ``py-clob-client-v2`` raises (V2-only commitment, no silent
  no-op).
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId, VenueOrderId
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.venue.polymarket.clob_wallet_sync import (
    USDC_SCALE,
    _parse_open_order_row,
    _sync_wallet_from_clob,
    _v2_balance_to_usd,
    refresh_wallet_from_clob,
)


# ---------------------------------------------------------------------------
# _v2_balance_to_usd helper: pure shape detection + scaling
# ---------------------------------------------------------------------------


def test_v2_balance_to_usd_constant_is_one_million() -> None:
    """USDC / pUSD are 6-decimal tokens; the scale factor must be 10**6."""
    assert USDC_SCALE == Decimal(1_000_000)


def test_v2_balance_to_usd_real_shape_scales_and_takes_min() -> None:
    bal = {
        "balance": "30625001",
        "allowances": {
            "exA": "1000000000",  # 1000.0 USD
            "exB": "5000000",     #    5.0 USD ← binding
            "exC": "9000000000",  # 9000.0 USD
        },
    }
    b, a = _v2_balance_to_usd(bal)
    assert b == Decimal("30.625001")
    assert a == Decimal("5")


def test_v2_balance_to_usd_legacy_shape_returns_decimals_unchanged() -> None:
    bal = {"balance": "100.5", "allowance": "1000.25"}
    b, a = _v2_balance_to_usd(bal)
    assert b == Decimal("100.5")
    assert a == Decimal("1000.25")


def test_v2_balance_to_usd_no_allowance_key_returns_None_for_allowance() -> None:
    """Regression guard for the 1e30 silent-mask bug."""
    b, a = _v2_balance_to_usd({"balance": "100000000"})
    assert a is None  # caller leaves WalletStore.usdc_allowance unchanged


def test_v2_balance_to_usd_no_balance_key_returns_None_for_balance() -> None:
    b, a = _v2_balance_to_usd({"allowances": {"x": "1000000"}})
    assert b is None
    assert a == Decimal("1")


# ---------------------------------------------------------------------------
# Balance / allowance via V2 BalanceAllowanceParams + AssetType.COLLATERAL
# ---------------------------------------------------------------------------
#
# Two response shapes are accepted:
#   * Real V2 — raw 6-decimal token units + ``"allowances"`` plural dict.
#   * Legacy  — USD-decimal strings + ``"allowance"`` singular.
# Tests below cover both, plus the failure / missing-key edges.


def test_sync_v2_real_shape_scales_balance_and_min_reduces_allowances() -> None:
    """Real production-shape: raw 6-decimal balance, plural allowances dict.

    Mirrors the response we observed from clob-v2.polymarket.com on the
    successful live-attest run on 2026-04-19::

        balance:    "30625001"               -> 30.625001 pUSD
        allowances: {a: MAX, b: MAX, c: MAX} -> min/scale = MAX/1e6 in USD

    Verifies:
    - balance is divided by 10**6 (raw 6-decimal token units)
    - allowance is the *min* across the per-exchange dict, also /1e6
    """
    from py_clob_client_v2 import AssetType, BalanceAllowanceParams

    wallet = WalletStore()
    client = MagicMock()
    client.get_balance_allowance.return_value = {
        "balance": "30625001",
        "allowances": {
            "0xE111180000d2663C0091e4f400237545B87B996B": "1000000000",  # 1000.0 USD
            "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296": "5000000",     #    5.0 USD ← binding
            "0xe2222d279d744050d28e00520010520000310F59": "9000000000",  # 9000.0 USD
        },
    }
    client.get_open_orders.return_value = []

    _sync_wallet_from_clob(wallet, client)

    client.get_balance_allowance.assert_called_once()
    (params,), _ = client.get_balance_allowance.call_args
    assert isinstance(params, BalanceAllowanceParams)
    assert params.asset_type == AssetType.COLLATERAL == "COLLATERAL"

    assert wallet.usdc_balance == Decimal("30.625001")
    assert wallet.usdc_allowance == Decimal("5")  # binding = min/1e6
    assert wallet.last_sync_ts is not None


def test_sync_v2_real_shape_max_uint256_allowance_round_trips() -> None:
    """Sanity check: a MAX_UINT256 raw allowance survives Decimal arithmetic."""
    wallet = WalletStore()
    client = MagicMock()
    max_uint256 = "115792089237316195423570985008687907853269984665640564039457584007913129639935"
    client.get_balance_allowance.return_value = {
        "balance": "100000000",  # 100 pUSD raw
        "allowances": {
            "0xE111180000d2663C0091e4f400237545B87B996B": max_uint256,
            "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296": max_uint256,
            "0xe2222d279d744050d28e00520010520000310F59": max_uint256,
        },
    }
    client.get_open_orders.return_value = []

    _sync_wallet_from_clob(wallet, client)

    assert wallet.usdc_balance == Decimal("100")
    # MAX_UINT256 / 1e6 — large but a valid Decimal; what matters is that the
    # capital gate sees a number much larger than any conceivable order, not
    # the silent 1e30 fallback we used to mask the V2 plural-dict shape with.
    expected = Decimal(max_uint256) / Decimal(10**6)
    assert wallet.usdc_allowance == expected
    assert wallet.usdc_allowance > Decimal("1e60")


def test_sync_v2_real_shape_zero_allowance_in_dict_makes_min_zero() -> None:
    """If any per-exchange allowance is zero, min is zero so capital denies."""
    wallet = WalletStore()
    client = MagicMock()
    client.get_balance_allowance.return_value = {
        "balance": "100000000",
        "allowances": {
            "0xE111180000d2663C0091e4f400237545B87B996B": "1000000000",
            "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296": "0",  # not approved
            "0xe2222d279d744050d28e00520010520000310F59": "1000000000",
        },
    }
    client.get_open_orders.return_value = []

    _sync_wallet_from_clob(wallet, client)

    assert wallet.usdc_balance == Decimal("100")
    assert wallet.usdc_allowance == Decimal("0")


def test_sync_v2_shape_without_balance_field_leaves_balance_unchanged() -> None:
    """Missing ``balance`` must leave WalletStore.usdc_balance as-is (None)."""
    wallet = WalletStore()
    client = MagicMock()
    client.get_balance_allowance.return_value = {
        "allowances": {"0xE111180000d2663C0091e4f400237545B87B996B": "1000000"},
    }
    client.get_open_orders.return_value = []

    _sync_wallet_from_clob(wallet, client)

    assert wallet.usdc_balance is None  # no silent zero
    assert wallet.usdc_allowance == Decimal("1")


def test_sync_v2_response_missing_allowance_keeps_None_not_1e30() -> None:
    """Regression: previously fell back to 1e30 which masked under-approvals."""
    wallet = WalletStore()
    client = MagicMock()
    client.get_balance_allowance.return_value = {"balance": "100000000"}
    client.get_open_orders.return_value = []

    _sync_wallet_from_clob(wallet, client)

    assert wallet.usdc_allowance is None  # NOT Decimal("1e30")


def test_sync_legacy_v1_shape_populates_balance_and_allowance() -> None:
    """Legacy V1-style USD-decimal singular response; used by mocks/tests."""
    from py_clob_client_v2 import AssetType, BalanceAllowanceParams

    wallet = WalletStore()
    client = MagicMock()
    client.get_balance_allowance.return_value = {
        "balance": "100.5",
        "allowance": "1000.25",
    }
    client.get_open_orders.return_value = []

    _sync_wallet_from_clob(wallet, client)

    client.get_balance_allowance.assert_called_once()
    (params,), _ = client.get_balance_allowance.call_args
    assert isinstance(params, BalanceAllowanceParams)
    assert params.asset_type == AssetType.COLLATERAL == "COLLATERAL"

    assert wallet.usdc_balance == Decimal("100.5")
    assert wallet.usdc_allowance == Decimal("1000.25")
    assert wallet.last_sync_ts is not None


def test_sync_legacy_v1_shape_falls_back_to_available_and_allowance_balance() -> None:
    """Some V1 venue snapshots used ``available`` / ``allowance_balance`` keys."""
    wallet = WalletStore()
    client = MagicMock()
    client.get_balance_allowance.return_value = {
        "available": "42",
        "allowance_balance": "9999",
    }
    client.get_open_orders.return_value = []

    _sync_wallet_from_clob(wallet, client)

    assert wallet.usdc_balance == Decimal("42")
    assert wallet.usdc_allowance == Decimal("9999")


def test_sync_tolerates_balance_failure_and_continues_to_open_orders() -> None:
    """Balance call exception must not abort the open-orders refresh."""
    wallet = WalletStore()
    client = MagicMock()
    client.get_balance_allowance.side_effect = RuntimeError("venue 500")
    client.get_open_orders.return_value = []

    _sync_wallet_from_clob(wallet, client)

    assert wallet.usdc_balance is None
    assert wallet.usdc_allowance is None
    assert wallet.last_sync_ts is not None
    client.get_open_orders.assert_called_once()


# ---------------------------------------------------------------------------
# Open orders via V2 get_open_orders (no V1 get_orders)
# ---------------------------------------------------------------------------


def test_sync_uses_v2_get_open_orders_not_v1_get_orders() -> None:
    wallet = WalletStore()
    client = MagicMock()
    client.get_balance_allowance.return_value = {"balance": "0", "allowance": "0"}
    client.get_open_orders.return_value = []

    _sync_wallet_from_clob(wallet, client)

    assert client.get_open_orders.call_count == 1
    assert client.get_orders.call_count == 0  # V1 path must not be used


def test_sync_parses_v2_open_orders_into_wallet_views() -> None:
    wallet = WalletStore()
    client = MagicMock()
    client.get_balance_allowance.return_value = {"balance": "100", "allowance": "100"}
    client.get_open_orders.return_value = [
        {
            "id": "0xord1",
            "asset_id": "1234567890",
            "side": "BUY",
            "original_size": "10",
            "size_matched": "3",
            "price": "0.42",
            "status": "live",
        },
        {
            "id": "0xord2",
            "asset_id": "9876543210",
            "side": "SELL",
            "original_size": "5",
            "size_matched": "5",
            "price": "0.60",
            "status": "matched",
        },
        {
            "id": "0xord3",
            "asset_id": "5555555555",
            "side": "BUY",
            "size": "7",
            "price": "0.10",
        },
    ]

    _sync_wallet_from_clob(wallet, client)

    rest_views = wallet._rest_open_orders
    # ord2 fully matched → remaining 0 → dropped
    assert len(rest_views) == 2

    by_token = {str(v.token_id): v for v in rest_views}
    assert "1234567890" in by_token
    v1 = by_token["1234567890"]
    assert v1.side == Side.BUY
    assert v1.remaining_size == Decimal("7")
    assert v1.original_size == Decimal("10")
    assert v1.size_matched == Decimal("3")
    assert v1.limit_price == Decimal("0.42")
    assert v1.venue_order_id == VenueOrderId("0xord1")
    assert v1.venue_state_source == "rest"
    assert v1.order_status == "live"

    assert "5555555555" in by_token
    v3 = by_token["5555555555"]
    assert v3.remaining_size == Decimal("7")
    assert v3.original_size is None  # ``size``-only path
    assert v3.size_matched is None


def test_sync_unwraps_dict_response_with_data_key_defensively() -> None:
    """If a caller hands back a raw single-page dict, parser still copes."""
    wallet = WalletStore()
    client = MagicMock()
    client.get_balance_allowance.return_value = {"balance": "0", "allowance": "0"}
    client.get_open_orders.return_value = {
        "data": [
            {
                "id": "0xord-a",
                "asset_id": "111",
                "side": "BUY",
                "original_size": "2",
                "size_matched": "0",
                "price": "0.5",
            }
        ],
        "next_cursor": "LTE=",
    }

    _sync_wallet_from_clob(wallet, client)
    assert len(wallet._rest_open_orders) == 1


def test_sync_skips_malformed_rows() -> None:
    wallet = WalletStore()
    client = MagicMock()
    client.get_balance_allowance.return_value = {"balance": "0", "allowance": "0"}
    client.get_open_orders.return_value = [
        "not-a-dict",
        {},
        {"asset_id": "x"},  # neither original_size nor size
        {"asset_id": "y", "size": "0"},  # remaining 0
    ]

    _sync_wallet_from_clob(wallet, client)
    assert wallet._rest_open_orders == ()


def test_parse_open_order_row_negative_remaining_returns_none() -> None:
    row = {
        "asset_id": "1",
        "side": "BUY",
        "original_size": "5",
        "size_matched": "10",
        "price": "0.5",
    }
    assert _parse_open_order_row(row) is None


def test_parse_open_order_row_sell_side() -> None:
    row = {
        "asset_id": "1",
        "side": "SELL",
        "original_size": "3",
        "size_matched": "0",
        "price": "0.4",
        "id": "0xz",
    }
    view = _parse_open_order_row(row)
    assert view is not None
    assert view.side == Side.SELL


# ---------------------------------------------------------------------------
# V2-only commitment: missing SDK fails loud (not silent)
# ---------------------------------------------------------------------------


def test_sync_raises_when_v2_sdk_missing(monkeypatch) -> None:
    """V1 used to silently no-op on ImportError. V2-only must surface the misconfig."""
    wallet = WalletStore()
    client = MagicMock()

    real_import = __import__

    def fake_import(name, *a, **kw):
        if name.startswith("py_clob_client_v2"):
            raise ImportError("simulated missing V2 SDK")
        return real_import(name, *a, **kw)

    with patch("builtins.__import__", side_effect=fake_import):
        with pytest.raises(ImportError, match="simulated missing V2 SDK"):
            _sync_wallet_from_clob(wallet, client)

    # WalletStore must remain pristine — no half-populated state on misconfig
    assert wallet.usdc_balance is None
    assert wallet.usdc_allowance is None
    assert wallet.last_sync_ts is None


# ---------------------------------------------------------------------------
# refresh_wallet_from_clob: thin async wrapper dispatches to executor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_wallet_from_clob_dispatches_off_loop() -> None:
    import threading

    wallet = WalletStore()
    captured: dict = {}

    def _record_thread(*_a, **_kw):
        captured["thread"] = threading.current_thread()
        return {"balance": "5", "allowance": "5"}

    client = MagicMock()
    client.get_balance_allowance.side_effect = _record_thread
    client.get_open_orders.return_value = []

    await refresh_wallet_from_clob(wallet, client)

    assert captured["thread"] is not threading.main_thread()
    assert wallet.usdc_balance == Decimal("5")
    assert wallet.usdc_allowance == Decimal("5")
