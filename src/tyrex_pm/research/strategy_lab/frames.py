"""Result bundles → pandas frames (numbers converted, times in seconds-to-close)."""

from __future__ import annotations

from typing import Any

import pandas as pd

NS = 1_000_000_000
NUMERIC = (
    "tau_s",
    "binance_raw",
    "model_spot",
    "chainlink_raw",
    "sealed_k",
    "reference_price",
    "aligned_reference",
    "trading_reference_raw",
    "k",
    "sigma",
    "sigma_raw",
    "z",
    "p_up_point",
    "p_up_low",
    "p_up_high",
    "book_implied_up",
    "up_bid",
    "up_ask",
    "down_bid",
    "down_ask",
    "up_first_level_edge",
    "down_first_level_edge",
    "up_ev_conservative",
    "down_ev_conservative",
    "endpoint_p_up",
    "endpoint_p_up_low",
    "endpoint_p_up_high",
    "twap_p_up",
    "twap_p_up_low",
    "twap_p_up_high",
    "twap_basis_usd",
    "twap_mean",
    "twap_sd",
)


def decisions(result: dict) -> pd.DataFrame:
    frame = pd.DataFrame(result["decisions"])
    if frame.empty:
        return frame
    for column in NUMERIC:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    end = result["window"]["window_end_ns"]
    frame["t_minus_s"] = (end - frame["at_ns"]) / NS
    return frame


def summary(result: dict) -> dict[str, Any]:
    econ = result["economics"]
    entry = next((d for d in result["decisions"] if d.get("action") == "ENTER"), None)
    return {
        "slug": result["window"]["slug"],
        "scenario": result["scenario_name"],
        "winner": econ["winner"],
        "label_type": econ["label_type"],
        "entered": econ["entered"],
        "filled": econ["filled"],
        "side": econ["side"] or (entry or {}).get("chosen_side"),
        "entry_tau_s": None if entry is None else entry.get("tau_s"),
        "p_conservative": econ["p_conservative_at_decision"],
        "book_implied_up_at_entry": None if entry is None else entry.get("book_implied_up"),
        "limit_price": _f(econ["limit_price"]),
        "vwap": _f(econ["vwap"]),
        "debit": _f(econ["debit"]),
        "fill_ratio": _f(econ["fill_ratio"]),
        "ev_conservative": _f(econ["ev_conservative_at_decision"]),
        "ev_point": _f(econ["ev_point_at_decision"]),
        "realized_pnl": _f(econ["realized_pnl"]),
        # v3 execution and lifecycle counters (absent in v2 bundles).
        "entry_attempts": econ.get("entry_attempts"),
        "entry_not_sent": econ.get("entry_not_sent"),
        "entry_zero_fill": econ.get("entry_zero_fill"),
        "entry_posts": econ.get("entry_posts"),
        "exit_attempts": econ.get("exit_attempts"),
        "filled_episodes": econ.get("filled_episodes"),
        "sold_shares": _f(econ.get("sold_shares")),
        "exit_proceeds_net": _f(econ.get("exit_proceeds_net")),
        "final_net_shares": _f(econ.get("final_net_shares")),
        "spend_amount": _f(econ.get("spend_amount")),
        "limitations": ",".join(result["engine"]["limitations"]),
        "digest": result.get("decision_digest"),
    }


def _f(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def reasons(result: dict) -> pd.Series:
    frame = decisions(result)
    if frame.empty:
        return pd.Series(dtype=int)
    return frame.groupby(["action", "reason"]).size().sort_values(ascending=False)
