from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import ClientOrderId, TokenId, VenueOrderId
from tyrex_pm.core.models import ApprovedIntent, GuruTradeSignal, Intent, RiskDecision


@dataclass(frozen=True)
class MarketBookUpdated:
    token_id: TokenId
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    seq: int | None = None


@dataclass(frozen=True)
class UserOrderUpdated:
    venue_order_id: VenueOrderId
    client_order_id: ClientOrderId | None
    state: str
    token_id: TokenId
    side: Side
    price: Decimal
    remaining: Decimal
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class WalletSnapshotUpdated:
    sync_id: str
    positions: tuple[Any, ...]
    open_orders: tuple[Any, ...]
    ts: datetime


@dataclass(frozen=True)
class HealthChanged:
    component: str
    status: str
    detail: str | None = None


@dataclass(frozen=True)
class ReconcileComplete:
    sync_id: str
    drift_flags: tuple[str, ...]


EventPayload = (
    MarketBookUpdated
    | UserOrderUpdated
    | WalletSnapshotUpdated
    | HealthChanged
    | ReconcileComplete
    | GuruTradeSignal
    | Intent
    | RiskDecision
)
