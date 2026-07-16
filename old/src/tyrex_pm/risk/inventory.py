from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import ExitIntent, ReduceIntent, RiskContext, WalletPosition


def available_to_sell(
    *,
    token_id: TokenId,
    positions: dict[TokenId, WalletPosition],
    in_flight: dict[TokenId, Decimal],
) -> Decimal:
    pos = positions.get(token_id)
    q = pos.qty if pos else Decimal("0")
    reserved = in_flight.get(token_id, Decimal("0"))
    return q - reserved


def check_inventory_sell(
    intent: ExitIntent | ReduceIntent,
    ctx: RiskContext,
    *,
    require_position: bool,
) -> tuple[bool, str | None]:
    if intent.side != Side.SELL:
        return True, None
    if not require_position:
        return True, None
    positions = {p.token_id: p for p in ctx.wallet_positions}
    avail = available_to_sell(
        token_id=intent.token_id,
        positions=positions,
        in_flight=ctx.orders_in_flight_by_token,
    )
    if intent.size > avail:
        return False, rc.NAKED_SELL if avail <= 0 else rc.INSUFFICIENT_INVENTORY
    return True, None
