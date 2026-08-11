"""Authoritative read-only reconciliation for an execution session."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from tyrex_pm.execution.evidence import (
    ExecutionRole,
    OrderSnapshotObserved,
    ReconciliationObserved,
    TradeStatus,
    TradeStatusObserved,
    new_envelope,
)
from tyrex_pm.execution.orders import OrderSide
from tyrex_pm.execution.session_state import ExecutionSessionState


def _value(model: Any, name: str, default: Any = None) -> Any:
    if isinstance(model, dict):
        return model.get(name, default)
    return getattr(model, name, default)


def _trade_order_id(trade: Any, owned: set[str]) -> str | None:
    taker = str(_value(trade, "taker_order_id", "") or "")
    if taker in owned:
        return taker
    for maker in _value(trade, "maker_orders", ()) or ():
        candidate = str(_value(maker, "order_id", "") or "")
        if candidate in owned:
            return candidate
    return None


def _trade_status(raw: Any) -> TradeStatus | None:
    value = str(raw or "").removeprefix("TRADE_STATUS_").upper()
    if value == "MATCHED_NOT_BROADCASTED":
        value = TradeStatus.MATCHED.value
    try:
        return TradeStatus(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class ReconciliationResult:
    complete: bool
    position_shares: Decimal
    sellable_shares: Decimal
    open_order_ids: tuple[str, ...]
    notes: tuple[str, ...]


class SessionReconciler:
    """Backfill venue evidence and close the loop against account balances.

    It never guesses ownership. Only the session's recorded venue order IDs and
    selected token are admitted. Account balance is used as the final position
    authority relative to the immutable session baseline.
    """

    def __init__(self, gateway: Any) -> None:
        self._gateway = gateway

    async def reconcile(self, coordinator: Any, session_id: str) -> ReconciliationResult:
        state: ExecutionSessionState = coordinator.state(session_id)
        if state.identity is None:
            raise RuntimeError("cannot reconcile an unopened execution session")
        identity = state.identity
        owned_by_venue: dict[str, tuple[str, ExecutionRole]] = {}
        for order in state.orders.values():
            if order.venue_order_id:
                owned_by_venue[order.venue_order_id] = (
                    order.spec.order_id,
                    order.role,
                )

        notes: list[str] = []
        complete = True
        open_ids: list[str] = []
        try:
            open_orders = await self._gateway.list_open_orders(
                token_id=identity.token_id, market_id=identity.market_id
            )
            for order in open_orders:
                venue_id = str(_value(order, "id", "") or "")
                owned = owned_by_venue.get(venue_id)
                if owned is None:
                    notes.append(f"unowned_open_order:{venue_id or 'missing_id'}")
                    complete = False
                    continue
                local_id, _role = owned
                open_ids.append(venue_id)
                await coordinator.apply(
                    OrderSnapshotObserved(
                        **new_envelope(
                            session_id=session_id,
                            dedupe_key=(
                                f"rest-order:{venue_id}:{_value(order, 'status', '')}:"
                                f"{_value(order, 'size_matched', '0')}"
                            ),
                        ),
                        source="readonly_rest",
                        venue_order_id=venue_id,
                        order_id=local_id,
                        side=OrderSide(str(_value(order, "side", "BUY")).upper()),
                        original_shares=Decimal(str(_value(order, "original_size", "0"))),
                        cumulative_matched_shares=Decimal(str(_value(order, "size_matched", "0"))),
                        status=str(_value(order, "status", "UNKNOWN")),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - incomplete is an explicit result
            notes.append(f"open_orders:{type(exc).__name__}:{exc}")
            complete = False

        try:
            trades = await self._gateway.list_account_trades(
                token_id=identity.token_id, market_id=identity.market_id
            )
            owned_ids = set(owned_by_venue)
            for trade in trades:
                venue_order_id = _trade_order_id(trade, owned_ids)
                if venue_order_id is None:
                    continue
                local_id, _role = owned_by_venue[venue_order_id]
                status = _trade_status(_value(trade, "status"))
                if status is None:
                    notes.append(f"unknown_trade_status:{_value(trade, 'status', '')}")
                    complete = False
                    continue
                updated_at = _value(trade, "updated_at")
                if not isinstance(updated_at, datetime):
                    updated_at = datetime.now(timezone.utc)
                await coordinator.apply(
                    TradeStatusObserved(
                        **new_envelope(
                            session_id=session_id,
                            dedupe_key=(f"rest-trade:{_value(trade, 'id', '')}:{status.value}"),
                        ),
                        source="readonly_rest",
                        venue_trade_id=str(_value(trade, "id")),
                        venue_order_id=venue_order_id,
                        order_id=local_id,
                        token_id=identity.token_id,
                        side=OrderSide(str(_value(trade, "side", "BUY")).upper()),
                        shares=Decimal(str(_value(trade, "size", "0"))),
                        price=Decimal(str(_value(trade, "price", "0"))),
                        status=status,
                        venue_event_at=updated_at,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            notes.append(f"trades:{type(exc).__name__}:{exc}")
            complete = False

        current_balance = state.baseline_position_shares
        current_allowance: Decimal | None = None
        try:
            current_balance, current_allowance = await self._gateway.conditional_balance(
                identity.token_id
            )
        except Exception as exc:  # noqa: BLE001
            notes.append(f"balance:{type(exc).__name__}:{exc}")
            complete = False

        account_position = max(Decimal("0"), current_balance - state.baseline_position_shares)
        # Either source may lag.  Confirmed session trades create an exposure
        # obligation that a stale balance read cannot erase; conversely, a
        # lagging post-SELL balance prevents a premature flat declaration.
        position = max(
            Decimal("0"),
            account_position,
            state.confirmed_trade_net_shares,
        )
        if current_allowance is None:
            sellable = account_position
        else:
            baseline_allowance = state.baseline_sellable_shares
            allowance_delta = max(Decimal("0"), current_allowance - baseline_allowance)
            sellable = min(
                account_position,
                allowance_delta if baseline_allowance else current_allowance,
            )

        await coordinator.apply(
            ReconciliationObserved(
                **new_envelope(
                    session_id=session_id,
                    dedupe_key=(
                        f"reconcile:{position}:{sellable}:{','.join(sorted(open_ids))}:"
                        f"{complete}:{len(state.trades)}"
                    ),
                ),
                source="readonly_rest",
                complete=complete,
                confirmed_position_shares=position,
                sellable_shares=sellable,
                open_order_ids=tuple(sorted(open_ids)),
                notes=tuple(notes),
            )
        )
        return ReconciliationResult(
            complete=complete,
            position_shares=position,
            sellable_shares=sellable,
            open_order_ids=tuple(sorted(open_ids)),
            notes=tuple(notes),
        )
