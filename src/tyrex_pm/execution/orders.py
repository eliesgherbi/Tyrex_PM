"""Permanent execution order contracts.

The contracts deliberately distinguish collateral-denominated market BUYs from
share-denominated SELLs.  An estimated BUY share quantity is diagnostic only;
the venue-confirmed trades own the acquired quantity.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, TypeAlias


def _decimal(value: Decimal | int | float | str, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - normalize public input
        raise ValueError(f"{field} must be a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _positive(value: Decimal | int | float | str, *, field: str) -> Decimal:
    result = _decimal(value, field=field)
    if result <= 0:
        raise ValueError(f"{field} must be > 0")
    return result


def _price(value: Decimal | int | float | str, *, field: str) -> Decimal:
    result = _positive(value, field=field)
    if result > 1:
        raise ValueError(f"{field} must be <= 1")
    return result


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TimeInForce(str, Enum):
    FAK = "FAK"
    FOK = "FOK"
    GTC = "GTC"
    GTD = "GTD"


class OrderKind(str, Enum):
    MARKET_BUY = "MARKET_BUY"
    MARKET_SELL = "MARKET_SELL"
    LIMIT = "LIMIT"


@dataclass(frozen=True, kw_only=True)
class MarketBuyOrderSpec:
    order_id: str
    market_id: str
    instrument_id: str
    token_id: str
    spend_amount: Decimal
    maximum_total_debit: Decimal
    worst_price: Decimal
    estimated_shares: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.FAK
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for field_name in ("order_id", "market_id", "instrument_id", "token_id"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} must be non-empty")
        spend = _positive(self.spend_amount, field="spend_amount")
        debit = _positive(self.maximum_total_debit, field="maximum_total_debit")
        if spend > debit:
            raise ValueError("spend_amount cannot exceed maximum_total_debit")
        if self.time_in_force not in {TimeInForce.FAK, TimeInForce.FOK}:
            raise ValueError("market BUY requires FAK or FOK")
        estimate = (
            None
            if self.estimated_shares is None
            else _positive(self.estimated_shares, field="estimated_shares")
        )
        object.__setattr__(self, "spend_amount", spend)
        object.__setattr__(self, "maximum_total_debit", debit)
        object.__setattr__(self, "worst_price", _price(self.worst_price, field="worst_price"))
        object.__setattr__(self, "estimated_shares", estimate)
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @property
    def kind(self) -> OrderKind:
        return OrderKind.MARKET_BUY

    @property
    def side(self) -> OrderSide:
        return OrderSide.BUY


@dataclass(frozen=True, kw_only=True)
class MarketSellOrderSpec:
    order_id: str
    market_id: str
    instrument_id: str
    token_id: str
    shares: Decimal
    minimum_price: Decimal
    time_in_force: TimeInForce = TimeInForce.FAK
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for field_name in ("order_id", "market_id", "instrument_id", "token_id"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} must be non-empty")
        if self.time_in_force not in {TimeInForce.FAK, TimeInForce.FOK}:
            raise ValueError("market SELL requires FAK or FOK")
        object.__setattr__(self, "shares", _positive(self.shares, field="shares"))
        object.__setattr__(self, "minimum_price", _price(self.minimum_price, field="minimum_price"))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @property
    def kind(self) -> OrderKind:
        return OrderKind.MARKET_SELL

    @property
    def side(self) -> OrderSide:
        return OrderSide.SELL


@dataclass(frozen=True, kw_only=True)
class LimitOrderSpec:
    order_id: str
    market_id: str
    instrument_id: str
    token_id: str
    side: OrderSide
    shares: Decimal
    limit_price: Decimal
    time_in_force: TimeInForce = TimeInForce.GTC
    expiration_epoch_s: int | None = None
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for field_name in ("order_id", "market_id", "instrument_id", "token_id"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} must be non-empty")
        if self.time_in_force not in {TimeInForce.GTC, TimeInForce.GTD}:
            raise ValueError("limit order requires GTC or GTD")
        if self.time_in_force is TimeInForce.GTD and self.expiration_epoch_s is None:
            raise ValueError("GTD order requires expiration_epoch_s")
        object.__setattr__(self, "shares", _positive(self.shares, field="shares"))
        object.__setattr__(self, "limit_price", _price(self.limit_price, field="limit_price"))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @property
    def kind(self) -> OrderKind:
        return OrderKind.LIMIT


OrderSpec: TypeAlias = MarketBuyOrderSpec | MarketSellOrderSpec | LimitOrderSpec


def order_spec_to_dict(spec: OrderSpec) -> dict[str, Any]:
    payload = asdict(spec)
    payload["kind"] = spec.kind.value
    payload["side"] = spec.side.value
    payload["time_in_force"] = spec.time_in_force.value
    for key, value in tuple(payload.items()):
        if isinstance(value, Decimal):
            payload[key] = str(value)
    return payload


def order_spec_from_dict(payload: Mapping[str, Any]) -> OrderSpec:
    data = dict(payload)
    kind = OrderKind(str(data.pop("kind")))
    data.pop("side", None)
    data["time_in_force"] = TimeInForce(str(data["time_in_force"]))
    if kind is OrderKind.MARKET_BUY:
        return MarketBuyOrderSpec(**data)
    if kind is OrderKind.MARKET_SELL:
        return MarketSellOrderSpec(**data)
    data["side"] = OrderSide(str(payload["side"]))
    return LimitOrderSpec(**data)
