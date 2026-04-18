from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import ClientOrderId, TokenId, VenueOrderId
from tyrex_pm.core.models import OpenOrderView
from tyrex_pm.core.time import utc_now
from tyrex_pm.state.wallet_store import WalletStore

log = logging.getLogger(__name__)


def _dec(x: Any) -> Decimal:
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


def _sync_wallet_from_clob(wallet: WalletStore, client: Any) -> None:
    """Populate USDC collateral + open orders from CLOB REST (best-effort)."""
    try:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
    except ImportError:
        return

    try:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        bal = client.get_balance_allowance(params)
        if isinstance(bal, dict):
            wallet.usdc_balance = _dec(bal.get("balance") or bal.get("available") or 0)
            wallet.usdc_allowance = _dec(bal.get("allowance") or bal.get("allowance_balance") or 10**30)
    except Exception:
        log.exception("get_balance_allowance failed")

    open_views: list[OpenOrderView] = []
    try:
        raw = client.get_orders()
        rows = raw
        if isinstance(raw, dict):
            rows = raw.get("data") or raw.get("orders") or []
        if not isinstance(rows, list):
            rows = []
        for o in rows:
            if not isinstance(o, dict):
                continue
            tid_raw = o.get("asset_id") or o.get("token_id") or o.get("tokenID")
            if not tid_raw:
                continue
            side_raw = str(o.get("side", "BUY")).upper()
            side = Side.BUY if side_raw == "BUY" else Side.SELL
            orig_raw = o.get("original_size")
            matched_raw = o.get("size_matched")
            size_only = o.get("size")
            orig_dec: Decimal | None = None
            matched_dec = Decimal("0")
            if orig_raw is not None:
                orig_dec = _dec(orig_raw)
                matched_dec = _dec(matched_raw or 0)
                remaining = orig_dec - matched_dec
            elif size_only is not None:
                remaining = _dec(size_only)
            else:
                continue
            if remaining <= 0:
                continue
            px = _dec(o.get("price") or 0)
            oid = o.get("id") or o.get("orderID")
            vid = VenueOrderId(str(oid)) if oid else None
            st = o.get("status")
            open_views.append(
                OpenOrderView(
                    token_id=TokenId(str(tid_raw)),
                    side=side,
                    remaining_size=remaining,
                    limit_price=px,
                    client_order_id=None,
                    venue_order_id=vid,
                    original_size=orig_dec,
                    size_matched=matched_dec if orig_raw is not None else None,
                    venue_state_source="rest",
                    order_status=str(st) if st is not None else None,
                )
            )
    except Exception:
        log.exception("get_orders failed")

    wallet._rest_open_orders = tuple(open_views)
    wallet.rebuild_open_orders_merged()
    wallet.last_sync_ts = utc_now()


async def refresh_wallet_from_clob(wallet: WalletStore, client: Any) -> None:
    await asyncio.to_thread(_sync_wallet_from_clob, wallet, client)
