from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from tyrex_pm.core.enums import EventSource, ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, IntentId, RunId, TokenId, VenueOrderId


@dataclass(frozen=True)
class EventEnvelope:
    """Internal bus envelope (see EVENT_CATALOG.md)."""

    event_id: UUID
    schema_version: int
    ts_recv: datetime
    source: EventSource
    payload: Any  # discriminated by type(payload)


@dataclass(frozen=True)
class GuruTradeSignal:
    """Normalized guru activity row — always has canonical token_id."""

    guru_wallet: str
    token_id: TokenId
    side: Side
    size: Decimal
    price: Decimal | None
    notional_usd: Decimal | None
    dedup_key: str
    ts_venue: datetime | None
    raw_ref: str | None = None
    conviction_score: Decimal | None = None


@dataclass(frozen=True)
class EnterIntent:
    token_id: TokenId
    side: Side
    size: Decimal
    limit_price: Decimal | None
    order_style: OrderStyle
    intent_id: IntentId = field(default_factory=lambda: IntentId(str(uuid4())))


@dataclass(frozen=True)
class ExitIntent:
    token_id: TokenId
    side: Side
    size: Decimal
    limit_price: Decimal | None
    order_style: OrderStyle
    intent_id: IntentId = field(default_factory=lambda: IntentId(str(uuid4())))


@dataclass(frozen=True)
class ReduceIntent:
    token_id: TokenId
    side: Side
    size: Decimal
    limit_price: Decimal | None
    order_style: OrderStyle
    intent_id: IntentId = field(default_factory=lambda: IntentId(str(uuid4())))


@dataclass(frozen=True)
class CancelIntent:
    venue_order_id: VenueOrderId | None
    client_order_id: ClientOrderId | None
    intent_id: IntentId = field(default_factory=lambda: IntentId(str(uuid4())))


Intent = EnterIntent | ExitIntent | ReduceIntent | CancelIntent


@dataclass(frozen=True)
class ApprovedIntent:
    intent: EnterIntent | ExitIntent | ReduceIntent
    client_order_id: ClientOrderId
    run_id: RunId


@dataclass(frozen=True)
class ApprovedCancel:
    """Risk-approved cancel request; venue id may be resolved from OrderStore in the pipeline."""

    venue_order_id: VenueOrderId | None
    client_order_id: ClientOrderId | None
    run_id: RunId
    intent_id: IntentId


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason_codes: tuple[str, ...]
    approved_intent: ApprovedIntent | None
    detail: str | None = None
    approved_cancel: ApprovedCancel | None = None
    #: Extra operator-visible fields merged into risk_decision facts (notional policy, etc.).
    extensions: dict[str, Any] | None = None


@dataclass(frozen=True)
class WalletPosition:
    token_id: TokenId
    qty: Decimal  # + long outcome tokens
    avg_price_usd: Decimal | None = None


@dataclass(frozen=True)
class OpenOrderView:
    token_id: TokenId
    side: Side
    remaining_size: Decimal
    limit_price: Decimal
    client_order_id: ClientOrderId | None
    venue_order_id: VenueOrderId | None
    #: When known (REST/WS), venue original order size and matched amount for observability / reconcile.
    original_size: Decimal | None = None
    size_matched: Decimal | None = None
    #: Which channel last populated this row in the merged view: ``user_ws`` (primary live) or ``rest`` (repair).
    venue_state_source: str | None = None
    #: Raw venue status when provided (e.g. order lifecycle on user channel).
    order_status: str | None = None


@dataclass(frozen=True)
class TradeFillRecord:
    """User-channel trade line (MATCHED / MINED / CONFIRMED); positions updated only on CONFIRMED."""

    token_id: TokenId
    side: Side
    size: Decimal
    price: Decimal
    status: str
    ts_utc: datetime
    source: str = "user_ws"


@dataclass(frozen=True)
class RiskContext:
    execution_mode: ExecutionMode
    wallet_positions: tuple[WalletPosition, ...]
    open_orders: tuple[OpenOrderView, ...]
    usdc_balance: Decimal | None
    usdc_allowance: Decimal | None
    last_wallet_sync_ts: datetime | None
    mark_prices: dict[TokenId, Decimal]
    kill_switch: bool
    health_ok: bool
    heartbeat_ok: bool
    clob_session_ok: bool
    in_flight_order_count: int
    orders_in_flight_by_token: dict[TokenId, Decimal]
    reconcile_drift: bool = False
    venue_truth_stale: bool = False
    #: Synthetic resting BUY views for local OMS rows that the venue has accepted (or about to)
    #: but ``open_orders`` (built from venue truth) does not yet reflect. Closes the
    #: register_submit → wallet.open_orders mirror gap during which both the deployment cap
    #: and the USDC capital gate would otherwise approve orders the venue has already locked
    #: collateral for. Derived (not stored): see :func:`tyrex_pm.risk.in_flight.derive_in_flight_buy_reservations`.
    in_flight_buy_reservations: tuple[OpenOrderView, ...] = ()


@dataclass(frozen=True)
class StrategyContext:
    run_id: RunId
