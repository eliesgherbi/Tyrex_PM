"""Plotting helpers for M2B.4 / M2B.4-B notebooks."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from research.lib.loaders import DayPartition, table


def try_import_matplotlib():
    try:
        import matplotlib.pyplot as plt

        return plt
    except ImportError:
        return None


def save_figure(path: str | Path, *, title: str = "") -> bool:
    plt = try_import_matplotlib()
    if plt is None:
        return False
    if title:
        plt.suptitle(title)
    plt.tight_layout()
    plt.savefig(path)
    plt.close()
    return True


def histogram(series: Any, *, bins: int = 30, title: str = "", path: str | Path | None = None) -> bool:
    plt = try_import_matplotlib()
    if plt is None:
        return False
    plt.figure(figsize=(8, 4))
    data = series.dropna() if hasattr(series, "dropna") else series
    plt.hist(data, bins=bins)
    if title:
        plt.title(title)
    plt.tight_layout()
    if path:
        plt.savefig(path)
        plt.close()
        return True
    return True


def _parse_ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce")


def plot_market_story(
    day: DayPartition,
    market_id: str,
    *,
    out_path: str | Path | None = None,
    title: str | None = None,
) -> bool:
    """Multi-panel market narrative: Chainlink, Binance, PM quotes, trades, event lines."""
    plt = try_import_matplotlib()
    if plt is None:
        return False

    markets = table(day, "markets")
    m = markets.loc[markets["market_id"] == market_id]
    if m.empty:
        return False
    m = m.iloc[0]
    yes_t = str(m["yes_token_id"])
    no_t = str(m["no_token_id"])
    start = _parse_ts(pd.Series([m["event_start_ts"]])).iloc[0]
    end = _parse_ts(pd.Series([m["event_end_ts"]])).iloc[0]
    ptb_val = pd.to_numeric(m.get("price_to_beat"), errors="coerce")

    ref = table(day, "reference_prices")
    btc = table(day, "btc_ticks")
    bba = table(day, "best_bid_ask")
    trades = table(day, "trade_prices")
    ptb = table(day, "price_to_beat")

    fig, axes = plt.subplots(4, 1, figsize=(12, 10), sharex=True)
    ax_cl, ax_bn, ax_pm, ax_tr = axes

    # Chainlink
    if not ref.empty:
        ref = ref.copy()
        ref["event_ts"] = _parse_ts(ref["source_ts"].where(ref["source_ts"].notna(), ref["recv_ts"]))
        sub = ref.loc[ref["feed"].astype(str).str.contains("chainlink", case=False, na=False)]
        if start is not pd.NaT and end is not pd.NaT:
            sub = sub.loc[(sub["event_ts"] >= start - timedelta(minutes=2)) & (sub["event_ts"] <= end + timedelta(minutes=2))]
        if not sub.empty:
            ax_cl.plot(sub["event_ts"], pd.to_numeric(sub["value"], errors="coerce"), label="Chainlink", color="tab:blue")
    if pd.notna(ptb_val):
        ax_cl.axhline(float(ptb_val), color="tab:orange", linestyle="--", label="price_to_beat")
    ax_cl.set_ylabel("Chainlink USD")
    ax_cl.legend(loc="upper left", fontsize=8)
    ax_cl.set_title(title or f"Market story: {market_id}")

    # Binance
    if not btc.empty:
        btc = btc.copy()
        btc["event_ts"] = _parse_ts(btc["source_ts"].where(btc["source_ts"].notna(), btc["recv_ts"]))
        sub = btc
        if start is not pd.NaT and end is not pd.NaT:
            sub = sub.loc[(sub["event_ts"] >= start - timedelta(minutes=2)) & (sub["event_ts"] <= end + timedelta(minutes=2))]
        if not sub.empty:
            ax_bn.plot(sub["event_ts"], pd.to_numeric(sub["mid"], errors="coerce"), label="Binance mid", color="tab:green", alpha=0.8)
    ax_bn.set_ylabel("Binance USD")
    ax_bn.legend(loc="upper left", fontsize=8)

    # PM mids
    if not bba.empty:
        bba = bba.copy()
        bba["event_ts"] = _parse_ts(bba["source_ts"].where(bba["source_ts"].notna(), bba["recv_ts"]))
        sub = bba.loc[bba["market_id"] == market_id]
        if start is not pd.NaT and end is not pd.NaT:
            sub = sub.loc[(sub["event_ts"] >= start - timedelta(minutes=2)) & (sub["event_ts"] <= end + timedelta(minutes=2))]
        for tok, label, color in ((yes_t, "UP mid", "tab:purple"), (no_t, "DOWN mid", "tab:red")):
            tsub = sub.loc[sub["token_id"].astype(str) == tok]
            if not tsub.empty:
                mid = (pd.to_numeric(tsub["best_bid"], errors="coerce") + pd.to_numeric(tsub["best_ask"], errors="coerce")) / 2
                ax_pm.plot(tsub["event_ts"], mid, label=label, color=color, alpha=0.8)
                spread = pd.to_numeric(tsub["spread"], errors="coerce")
                ax_pm.fill_between(tsub["event_ts"], mid - spread / 2, mid + spread / 2, alpha=0.1, color=color)
    ax_pm.set_ylabel("PM prob")
    ax_pm.legend(loc="upper left", fontsize=8)

    # Trades
    if not trades.empty:
        trades = trades.copy()
        trades["event_ts"] = _parse_ts(trades["source_ts"].where(trades["source_ts"].notna(), trades["recv_ts"]))
        sub = trades.loc[trades["market_id"] == market_id]
        if start is not pd.NaT and end is not pd.NaT:
            sub = sub.loc[(sub["event_ts"] >= start - timedelta(minutes=2)) & (sub["event_ts"] <= end + timedelta(minutes=2))]
        if not sub.empty:
            ax_tr.scatter(
                sub["event_ts"],
                pd.to_numeric(sub["price"], errors="coerce"),
                c=sub["token_id"].astype(str).map({yes_t: "tab:purple", no_t: "tab:red"}),
                s=12,
                alpha=0.7,
            )
    ax_tr.set_ylabel("Trade px")
    ax_tr.set_xlabel("UTC time")

    for ax in axes:
        if start is not pd.NaT:
            ax.axvline(start, color="gray", linestyle=":", linewidth=1, label="event_start")
        if end is not pd.NaT:
            ax.axvline(end, color="black", linestyle=":", linewidth=1, label="event_end")

    # PTB crossings from reference vs ptb table
    if not ptb.empty:
        prow = ptb.loc[ptb["market_id"] == market_id]
        if not prow.empty and pd.notna(ptb_val) and not ref.empty:
            crossings = ref.copy()
            crossings["event_ts"] = _parse_ts(crossings["source_ts"].where(crossings["source_ts"].notna(), crossings["recv_ts"]))
            crossings["value"] = pd.to_numeric(crossings["value"], errors="coerce")
            crossings = crossings.sort_values("event_ts")
            diff = crossings["value"] - float(ptb_val)
            sign = diff.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
            flips = sign.diff().fillna(0).ne(0)
            for ts in crossings.loc[flips, "event_ts"].head(3):
                ax_cl.axvline(ts, color="tab:orange", alpha=0.4, linewidth=0.8)

    plt.tight_layout()
    if out_path:
        plt.savefig(out_path)
        plt.close()
        return True
    return True


def plot_feed_offset_diagnostic(day: DayPartition, *, out_path: str | Path | None = None) -> bool:
    """recv_ts vs source_ts offset by feed — shows clock/alignment issues."""
    plt = try_import_matplotlib()
    if plt is None:
        return False
    frames: list[pd.DataFrame] = []
    for name, feed_col in (
        ("reference_prices", "feed"),
        ("btc_ticks", "source"),
        ("best_bid_ask", None),
        ("trade_prices", None),
    ):
        df = table(day, name)
        if df.empty:
            continue
        df = df.copy()
        df["recv_ts"] = _parse_ts(df["recv_ts"])
        df["source_ts"] = _parse_ts(df["source_ts"])
        df["offset_ms"] = (df["recv_ts"] - df["source_ts"]).dt.total_seconds() * 1000
        df["feed"] = df[feed_col].astype(str) if feed_col and feed_col in df.columns else name
        frames.append(df[["feed", "offset_ms"]].dropna())
    if not frames:
        return False
    all_df = pd.concat(frames, ignore_index=True)
    plt.figure(figsize=(10, 4))
    for feed, grp in all_df.groupby("feed"):
        plt.hist(grp["offset_ms"], bins=40, alpha=0.5, label=str(feed)[:20])
    plt.xlabel("recv_ts - source_ts (ms)")
    plt.ylabel("count")
    plt.title("Feed timestamp offset diagnostic (exploratory)")
    plt.legend(fontsize=7)
    plt.tight_layout()
    if out_path:
        plt.savefig(out_path)
        plt.close()
    return True
