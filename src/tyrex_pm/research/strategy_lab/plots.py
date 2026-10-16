"""Window views for strategy notebooks (matplotlib, static).

Small multiples on one shared time axis (seconds to window close), one y-scale per
panel, fixed categorical slots: blue, orange, aqua (validated together), neutral grays
for reference lines. Identity is carried by legend and direct labels, never by color
alone.
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import pandas as pd

from .frames import decisions

BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def _style(ax, title: str, ylabel: str) -> None:
    ax.set_title(title, loc="left", fontsize=10, color=INK)
    ax.set_ylabel(ylabel, fontsize=9, color=MUTED)
    ax.grid(True, color=GRID, linewidth=0.6)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=8)


def _entry(frame: pd.DataFrame) -> pd.Series | None:
    rows = frame[frame["action"] == "ENTER"]
    return None if rows.empty else rows.iloc[0]


def plot_window(result: dict, *, title: str | None = None):
    """Four panels: prices, probabilities, books, decision reasons."""
    frame = decisions(result)
    if frame.empty:
        raise ValueError("result has no decisions")
    fig, axes = plt.subplots(4, 1, figsize=(11, 11), sharex=True,
                             gridspec_kw={"height_ratios": [3, 3, 3, 2]})
    window = result["window"]
    fig.suptitle(
        title or f"{window['slug']} · scenario={result['scenario_name']} · "
        f"label={result['label']['label_type']} winner={result['label']['winner']}",
        x=0.01, ha="left", fontsize=11, color=INK,
    )
    x = frame["t_minus_s"]
    entry = _entry(frame)

    ax = axes[0]
    if "binance_raw" in frame:
        ax.plot(x, frame["binance_raw"], color=BLUE, lw=1.4, label="Binance BTCUSDT (raw)")
    if "chainlink_raw" in frame:
        ax.plot(x, frame["chainlink_raw"], color=ORANGE, lw=1.4,
                label="Chainlink TWAP-60 (replayed series)")
    if "sealed_k" in frame and frame["sealed_k"].notna().any():
        k = frame["sealed_k"].dropna().iloc[0]
        ax.axhline(k, color=MUTED, lw=1, ls="--")
        ax.annotate(f"K (PROVISIONAL) {k:,.2f}", (x.max(), k), fontsize=8, color=MUTED,
                    va="bottom")
    _style(ax, "Reference prices and threshold", "USD")
    ax.legend(fontsize=8, frameon=False, loc="upper left")

    ax = axes[1]
    model = frame.dropna(subset=["p_up_point"]) if "p_up_point" in frame else frame.iloc[0:0]
    if not model.empty:
        ax.fill_between(model["t_minus_s"], model["p_up_low"], model["p_up_high"],
                        color=BLUE, alpha=0.15, linewidth=0, label="model band (conservative)")
        ax.plot(model["t_minus_s"], model["p_up_point"], color=BLUE, lw=1.6,
                label="model P(Up)")
    if "book_implied_up" in frame:
        ax.plot(x, frame["book_implied_up"], color=ORANGE, lw=1.4, label="book mid P(Up)")
    ax.set_ylim(0, 1)
    _style(ax, "Probability of Up: model vs book", "probability")
    ax.legend(fontsize=8, frameon=False, loc="upper left")

    ax = axes[2]
    for side, color in (("up", BLUE), ("down", ORANGE)):
        if f"{side}_ask" in frame:
            ax.plot(x, frame[f"{side}_ask"], color=color, lw=1.4, label=f"{side.title()} ask")
            ax.plot(x, frame[f"{side}_bid"], color=color, lw=0.9, ls=":",
                    label=f"{side.title()} bid")
    for order in result["orders"]:
        t = (window["window_end_ns"] - order["decided_at_ns"]) / 1e9
        color = BLUE if order["side"] == "UP" else ORANGE
        ax.scatter([t], [float(order["limit_price"])], s=64, color=color, edgecolor="white",
                   linewidth=2, zorder=5)
        action = order.get("action", "BUY")
        bound = "≤" if action == "BUY" else "≥"
        ax.annotate(f"{action} {order['side']} {order['quantity']} @{bound}"
                    f"{float(order['limit_price']):.3f}",
                    (t, float(order["limit_price"])), xytext=(6, 6), textcoords="offset points",
                    fontsize=8, color=INK)
    ax.set_ylim(0, 1)
    _style(ax, "Outcome books (executable prices)", "USDC / share")
    ax.legend(fontsize=8, frameon=False, loc="upper left", ncol=2)

    ax = axes[3]
    codes = frame["reason"].fillna("").astype(str).str.slice(0, 40)
    order_of = {c: i for i, c in enumerate(dict.fromkeys(codes))}
    ax.scatter(x, codes.map(order_of), s=8, color=AQUA)
    ax.set_yticks(list(order_of.values()), list(order_of.keys()), fontsize=7)
    _style(ax, "Decision reason per evaluation", "")
    ax.set_xlabel("seconds to window close (left = earlier)", fontsize=9, color=MUTED)

    for a in axes:
        a.axvline(300, color=GRID, lw=1)
        a.axvline(0, color=GRID, lw=1)
        if entry is not None:
            a.axvline(entry["t_minus_s"], color=AQUA, lw=1, ls="--")
    axes[0].invert_xaxis()
    fig.tight_layout()
    return fig


def plot_reliability(table: pd.DataFrame, *, bins: int = 10, title: str = ""):
    """Calibration of model and book P(Up) against outcomes (one row per decision)."""
    fig, ax = plt.subplots(figsize=(5.5, 5))
    edges = [i / bins for i in range(bins + 1)]
    for column, color, name in (("p_up_point", BLUE, "model"), ("book_implied_up", ORANGE,
                                                                 "book mid")):
        data = table.dropna(subset=[column, "up_won"])
        if data.empty:
            continue
        grouped = data.groupby(pd.cut(data[column], edges, include_lowest=True), observed=True)
        ax.plot(grouped[column].mean(), grouped["up_won"].mean(), marker="o", ms=5,
                color=color, lw=1.6, label=f"{name} (n={len(data)})")
    ax.plot([0, 1], [0, 1], color=MUTED, lw=1, ls="--", label="perfect calibration")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    _style(ax, title or "Reliability: predicted vs observed P(Up)", "observed frequency")
    ax.set_xlabel("predicted P(Up)", fontsize=9, color=MUTED)
    ax.legend(fontsize=8, frameon=False, loc="upper left")
    fig.tight_layout()
    return fig
