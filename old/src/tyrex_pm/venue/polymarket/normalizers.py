from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import GuruTradeSignal


def _parse_ts_ms(val: Any) -> datetime | None:
    if val is None:
        return None
    try:
        # Polymarket often uses unix seconds or ms
        n = int(val)
        if n > 10_000_000_000:  # ms
            n //= 1000
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def normalize_data_api_activity_row(row: dict[str, Any], guru_wallet: str) -> GuruTradeSignal | None:
    """
    Map one Data API activity / trade row to GuruTradeSignal.

    Expected keys (flexible for fixture + live):
    - asset / token_id / clobTokenId -> token_id
    - side: BUY | SELL
    - size / amount / makerAmount
    - price / avgPrice
    - id / transactionHash (+ optional log index in dedup)
    - timestamp / createdAt
    """
    token_raw = row.get("asset") or row.get("token_id") or row.get("clobTokenId")
    if not token_raw:
        return None
    token_id = TokenId(str(token_raw))

    side_raw = str(row.get("side", "")).upper()
    if side_raw not in ("BUY", "SELL"):
        return None
    side = Side.BUY if side_raw == "BUY" else Side.SELL

    size = Decimal(str(row.get("size") or row.get("amount") or row.get("makerAmount") or "0"))
    price_raw = row.get("price") or row.get("avgPrice")
    price = Decimal(str(price_raw)) if price_raw is not None else None
    notional_raw = row.get("usdcSize") or row.get("notional")
    notional = Decimal(str(notional_raw)) if notional_raw is not None else None

    dedup = str(row.get("id") or row.get("transactionHash") or row.get("txHash") or "")
    if not dedup:
        dedup = f"{row.get('timestamp')}|{token_id}|{side}|{size}"

    ts = _parse_ts_ms(row.get("timestamp") or row.get("createdAt") or row.get("t"))

    conv_raw = row.get("conviction") or row.get("convictionScore") or row.get("score")
    conv: Decimal | None = None
    if conv_raw is not None:
        try:
            conv = Decimal(str(conv_raw))
        except Exception:
            conv = None

    return GuruTradeSignal(
        guru_wallet=guru_wallet,
        token_id=token_id,
        side=side,
        size=size,
        price=price,
        notional_usd=notional,
        dedup_key=dedup,
        ts_venue=ts,
        raw_ref=str(row.get("transactionHash") or row.get("id") or "") or None,
        conviction_score=conv,
    )


def normalize_market_book_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Placeholder — wire to Polymarket market WS schema in integration."""
    return msg
