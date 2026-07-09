"""Authoritative fill / cashflow reconciliation for realized PnL.

Separates planned, submitted, OMS-ack, and venue-confirmed trade evidence.
Only venue-confirmed (user-WS CONFIRMED) cashflows qualify as FINAL for
``paired_binary_realized_pnl`` when ``require_final_for_realized_pnl`` is true.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.runtime.cashflows import (
    SOURCE_OMS_MATCH_EVIDENCE,
    SOURCE_SHADOW_FILL,
    SOURCE_USER_WS_TRADE_FILL,
    SOURCE_VENUE_TRADE_REPAIR,
)
from tyrex_pm.state import fill_state


class FillSource(str, Enum):
    PLANNED = "planned"
    LIMIT_ORDER = "limit_order"
    OMS_ACK = "oms_ack"
    WS_TRADE = "ws_trade"
    REST_ORDER = "rest_order"
    WALLET_POSITION = "wallet_position"
    VENUE_RECONCILED = "venue_reconciled"
    MANUAL = "manual"


class FillConfidence(str, Enum):
    FINAL = "final"
    TENTATIVE = "tentative"
    UNRECONCILED = "unreconciled"
    MISSING = "missing"


LegName = Literal["yes", "no"]
LegSide = Literal["entry", "exit"]


@dataclass(frozen=True)
class FillReconciliationConfig:
    price_tolerance: Decimal = Decimal("0.001")
    cash_tolerance_usd: Decimal = Decimal("0.01")
    require_final_for_realized_pnl: bool = True


@dataclass(frozen=True)
class FillCashflow:
    token_id: str
    side: Literal["BUY", "SELL"]
    leg: str
    qty: Decimal
    avg_price: Decimal | None
    cash: Decimal | None
    source: FillSource
    confidence: FillConfidence
    order_id: str | None = None
    trade_ids: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconciledTradeCashflows:
    entry_yes: FillCashflow | None
    entry_no: FillCashflow | None
    exit_yes: FillCashflow | None
    exit_no: FillCashflow | None
    all_final: bool
    has_discrepancy: bool
    discrepancies: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ReconciledPnL:
    status: Literal["final", "tentative", "unavailable"]
    pnl_total: Decimal | None
    pnl_per_pair: Decimal | None
    buy_cash_total: Decimal | None
    sell_cash_total: Decimal | None
    missing_fields: tuple[str, ...]
    confidence: FillConfidence
    cashflows: ReconciledTradeCashflows
    evidence: dict[str, Any] = field(default_factory=dict)


def _map_legacy_source(source: str | None) -> tuple[FillSource, FillConfidence]:
    src = str(source or "").strip().lower()
    if src in {SOURCE_USER_WS_TRADE_FILL, "user_ws_confirmed_trade"}:
        return FillSource.WS_TRADE, FillConfidence.FINAL
    if src == SOURCE_VENUE_TRADE_REPAIR:
        return FillSource.WALLET_POSITION, FillConfidence.TENTATIVE
    if src == SOURCE_OMS_MATCH_EVIDENCE:
        return FillSource.OMS_ACK, FillConfidence.TENTATIVE
    if src == SOURCE_SHADOW_FILL:
        return FillSource.PLANNED, FillConfidence.UNRECONCILED
    if src:
        return FillSource.OMS_ACK, FillConfidence.TENTATIVE
    return FillSource.PLANNED, FillConfidence.MISSING


def _aggregate_ws_trades(
    trades: list[Any],
    *,
    token_id: str,
    side: Side,
    final_only: bool,
) -> FillCashflow | None:
    selected = []
    for rec in trades:
        if str(rec.token_id) != str(token_id) or rec.side != side:
            continue
        cls = fill_state.classify(rec.status)
        if final_only:
            if not cls.realized_pnl:
                continue
        elif not cls.execution_evidence:
            continue
        if rec.size <= 0 or rec.price < 0:
            continue
        selected.append(rec)
    if not selected:
        return None
    qty = sum((rec.size for rec in selected), Decimal("0"))
    cash = sum((rec.size * rec.price for rec in selected), Decimal("0"))
    if qty <= 0:
        return None
    confidence = (
        FillConfidence.FINAL
        if all(fill_state.counts_for_realized_pnl(rec.status) for rec in selected)
        else FillConfidence.TENTATIVE
    )
    source = FillSource.VENUE_RECONCILED if confidence == FillConfidence.FINAL else FillSource.WS_TRADE
    return FillCashflow(
        token_id=str(token_id),
        side="BUY" if side == Side.BUY else "SELL",
        leg="",
        qty=qty,
        avg_price=cash / qty,
        cash=cash,
        source=source,
        confidence=confidence,
        trade_ids=tuple(str(getattr(rec, "trade_id", "")) for rec in selected if getattr(rec, "trade_id", None)),
        evidence={"trade_count": len(selected), "statuses": sorted({str(rec.status) for rec in selected})},
    )


def _cashflow_from_leg_state(
    *,
    leg_name: str,
    side: Side,
    token_id: str,
    entry_cash: Decimal | None,
    entry_qty: Decimal | None,
    entry_source: str | None,
    exit_cash: Decimal | None,
    exit_qty: Decimal | None,
    exit_source: str | None,
    order_id: str | None = None,
) -> FillCashflow | None:
    if side == Side.BUY:
        cash, qty, src = entry_cash, entry_qty, entry_source
    else:
        cash, qty, src = exit_cash, exit_qty, exit_source
    if cash is None or qty is None or qty <= 0:
        return None
    fill_source, confidence = _map_legacy_source(src)
    return FillCashflow(
        token_id=str(token_id),
        side="BUY" if side == Side.BUY else "SELL",
        leg=leg_name,
        qty=qty,
        avg_price=cash / qty,
        cash=cash,
        source=fill_source,
        confidence=confidence,
        order_id=order_id,
        evidence={"local_cash_source": src},
    )


def _pick_cashflow(
    *,
    leg_name: str,
    side: Side,
    token_id: str,
    ws_final: FillCashflow | None,
    ws_tentative: FillCashflow | None,
    local: FillCashflow | None,
) -> FillCashflow | None:
    for candidate in (ws_final, ws_tentative, local):
        if candidate is None:
            continue
        out = FillCashflow(
            token_id=candidate.token_id,
            side=candidate.side,
            leg=leg_name,
            qty=candidate.qty,
            avg_price=candidate.avg_price,
            cash=candidate.cash,
            source=candidate.source,
            confidence=candidate.confidence,
            order_id=candidate.order_id,
            trade_ids=candidate.trade_ids,
            evidence=dict(candidate.evidence),
        )
        return out
    return None


def _compare_cashflows(
    local: FillCashflow | None,
    venue: FillCashflow | None,
    *,
    cfg: FillReconciliationConfig,
    leg: str,
    side: str,
) -> dict[str, Any] | None:
    if local is None or venue is None:
        return None
    if local.cash is None or venue.cash is None:
        return None
    if local.avg_price is None or venue.avg_price is None:
        return None
    price_delta = abs(local.avg_price - venue.avg_price)
    cash_delta = abs(local.cash - venue.cash)
    if price_delta <= cfg.price_tolerance and cash_delta <= cfg.cash_tolerance_usd:
        return None
    severity = "warning"
    if cash_delta > cfg.cash_tolerance_usd * 3 or price_delta > cfg.price_tolerance * 5:
        severity = "error"
    return {
        "leg": leg,
        "side": side,
        "local_price": str(local.avg_price),
        "venue_price": str(venue.avg_price),
        "local_cash": str(local.cash),
        "venue_cash": str(venue.cash),
        "price_delta": str(price_delta),
        "cash_delta": str(cash_delta),
        "threshold_price": str(cfg.price_tolerance),
        "threshold_cash": str(cfg.cash_tolerance_usd),
        "severity": severity,
        "local_source": local.source.value,
        "venue_source": venue.source.value,
    }


def reconcile_paired_binary_cashflows(
    state,
    *,
    coord=None,
    cfg: FillReconciliationConfig | None = None,
) -> ReconciledTradeCashflows:
    """Build best-effort cashflows for paired-binary entry/exit legs."""
    cfg = cfg or FillReconciliationConfig()
    trades = list(getattr(getattr(coord, "wallet", None), "trade_fill_records", []) or [])

    def leg_bundle(
        leg_name: str, token_id: str, leg_rt
    ) -> tuple[FillCashflow | None, FillCashflow | None, FillCashflow | None, FillCashflow | None]:
        buy_side = Side.BUY
        sell_side = Side.SELL
        ws_final_entry = _aggregate_ws_trades(trades, token_id=token_id, side=buy_side, final_only=True)
        ws_tent_entry = _aggregate_ws_trades(trades, token_id=token_id, side=buy_side, final_only=False)
        ws_final_exit = _aggregate_ws_trades(trades, token_id=token_id, side=sell_side, final_only=True)
        ws_tent_exit = _aggregate_ws_trades(trades, token_id=token_id, side=sell_side, final_only=False)
        local_entry = _cashflow_from_leg_state(
            leg_name=leg_name,
            side=buy_side,
            token_id=token_id,
            entry_cash=leg_rt.entry_cash,
            entry_qty=leg_rt.entry_qty,
            entry_source=leg_rt.entry_cash_source,
            exit_cash=None,
            exit_qty=None,
            exit_source=None,
            order_id=leg_rt.entry_client_order_id,
        )
        local_exit = _cashflow_from_leg_state(
            leg_name=leg_name,
            side=sell_side,
            token_id=token_id,
            entry_cash=None,
            entry_qty=None,
            entry_source=None,
            exit_cash=leg_rt.exit_cash,
            exit_qty=leg_rt.exit_qty,
            exit_source=leg_rt.exit_cash_source,
            order_id=leg_rt.entry_client_order_id,
        )
        entry = _pick_cashflow(
            leg_name=leg_name,
            side=buy_side,
            token_id=token_id,
            ws_final=ws_final_entry,
            ws_tentative=ws_tent_entry,
            local=local_entry,
        )
        exit_cf = _pick_cashflow(
            leg_name=leg_name,
            side=sell_side,
            token_id=token_id,
            ws_final=ws_final_exit,
            ws_tentative=ws_tent_exit,
            local=local_exit,
        )
        return entry, local_entry, exit_cf, local_exit

    yes_entry, yes_local_entry, yes_exit, yes_local_exit = leg_bundle("yes", state.yes_token_id, state.yes)
    no_entry, no_local_entry, no_exit, no_local_exit = leg_bundle("no", state.no_token_id, state.no)

    token_map = {"yes": state.yes_token_id, "no": state.no_token_id}
    discrepancies: list[dict[str, Any]] = []
    for leg, side_label, local, picked, trade_side in (
        ("yes", "entry", yes_local_entry, yes_entry, Side.BUY),
        ("no", "entry", no_local_entry, no_entry, Side.BUY),
        ("yes", "exit", yes_local_exit, yes_exit, Side.SELL),
        ("no", "exit", no_local_exit, no_exit, Side.SELL),
    ):
        ws_venue = _aggregate_ws_trades(
            trades, token_id=str(token_map[leg]), side=trade_side, final_only=True
        ) or _aggregate_ws_trades(
            trades, token_id=str(token_map[leg]), side=trade_side, final_only=False
        )
        venue = ws_venue if ws_venue is not None else (picked if picked and picked.source != FillSource.OMS_ACK else None)
        if local is not None and venue is not None and local.source == FillSource.OMS_ACK:
            disc = _compare_cashflows(local, venue, cfg=cfg, leg=leg, side=side_label)
            if disc:
                discrepancies.append(disc)

    slots = (yes_entry, no_entry, yes_exit, no_exit)
    all_final = all(cf is not None and cf.confidence == FillConfidence.FINAL for cf in slots)
    return ReconciledTradeCashflows(
        entry_yes=yes_entry,
        entry_no=no_entry,
        exit_yes=yes_exit,
        exit_no=no_exit,
        all_final=all_final,
        has_discrepancy=bool(discrepancies),
        discrepancies=tuple(discrepancies),
    )


def compute_reconciled_pnl(
    cashflows: ReconciledTradeCashflows,
    *,
    effective_qty: Decimal,
    cfg: FillReconciliationConfig | None = None,
) -> ReconciledPnL:
    cfg = cfg or FillReconciliationConfig()
    missing: list[str] = []
    for name, cf in (
        ("entry_yes", cashflows.entry_yes),
        ("entry_no", cashflows.entry_no),
        ("exit_yes", cashflows.exit_yes),
        ("exit_no", cashflows.exit_no),
    ):
        if cf is None or cf.cash is None:
            missing.append(name)

    if missing:
        return ReconciledPnL(
            status="unavailable",
            pnl_total=None,
            pnl_per_pair=None,
            buy_cash_total=None,
            sell_cash_total=None,
            missing_fields=tuple(missing),
            confidence=FillConfidence.MISSING,
            cashflows=cashflows,
            evidence={"require_final": cfg.require_final_for_realized_pnl},
        )

    assert cashflows.entry_yes and cashflows.entry_no and cashflows.exit_yes and cashflows.exit_no
    buy_total = cashflows.entry_yes.cash + cashflows.entry_no.cash
    sell_total = cashflows.exit_yes.cash + cashflows.exit_no.cash
    assert buy_total is not None and sell_total is not None
    qty = effective_qty
    if qty <= 0:
        qty = min(
            cashflows.entry_yes.qty,
            cashflows.entry_no.qty,
            cashflows.exit_yes.qty,
            cashflows.exit_no.qty,
        )
    pnl_total = sell_total - buy_total
    pnl_per_pair = pnl_total / qty if qty > 0 else None

    confidences = {
        cashflows.entry_yes.confidence,
        cashflows.entry_no.confidence,
        cashflows.exit_yes.confidence,
        cashflows.exit_no.confidence,
    }
    if cfg.require_final_for_realized_pnl and not cashflows.all_final:
        status: Literal["final", "tentative", "unavailable"] = "tentative"
        confidence = (
            FillConfidence.TENTATIVE
            if FillConfidence.UNRECONCILED not in confidences
            else FillConfidence.UNRECONCILED
        )
    elif cashflows.all_final:
        status = "final"
        confidence = FillConfidence.FINAL
    else:
        status = "tentative"
        confidence = FillConfidence.TENTATIVE

    if cashflows.has_discrepancy and cfg.require_final_for_realized_pnl:
        status = "tentative"

    return ReconciledPnL(
        status=status,
        pnl_total=pnl_total,
        pnl_per_pair=pnl_per_pair,
        buy_cash_total=buy_total,
        sell_cash_total=sell_total,
        missing_fields=tuple(),
        confidence=confidence,
        cashflows=cashflows,
        evidence={
            "cashflow_sources": {
                "entry_yes": cashflows.entry_yes.source.value,
                "entry_no": cashflows.entry_no.source.value,
                "exit_yes": cashflows.exit_yes.source.value,
                "exit_no": cashflows.exit_no.source.value,
            },
            "cashflow_confidence": {
                "entry_yes": cashflows.entry_yes.confidence.value,
                "entry_no": cashflows.entry_no.confidence.value,
                "exit_yes": cashflows.exit_yes.confidence.value,
                "exit_no": cashflows.exit_no.confidence.value,
            },
            "has_discrepancy": cashflows.has_discrepancy,
            "require_final_for_realized_pnl": cfg.require_final_for_realized_pnl,
        },
    )


def manual_cashflows_from_fills(manual: dict[str, dict[str, str]]) -> ReconciledTradeCashflows:
    """Build cashflows from operator-supplied PM UI fills."""

    def _one(key: str, leg: str, side: Literal["BUY", "SELL"]) -> FillCashflow:
        row = manual[key]
        qty = Decimal(str(row["qty"]))
        price = Decimal(str(row["price"]))
        cash = qty * price
        return FillCashflow(
            token_id="manual",
            side=side,
            leg=leg,
            qty=qty,
            avg_price=price,
            cash=cash,
            source=FillSource.MANUAL,
            confidence=FillConfidence.FINAL,
            evidence={"manual_key": key},
        )

    entry_yes = _one("yes_buy", "yes", "BUY")
    entry_no = _one("no_buy", "no", "BUY")
    exit_no = _one("no_sell", "no", "SELL")
    exit_yes = _one("yes_sell", "yes", "SELL")
    return ReconciledTradeCashflows(
        entry_yes=entry_yes,
        entry_no=entry_no,
        exit_yes=exit_yes,
        exit_no=exit_no,
        all_final=True,
        has_discrepancy=False,
        discrepancies=(),
    )
