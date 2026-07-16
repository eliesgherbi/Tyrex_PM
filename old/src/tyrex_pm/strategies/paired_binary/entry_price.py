"""True fill-price resolution for paired-binary pair-PnL math (Phase 4.6 robustness)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import ClientOrderId, TokenId
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.exit_lifecycle import MATCHED_STATUSES
from tyrex_pm.state import fill_state


SOURCE_OMS_MATCHED_AVG = "oms_matched_avg"
SOURCE_OMS_LIMIT_PRICE_FALLBACK = "oms_limit_price_fallback"
SOURCE_USER_WS_CONFIRMED = "user_ws_confirmed_trade"
SOURCE_VENUE_TRADE_REPAIR = "venue_trade_repair"
SOURCE_SHADOW_FILL = "shadow_fill"
SOURCE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class LegEntryPriceResolution:
    price: Decimal | None
    source: str
    oms_price: Decimal | None = None
    ws_price: Decimal | None = None
    reconcile_price: Decimal | None = None


@dataclass(frozen=True)
class PairEntryPriceResolution:
    yes: LegEntryPriceResolution
    no: LegEntryPriceResolution

    @property
    def ready(self) -> bool:
        return (
            self.yes.price is not None
            and self.no.price is not None
            and self.yes.source != SOURCE_UNKNOWN
            and self.no.source != SOURCE_UNKNOWN
        )


@dataclass(frozen=True)
class EntryPriceMismatch:
    leg: str
    oms_price: Decimal | None
    ws_price: Decimal | None
    reconcile_price: Decimal | None
    chosen_price: Decimal
    chosen_source: str
    delta: Decimal
    tolerance: Decimal


def _oms_fill_price(coord: RuntimeCoordinator, client_order_id: str | None) -> Decimal | None:
    """Legacy limit-price fallback only — not authoritative for PnL."""
    if not client_order_id:
        return None
    lo = coord.orders.orders.get(ClientOrderId(client_order_id))
    if lo is None or lo.side != Side.BUY:
        return None
    if lo.limit_price is None:
        return None
    if lo.size_matched is not None and lo.size_matched > 0:
        return lo.limit_price
    if lo.ack_status and str(lo.ack_status).lower() in MATCHED_STATUSES:
        return lo.limit_price
    return None


def _ws_fill_price(coord: RuntimeCoordinator, token_id: TokenId) -> Decimal | None:
    for rec in reversed(coord.wallet.trade_fill_records):
        if rec.token_id != token_id or rec.side != Side.BUY:
            continue
        status = str(rec.status).upper()
        if fill_state.is_allocation_final(status) or fill_state.is_execution_evidence(status):
            return rec.price
    return None


def _wallet_avg_price(coord: RuntimeCoordinator, token_id: TokenId) -> Decimal | None:
    pos = coord.wallet.positions.get(token_id)
    if pos is None or pos.qty <= 0:
        return None
    return pos.avg_price_usd


def resolve_leg_entry_price(
    coord: RuntimeCoordinator,
    token_id: TokenId,
    *,
    client_order_id: str | None = None,
    apply_shadow_fill: bool = False,
    leg_runtime: Any | None = None,
) -> LegEntryPriceResolution:
    from tyrex_pm.strategies.paired_binary.state import LegRuntime, leg_entry_avg_price

    if isinstance(leg_runtime, LegRuntime):
        avg = leg_entry_avg_price(leg_runtime)
        if avg is not None and leg_runtime.entry_cash_source:
            return LegEntryPriceResolution(
                price=avg,
                source=leg_runtime.entry_cash_source,
                oms_price=avg,
                ws_price=_ws_fill_price(coord, token_id),
                reconcile_price=_wallet_avg_price(coord, token_id),
            )

    oms_price = _oms_fill_price(coord, client_order_id)
    ws_price = _ws_fill_price(coord, token_id)
    reconcile_price = _wallet_avg_price(coord, token_id)

    if oms_price is not None:
        return LegEntryPriceResolution(
            price=oms_price,
            source=SOURCE_OMS_LIMIT_PRICE_FALLBACK,
            oms_price=oms_price,
            ws_price=ws_price,
            reconcile_price=reconcile_price,
        )
    if ws_price is not None:
        return LegEntryPriceResolution(
            price=ws_price,
            source=SOURCE_USER_WS_CONFIRMED,
            oms_price=oms_price,
            ws_price=ws_price,
            reconcile_price=reconcile_price,
        )
    if reconcile_price is not None:
        return LegEntryPriceResolution(
            price=reconcile_price,
            source=SOURCE_VENUE_TRADE_REPAIR,
            oms_price=oms_price,
            ws_price=ws_price,
            reconcile_price=reconcile_price,
        )
    if apply_shadow_fill and client_order_id:
        lo = coord.orders.orders.get(ClientOrderId(client_order_id))
        if lo is not None and lo.limit_price is not None:
            return LegEntryPriceResolution(
                price=lo.limit_price,
                source=SOURCE_SHADOW_FILL,
                oms_price=lo.limit_price,
                ws_price=ws_price,
                reconcile_price=reconcile_price,
            )
    return LegEntryPriceResolution(
        price=None,
        source=SOURCE_UNKNOWN,
        oms_price=oms_price,
        ws_price=ws_price,
        reconcile_price=reconcile_price,
    )


def resolve_pair_entry_prices(
    coord: RuntimeCoordinator,
    *,
    yes_token_id: str,
    no_token_id: str,
    yes_client_order_id: str | None,
    no_client_order_id: str | None,
    apply_shadow_fill: bool = False,
    state: Any | None = None,
) -> PairEntryPriceResolution:
    yes_leg = getattr(state, "yes", None) if state is not None else None
    no_leg = getattr(state, "no", None) if state is not None else None
    yes = resolve_leg_entry_price(
        coord,
        TokenId(yes_token_id),
        client_order_id=yes_client_order_id,
        apply_shadow_fill=apply_shadow_fill,
        leg_runtime=yes_leg,
    )
    no = resolve_leg_entry_price(
        coord,
        TokenId(no_token_id),
        client_order_id=no_client_order_id,
        apply_shadow_fill=apply_shadow_fill,
        leg_runtime=no_leg,
    )
    return PairEntryPriceResolution(yes=yes, no=no)


def detect_entry_price_mismatch(
    leg: str,
    res: LegEntryPriceResolution,
    *,
    tolerance: Decimal,
) -> EntryPriceMismatch | None:
    if res.price is None:
        return None
    candidates: list[tuple[str, Decimal]] = []
    if res.oms_price is not None:
        candidates.append(("oms", res.oms_price))
    if res.ws_price is not None:
        candidates.append(("ws", res.ws_price))
    if res.reconcile_price is not None:
        candidates.append(("reconcile", res.reconcile_price))
    if len(candidates) < 2:
        return None
    prices = [p for _, p in candidates]
    spread = max(prices) - min(prices)
    if spread <= tolerance:
        return None
    return EntryPriceMismatch(
        leg=leg,
        oms_price=res.oms_price,
        ws_price=res.ws_price,
        reconcile_price=res.reconcile_price,
        chosen_price=res.price,
        chosen_source=res.source,
        delta=spread,
        tolerance=tolerance,
    )


def max_entry_price_mismatch(res: PairEntryPriceResolution) -> Decimal:
    spreads: list[Decimal] = []
    for leg_res in (res.yes, res.no):
        prices = [
            p
            for p in (leg_res.oms_price, leg_res.ws_price, leg_res.reconcile_price)
            if p is not None
        ]
        if len(prices) >= 2:
            spreads.append(max(prices) - min(prices))
    if not spreads:
        return Decimal("0")
    return max(spreads)
