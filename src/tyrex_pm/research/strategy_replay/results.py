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
    """Identity of a strategy configuration (the plugin's research identity when declared)."""
    from tyrex_pm.strategies.registry import get_strategy_plugin

    research = get_strategy_plugin(strategy_kind).research
    params: Any = jsonable(config) if research is None else research.identity_parameters(config)
    payload = _canonical({"kind": strategy_kind, "params": params})
    return hashlib.sha256(payload.encode()).hexdigest()


_EXECUTION_SOURCES = (
    "src/tyrex_pm/market_data/reconcile.py",
    "src/tyrex_pm/market_data/book_store.py",
    "src/tyrex_pm/market_data/depth_snapshot.py",
    "src/tyrex_pm/data_warehouse/reconstruction/books.py",
    "src/tyrex_pm/execution/planner.py",
    "src/tyrex_pm/execution/final_gate.py",
    "src/tyrex_pm/execution/outcome_reasons.py",
    "src/tyrex_pm/execution/exit_arbitration.py",
    "src/tyrex_pm/execution/orders.py",
    "src/tyrex_pm/research/replay/engine.py",
    "src/tyrex_pm/research/replay/config.py",
    "src/tyrex_pm/research/strategy_replay/run.py",
    "src/tyrex_pm/strategies/outcomes.py",
    "src/tyrex_pm/protection/triggers.py",
    "src/tyrex_pm/protection/monitor.py",
    "src/tyrex_pm/protection/state.py",
)
_EXECUTION_IDENTITY: dict | None = None


def execution_source_identity() -> dict:
    """Digest of the result-affecting execution/simulator sources (computed once)."""
    global _EXECUTION_IDENTITY
    if _EXECUTION_IDENTITY is None:
        root = Path(__file__).resolve().parents[4]
        digest = hashlib.sha256()
        present = []
        for rel in _EXECUTION_SOURCES:
            path = root / rel
            if path.exists():
                digest.update(rel.encode())
                digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
                present.append(rel)
        _EXECUTION_IDENTITY = {"digest": digest.hexdigest(), "files": present}
    return _EXECUTION_IDENTITY


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def economics(
    engine_result: dict, attempts: list[dict], label: OutcomeLabel, window: WindowData
) -> dict:
    """Window economics plus per-episode rows.

    Legacy keys keep their v2 meaning for a single-attempt hold run; ``intended_quantity``
    is the first attempt's estimated shares (the strategy's gross shares under the v2
    share contract, the planner's spend / limit under the amount contract).
    P&L = payout of inventory still held at the end + net sale proceeds - entry debit.
    """
    fills = [e for e in engine_result["ledger"] if e["event"] == "fill"]
    buys = [f for f in fills if f.get("side", "BUY") == "BUY"]
    sells = [f for f in fills if f.get("side") == "SELL"]
    debit = sum((_dec(f["quantity"]) * _dec(f["price"]) for f in buys), Decimal(0))
    gross = sum((_dec(f["quantity"]) for f in buys), Decimal(0))
    net = sum((_dec(f["net_quantity"]) for f in buys), Decimal(0))
    sold = sum((_dec(f["quantity"]) for f in sells), Decimal(0))
    proceeds = sum(
        (_dec(f["quantity"]) * _dec(f["price"]) - _dec(f["fee"]) for f in sells), Decimal(0)
    )
    fee_shares = gross - net
    side = None
    if buys:
        side = "UP" if buys[0]["instrument_id"] == window.up_instrument_id else "DOWN"
    entries = [a for a in attempts if a.get("role") == "ENTRY"]
    exits = [a for a in attempts if a.get("role") == "EXIT"]
    held: dict[str, Decimal] = {}
    for f in fills:
        delta = (
            _dec(f["net_quantity"]) if f.get("side", "BUY") == "BUY" else -_dec(f["quantity"])
        )
        held[f["instrument_id"]] = held.get(f["instrument_id"], Decimal(0)) + delta
    payout: Decimal | None = None
    pnl: Decimal | None = None
    if fills and label.available:
        payout = Decimal(0)
        for iid, shares in held.items():
            iid_side = "UP" if iid == window.up_instrument_id else "DOWN"
            if iid_side == label.winner and shares > 0:
                payout += shares
        pnl = payout + proceeds - debit
    intended = entries[0] if entries else {}
    first_filled = next((a for a in entries if a.get("outcome") in ("FILLED", "PARTIAL")), None)
    fill_ratio = None
    if first_filled and first_filled.get("estimated_shares"):
        fill_ratio = _dec(first_filled.get("filled_gross") or 0) / _dec(
            first_filled["estimated_shares"]
        )
    episodes: dict[int, dict] = {}
    current = 1
    for a in attempts:
        if a.get("episode_seq"):
            current = int(a["episode_seq"])
        ep = episodes.setdefault(
            current,
            {"episode_seq": current, "side": a.get("side"), "entry_attempts": 0,
             "entry_not_sent": 0, "entry_zero_fill": 0, "posts": 0, "filled": False,
             "exits": 0, "exit_reasons": []},
        )
        if a.get("role") == "ENTRY":
            ep["entry_attempts"] += 1
            ep["entry_not_sent"] += int(a.get("outcome") == "NOT_SENT")
            ep["entry_zero_fill"] += int(a.get("outcome") == "ZERO_FILL")
            ep["posts"] += int(a.get("outcome") not in (None, "NOT_SENT"))
            ep["filled"] = ep["filled"] or a.get("outcome") in ("FILLED", "PARTIAL")
        else:
            ep["exits"] += 1
            ep["exit_reasons"].append(a.get("exit_reason"))
    return {
        "entered": bool(entries),
        "filled": bool(buys),
        "side": side,
        "intended_quantity": intended.get("estimated_shares"),
        "intended_debit": intended.get("intended_debit"),
        "limit_price": intended.get("limit_price"),
        "spend_amount": intended.get("spend_amount"),
        "ev_conservative_at_decision": intended.get("ev_conservative"),
        "ev_point_at_decision": intended.get("ev_point"),
        "p_conservative_at_decision": intended.get("p_conservative"),
        "p_point_at_decision": intended.get("p_point"),
        "filled_gross_shares": gross,
        "filled_net_shares": net,
        "fee_shares": fee_shares,
        "debit": debit,
        "vwap": None if gross == 0 else debit / gross,
        "sold_shares": sold,
        "exit_proceeds_net": proceeds,
        "final_net_shares": sum((s for s in held.values() if s > 0), Decimal(0)),
        "winner": label.winner,
        "label_type": label.label_type,
        "payout": payout,
        "realized_pnl": pnl,
        "fill_ratio": fill_ratio,
        "entry_attempts": len(entries),
        "entry_not_sent": sum(1 for a in entries if a.get("outcome") == "NOT_SENT"),
        "entry_zero_fill": sum(1 for a in entries if a.get("outcome") == "ZERO_FILL"),
        "entry_posts": sum(1 for a in entries if a.get("outcome") not in (None, "NOT_SENT")),
        "exit_attempts": len(exits),
        "episodes": sorted(episodes.values(), key=lambda e: e["episode_seq"]),
        "filled_episodes": sum(1 for e in episodes.values() if e["filled"]),
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
