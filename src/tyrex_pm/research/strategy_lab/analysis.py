"""Analysis of S2 bundles: probability quality and selected-trade economics, kept apart.

Probability quality compares model and book probabilities on the *same* evaluation rows
(rows where every compared column exists), sampled at fixed time-to-close checkpoints,
against proxy outcomes. Trade economics summarise what the strategy actually selected
after costs. Window-bootstrap intervals show how little ~45 windows can say.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

import numpy as np
import pandas as pd

CHECKPOINTS_S = (240.0, 180.0, 120.0, 60.0, 30.0)
PROB_COLUMNS = ("endpoint_p_up", "twap_p_up", "book_implied_up")
EPS = 1e-6


def checkpoint_rows(decisions: pd.DataFrame, checkpoints: Iterable[float] = CHECKPOINTS_S):
    """The first evaluation at or after each checkpoint (tau <= checkpoint) per run."""
    frame = decisions.copy()
    for column in (*PROB_COLUMNS, "tau_s"):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[frame["label_type"] == "proxy_outcome"]
    rows = []
    keys = ["config", "scenario_name", "market_id"]
    for _, group in frame.groupby(keys, sort=False):
        group = group.sort_values("at_ns")
        for cp in checkpoints:
            hit = group[(group["tau_s"] <= cp) & group["endpoint_p_up"].notna()]
            if hit.empty:
                continue
            row = hit.iloc[0].to_dict()
            row["checkpoint_s"] = cp
            rows.append(row)
    out = pd.DataFrame(rows)
    if not out.empty:
        out["up_won"] = (out["winner"] == "UP").astype(float)
    return out


def _brier(p: pd.Series, y: pd.Series) -> float:
    return float(((p - y) ** 2).mean())


def _logloss(p: pd.Series, y: pd.Series) -> float:
    q = p.clip(EPS, 1 - EPS)
    return float(-(y * np.log(q) + (1 - y) * np.log(1 - q)).mean())


def probability_quality(rows: pd.DataFrame, columns=PROB_COLUMNS) -> pd.DataFrame:
    """Brier / log loss per config, checkpoint and source on a common row set."""
    out = []
    for (config, cp), group in rows.groupby(["config", "checkpoint_s"]):
        common = group.dropna(subset=list(columns))
        for column in columns:
            out.append(
                {
                    "config": config,
                    "checkpoint_s": cp,
                    "source": column,
                    "n": int(len(common)),
                    "windows": int(common["market_id"].nunique()),
                    "brier": _brier(common[column], common["up_won"]) if len(common) else math.nan,
                    "log_loss": _logloss(common[column], common["up_won"])
                    if len(common)
                    else math.nan,
                }
            )
    return pd.DataFrame(out)


def reliability_table(rows: pd.DataFrame, column: str, bins: int = 5) -> pd.DataFrame:
    data = rows.dropna(subset=[column, "up_won"])
    edges = np.linspace(0, 1, bins + 1)
    grouped = data.groupby(pd.cut(data[column], edges, include_lowest=True), observed=True)
    return pd.DataFrame(
        {
            "mean_predicted": grouped[column].mean(),
            "observed_up": grouped["up_won"].mean(),
            "n": grouped.size(),
        }
    )


def bootstrap_total(values: np.ndarray, *, draws: int = 2000, seed: int = 0) -> tuple[float, float]:
    """95% window-bootstrap interval of the sum (windows resampled with replacement)."""
    if len(values) == 0:
        return math.nan, math.nan
    rng = np.random.default_rng(seed)
    sums = rng.choice(values, size=(draws, len(values)), replace=True).sum(axis=1)
    return float(np.percentile(sums, 2.5)), float(np.percentile(sums, 97.5))


def trade_economics(summaries: pd.DataFrame, by=("config", "scenario_name")) -> pd.DataFrame:
    """Selected-trade results after costs (proxy labels), per configuration and scenario."""
    out = []
    for keys, group in summaries.groupby(list(by)):
        filled = group[group["filled"] == True]  # noqa: E712 - pandas boolean column
        pnl = filled["realized_pnl"].astype(float).fillna(0).to_numpy()
        per_window = group.assign(
            pnl=group["realized_pnl"].astype(float).fillna(0)
        )["pnl"].to_numpy()
        low, high = bootstrap_total(per_window)
        debit = float(filled["debit"].astype(float).sum())
        record = dict(zip(by, keys if isinstance(keys, tuple) else (keys,)))
        record.update(
            windows=int(len(group)),
            entries=int(group["entered"].sum()),
            fills=int(len(filled)),
            fill_rate=float(len(filled) / max(1, group["entered"].sum())),
            wins=int((pnl > 0).sum()),
            total_debit=debit,
            total_pnl=float(pnl.sum()),
            pnl_per_debit=float(pnl.sum() / debit) if debit else math.nan,
            mean_ev_conservative=float(filled["ev_conservative"].astype(float).mean())
            if len(filled)
            else math.nan,
            pnl_ci95_low=low,
            pnl_ci95_high=high,
        )
        out.append(record)
    return pd.DataFrame(out).sort_values(list(by)).reset_index(drop=True)


def parse_config(label: str) -> dict:
    import json

    return json.loads(label)


def flatten_config(frame: pd.DataFrame) -> pd.DataFrame:
    """Expand the JSON ``config`` column into ``section.key`` columns."""
    expanded = []
    for label in frame["config"]:
        flat = {}
        for section, values in parse_config(label).items():
            if isinstance(values, dict):
                for key, value in values.items():
                    flat[f"{section}.{key}"] = value
        expanded.append(flat)
    return pd.concat([frame.reset_index(drop=True), pd.DataFrame(expanded)], axis=1)


def neighbour_scores(
    economics: pd.DataFrame,
    *,
    axes: Iterable[str],
    scenario: str = "friction",
    metric: str = "pnl_per_debit",
    min_fills: int = 5,
    min_neighbourhood: int = 2,
) -> pd.DataFrame:
    """Score = mean metric over a configuration and its one-axis neighbours.

    Rewards regions that behave reasonably rather than a single best historical
    configuration. Configurations with fewer than ``min_fills`` fills are not scored, and
    an isolated configuration (neighbourhood smaller than ``min_neighbourhood``, itself
    included) is not scored either.
    """
    axes = list(axes)
    table = flatten_config(economics[economics["scenario_name"] == scenario])
    for axis in axes:
        table[axis] = table[axis].astype(str)
    eligible = table[table["fills"] >= min_fills]
    rows = []
    for _, row in eligible.iterrows():
        mask = pd.Series(False, index=table.index)
        for axis in axes:
            others = [a for a in axes if a != axis]
            same = pd.Series(True, index=table.index)
            for other in others:
                same &= table[other] == row[other]
            mask |= same
        neighbourhood = table[mask & (table["fills"] >= min_fills)]
        if len(neighbourhood) < min_neighbourhood:
            continue
        rows.append(
            {
                "config": row["config"],
                "fills": int(row["fills"]),
                metric: row[metric],
                "neighbourhood_mean": float(neighbourhood[metric].mean()),
                "neighbourhood_min": float(neighbourhood[metric].min()),
                "neighbours": int(len(neighbourhood)),
            }
        )
    columns = ["config", "fills", metric, "neighbourhood_mean", "neighbourhood_min", "neighbours"]
    frame = pd.DataFrame(rows, columns=columns)
    return frame.sort_values("neighbourhood_mean", ascending=False).reset_index(drop=True)


def select_shortlist(scores: pd.DataFrame, *, k: int = 3) -> list[dict]:
    """Top-k by neighbourhood mean (development data only); fixed before any holdout."""
    return [parse_config(label) for label in scores["config"].head(k)]
