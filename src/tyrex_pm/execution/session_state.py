"""Authoritative in-process execution-session state.

The state is reconstructed exclusively by replaying normalized execution
evidence.  Venue observations are facts; reports, portfolio views and safety
decisions are projections of this state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from tyrex_pm.execution.evidence import ExecutionRole, TradeStatus
from tyrex_pm.execution.orders import OrderSpec


class ExecutionPhase(str, Enum):
    NEW = "NEW"
    FLAT = "FLAT"
    ENTRY_PREPARING = "ENTRY_PREPARING"
    ENTRY_DISPATCHING = "ENTRY_DISPATCHING"
    POSITION_PENDING_CONFIRMATION = "POSITION_PENDING_CONFIRMATION"
    POSITION_OPEN = "POSITION_OPEN"
    EXIT_PREPARING = "EXIT_PREPARING"
    EXIT_DISPATCHING = "EXIT_DISPATCHING"
    RECONCILING = "RECONCILING"
    COMPLETED_NO_DISPATCH = "COMPLETED_NO_DISPATCH"
    COMPLETED_NO_FILL = "COMPLETED_NO_FILL"
    COMPLETED_FLAT = "COMPLETED_FLAT"
    COMPLETED_FLAT_WITH_DUST = "COMPLETED_FLAT_WITH_DUST"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"


TERMINAL_PHASES = {
    ExecutionPhase.COMPLETED_NO_DISPATCH,
    ExecutionPhase.COMPLETED_NO_FILL,
    ExecutionPhase.COMPLETED_FLAT,
    ExecutionPhase.COMPLETED_FLAT_WITH_DUST,
    ExecutionPhase.MANUAL_INTERVENTION,
}


@dataclass(frozen=True)
class SessionIdentity:
    session_id: str
    strategy_id: str
    market_id: str
    window_id: str
    token_id: str


@dataclass
class TradeRecord:
    venue_trade_id: str
    venue_order_id: str
    local_order_id: str | None
    token_id: str
    side: str
    shares: Decimal
    price: Decimal
    status: TradeStatus
    sources: set[str] = field(default_factory=set)
    position_applied: bool = False
    attribution_digest: str | None = None


@dataclass
class OrderExecutionState:
    role: ExecutionRole
    spec: OrderSpec
    prepared_order_digest: str | None = None
    requested_protection_price: Decimal | None = None
    effective_protection_price: Decimal | None = None
    tick_size: Decimal | None = None
    prepare_queue_wait_ms: float | None = None
    tick_resolve_ms: float | None = None
    create_market_order_ms: float | None = None
    refresh_wait_ms: float | None = None
    refresh_elapsed_ms: float | None = None
    order_metadata_cache_miss: bool = False
    pre_dispatch_stage: str | None = None
    pre_dispatch_error_code: str | None = None
    dispatch_authorized: bool = False
    attempt_ids: list[str] = field(default_factory=list)
    venue_order_id: str | None = None
    accepted: bool | None = None
    venue_status: str | None = None
    cumulative_matched_hwm: Decimal = Decimal("0")
    trade_ids_from_response: set[str] = field(default_factory=set)
    ambiguous: bool = False
    last_error: str | None = None


@dataclass(frozen=True)
class KnownDustResidual:
    token_id: str
    shares: Decimal
    market_id: str
    window_id: str
    classification: str
    last_bid: Decimal | None = None
    min_order_size: Decimal | None = None
    sell_size_increment: Decimal | None = None
    notional: Decimal | None = None
    reason: str = "classified_dust"


@dataclass
class ExecutionSessionState:
    identity: SessionIdentity | None = None
    phase: ExecutionPhase = ExecutionPhase.NEW
    orders: dict[str, OrderExecutionState] = field(default_factory=dict)
    entry_order_ids: list[str] = field(default_factory=list)
    exit_order_ids: list[str] = field(default_factory=list)
    trades: dict[str, TradeRecord] = field(default_factory=dict)
    baseline_position_shares: Decimal = Decimal("0")
    baseline_sellable_shares: Decimal = Decimal("0")
    baseline_open_order_ids: tuple[str, ...] = ()
    confirmed_position_shares: Decimal = Decimal("0")
    sellable_shares: Decimal = Decimal("0")
    last_balance_source: str | None = None
    exit_requested_reason: str | None = None
    protective_exit: bool = False
    reconciliation_complete: bool = False
    reconciliation_notes: tuple[str, ...] = ()
    open_venue_order_ids: tuple[str, ...] = ()
    dust_residual: KnownDustResidual | None = None
    applied_event_ids: set[str] = field(default_factory=set)
    event_count: int = 0
    last_error: str | None = None

    @property
    def session_id(self) -> str | None:
        return None if self.identity is None else self.identity.session_id

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @property
    def needs_venue_followup(self) -> bool:
        return bool(
            self.has_exposure
            or self.working_maker_entry
            or self.resting_limit_venue_ids
            or self.has_unresolved_mutation_attempt
            or self.entry_match_awaiting_confirmation
            or (self.phase is ExecutionPhase.RECONCILING)
        )

    @property
    def has_journal_uncertainty(self) -> bool:
        return bool(
            self.has_unresolved_mutation_attempt
            or self.entry_match_awaiting_confirmation
            or self.resting_limit_venue_ids
        )

    @property
    def safe_to_open_entry(self) -> bool:
        """Finished reporting is not the same as safe to start a new BUY."""
        if self.phase is ExecutionPhase.MANUAL_INTERVENTION:
            return False
        if self.has_exposure or self.has_journal_uncertainty:
            return False
        if self.phase is ExecutionPhase.RECONCILING:
            return False
        return self.terminal

    @property
    def has_exposure(self) -> bool:
        if self.phase is ExecutionPhase.COMPLETED_FLAT_WITH_DUST:
            return False
        return self.confirmed_position_shares > 0

    @property
    def confirmed_buy_shares(self) -> Decimal:
        return sum(
            (
                trade.shares
                for trade in self.trades.values()
                if trade.status is TradeStatus.CONFIRMED and trade.side == "BUY"
            ),
            Decimal("0"),
        )

    @property
    def confirmed_sell_shares(self) -> Decimal:
        return sum(
            (
                trade.shares
                for trade in self.trades.values()
                if trade.status is TradeStatus.CONFIRMED and trade.side == "SELL"
            ),
            Decimal("0"),
        )

    @property
    def exit_matched_hwm(self) -> Decimal:
        return sum(
            (
                order.cumulative_matched_hwm
                for order in self.orders.values()
                if order.role is ExecutionRole.EXIT
            ),
            Decimal("0"),
        )

    @property
    def confirmed_trade_net_shares(self) -> Decimal:
        """Net position proven by confirmed, session-owned venue trades."""
        return self.confirmed_buy_shares - self.confirmed_sell_shares

    @property
    def has_confirmed_buy(self) -> bool:
        return self.confirmed_buy_shares > 0

    @property
    def has_confirmed_sell(self) -> bool:
        return self.confirmed_sell_shares > 0

    @property
    def entry_authoritatively_unfilled(self) -> bool:
        entry = self.entry
        if entry is None or entry.cumulative_matched_hwm > 0 or entry.trade_ids_from_response:
            return False
        if entry.accepted is False:
            return True
        status = str(entry.venue_status or "").lower()
        return entry.accepted is True and status in {
            "unmatched",
            "cancelled",
            "canceled",
            "expired",
        }

    @property
    def entry_match_awaiting_confirmation(self) -> bool:
        entry = self.entry
        return bool(
            entry is not None
            and (entry.cumulative_matched_hwm > 0 or entry.trade_ids_from_response)
            and not self.has_confirmed_buy
        )

    @property
    def has_unresolved_mutation_attempt(self) -> bool:
        return any(
            bool(order.attempt_ids) and (order.accepted is None or order.ambiguous)
            for order in self.orders.values()
        )

    @property
    def mutations_attempted(self) -> int:
        return sum(len(order.attempt_ids) for order in self.orders.values())

    @property
    def entry(self) -> OrderExecutionState | None:
        return None if not self.entry_order_ids else self.orders[self.entry_order_ids[-1]]

    @property
    def exit(self) -> OrderExecutionState | None:
        return None if not self.exit_order_ids else self.orders[self.exit_order_ids[-1]]

    def order_for_role(self, role: ExecutionRole) -> OrderExecutionState | None:
        return self.entry if role is ExecutionRole.ENTRY else self.exit

    @property
    def working_maker_entry(self) -> bool:
        entry = self.entry
        if entry is None or self.has_confirmed_buy:
            return False
        status = str(entry.venue_status or "").lower()
        return bool(entry.accepted) and status in {"live", "open", "resting"}

    @property
    def resting_limit_venue_ids(self) -> tuple[str, ...]:
        ids: list[str] = []
        for order in self.orders.values():
            status = str(order.venue_status or "").lower()
            if order.venue_order_id and status in {"live", "open", "resting"}:
                ids.append(order.venue_order_id)
        for venue_id in self.open_venue_order_ids:
            if venue_id not in ids:
                ids.append(venue_id)
        return tuple(ids)
