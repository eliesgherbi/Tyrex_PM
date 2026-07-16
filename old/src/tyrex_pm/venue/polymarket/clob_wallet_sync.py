"""Phase 3 — V2 wallet sync.

Populate :class:`WalletStore` from the V2 CLOB REST surface:

* ``ClobClient.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))``
  — Polymarket USD collateral (V2's native collateral, replaces V1 USDC.e).
  V2's GET /balance-allowance response shape::

      {
        "balance": "30625001",                       # raw 6-decimal token units
        "allowances": {
          "<exchange_v2_addr>":         "115792...",  # raw 6-decimal
          "<negrisk_adapter_addr>":     "115792...",
          "<negrisk_exchange_v2_addr>": "115792..."
        }
      }

  Two material differences from V1 we must compensate for:

  1. ``balance`` is in raw token units (Polymarket USD has 6 decimals), not
     USD-decimal strings. We divide by ``10**6`` to populate the
     USD-denominated ``WalletStore.usdc_balance`` field that the rest of the
     stack assumes.
  2. ``allowance`` is gone; ``allowances`` is a per-exchange dict. The
     binding allowance for any given trade is whichever V2 exchange contract
     ends up settling it (plain V2 / NegRisk Adapter / NegRisk Exchange v2),
     so for the *risk gate* we conservatively report the **min** across the
     dict — that way, if any one exchange has an under-approval the capital
     gate denies before we get a venue 4xx.

  The V1 shape (``"allowance"`` singular, USD-decimal) is still tolerated as
  a back-compat path for tests/mocks that pre-date V2.

* ``ClobClient.get_open_orders()`` — V2 replaces V1's ``get_orders`` and
  auto-paginates internally, returning a flat ``list`` of order dicts.

V2-only commitment: if ``py-clob-client-v2`` is not importable we raise rather
than silently degrading. Phase 0 already pinned this dependency in the
``[live]`` extra; missing it here is a misconfiguration, not a graceful
fallback case.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId, VenueOrderId
from tyrex_pm.core.models import OpenOrderView
from tyrex_pm.core.time import utc_now
from tyrex_pm.state.wallet_store import WalletStore

log = logging.getLogger(__name__)


USDC_DECIMALS = 6
USDC_SCALE = Decimal(10) ** USDC_DECIMALS  # 1_000_000


def _dec(x: Any) -> Decimal:
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


def _v2_balance_to_usd(bal: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    """Translate a venue ``GET /balance-allowance`` payload into USD-decimals.

    Returns ``(usdc_balance, usdc_allowance)``. Either may be ``None`` when the
    venue did not include a value we can interpret — leaving the field
    unchanged is the correct conservative behavior because downstream risk
    gates treat ``None`` as "wallet not yet synced" and deny BUYs.

    Detection:

    * ``"allowances"`` (plural dict) present → real V2 shape, raw 6-decimal
      token units. Balance is divided by ``10**6``; allowance is the **min**
      of the per-exchange values, also divided by ``10**6``.
    * ``"allowance"`` (singular) or ``"allowance_balance"`` present → legacy
      USD-decimal shape (still used by some tests/mocks); use as-is, no
      scaling.
    * Neither present → return ``None`` for allowance so the capital gate
      stays conservative instead of being masked by a fake 1e30 default.
    """
    allowances_dict = bal.get("allowances")
    is_v2_shape = isinstance(allowances_dict, dict) and bool(allowances_dict)

    raw_balance = bal.get("balance")
    if raw_balance is None:
        raw_balance = bal.get("available")
    if raw_balance is None:
        usdc_balance: Decimal | None = None
    else:
        val = _dec(raw_balance)
        usdc_balance = (val / USDC_SCALE) if is_v2_shape else val

    if is_v2_shape:
        # Conservative: report the *binding* (smallest) per-exchange allowance
        # in USD-decimals. If any single V2 contract is under-approved, the
        # capital gate denies before we hit a venue 4xx.
        try:
            min_raw = min(_dec(v) for v in allowances_dict.values())
        except ValueError:
            usdc_allowance: Decimal | None = None
        else:
            usdc_allowance = min_raw / USDC_SCALE
    else:
        legacy = bal.get("allowance")
        if legacy is None:
            legacy = bal.get("allowance_balance")
        usdc_allowance = _dec(legacy) if legacy is not None else None

    return usdc_balance, usdc_allowance


def _parse_open_order_row(o: dict[str, Any]) -> OpenOrderView | None:
    """Translate one V2 open-order venue row into an :class:`OpenOrderView`.

    V2 keeps the V1 row shape (``asset_id`` / ``side`` / ``original_size`` /
    ``size_matched`` / ``price`` / ``id`` / ``status``); defensive fallbacks
    are kept for minor key-naming variants seen in the wild.
    """
    tid_raw = o.get("asset_id") or o.get("token_id") or o.get("tokenID")
    if not tid_raw:
        return None

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
        return None
    if remaining <= 0:
        return None

    px = _dec(o.get("price") or 0)
    oid = o.get("id") or o.get("orderID")
    vid = VenueOrderId(str(oid)) if oid else None
    st = o.get("status")
    return OpenOrderView(
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


def _sync_wallet_from_clob(wallet: WalletStore, client: Any) -> None:
    """Populate Polymarket USD collateral + open orders from V2 REST.

    Raises :class:`ImportError` if the V2 SDK is not installed — this codebase
    is V2-only after Phase 0 and silent degradation hides misconfiguration.
    """
    from py_clob_client_v2 import AssetType, BalanceAllowanceParams

    try:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        bal = client.get_balance_allowance(params)
        if isinstance(bal, dict):
            usdc_balance, usdc_allowance = _v2_balance_to_usd(bal)
            if usdc_balance is not None:
                wallet.usdc_balance = usdc_balance
            if usdc_allowance is not None:
                wallet.usdc_allowance = usdc_allowance
    except Exception:
        log.exception("get_balance_allowance failed")

    open_views: list[OpenOrderView] = []
    try:
        rows = client.get_open_orders()
        # V2 ``get_open_orders`` already auto-paginates and returns a flat list.
        # Defensive unwrap kept in case a caller passes a raw single-page dict.
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("orders") or []
        if not isinstance(rows, list):
            rows = []
        for o in rows:
            if not isinstance(o, dict):
                continue
            view = _parse_open_order_row(o)
            if view is not None:
                open_views.append(view)
    except Exception:
        log.exception("get_open_orders failed")

    wallet._rest_open_orders = tuple(open_views)
    wallet.rebuild_open_orders_merged()
    wallet.last_sync_ts = utc_now()


async def refresh_wallet_from_clob(wallet: WalletStore, client: Any) -> None:
    await asyncio.to_thread(_sync_wallet_from_clob, wallet, client)
