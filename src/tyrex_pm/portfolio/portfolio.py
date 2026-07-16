"""Portfolio owns positions. Long-only for the validation strategy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tyrex_pm.core.execution_events import OrderFilled, OrderPartiallyFilled
from tyrex_pm.core.ids import InstrumentId, MarketId
from tyrex_pm.core.intents import OrderSide
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.execution.fill_ledger import FillLedger


class PortfolioError(RuntimeError):
    pass


@dataclass
class Position:
    instrument_id: InstrumentId
    market_id: MarketId
    quantity: Decimal
    average_entry_price: Decimal | None
    total_cost: Decimal
    realized_pnl: Decimal
    fees: Decimal
    updated_at: datetime | None

    def to_dict(self) -> dict:
        return {
            "instrument_id": self.instrument_id.value,
            "market_id": self.market_id.value,
            "quantity": str(self.quantity),
            "average_entry_price": None
            if self.average_entry_price is None
            else str(self.average_entry_price),
            "total_cost": str(self.total_cost),
            "realized_pnl": str(self.realized_pnl),
            "fees": str(self.fees),
            "updated_at": None if self.updated_at is None else self.updated_at.isoformat(),
        }


class Portfolio:
    """Consumes new ledger fills to update positions."""

    PRIORITY = 90

    def __init__(self, *, fill_ledger: FillLedger, market_id: MarketId | None = None) -> None:
        self._ledger = fill_ledger
        self._market_id = market_id
        self._positions: dict[str, Position] = {}
        self._applied: set[str] = set()

    def attach(self, dispatcher: EventDispatcher) -> None:
        dispatcher.subscribe(OrderPartiallyFilled, self.on_fill, priority=self.PRIORITY)
        dispatcher.subscribe(OrderFilled, self.on_fill, priority=self.PRIORITY)

    def set_market_id(self, market_id: MarketId) -> None:
        self._market_id = market_id

    def get(self, instrument_id: InstrumentId) -> Position | None:
        return self._positions.get(instrument_id.value)

    def net_quantity(self, instrument_id: InstrumentId) -> Decimal:
        pos = self.get(instrument_id)
        return Decimal("0") if pos is None else pos.quantity

    def is_flat(self) -> bool:
        return all(p.quantity == 0 for p in self._positions.values())

    def total_notional_at_cost(self) -> Decimal:
        return sum((p.total_cost for p in self._positions.values() if p.quantity > 0), Decimal("0"))

    def on_fill(self, event: OrderPartiallyFilled | OrderFilled) -> None:
        key = event.execution_id.value
        if key in self._applied:
            return
        if not self._ledger.has(event.execution_id):
            # Ledger runs at same priority; if not yet recorded, still apply once.
            pass
        self._apply(
            instrument_id=event.instrument_id,
            side=event.side,
            qty=event.fill_quantity,
            price=event.fill_price,
            fee=event.fee_amount,
            when=event.ts_event,
        )
        self._applied.add(key)

    def _apply(
        self,
        *,
        instrument_id: InstrumentId,
        side: OrderSide,
        qty: Decimal,
        price: Decimal,
        fee: Decimal,
        when: datetime,
    ) -> None:
        if self._market_id is None:
            raise PortfolioError("market_id not set")
        pos = self._positions.get(instrument_id.value)
        if pos is None:
            pos = Position(
                instrument_id=instrument_id,
                market_id=self._market_id,
                quantity=Decimal("0"),
                average_entry_price=None,
                total_cost=Decimal("0"),
                realized_pnl=Decimal("0"),
                fees=Decimal("0"),
                updated_at=None,
            )
            self._positions[instrument_id.value] = pos

        if side is OrderSide.BUY:
            new_qty = pos.quantity + qty
            new_cost = pos.total_cost + qty * price
            pos.quantity = new_qty
            pos.total_cost = new_cost
            pos.average_entry_price = None if new_qty == 0 else new_cost / new_qty
            pos.fees += fee
            pos.updated_at = when
            return

        if side is OrderSide.SELL:
            if qty > pos.quantity:
                raise PortfolioError(
                    f"oversell: sell {qty} > position {pos.quantity} on {instrument_id.value}"
                )
            avg = pos.average_entry_price or Decimal("0")
            realized = (price - avg) * qty - fee
            pos.quantity -= qty
            pos.total_cost = avg * pos.quantity
            pos.realized_pnl += realized
            pos.fees += fee
            if pos.quantity == 0:
                pos.average_entry_price = None
                pos.total_cost = Decimal("0")
            pos.updated_at = when
            return

        raise PortfolioError(f"unsupported side {side}")

    def snapshot(self) -> dict:
        return {
            "market_id": None if self._market_id is None else self._market_id.value,
            "positions": [p.to_dict() for p in self._positions.values()],
            "applied_execution_ids": sorted(self._applied),
        }

    def restore(self, data: dict) -> None:
        self._positions.clear()
        self._applied = set(data.get("applied_execution_ids") or [])
        mid = data.get("market_id")
        self._market_id = None if mid is None else MarketId(mid)
        for row in data.get("positions") or []:
            iid = InstrumentId(row["instrument_id"])
            self._positions[iid.value] = Position(
                instrument_id=iid,
                market_id=MarketId(row["market_id"]),
                quantity=Decimal(row["quantity"]),
                average_entry_price=None
                if row["average_entry_price"] is None
                else Decimal(row["average_entry_price"]),
                total_cost=Decimal(row["total_cost"]),
                realized_pnl=Decimal(row["realized_pnl"]),
                fees=Decimal(row["fees"]),
                updated_at=None
                if row.get("updated_at") is None
                else datetime.fromisoformat(row["updated_at"]),
            )
