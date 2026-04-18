from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.models import ExitIntent
from tyrex_pm.signals.base import GuruCopySignal
from tyrex_pm.runtime.config import StrategyConfig

from tyrex_pm.strategies.guru_follow import sizing


def maybe_exit_intent(
    sig: GuruCopySignal,
    cfg: StrategyConfig,
    holdings: dict[TokenId, Decimal],
) -> tuple[ExitIntent | None, str | None]:
    """
    Map guru SELL to ExitIntent sized vs bot holdings.
    Returns (None, reason) when skipped (dust); (intent, None) on success.
    """
    t = sig.trade
    if t.side != Side.SELL:
        return None, None
    bot_qty = holdings.get(t.token_id, Decimal("0"))
    mult = sizing.conviction_multiplier(t.conviction_score, cfg.sizing.conviction)
    guru_scaled = t.size * cfg.sizing.copy_scale * mult
    if cfg.exits.sell_mode == "full_bot_position":
        raw = bot_qty
    else:
        raw = min(guru_scaled, bot_qty)
    price = t.price
    if raw <= 0:
        return None, rc.GURU_NO_BOT_INVENTORY
    notional = raw * price if price is not None else Decimal("0")
    if notional > 0 and notional < cfg.exits.dust_notional_usd:
        return None, rc.GURU_EXIT_BELOW_DUST
    return (
        ExitIntent(
            token_id=t.token_id,
            side=Side.SELL,
            size=raw,
            limit_price=price,
            order_style=OrderStyle.GTC,
        ),
        None,
    )


def holdings_from_wallet(positions: dict[TokenId, object]) -> dict[TokenId, Decimal]:
    out: dict[TokenId, Decimal] = {}
    for tid, p in positions.items():
        out[tid] = getattr(p, "qty", Decimal("0"))
    return out
