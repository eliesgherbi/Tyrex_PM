from __future__ import annotations

from decimal import Decimal
from typing import Any

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import ExitIntent
from tyrex_pm.runtime.allocation_ids import OWNER_GURU_FOLLOW
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.exit_lifecycle import inventory_snapshot
from tyrex_pm.signals.base import GuruCopySignal
from tyrex_pm.runtime.config import StrategyConfig

from tyrex_pm.strategies.guru_follow import sizing


def _allocated_available(coord: RuntimeCoordinator, owner_id: str, token_id: TokenId) -> Decimal:
    ledger = coord.allocation_ledger
    if ledger is None:
        return Decimal("0")
    return ledger.get_available_allocated(owner_id, token_id)


def _guru_scaled_size(sig: GuruCopySignal, cfg: StrategyConfig) -> Decimal:
    t = sig.trade
    mult = sizing.conviction_multiplier(t.conviction_score, cfg.sizing.conviction)
    return t.size * cfg.sizing.copy_scale * mult


def _exit_meta(
    *,
    planned: Decimal,
    wallet_qty: Decimal,
    allocated: Decimal,
    available_to_sell: Decimal,
    final_size: Decimal,
    dedup_key: str,
) -> dict[str, str]:
    return {
        "owner_id": OWNER_GURU_FOLLOW,
        "planned_size": str(planned),
        "wallet_position_qty": str(wallet_qty),
        "allocated_available": str(allocated),
        "available_to_sell": str(available_to_sell),
        "final_size": str(final_size),
        "dedup_key": dedup_key,
    }


def _sizing_extension(
    *,
    planned: Decimal,
    wallet_qty: Decimal,
    allocated: Decimal,
    available_to_sell: Decimal,
    final_size: Decimal,
) -> dict[str, str]:
    return {
        "owner_id": OWNER_GURU_FOLLOW,
        "planned_before_clamp": str(planned),
        "wallet_position_qty": str(wallet_qty),
        "allocated_available": str(allocated),
        "available_to_sell": str(available_to_sell),
        "final_size": str(final_size),
    }


def maybe_exit_intent(
    sig: GuruCopySignal,
    cfg: StrategyConfig,
    coord: RuntimeCoordinator,
) -> tuple[ExitIntent | None, str | None, dict[str, Any] | None]:
    """
    Map guru SELL to ExitIntent sized vs guru_follow allocation and venue availability.

    Returns (intent, skip_reason, side_meta). side_meta may include guru_exit_health and
    guru_exit_sizing for pipeline fact emission.
    """
    t = sig.trade
    if t.side != Side.SELL:
        return None, None, None

    snap = inventory_snapshot(coord, t.token_id)
    wallet_qty = Decimal(snap["wallet_position_qty"])
    available_to_sell = Decimal(snap["available_to_sell"])
    guru_scaled = _guru_scaled_size(sig, cfg)
    price = t.price

    allocated = _allocated_available(coord, OWNER_GURU_FOLLOW, t.token_id)
    if cfg.exits.sell_mode == "full_bot_position":
        planned = allocated
    else:
        planned = guru_scaled
    final_size = min(planned, allocated, available_to_sell)
    fields = _exit_meta(
        planned=planned,
        wallet_qty=wallet_qty,
        allocated=allocated,
        available_to_sell=available_to_sell,
        final_size=final_size,
        dedup_key=sig.trade.dedup_key,
    )

    if final_size <= 0:
        if wallet_qty > 0 and allocated <= 0:
            skip = rc.GURU_NO_ALLOCATED_INVENTORY
            health = {
                "event": "guru_exit_allocation_blocked",
                "token_id": str(t.token_id),
                "reason": "insufficient_allocation",
                **fields,
            }
            return None, skip, {"guru_exit_health": health}
        return None, rc.GURU_NO_BOT_INVENTORY, None

    notional = final_size * price if price is not None else Decimal("0")
    if notional > 0 and notional < cfg.exits.dust_notional_usd:
        return None, rc.GURU_EXIT_BELOW_DUST, None

    side_meta: dict[str, Any] = {
        "guru_exit_sizing": _sizing_extension(
            planned=planned,
            wallet_qty=wallet_qty,
            allocated=allocated,
            available_to_sell=available_to_sell,
            final_size=final_size,
        ),
    }
    if final_size < planned:
        side_meta["guru_exit_health"] = {
            "event": "guru_exit_allocation_clamped",
            "token_id": str(t.token_id),
            **fields,
        }
    return (
        ExitIntent(
            token_id=t.token_id,
            side=Side.SELL,
            size=final_size,
            limit_price=price,
            order_style=OrderStyle.GTC,
        ),
        None,
        side_meta,
    )
