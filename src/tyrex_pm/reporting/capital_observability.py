"""Capital / wallet observability helpers (reporting-only; no trading decisions)."""

from __future__ import annotations

import json
from typing import Any


def trim_json_text(obj: Any, *, max_chars: int = 2048) -> str | None:
    """Serialize *obj* to JSON for facts; truncate with ellipsis when oversized."""
    try:
        text = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def venue_denial_insufficient_balance_likely(reason: str | None) -> bool:
    """
    Heuristic classifier for venue/framework denial text (preserves raw ``reason`` separately).
    """
    if not reason:
        return False
    r = reason.lower()
    needles = (
        "balance",
        "insufficient",
        "allowance",
        "collateral",
        "funds",
        "underfund",
        "not enough",
        "lack of",
        "usdc",
    )
    return any(n in r for n in needles)


def parse_risk_capital_flags_from_config_json(config_json: str) -> dict[str, Any]:
    """
    Best-effort read of ``risk.capital_gate_enabled`` from a ``config_snapshot`` payload.

    Returns keys: ``capital_gate_enabled`` (bool | None), ``parse_ok`` (bool).
    """
    out: dict[str, Any] = {"capital_gate_enabled": None, "parse_ok": False}
    try:
        root = json.loads(config_json)
    except json.JSONDecodeError:
        return out
    if not isinstance(root, dict):
        return out
    risk = root.get("risk")
    if not isinstance(risk, dict):
        out["parse_ok"] = True
        return out
    raw = risk.get("capital_gate_enabled")
    if isinstance(raw, bool):
        out["capital_gate_enabled"] = raw
    elif raw is not None:
        out["capital_gate_enabled"] = bool(raw)
    out["parse_ok"] = True
    return out


def compute_buy_headroom_usd(
    balance: float | None,
    reserve_usd: float,
    intent_notional_usd: float | None,
) -> float | None:
    """
    After reserve and intended BUY notional: ``balance - reserve - n``.

    ``None`` when inputs are insufficient (does not mean infinite headroom).
    """
    if balance is None:
        return None
    r = float(reserve_usd)
    if intent_notional_usd is None:
        return None
    n = float(intent_notional_usd)
    return float(balance) - r - n
