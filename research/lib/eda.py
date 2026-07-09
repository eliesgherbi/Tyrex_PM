"""Exploratory EDA helpers (M2B.4-B EX3)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from research.lib.loaders import DayPartition, table
from research.lib.markets import included_market_ids, load_clean_markets


def _parse_ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce")


def _quantiles(series: pd.Series) -> dict[str, float | None]:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return {"p25": None, "p50": None, "p75": None, "p90": None}
    return {
        "p25": float(s.quantile(0.25)),
        "p50": float(s.quantile(0.50)),
        "p75": float(s.quantile(0.75)),
        "p90": float(s.quantile(0.90)),
    }


def build_market_feature_frame(
    day: DayPartition,
    clean_path: str | Any,
) -> pd.DataFrame:
    """One row per included market with coarse EDA features."""
    clean = load_clean_markets(clean_path) if not isinstance(clean_path, pd.DataFrame) else clean_path
    mids = included_market_ids(clean)
    markets = table(day, "markets")
    bba = table(day, "best_bid_ask")
    trades = table(day, "trade_prices")
    ptb = table(day, "price_to_beat")
    btc = table(day, "btc_ticks")

    rows: list[dict[str, Any]] = []
    for mid in mids:
        m = markets.loc[markets["market_id"] == mid]
        if m.empty:
            continue
        m = m.iloc[0]
        sub_bba = bba.loc[bba["market_id"] == mid] if not bba.empty else pd.DataFrame()
        sub_tr = trades.loc[trades["market_id"] == mid] if not trades.empty else pd.DataFrame()
        sub_ptb = ptb.loc[ptb["market_id"] == mid] if not ptb.empty else pd.DataFrame()

        spread_med = None
        top_size_med = None
        quote_rate = None
        if not sub_bba.empty:
            spreads = pd.to_numeric(sub_bba["spread"], errors="coerce").dropna()
            spread_med = float(spreads.median()) if not spreads.empty else None
            bids = pd.to_numeric(sub_bba["best_bid"], errors="coerce").dropna()
            if not bids.empty:
                sub_bba = sub_bba.copy()
                sub_bba["event_ts"] = _parse_ts(sub_bba["source_ts"].where(sub_bba["source_ts"].notna(), sub_bba["recv_ts"]))
                sub_bba = sub_bba.sort_values("event_ts")
                diffs = bids.diff().abs().dropna()
                jump_p90 = float(diffs.quantile(0.90)) if not diffs.empty else None
            else:
                jump_p90 = None
            if not sub_bba.empty and "event_ts" in sub_bba.columns:
                span = (sub_bba["event_ts"].max() - sub_bba["event_ts"].min()).total_seconds()
                quote_rate = len(sub_bba) / max(span, 1.0)
        else:
            jump_p90 = None

        trade_intensity = len(sub_tr) if not sub_tr.empty else 0
        gap_rate = float(clean.loc[clean["market_id"] == mid, "gap_rate"].iloc[0]) if mid in clean["market_id"].values else None

        dist_beat = None
        direction = None
        if not sub_ptb.empty:
            row = sub_ptb.iloc[0]
            dist_beat = pd.to_numeric(row.get("price_to_beat"), errors="coerce")
            direction = row.get("direction_vs_price_to_beat")

        btc_vol = None
        if not btc.empty and not sub_bba.empty:
            btc = btc.copy()
            btc["event_ts"] = _parse_ts(btc["source_ts"].where(btc["source_ts"].notna(), btc["recv_ts"]))
            start = _parse_ts(pd.Series([m["event_start_ts"]])).iloc[0]
            end = _parse_ts(pd.Series([m["event_end_ts"]])).iloc[0]
            if pd.notna(start) and pd.notna(end):
                w = btc.loc[(btc["event_ts"] >= start) & (btc["event_ts"] <= end)]
                mids_btc = pd.to_numeric(w["mid"], errors="coerce").dropna()
                if len(mids_btc) > 2:
                    btc_vol = float(mids_btc.pct_change().std())

        rows.append(
            {
                "market_id": mid,
                "spread_median": spread_med,
                "top_of_book_size_median": top_size_med,
                "quote_rate_per_s": quote_rate,
                "trade_intensity": trade_intensity,
                "jump_p90": jump_p90,
                "abs_distance_to_beat": abs(float(dist_beat)) if pd.notna(dist_beat) else None,
                "btc_short_vol": btc_vol,
                "gap_rate_diagnostic_only": gap_rate,
                "direction_outcome": direction,
                "include_for_analysis": True,
            }
        )
    return pd.DataFrame(rows)


def build_bucket_feature_frame(feature_frame: pd.DataFrame, *, bucket_col: str = "tte_bucket") -> pd.DataFrame:
    """Aggregate market feature frame by bucket column if present."""
    if feature_frame.empty or bucket_col not in feature_frame.columns:
        return pd.DataFrame()
    numeric = feature_frame.select_dtypes(include="number").columns.tolist()
    agg = feature_frame.groupby(bucket_col)[numeric].median().reset_index()
    counts = feature_frame.groupby(bucket_col).size().reset_index(name="n_markets")
    return agg.merge(counts, on=bucket_col)
