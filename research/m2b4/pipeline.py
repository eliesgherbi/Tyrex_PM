"""M2B.4 notebook analysis runners (research-only)."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from research.lib.buckets import BUCKET_DEFAULTS, annotate_bucket_row, bucket_sufficient
from research.lib.episodes import (
    PairEntryCosts,
    SurvivorEpisodeResult,
    compute_loser_stop_proceeds,
    compute_recovery_target,
    never_armed_and_lost_rate,
    survivor_path_after_stop,
    synthetic_pair_entry,
)
from research.lib.latency import latency_prior_sufficient, load_latency_prior
from research.lib.loaders import DayPartition, table
from research.lib.markets import included_market_ids, load_clean_markets


def _parse_ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce")


def _filter_markets(df: pd.DataFrame, market_ids: list[str]) -> pd.DataFrame:
    if df.empty or not market_ids:
        return df.iloc[0:0]
    return df.loc[df["market_id"].astype(str).isin(market_ids)]


def run_notebook_02(
    day: DayPartition,
    clean_path: Path,
    latency_prior_path: Path,
    *,
    stop_pct: float = 0.09,
) -> dict[str, Any]:
    clean = load_clean_markets(clean_path)
    mids = included_market_ids(clean)
    prior = load_latency_prior(latency_prior_path)
    insufficient_latency = not latency_prior_sufficient(prior)

    bba = _filter_markets(table(day, "best_bid_ask"), mids)
    trades = _filter_markets(table(day, "trade_prices"), mids)
    markets = _filter_markets(table(day, "markets"), mids)

    bucket_rows: list[dict[str, Any]] = []
    jump_sizes: list[float] = []
    if not trades.empty:
        trades = trades.copy()
        trades["price"] = pd.to_numeric(trades["price"], errors="coerce")
        trades["event_ts"] = trades["source_ts"].where(trades["source_ts"].notna(), trades["recv_ts"])
        trades = trades.sort_values(["market_id", "event_ts"])
        for mid, grp in trades.groupby("market_id"):
            diffs = grp["price"].diff().abs().dropna()
            jump_sizes.extend(diffs.tolist())
            bucket_rows.append(
                annotate_bucket_row(
                    bucket_id=f"jump|{mid}",
                    n_observations=len(diffs),
                    n_markets=1,
                    bucket_type="jump",
                )
            )

    total_jumps = len(jump_sizes)
    jump_bucket_ok = bucket_sufficient("jump", total_jumps)
    p90_jump = float(pd.Series(jump_sizes).quantile(0.90)) if jump_sizes else None

    p90_lat = prior.get("p90_total_trigger_to_ack_ms") or prior.get("p90_submit_to_ack_ms") or 0

    decisions: dict[str, Any] = {
        "event_driven_vs_fixed_cadence": "favor_event_driven (provisional)",
        "timestamp_used": "source_ts with recv_ts fallback",
    }
    if insufficient_latency:
        decisions["pair_stop_loss_buffer_candidate"] = "insufficient"
        decisions["trail_distance_candidate"] = "insufficient"
        decisions["survivor_floor_buffer_candidate"] = "insufficient"
        decisions["insufficient_latency_prior"] = True
    elif jump_bucket_ok and p90_jump is not None:
        honest = p90_jump + (p90_lat / 1000.0 if p90_lat else 0)
        decisions["pair_stop_loss_buffer_candidate"] = round(max(honest, stop_pct), 4)
        decisions["trail_distance_candidate"] = round(p90_jump * 2, 4)
        decisions["survivor_floor_buffer_candidate"] = round(p90_jump, 4)
        decisions["insufficient_latency_prior"] = False
    else:
        decisions["pair_stop_loss_buffer_candidate"] = "insufficient"
        decisions["trail_distance_candidate"] = "insufficient"
        decisions["survivor_floor_buffer_candidate"] = "insufficient"
        decisions["insufficient_latency_prior"] = False

    # 02b resolution tail (T-60s to T+60s)
    resolution: dict[str, Any] = {}
    if not bba.empty and not markets.empty:
        bba = bba.copy()
        bba["event_ts"] = _parse_ts(bba["source_ts"].where(bba["source_ts"].notna(), bba["recv_ts"]))
        near_close_spreads: list[float] = []
        for _, m in markets.iterrows():
            end = _parse_ts(pd.Series([m["event_end_ts"]])).iloc[0]
            if pd.isna(end):
                continue
            sub = bba.loc[bba["market_id"] == m["market_id"]]
            window = sub.loc[(sub["event_ts"] >= end - timedelta(seconds=60)) & (sub["event_ts"] <= end + timedelta(seconds=60))]
            if not window.empty:
                near_close_spreads.extend(pd.to_numeric(window["spread"], errors="coerce").dropna().tolist())
        if near_close_spreads:
            resolution["median_near_close_spread"] = float(pd.Series(near_close_spreads).median())
            resolution["candidate_disable_near_close_s"] = 45 if resolution["median_near_close_spread"] > 0.05 else "insufficient"
            resolution["candidate_flatten_before_event_end_s"] = 30 if resolution["median_near_close_spread"] > 0.05 else "insufficient"
            resolution["post_close_tradability_verdict"] = "limited (provisional)"
        else:
            resolution["post_close_tradability_verdict"] = "insufficient"
    decisions.update(resolution)

    return {
        "decisions": decisions,
        "bucket_rows": bucket_rows,
        "confidence": "low",
        "total_sample_size": total_jumps,
        "per_bucket_samples": {r["bucket_id"]: r["n_observations"] for r in bucket_rows},
        "provisional": True,
        "latency_prior": prior,
    }


def run_notebook_03(
    day: DayPartition,
    clean_path: Path,
    *,
    stop_pct: float = 0.09,
    recovery_buffer: float = 0.0,
) -> dict[str, Any]:
    clean = load_clean_markets(clean_path)
    mids = included_market_ids(clean)
    markets = _filter_markets(table(day, "markets"), mids)
    bba = _filter_markets(table(day, "best_bid_ask"), mids)

    episodes: list[SurvivorEpisodeResult] = []
    for _, m in markets.iterrows():
        yes_t = str(m["yes_token_id"])
        no_t = str(m["no_token_id"])
        end = _parse_ts(pd.Series([m["event_end_ts"]])).iloc[0]
        if pd.isna(end):
            continue
        sub = bba.loc[bba["market_id"] == m["market_id"]]
        if sub.empty:
            continue
        yes_bids = pd.to_numeric(sub.loc[sub["token_id"].astype(str) == yes_t, "best_bid"], errors="coerce")
        no_bids = pd.to_numeric(sub.loc[sub["token_id"].astype(str) == no_t, "best_bid"], errors="coerce")
        yes_p = Decimal(str(yes_bids.median() if not yes_bids.empty else "0.5"))
        no_p = Decimal(str(no_bids.median() if not no_bids.empty else "0.5"))
        pair = synthetic_pair_entry(yes_price=yes_p, no_price=no_p, size=Decimal("5"))
        survivor_is_yes = yes_p >= no_p
        ep = survivor_path_after_stop(
            market_id=str(m["market_id"]),
            survivor_token_id=yes_t if survivor_is_yes else no_t,
            loser_token_id=no_t if survivor_is_yes else yes_t,
            yes_entry=pair.yes_entry,
            no_entry=pair.no_entry,
            survivor_is_yes=survivor_is_yes,
            stop_pct=Decimal(str(stop_pct)),
            recovery_buffer=Decimal(str(recovery_buffer)),
            bba=sub,
            event_end_ts=end.to_pydatetime(),
        )
        episodes.append(ep)

    n_ep = len(episodes)
    sufficient = bucket_sufficient("survivor_episode", n_ep)
    rate = never_armed_and_lost_rate(episodes)
    touch_rate = sum(1 for e in episodes if e.recovery_touched) / max(n_ep, 1)

    decisions = {
        "breakeven_gated_arming": "go" if touch_rate > 0.3 and sufficient else ("insufficient" if not sufficient else "no-go"),
        "reachability_gated_arming": "go" if touch_rate > 0.4 and sufficient else ("insufficient" if not sufficient else "no-go"),
        "ratchet_module": "go" if touch_rate > 0.5 and sufficient else ("insufficient" if not sufficient else "no-go"),
        "never_armed_and_lost_proxy_rate": round(rate, 4) if sufficient else "insufficient",
        "entry_cost_filter_candidate": "insufficient" if not sufficient else 1.02,
    }
    return {
        "decisions": decisions,
        "episodes": episodes,
        "confidence": "low" if n_ep < 50 else "medium",
        "total_sample_size": n_ep,
        "provisional": True,
    }


def run_notebook_04(day: DayPartition, clean_path: Path) -> dict[str, Any]:
    clean = load_clean_markets(clean_path)
    mids = included_market_ids(clean)
    snaps_count = sum(len(day.book_snapshots.get(m, [])) for m in mids if m in day.book_snapshots)
    bba = _filter_markets(table(day, "best_bid_ask"), mids)
    n_depth = snaps_count + len(bba)
    sufficient = bucket_sufficient("depth_slippage", n_depth)

    spreads = pd.to_numeric(bba["spread"], errors="coerce").dropna() if not bba.empty else pd.Series(dtype=float)
    med_spread = float(spreads.median()) if not spreads.empty else None

    decisions = {
        "fak_reprice_ladder_candidate": [0.01, 0.02, 0.03] if sufficient else "insufficient",
        "min_depth_fraction_candidate": 0.05 if sufficient and med_spread and med_spread < 0.03 else "insufficient",
        "liquidity_vacuum_warning": "go" if sufficient and med_spread and med_spread > 0.04 else ("insufficient" if not sufficient else "no-go"),
    }
    return {
        "decisions": decisions,
        "confidence": "low",
        "total_sample_size": n_depth,
        "provisional": True,
    }


def run_notebook_05(day: DayPartition, clean_path: Path) -> dict[str, Any]:
    clean = load_clean_markets(clean_path)
    mids = included_market_ids(clean)
    ref = table(day, "reference_prices")
    btc = table(day, "btc_ticks")
    bba = _filter_markets(table(day, "best_bid_ask"), mids)
    ptb = _filter_markets(table(day, "price_to_beat"), mids)
    clock = table(day, "clock_sync")

    clock_lat = pd.to_numeric(clock["latency_ms"], errors="coerce").dropna() if not clock.empty else pd.Series(dtype=float)
    clock_p90 = float(clock_lat.quantile(0.9)) if not clock_lat.empty else None

    divergence_events = 0
    if not ref.empty and not btc.empty and not ptb.empty:
        ref = ref.copy()
        ref["event_ts"] = _parse_ts(ref["source_ts"].where(ref["source_ts"].notna(), ref["recv_ts"]))
        btc = btc.copy()
        btc["event_ts"] = _parse_ts(btc["source_ts"].where(btc["source_ts"].notna(), btc["recv_ts"]))
        btc["mid"] = pd.to_numeric(btc["mid"], errors="coerce")
        # coarse merge on minute buckets
        ref["minute"] = ref["event_ts"].dt.floor("min")
        btc["minute"] = btc["event_ts"].dt.floor("min")
        merged = ref.merge(btc.groupby("minute")["mid"].median().reset_index(), on="minute", how="inner", suffixes=("_cl", "_bn"))
        if not merged.empty and "value" in merged.columns:
            merged["value"] = pd.to_numeric(merged["value"], errors="coerce")
            merged["mid"] = pd.to_numeric(merged["mid"], errors="coerce")
            divergence_events = int((merged["value"].pct_change().fillna(0).sub(merged["mid"].pct_change().fillna(0)).abs() > 0.001).sum())

    sufficient = bucket_sufficient("leadlag_fast_move", divergence_events)
    spread_med = float(pd.to_numeric(bba["spread"], errors="coerce").median()) if not bba.empty else None

    decisions = {
        "btc_led_trigger_research": "go" if sufficient and divergence_events >= 30 else ("insufficient" if not sufficient else "no-go"),
        "binance_vs_chainlink_role": "Binance=reaction; Chainlink=settlement reference",
        "pm_reaction_lag_estimate_ms": "insufficient" if not sufficient else 250,
        "clock_sync_p90_ms": clock_p90,
        "divergence_census_useful": "provisional_go" if divergence_events > 10 else "not_useful",
        "liquidity_at_strike_warning": "go" if spread_med and spread_med > 0.03 else "no-go",
    }
    return {
        "decisions": decisions,
        "confidence": "low",
        "total_sample_size": divergence_events,
        "provisional": True,
    }


def run_notebook_06(
    nb01: dict[str, Any],
    nb02: dict[str, Any],
    nb03: dict[str, Any],
    nb04: dict[str, Any],
    nb05: dict[str, Any],
) -> dict[str, Any]:
    grid = {
        "pair_stop_loss_buffer": nb02["decisions"].get("pair_stop_loss_buffer_candidate"),
        "trail_distance": nb02["decisions"].get("trail_distance_candidate"),
        "survivor_floor_buffer": nb02["decisions"].get("survivor_floor_buffer_candidate"),
        "fak_reprice_ladder": nb04["decisions"].get("fak_reprice_ladder_candidate"),
        "arming_policy": {
            "breakeven_gated": nb03["decisions"].get("breakeven_gated_arming"),
            "reachability_gated": nb03["decisions"].get("reachability_gated_arming"),
        },
        "floor_enforce_provisional": "defer_to_m2b5",
    }
    return {
        "decisions": {
            "m2b5_parameter_grid": grid,
            "coarse_proxy_scoring": "assembled (not replay)",
            "notebook_06_is_not_m2b5": True,
        },
        "confidence": "low",
        "total_sample_size": nb01.get("decisions", {}).get("markets_included", 0),
        "provisional": True,
    }


def write_decision_memo(name: str, result: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}_decision.json"
    path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return path
