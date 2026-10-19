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
    TradeAttributionCorrected,
    TradeStatus,
    TradeStatusObserved,
    new_envelope,
)
from tyrex_pm.execution.exit_sizing import trade_remaining_after_exits
from tyrex_pm.execution.fill_attribution import (
    attribute_owned_fill,
    is_known_normalization_defect,
)
from tyrex_pm.execution.orders import OrderSide
from tyrex_pm.execution.session_state import ExecutionSessionState


def _value(model: Any, name: str, default: Any = None) -> Any:
    if isinstance(model, dict):
        return model.get(name, default)
    return getattr(model, name, default)


def _owned_sides(state: ExecutionSessionState) -> dict[str, str]:
    sides: dict[str, str] = {}
    for order in state.orders.values():
        if order.venue_order_id:
            sides[order.venue_order_id] = order.spec.side.value
    return sides


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


def _may_own_balance(state: ExecutionSessionState) -> bool:
    """False only for a provably never-sent attempt: it has entry orders, none of them
    reached a POST attempt or got a venue order id, and it holds no BUY trade. Such a
    session cannot own shares. Sessions without entry orders (adopted or recovered
    holdings proven by observation) and any posted or uncertain attempt keep the
    balance as an authority, so genuine late fills are still detected."""
    entries = [o for o in state.orders.values() if o.role is ExecutionRole.ENTRY]
    if not entries:
        return True
    if any(order.attempt_ids or order.venue_order_id for order in entries):
        return True
    return any(trade.side == "BUY" for trade in state.trades.values())


def _sibling_owned_since(coordinator: Any, state: ExecutionSessionState) -> Decimal:
    """Shares other sessions on the same token and window acquired after ``state``
    opened, proven by their own trades.

    The account balance is token-wide: without this, every session would claim the same
    shares. A sibling opened later owns all its trade-proven inventory; an earlier one
    only what it gained after this session's baseline was taken.
    """
    identity = state.identity
    if identity is None:
        return Decimal("0")
    owned = Decimal("0")
    for other in getattr(coordinator, "states", {}).values():
        if other is state or other.identity is None or other.session_id == state.session_id:
            continue
        if (
            other.identity.token_id != identity.token_id
            or other.identity.window_id != identity.window_id
        ):
            continue
        remaining = trade_remaining_after_exits(other)
        if remaining <= 0:
            continue
        opened_after = (
            other.opened_at is not None
            and state.opened_at is not None
            and other.opened_at >= state.opened_at
        )
        if opened_after:
            owned += remaining
        else:
            already_in_baseline = max(
                Decimal("0"), state.baseline_position_shares - other.baseline_position_shares
            )
            owned += max(Decimal("0"), remaining - already_in_baseline)
    return owned


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
            state = coordinator.state(session_id)
            owned_ids = set(owned_by_venue)
            sides = _owned_sides(state)
            for trade in trades:
                attributed = attribute_owned_fill(
                    trade,
                    owned_ids,
                    identity.token_id,
                    owned_side_by_venue=sides,
                )
                clip = attributed.clip
                if clip is None:
                    if attributed.reason in {
                        "multiple_owned_clips",
                        "both_roles",
                        "token_mismatch",
                    }:
                        notes.append(f"trade_attribution:{attributed.reason}")
                        complete = False
                    continue
                venue_order_id = clip.venue_order_id
                local_id, _role = owned_by_venue[venue_order_id]
                status = _trade_status(_value(trade, "status"))
                if status is None:
                    notes.append(f"unknown_trade_status:{_value(trade, 'status', '')}")
                    complete = False
                    continue
                updated_at = _value(trade, "updated_at")
                if not isinstance(updated_at, datetime):
                    updated_at = datetime.now(timezone.utc)
                venue_trade_id = str(_value(trade, "id"))
                existing = state.trades.get(venue_trade_id)
                if existing is not None and (
                    existing.venue_order_id != clip.venue_order_id
                    or existing.side != clip.side
                    or existing.shares != clip.shares
                    or existing.price != clip.price
                    or existing.token_id != clip.token_id
                ):
                    if is_known_normalization_defect(
                        stored_venue_order_id=existing.venue_order_id,
                        clip_venue_order_id=clip.venue_order_id,
                        owned_venue_ids=owned_ids,
                    ):
                        await coordinator.apply(
                            TradeAttributionCorrected(
                                **new_envelope(
                                    session_id=session_id,
                                    dedupe_key=(
                                        f"trade-attribution-corrected:{venue_trade_id}:"
                                        f"{clip.venue_order_id}:{clip.shares}:{clip.side}"
                                    ),
                                ),
                                source="readonly_rest",
                                venue_trade_id=venue_trade_id,
                                original_event_id=None,
                                previous_venue_order_id=existing.venue_order_id,
                                previous_side=OrderSide(existing.side),
                                previous_shares=existing.shares,
                                previous_price=existing.price,
                                previous_token_id=existing.token_id,
                                venue_order_id=clip.venue_order_id,
                                order_id=local_id,
                                token_id=clip.token_id or identity.token_id,
                                side=OrderSide(clip.side),
                                shares=clip.shares,
                                price=clip.price,
                                status=status,
                            )
                        )
                        state = coordinator.state(session_id)
                        continue
                    notes.append("trade_attribution:conflicting_trade_identity")
                    complete = False
                    continue
                await coordinator.apply(
                    TradeStatusObserved(
                        **new_envelope(
                            session_id=session_id,
                            dedupe_key=(f"rest-trade:{venue_trade_id}:{status.value}"),
                        ),
                        source="readonly_rest",
                        venue_trade_id=venue_trade_id,
                        venue_order_id=venue_order_id,
                        order_id=local_id,
                        token_id=clip.token_id or identity.token_id,
                        side=OrderSide(clip.side),
                        shares=clip.shares,
                        price=clip.price,
                        status=status,
                        venue_event_at=updated_at,
                        raw_maker_order_ids=clip.raw_maker_order_ids,
                        attribution=clip.attribution,
                    )
                )
                state = coordinator.state(session_id)
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
        if not _may_own_balance(state):
            # Never sent (no POST attempt, no venue order, no trade): it cannot own
            # shares. A balance change on its token belongs to a sibling attempt.
            if account_position > 0:
                notes.append("balance_not_attributable:no_buy_evidence")
            account_position = Decimal("0")
        else:
            siblings = _sibling_owned_since(coordinator, state)
            if siblings > 0:
                notes.append(f"sibling_owned_since_open:{siblings}")
                account_position = max(Decimal("0"), account_position - siblings)
        # Either source may lag.  Confirmed session trades create an exposure
        # obligation that a stale balance read cannot erase; conversely, a
        # lagging post-SELL balance prevents a premature flat declaration.
        # A matched-but-unconfirmed EXIT HWM is subtracted so leftover crumbs
        # are not treated as the original buy clip.
        position = max(
            Decimal("0"),
            account_position,
            trade_remaining_after_exits(state),
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
