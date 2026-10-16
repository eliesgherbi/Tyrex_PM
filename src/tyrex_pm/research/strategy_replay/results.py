"""Result bundles: JSON conversion, provenance hashes, economics and digests.

Reproducibility is judged on a *decision digest*: ordered decisions (virtual time,
action, reason, side, size, limit), fills and economic outputs. Wall-clock run
times, durations, file paths and notebook metadata are excluded.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .labels import OutcomeLabel
from .loader import WindowData


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, set):
        return sorted(jsonable(v) for v in value)
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, float) and value != value:  # NaN
        return None
    return value


def _canonical(value: Any) -> str:
    return json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"), default=str)


def config_hash(strategy_kind: str, config: Any) -> str:
    if strategy_kind == "settlement_value":
        from tyrex_pm.strategies.settlement_value.schema import (
            settlement_value_config_as_parameters,
        )

        params: Any = settlement_value_config_as_parameters(config)
    else:
        params = jsonable(config)
    payload = _canonical({"kind": strategy_kind, "params": params})
    return hashlib.sha256(payload.encode()).hexdigest()


def economics(
    engine_result: dict, orders: list[dict], label: OutcomeLabel, window: WindowData
) -> dict:
    fills = [e for e in engine_result["ledger"] if e["event"] == "fill"]
    debit = sum((Decimal(str(f["quantity"])) * Decimal(str(f["price"])) for f in fills), Decimal(0))
    gross = sum((Decimal(str(f["quantity"])) for f in fills), Decimal(0))
    net = sum((Decimal(str(f["net_quantity"])) for f in fills), Decimal(0))
    fee_shares = gross - net
    side = None
    if fills:
        side = "UP" if fills[0]["instrument_id"] == window.up_instrument_id else "DOWN"
    entered = bool(orders)
    payout: Decimal | None = None
    pnl: Decimal | None = None
    if fills and label.available:
        payout = net if side == label.winner else Decimal(0)
        pnl = payout - debit
    intended = orders[0] if orders else {}
    return {
        "entered": entered,
        "filled": bool(fills),
        "side": side,
        "intended_quantity": intended.get("quantity"),
        "intended_debit": intended.get("intended_debit"),
        "limit_price": intended.get("limit_price"),
        "ev_conservative_at_decision": intended.get("ev_conservative"),
        "ev_point_at_decision": intended.get("ev_point"),
        "p_conservative_at_decision": intended.get("p_conservative"),
        "p_point_at_decision": intended.get("p_point"),
        "filled_gross_shares": gross,
        "filled_net_shares": net,
        "fee_shares": fee_shares,
        "debit": debit,
        "vwap": None if gross == 0 else debit / gross,
        "winner": label.winner,
        "label_type": label.label_type,
        "payout": payout,
        "realized_pnl": pnl,
        "fill_ratio": None
        if not intended.get("quantity")
        else gross / Decimal(str(intended["quantity"])),
    }


def decision_digest(result: dict) -> str:
    """Stable digest over decisions, fills and economics (no wall-clock fields)."""
    decisions = [
        (
            d.get("at_ns"),
            d.get("action"),
            d.get("reason"),
            d.get("chosen_side"),
            d.get("intended_gross_shares"),
            d.get("max_price"),
        )
        for d in result["decisions"]
    ]
    fills = [
        (e.get("at_ns"), e.get("order_id"), e.get("quantity"), e.get("price"), e.get("fee"))
        for e in result["engine"]["ledger"]
        if e.get("event") == "fill"
    ]
    payload = {
        "config_hash": result["config_hash"],
        "scenario": result["scenario"],
        "window": result["window"]["market_id"],
        "source_fingerprint": result["provenance"]["source_fingerprint"],
        "decisions": decisions,
        "fills": fills,
        "economics": result["economics"],
        "label": result["label"],
    }
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def write_result(result: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{result['window']['slug']}__{result['scenario_name']}__{result['config_hash'][:12]}"
    path = directory / f"{name}.json"
    bundle = {**result, "decision_digest": decision_digest(result)}
    path.write_text(json.dumps(jsonable(bundle), indent=1, sort_keys=True), encoding="utf-8")
    return path
