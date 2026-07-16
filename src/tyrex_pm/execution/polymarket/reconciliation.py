"""Venue ↔ local reconciliation — central R6 capability."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from tyrex_pm.core.ids import OrderId
from tyrex_pm.execution.order_store import OrderRecord, OrderStatus, OrderStore
from tyrex_pm.execution.polymarket.transport import (
    PolymarketTransport,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
    VenueTradeSnapshot,
)
from tyrex_pm.portfolio.portfolio import Portfolio


class ReconcileClass(str, Enum):
    MATCHED = "MATCHED"
    LOCAL_MISSING = "LOCAL_MISSING"
    VENUE_MISSING = "VENUE_MISSING"
    FILL_MISSING_LOCAL = "FILL_MISSING_LOCAL"
    ORDER_STATUS_MISMATCH = "ORDER_STATUS_MISMATCH"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    UNKNOWN_EXTERNAL_ORDER = "UNKNOWN_EXTERNAL_ORDER"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, kw_only=True)
class ReconcileFinding:
    classification: ReconcileClass
    detail: str
    venue_order_id: str | None = None
    local_order_id: str | None = None
    auto_repairable: bool = False
    blocks_entry: bool = False
    requires_manual: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconcileReport:
    findings: list[ReconcileFinding] = field(default_factory=list)
    repaired_fill_ids: list[str] = field(default_factory=list)

    @property
    def blocks_entry(self) -> bool:
        return any(f.blocks_entry for f in self.findings)

    @property
    def requires_manual(self) -> bool:
        return any(f.requires_manual for f in self.findings)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.classification.value] = out.get(f.classification.value, 0) + 1
        return out


_TERMINAL_LOCAL = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}
)


class ReconciliationService:
    """Compare local stores to venue snapshots. Never auto-cancels unknown orders."""

    def __init__(
        self,
        *,
        order_store: OrderStore,
        portfolio: Portfolio,
        owned_venue_ids: set[str] | None = None,
    ) -> None:
        self._orders = order_store
        self._portfolio = portfolio
        self._owned_venue_ids = owned_venue_ids if owned_venue_ids is not None else set()
        self._seen_trade_ids: set[str] = set()

    def register_owned(self, venue_order_id: str) -> None:
        self._owned_venue_ids.add(venue_order_id)

    def reconcile(
        self,
        *,
        venue_orders: list[VenueOrderSnapshot],
        venue_trades: list[VenueTradeSnapshot],
        venue_positions: list[VenuePositionSnapshot],
        missing_evidence: bool = False,
    ) -> ReconcileReport:
        report = ReconcileReport()
        if missing_evidence:
            report.findings.append(
                ReconcileFinding(
                    classification=ReconcileClass.UNRESOLVED,
                    detail="venue evidence incomplete — never assume flat",
                    blocks_entry=True,
                    requires_manual=True,
                )
            )
            return report

        venue_by_id = {o.venue_order_id: o for o in venue_orders if o.venue_order_id}
        local_open = [
            o
            for o in self._orders.working_orders()
            if o.status not in _TERMINAL_LOCAL
        ]
        local_by_venue: dict[str, OrderRecord] = {}
        for row in self._orders.snapshot():
            vid = row.get("venue_order_id")
            if vid:
                rec = self._orders.get(OrderId(row["order_id"]))
                if rec is not None:
                    local_by_venue[str(vid)] = rec

        # Unknown external open orders
        for vid, vo in venue_by_id.items():
            if vid not in self._owned_venue_ids and vid not in local_by_venue:
                report.findings.append(
                    ReconcileFinding(
                        classification=ReconcileClass.UNKNOWN_EXTERNAL_ORDER,
                        detail="venue open order not owned by this run — do not auto-cancel",
                        venue_order_id=vid,
                        blocks_entry=True,
                        requires_manual=True,
                        evidence={"status": vo.status, "token": vo.instrument_token_id},
                    )
                )

        # Local working with venue id but absent from venue open set
        for rec in local_open:
            if rec.venue_order_id is None:
                report.findings.append(
                    ReconcileFinding(
                        classification=ReconcileClass.UNRESOLVED,
                        detail="local working order lacks venue id",
                        local_order_id=rec.order_id.value,
                        blocks_entry=True,
                        requires_manual=True,
                    )
                )
                continue
            if rec.venue_order_id not in venue_by_id:
                report.findings.append(
                    ReconcileFinding(
                        classification=ReconcileClass.VENUE_MISSING,
                        detail="local open order absent from venue open orders — verify history",
                        venue_order_id=rec.venue_order_id,
                        local_order_id=rec.order_id.value,
                        blocks_entry=True,
                        requires_manual=True,
                    )
                )
            else:
                vo = venue_by_id[rec.venue_order_id]
                if Decimal(str(vo.size_matched)) != rec.filled_quantity:
                    report.findings.append(
                        ReconcileFinding(
                            classification=ReconcileClass.ORDER_STATUS_MISMATCH,
                            detail="size_matched mismatch",
                            venue_order_id=rec.venue_order_id,
                            local_order_id=rec.order_id.value,
                            blocks_entry=True,
                            evidence={
                                "venue_matched": str(vo.size_matched),
                                "local_filled": str(rec.filled_quantity),
                            },
                        )
                    )
                else:
                    report.findings.append(
                        ReconcileFinding(
                            classification=ReconcileClass.MATCHED,
                            detail="open order matched",
                            venue_order_id=rec.venue_order_id,
                            local_order_id=rec.order_id.value,
                        )
                    )

        # Fill missing local (unique trade id)
        for trade in venue_trades:
            tid = trade.venue_trade_id
            if not tid or tid in self._seen_trade_ids:
                continue
            if trade.status.upper() not in {"CONFIRMED", "MATCHED", "MINED"}:
                continue
            # Flag repair when uniquely linked to owned venue order.
            if trade.venue_order_id and trade.venue_order_id in self._owned_venue_ids:
                report.findings.append(
                    ReconcileFinding(
                        classification=ReconcileClass.FILL_MISSING_LOCAL,
                        detail="venue trade not yet in local ledger",
                        venue_order_id=trade.venue_order_id,
                        auto_repairable=True,
                        blocks_entry=False,
                        evidence={"trade_id": tid, "size": str(trade.size)},
                    )
                )

        # Position mismatch vs venue
        for vp in venue_positions:
            from tyrex_pm.core.ids import InstrumentId

            local_qty = self._portfolio.net_quantity(InstrumentId(vp.instrument_token_id))
            if local_qty != vp.size:
                report.findings.append(
                    ReconcileFinding(
                        classification=ReconcileClass.POSITION_MISMATCH,
                        detail="local portfolio differs from venue position",
                        blocks_entry=True,
                        requires_manual=True,
                        evidence={
                            "token": vp.instrument_token_id,
                            "local": str(local_qty),
                            "venue": str(vp.size),
                        },
                    )
                )

        return report

    def mark_trade_applied(self, venue_trade_id: str) -> None:
        self._seen_trade_ids.add(venue_trade_id)

    def reconcile_from_transport(
        self, transport: PolymarketTransport, *, market_id: str | None = None
    ) -> ReconcileReport:
        try:
            orders = transport.get_open_orders(market_id=market_id)
            trades = transport.get_trades(market_id=market_id)
            positions = transport.get_positions()
        except Exception as exc:  # noqa: BLE001
            report = ReconcileReport()
            report.findings.append(
                ReconcileFinding(
                    classification=ReconcileClass.UNRESOLVED,
                    detail=f"transport query failed: {type(exc).__name__}",
                    blocks_entry=True,
                    requires_manual=True,
                )
            )
            return report
        return self.reconcile(
            venue_orders=orders,
            venue_trades=trades,
            venue_positions=positions,
        )
