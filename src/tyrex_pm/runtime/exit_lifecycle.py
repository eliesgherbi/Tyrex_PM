"""P3.5 exit lifecycle helpers: match evidence, inventory arming, facts."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.risk.inventory import available_to_sell
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_EXIT_LIFECYCLE
from tyrex_pm.runtime.coordinator import RuntimeCoordinator

ArmSource = Literal[
    "websocket",
    "immediate_positions_refresh",
    "periodic_refresh",
    "post_buy_ack",
]

MATCHED_STATUSES = frozenset({"matched", "partially_matched", "partial"})


def parse_oms_match_evidence(oms_result: str | dict[str, Any]) -> dict[str, Any]:
    """Extract operator-visible match hints from an OMS JSON response."""
    if isinstance(oms_result, dict):
        parsed = oms_result
    else:
        try:
            parsed = json.loads(oms_result)
        except Exception:
            return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, Any] = {}
    status = parsed.get("status") or parsed.get("orderStatus")
    if status is not None:
        out["match_status"] = str(status)
    for key in ("orderID", "orderId", "order_id"):
        if parsed.get(key):
            out["order_id"] = str(parsed[key])
            break
    for key, out_key in (
        ("takingAmount", "taking_amount"),
        ("makingAmount", "making_amount"),
    ):
        raw = parsed.get(key)
        if raw is not None and str(raw).strip() != "":
            out[out_key] = str(raw)
    return out


def oms_status_is_matched(evidence: dict[str, Any]) -> bool:
    st = str(evidence.get("match_status", "")).lower()
    return st in MATCHED_STATUSES


def parse_taking_amount(evidence: dict[str, Any]) -> Decimal | None:
    raw = evidence.get("taking_amount")
    if raw is None:
        return None
    try:
        val = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return val if val > 0 else None


def required_sell_qty(
    planned_sell_size: Decimal,
    match_taking_amount: Decimal | None,
) -> Decimal:
    """Minimum sellable inventory required before arming the exit timer."""
    if match_taking_amount is not None and match_taking_amount > 0:
        return min(planned_sell_size, match_taking_amount)
    return planned_sell_size


def clamp_planned_sell_size(
    planned_sell_size: Decimal,
    *,
    match_taking_amount: Decimal | None,
    available: Decimal | None = None,
) -> Decimal:
    """Size to use for the scheduled SELL (partial-fill aware)."""
    size = planned_sell_size
    if match_taking_amount is not None and match_taking_amount > 0:
        size = min(size, match_taking_amount)
    if available is not None and available >= 0:
        size = min(size, available)
    return size


def inventory_snapshot(
    coord: RuntimeCoordinator,
    token_id: TokenId,
) -> dict[str, str]:
    positions = {p.token_id: p for p in coord.wallet.positions.values()}
    pos = positions.get(token_id)
    pos_qty = pos.qty if pos is not None else Decimal("0")
    in_flight = coord.orders.in_flight_by_token.get(token_id, Decimal("0"))
    avail = available_to_sell(
        token_id=token_id,
        positions=positions,
        in_flight=coord.orders.in_flight_by_token,
    )
    return {
        "wallet_position_qty": str(pos_qty),
        "in_flight_qty": str(in_flight),
        "available_to_sell": str(avail),
    }


def emit_exit_lifecycle(
    coord: RuntimeCoordinator,
    event: str,
    correlation_id: str,
    **payload: Any,
) -> None:
    if coord.exit_lifecycle_sink is None or coord.exit_lifecycle_run_id is None:
        return
    body = {"event": event, **payload}
    coord.exit_lifecycle_sink.write(
        make_fact(
            FACT_TYPE_EXIT_LIFECYCLE,
            coord.exit_lifecycle_run_id,
            body,
            correlation_id=correlation_id,
        )
    )


def emit_arm_attempt(
    coord: RuntimeCoordinator,
    *,
    event: str,
    token_id: TokenId,
    parent_correlation_id: str,
    planned_sell_size: Decimal,
    required_qty: Decimal,
    source: ArmSource,
    armed: bool,
    snap: dict[str, str],
    reason: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "token_id": str(token_id),
        "parent_correlation_id": parent_correlation_id,
        "planned_sell_size": str(planned_sell_size),
        "required_qty": str(required_qty),
        "source": source,
        "armed": armed,
        **snap,
    }
    if reason is not None:
        payload["reason"] = reason
    emit_exit_lifecycle(coord, event, parent_correlation_id, **payload)
