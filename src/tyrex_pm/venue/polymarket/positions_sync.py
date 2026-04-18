"""REST positions safety net.

In LIVE mode, ``WalletStore.positions`` was previously updated **only** by the user-WS
``TRADE`` event with ``status="CONFIRMED"``. Two pathologies followed:

1. WS reconnects could silently drop a BUY CONFIRMED, leaving the bot's view of its
   own long position empty. A subsequent SELL CONFIRMED then created a negative
   "ghost short" with ``avg_price_usd=None`` (mirror image; addressed in
   :mod:`tyrex_pm.state.shadow_wallet._add_qty`).
2. Manual UI activity that completed before the wallet had been hydrated would create
   the same ghost. A single such row taints **every** subsequent deployment-cap check
   with :data:`tyrex_pm.core.reason_codes.DEPLOYMENT_MARK_UNKNOWN` because the
   evaluator iterates all tokens in ``positions ∪ open_orders``.

This module fetches the canonical positions snapshot from
``GET https://data-api.polymarket.com/positions?user=<wallet>`` and replaces
``WalletStore.positions`` wholesale, so REST is the authoritative truth and WS is
treated as a sub-second incremental delta layered on top until the next REST tick.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import WalletPosition
from tyrex_pm.core.time import utc_now
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.venue.polymarket.data_api_client import DataApiClient

log = logging.getLogger(__name__)


def _dec(x: Any) -> Decimal | None:
    if x is None:
        return None
    try:
        d = Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None
    return d


def normalize_position_rows(rows: list[dict[str, Any]]) -> dict[TokenId, WalletPosition]:
    """Convert raw data-api position rows into ``{TokenId: WalletPosition}`` keyed by token.

    Polymarket's ``/positions`` schema has changed over time and varies across response
    envelopes; we accept the documented ``asset`` field plus a few historical aliases.
    Only positions with a non-zero ``size`` are retained; unparseable rows are skipped.
    """
    out: dict[TokenId, WalletPosition] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        tid_raw = (
            row.get("asset")
            or row.get("asset_id")
            or row.get("token_id")
            or row.get("tokenID")
            or row.get("tokenId")
        )
        if not tid_raw:
            continue
        size = _dec(row.get("size") if "size" in row else row.get("qty") or row.get("amount"))
        if size is None or size == 0:
            continue
        avg_px = _dec(
            row.get("avgPrice")
            if "avgPrice" in row
            else row.get("avg_price") or row.get("averagePrice")
        )
        if avg_px is not None and avg_px <= 0:
            avg_px = None
        token_id = TokenId(str(tid_raw))
        out[token_id] = WalletPosition(token_id=token_id, qty=size, avg_price_usd=avg_px)
    return out


def refresh_positions_into_wallet(
    wallet: WalletStore, rows: list[dict[str, Any]]
) -> dict[TokenId, WalletPosition]:
    """Replace ``wallet.positions`` with the REST snapshot and stamp ``last_positions_sync_ts``.

    Returns the new positions dict for caller observability/testing. The previous map
    is discarded; any in-flight WS deltas should be reapplied on the next message
    (idempotent for CONFIRMED events).
    """
    new_positions = normalize_position_rows(rows)
    wallet.positions = new_positions
    wallet.last_positions_sync_ts = utc_now()
    return new_positions


async def refresh_positions_from_data_api(
    wallet: WalletStore,
    client: DataApiClient,
    wallet_address: str,
) -> bool:
    """Fetch + apply positions snapshot. Returns True on success, False on transport error.

    Network/parse failures are logged and swallowed so the supervisor loop keeps running;
    the next tick will retry. ``wallet.positions`` is left untouched on failure.
    """
    if not wallet_address:
        return False
    try:
        rows = await client.fetch_positions(wallet_address)
    except Exception:
        log.exception("data-api positions fetch failed for wallet=%s", wallet_address)
        return False
    refresh_positions_into_wallet(wallet, rows)
    return True


async def positions_refresh_loop(
    wallet: WalletStore,
    client: DataApiClient,
    wallet_address: str,
    interval_s: float,
    stop: asyncio.Event,
) -> None:
    """Background loop: refresh positions every ``interval_s`` until ``stop`` is set."""
    while not stop.is_set():
        await refresh_positions_from_data_api(wallet, client, wallet_address)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except asyncio.TimeoutError:
            continue
