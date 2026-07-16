"""Matched-order cashflow extraction (venue/OMS/user-WS trade evidence).

Polymarket CLOB convention used throughout Tyrex:
  BUY  — ``makingAmount`` = USDC spent, ``takingAmount`` = outcome tokens received
  SELL — ``takingAmount`` = USDC received, ``makingAmount`` = outcome tokens sold
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from tyrex_pm.core.enums import Side
from tyrex_pm.runtime.exit_lifecycle import MATCHED_STATUSES, oms_status_is_matched, parse_taking_amount


SOURCE_OMS_MATCH_EVIDENCE = "oms_match_evidence"
SOURCE_USER_WS_TRADE_FILL = "user_ws_trade_fill"
SOURCE_VENUE_TRADE_REPAIR = "venue_trade_repair"
SOURCE_SHADOW_FILL = "shadow_fill"
SOURCE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class MatchedCashflow:
    cash: Decimal
    qty: Decimal
    avg_price: Decimal
    source: str


def _parse_amount(evidence: dict, *keys: str) -> Decimal | None:
    for key in keys:
        raw = evidence.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            val = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            continue
        if val >= 0:
            return val
    return None


def _evidence_is_matched(evidence: dict) -> bool:
    if oms_status_is_matched(evidence):
        return True
    st = str(evidence.get("status", "")).lower()
    return st in MATCHED_STATUSES


def extract_matched_cashflow(
    side: Side,
    match_evidence: dict | None,
    *,
    source: str = SOURCE_OMS_MATCH_EVIDENCE,
    apply_shadow_fill: bool = False,
    shadow_qty: Decimal | None = None,
    shadow_cash: Decimal | None = None,
) -> MatchedCashflow | None:
    """Return matched cash/qty/avg_price or None when evidence is insufficient."""
    if match_evidence:
        if _evidence_is_matched(match_evidence):
            taking = _parse_amount(match_evidence, "taking_amount", "takingAmount")
            making = _parse_amount(match_evidence, "making_amount", "makingAmount")
            if side == Side.BUY:
                qty = taking
                cash = making
            else:
                cash = taking
                qty = making
            if qty is not None and qty > 0 and cash is not None:
                return MatchedCashflow(
                    cash=cash,
                    qty=qty,
                    avg_price=cash / qty,
                    source=source,
                )
        if apply_shadow_fill and shadow_qty is not None and shadow_qty > 0:
            cash = shadow_cash
            if cash is None and match_evidence.get("limit_price"):
                try:
                    cash = Decimal(str(match_evidence["limit_price"])) * shadow_qty
                except (InvalidOperation, ValueError):
                    cash = None
            if cash is not None:
                return MatchedCashflow(
                    cash=cash,
                    qty=shadow_qty,
                    avg_price=cash / shadow_qty,
                    source=SOURCE_SHADOW_FILL,
                )
    if apply_shadow_fill and shadow_qty is not None and shadow_qty > 0 and shadow_cash is not None:
        return MatchedCashflow(
            cash=shadow_cash,
            qty=shadow_qty,
            avg_price=shadow_cash / shadow_qty,
            source=SOURCE_SHADOW_FILL,
        )
    return None


def cashflow_from_ws_trade(
    *,
    side: Side,
    size: Decimal,
    price: Decimal,
    status: str,
) -> MatchedCashflow | None:
    """Build cashflow from a user-WS trade line when OMS amounts are unavailable."""
    if size <= 0 or price < 0:
        return None
    st = str(status).upper()
    if st not in {"MATCHED", "MINED", "CONFIRMED"}:
        return None
    cash = size * price
    return MatchedCashflow(cash=cash, qty=size, avg_price=price, source=SOURCE_USER_WS_TRADE_FILL)


def ws_trade_cashflow_for_token(coord, token_id, side: Side) -> MatchedCashflow | None:
    """Most recent qualifying user-WS trade for token+side."""
    from tyrex_pm.state import fill_state

    for rec in reversed(coord.wallet.trade_fill_records):
        if rec.token_id != token_id or rec.side != side:
            continue
        cf = cashflow_from_ws_trade(
            side=side,
            size=rec.size,
            price=rec.price,
            status=rec.status,
        )
        if cf is not None:
            return cf
    return None
