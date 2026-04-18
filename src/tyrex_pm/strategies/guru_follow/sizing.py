from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.models import EnterIntent
from tyrex_pm.signals.base import GuruCopySignal
from tyrex_pm.runtime.config import ConvictionConfig, StrategyConfig


def conviction_multiplier(score: Decimal | None, cfg: ConvictionConfig) -> Decimal:
    if not cfg.enabled:
        return Decimal("1")
    if score is None:
        return Decimal("1")
    lo, hi = cfg.score_min, cfg.score_max
    s = max(lo, min(hi, score))
    if hi <= lo:
        t = Decimal("0")
    else:
        t = (s - lo) / (hi - lo)
    return cfg.min_multiplier + t * (cfg.max_multiplier - cfg.min_multiplier)


def build_enter_intent(sig: GuruCopySignal, cfg: StrategyConfig) -> EnterIntent | None:
    """BUY entry only. Returns None if price/static config cannot produce a size."""
    t = sig.trade
    price = t.price
    if cfg.sizing.static_enabled:
        if cfg.sizing.static_amount_usd <= 0:
            return None
        if price is None or price <= 0:
            return None
        size = cfg.sizing.static_amount_usd / price
    else:
        mult = conviction_multiplier(t.conviction_score, cfg.sizing.conviction)
        size = t.size * cfg.sizing.copy_scale * mult
    return EnterIntent(
        token_id=t.token_id,
        side=Side.BUY,
        size=size,
        limit_price=price,
        order_style=OrderStyle.GTC,
    )
