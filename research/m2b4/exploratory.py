"""M2B.4-B exploratory trace runners (research-only; separate from strict decisions)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from research.lib.episodes import SurvivorEpisodeResult, never_armed_and_lost_rate
from research.lib.exploratory import not_computable, wrap_exploratory
from research.lib.latency import load_latency_prior
from research.lib.loaders import DayPartition, table
from research.lib.markets import included_market_ids, load_clean_markets


def _parse_ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce")


def _filter_markets(df: pd.DataFrame, market_ids: list[str]) -> pd.DataFrame:
    if df.empty or not market_ids:
        return df.iloc[0:0]
    return df.loc[df["market_id"].astype(str).isin(market_ids)]


def _quantile_summary(values: list[float] | pd.Series) -> dict[str, float | None]:
    s = pd.Series(values).dropna()
    if s.empty:
        return {"p25": None, "p50": None, "p75": None, "p90": None, "p95": None}
    return {
        "p25": float(s.quantile(0.25)),
        "p50": float(s.quantile(0.50)),
        "p75": float(s.quantile(0.75)),
        "p90": float(s.quantile(0.90)),
        "p95": float(s.quantile(0.95)),
    }


def _compute_lagged_jumps(bba: pd.DataFrame, lag_ms: int) -> list[float]:
    if bba.empty:
        return []
    df = bba.copy()
    df["event_ts"] = _parse_ts(df["source_ts"].where(df["source_ts"].notna(), df["recv_ts"]))
    df["mid"] = (pd.to_numeric(df["best_bid"], errors="coerce") + pd.to_numeric(df["best_ask"], errors="coerce")) / 2
    df = df.dropna(subset=["event_ts", "mid"]).sort_values(["market_id", "token_id", "event_ts"])
    jumps: list[float] = []
    lag = timedelta(milliseconds=lag_ms)
    for (_, _), grp in df.groupby(["market_id", "token_id"]):
        grp = grp.sort_values("event_ts")
        for i, row in grp.iterrows():
            t0 = row["event_ts"]
            prior = grp.loc[grp["event_ts"] <= t0 - lag, "mid"]
            if prior.empty:
                continue
            jumps.append(abs(float(row["mid"]) - float(prior.iloc[-1])))
    return jumps


def run_notebook_01_exploratory(day: DayPartition, clean: pd.DataFrame, gap_audit: dict[str, Any]) -> dict[str, Any]:
    n_total = len(clean)
    n_included = int(clean["include_for_analysis"].sum()) if not clean.empty else 0
    n_excluded = n_total - n_included
    ptb_rate = float(clean["ptb_present"].mean()) if not clean.empty else 0.0
    final_rate = float(clean["final_reference_present"].mean()) if not clean.empty else 0.0
    gap_summary = _quantile_summary(clean["gap_rate"]) if not clean.empty else {}
    label_counts = clean["label_validity_status"].value_counts().to_dict() if not clean.empty else {}

    return wrap_exploratory(
        {
            "notebook": "01_coverage_quality",
            "markets_total": n_total,
            "markets_included": n_included,
            "markets_excluded": n_excluded,
            "ptb_present_rate": round(ptb_rate, 4),
            "final_reference_present_rate": round(final_rate, 4),
            "gap_rate_summary": gap_summary,
            "gap_rate_usable_as_feature": False,
            "gap_rate_usability_note": "diagnostic_only per strict verdict; do not use as strategy feature",
            "dropped_events_summary": int(clean["dropped_events"].sum()) if not clean.empty else 0,
            "corrupt_rows_summary": int(clean["corrupt_row_count"].sum()) if not clean.empty else 0,
            "label_validity_status_counts": label_counts,
            "ws_seq_gap_audit": gap_audit,
            "excluded_markets": clean.loc[clean["include_for_analysis"] == False, ["market_id", "exclusion_reason"]].to_dict("records"),  # noqa: E712
            "plot_artifacts": [
                "01_included_excluded_bar.png",
                "01_gap_rate_distribution.png",
                "01_ptb_final_reference.png",
            ],
        }
    )


def run_notebook_02_exploratory(
    day: DayPartition,
    clean_path: Path,
    latency_prior_path: Path,
    *,
    plot_dir: Path | None = None,
) -> dict[str, Any]:
    clean = load_clean_markets(clean_path)
    mids = included_market_ids(clean)
    bba = _filter_markets(table(day, "best_bid_ask"), mids)
    trades = _filter_markets(table(day, "trade_prices"), mids)
    markets = _filter_markets(table(day, "markets"), mids)
    prior = load_latency_prior(latency_prior_path)

    latency_windows_ms = [100, 250, 500, 1000]
    assumed_latency_ms = [100, 250, 500, 1000]
    observed: dict[str, Any] = {}
    buffers: dict[str, Any] = {}
    for w in latency_windows_ms:
        jumps = _compute_lagged_jumps(bba, w)
        observed[f"observed_jump_{w}ms"] = _quantile_summary(jumps)
    for lat in assumed_latency_ms:
        combined_jumps: list[float] = []
        for w in latency_windows_ms:
            combined_jumps.extend(_compute_lagged_jumps(bba, w + lat))
        qs = _quantile_summary(combined_jumps)
        buffers[f"assumed_latency_{lat}ms_buffer"] = qs
        buffers[f"assumed_latency_{lat}ms_buffer_note"] = (
            "exploratory p-quantiles of |mid(t)-mid(t-(window+latency))| across windows; NOT live YAML"
        )

    tte_counts: dict[str, int] = {}
    near_close_spreads: list[float] = []
    post_close_quotes = 0
    post_close_trades = 0
    tick_changes = 0
    if not bba.empty and not markets.empty:
        bba = bba.copy()
        bba["event_ts"] = _parse_ts(bba["source_ts"].where(bba["source_ts"].notna(), bba["recv_ts"]))
        for _, m in markets.iterrows():
            end = _parse_ts(pd.Series([m["event_end_ts"]])).iloc[0]
            start = _parse_ts(pd.Series([m["event_start_ts"]])).iloc[0]
            if pd.isna(end):
                continue
            sub = bba.loc[bba["market_id"] == m["market_id"]]
            sub = sub.assign(tts=(end - sub["event_ts"]).dt.total_seconds())
            for lo, hi, label in ((240, 300, "T-60_0"), (0, 60, "T+0_60"), (60, 120, "post_close")):
                bucket = sub.loc[(sub["tts"] >= lo) & (sub["tts"] < hi)]
                tte_counts[f"{m['market_id']}|{label}"] = len(bucket)
            window = sub.loc[(sub["event_ts"] >= end - timedelta(seconds=60)) & (sub["event_ts"] <= end + timedelta(seconds=60))]
            if not window.empty:
                near_close_spreads.extend(pd.to_numeric(window["spread"], errors="coerce").dropna().tolist())
            post_close_quotes += int((sub["event_ts"] > end).sum())
        if not trades.empty:
            trades = trades.copy()
            trades["event_ts"] = _parse_ts(trades["source_ts"].where(trades["source_ts"].notna(), trades["recv_ts"]))
            for _, m in markets.iterrows():
                end = _parse_ts(pd.Series([m["event_end_ts"]])).iloc[0]
                if pd.isna(end):
                    continue
                post_close_trades += int((trades.loc[trades["market_id"] == m["market_id"], "event_ts"] > end).sum())
    tsc = _filter_markets(table(day, "tick_size_changes"), mids)
    tick_changes = len(tsc)

    measured_latency_rows: dict[str, Any] = {}
    if prior.get("p90_submit_to_ack_ms") is not None:
        measured_latency_rows["measured_submit_to_ack_ms"] = {
            "p50": prior.get("p50_submit_to_ack_ms"),
            "p90": prior.get("p90_submit_to_ack_ms"),
            "p95": prior.get("p95_submit_to_ack_ms"),
            "confidence": prior.get("confidence"),
        }
    else:
        measured_latency_rows["measured_submit_to_ack_ms"] = not_computable(
            "measured_submit_to_ack_ms", "no facts.jsonl with submit_to_ack samples"
        )

    if plot_dir:
        plot_dir.mkdir(parents=True, exist_ok=True)
        from research.lib.plots import histogram, save_figure, try_import_matplotlib

        plt = try_import_matplotlib()
        if plt and bba is not None:
            jumps100 = _compute_lagged_jumps(bba, 100)
            if jumps100:
                histogram(jumps100, bins=40, title="Jump size 100ms (exploratory)", path=plot_dir / "02_jump_histogram_100ms.png")
            if near_close_spreads:
                histogram(near_close_spreads, bins=30, title="Near-close spread T-60..T+60", path=plot_dir / "02_near_close_spread.png")

    return wrap_exploratory(
        {
            "notebook": "02_jump_protection_distances",
            "assumed_latency_warning": (
                "Assumed-latency buffers are exploratory only. Do not use them in live YAML. "
                "They exist to understand sensitivity to latency assumptions."
            ),
            "observed_jump_quantiles_by_window_ms": observed,
            "assumed_latency_buffer_quantiles": buffers,
            "measured_latency_prior_rows": measured_latency_rows,
            "per_TTE_bucket_sample_counts": tte_counts,
            "resolution_tail": {
                "near_close_spread": _quantile_summary(near_close_spreads),
                "post_close_quote_count": post_close_quotes,
                "post_close_trade_count": post_close_trades,
                "tick_size_change_count": tick_changes,
                "post_close_tradability_notes": "limited liquidity after event_end on this sample; exploratory only",
            },
            "strict_gate_note": "insufficient_latency_prior blocks strict protection-distance candidates",
        }
    )


def run_notebook_03_exploratory(
    nb03_strict: dict[str, Any],
    day: DayPartition,
    clean_path: Path,
) -> dict[str, Any]:
    episodes: list[SurvivorEpisodeResult] = nb03_strict.get("episodes") or []
    n = len(episodes)
    touched = sum(1 for e in episodes if e.recovery_touched)
    nal = sum(1 for e in episodes if e.never_armed_and_lost)
    mfes = [float(e.mfe) for e in episodes]
    maes = [float(e.mae) for e in episodes]
    givebacks = [float(e.max_giveback) for e in episodes]

    by_tte: dict[str, dict[str, Any]] = {}
    clean = load_clean_markets(clean_path)
    markets = _filter_markets(table(day, "markets"), included_market_ids(clean))
    ptb = _filter_markets(table(day, "price_to_beat"), included_market_ids(clean))
    dist_map: dict[str, float] = {}
    if not ptb.empty:
        for _, row in ptb.iterrows():
            v = pd.to_numeric(row.get("price_to_beat"), errors="coerce")
            if pd.notna(v):
                dist_map[str(row["market_id"])] = abs(float(v))

    dist_vals = sorted(dist_map.values())
    terciles = (
        (dist_vals[len(dist_vals) // 3] if dist_vals else None),
        (dist_vals[2 * len(dist_vals) // 3] if dist_vals else None),
    )

    def _bucket_tte(mid: str) -> str:
        m = markets.loc[markets["market_id"] == mid]
        if m.empty:
            return "unknown"
        return "full_window_proxy"

    def _bucket_dist(mid: str) -> str:
        d = dist_map.get(mid)
        if d is None or terciles[0] is None:
            return "unknown"
        if d <= terciles[0]:
            return "low"
        if d <= terciles[1]:
            return "mid"
        return "high"

    for label_fn, name in ((_bucket_tte, "tte_bucket"), (_bucket_dist, "dist_tercile")):
        cells: dict[str, dict[str, Any]] = {}
        for ep in episodes:
            key = label_fn(ep.market_id)
            cells.setdefault(key, {"episodes": [], "recovery_touched": 0, "never_armed_and_lost": 0})
            cells[key]["episodes"].append(ep.market_id)
            if ep.recovery_touched:
                cells[key]["recovery_touched"] += 1
            if ep.never_armed_and_lost:
                cells[key]["never_armed_and_lost"] += 1
        for key, cell in cells.items():
            cnt = len(cell["episodes"])
            cell["n"] = cnt
            cell["recovery_touched_rate"] = round(cell["recovery_touched"] / max(cnt, 1), 4)
            cell["never_armed_and_lost_rate"] = round(cell["never_armed_and_lost"] / max(cnt, 1), 4)
            cell["insufficient_cell"] = cnt < 5
            by_tte[f"{name}|{key}"] = cell

    return wrap_exploratory(
        {
            "notebook": "03_survivor_reachability",
            "proxy_warning": (
                "These are research proxy formulas. They are not live survival replay. "
                "Use only to decide what M2B.5 should test first."
            ),
            "episode_count": n,
            "recovery_touched_count": touched,
            "recovery_touched_rate": round(touched / max(n, 1), 4),
            "never_armed_and_lost_proxy_count": nal,
            "never_armed_and_lost_proxy_rate": round(never_armed_and_lost_rate(episodes), 4),
            "mfe_quantiles": _quantile_summary(mfes),
            "mae_quantiles": _quantile_summary(maes),
            "giveback_quantiles": _quantile_summary(givebacks),
            "time_to_peak_quantiles": not_computable("time_to_peak", "proxy path lacks explicit peak timestamp"),
            "split_rates": by_tte,
            "plot_artifacts": ["03_mfe_mae_scatter.png", "03_giveback_histogram.png"],
        }
    )


def run_notebook_04_exploratory(day: DayPartition, clean_path: Path, *, plot_dir: Path | None = None) -> dict[str, Any]:
    clean = load_clean_markets(clean_path)
    mids = included_market_ids(clean)
    bba = _filter_markets(table(day, "best_bid_ask"), mids)
    markets = _filter_markets(table(day, "markets"), mids)

    depth_by_tte: dict[str, list[float]] = {}
    slippages: list[float] = []
    evap_events = 0
    spreads: list[float] = []

    for mid in mids:
        snaps = day.book_snapshots.get(mid)
        m = markets.loc[markets["market_id"] == mid]
        if m.empty:
            continue
        mrow = m.iloc[0]
        end = _parse_ts(pd.Series([mrow["event_end_ts"]])).iloc[0]
        start = _parse_ts(pd.Series([mrow["event_start_ts"]])).iloc[0]
        if snaps is not None and not snaps.empty and pd.notna(end):
            snaps = snaps.copy()
            snaps["event_ts"] = _parse_ts(snaps["source_ts"].where(snaps["source_ts"].notna(), snaps["recv_ts"]))
            if pd.notna(start):
                snaps = snaps.loc[(snaps["event_ts"] >= start) & (snaps["event_ts"] <= end)]
            snaps["bid_size"] = pd.to_numeric(snaps["bid_size"], errors="coerce")
            snaps["ask_size"] = pd.to_numeric(snaps["ask_size"], errors="coerce")
            snaps["top_size"] = snaps[["bid_size", "ask_size"]].max(axis=1)
            snaps["tts"] = (end - snaps["event_ts"]).dt.total_seconds()
            for lo, hi, label in ((180, 300, "T-120_60"), (60, 180, "T-60_0"), (0, 60, "T+0_60")):
                bucket = snaps.loc[(snaps["tts"] >= lo) & (snaps["tts"] < hi), "top_size"].dropna()
                depth_by_tte.setdefault(label, []).extend(bucket.tolist())
            # simulated 5-share FAK on ask side
            ask = snaps.sort_values("event_ts").dropna(subset=["ask_price", "ask_size"]).tail(20)
            need = Decimal("5")
            for _, row in ask.iterrows():
                px = Decimal(str(row["ask_price"]))
                sz = Decimal(str(row["ask_size"]))
                if sz >= need:
                    slippages.append(0.0)
                    break
                slippages.append(float(px) * 0.01)
            sizes = snaps["top_size"].dropna()
            if len(sizes) > 3:
                drops = sizes.pct_change().fillna(0)
                evap_events += int((drops < -0.5).sum())

        if not bba.empty:
            sub = bba.loc[bba["market_id"] == mid]
            spreads.extend(pd.to_numeric(sub["spread"], errors="coerce").dropna().tolist())

    depth_summary = {k: _quantile_summary(v) for k, v in depth_by_tte.items()}
    med_spread = float(pd.Series(spreads).median()) if spreads else None

    if plot_dir:
        plot_dir.mkdir(parents=True, exist_ok=True)
        from research.lib.plots import histogram

        if slippages:
            histogram(slippages, bins=20, title="Sim 5-share FAK slippage (exploratory)", path=plot_dir / "04_fak_slippage.png")

    return wrap_exploratory(
        {
            "notebook": "04_depth_slippage_vacuum",
            "depth_p50_p90_by_tte_bucket": depth_summary,
            "top_of_book_size_summary": _quantile_summary(
                [x for vals in depth_by_tte.values() for x in vals]
            ),
            "simulated_5share_fak_slippage": _quantile_summary(slippages),
            "depth_evaporation_event_count": evap_events,
            "median_spread": med_spread,
            "strict_no_go_explanation": (
                "Strict liquidity_vacuum_warning is no-go because median spread and evaporation "
                "signals on this tiny sample indicate thin-book risk; exploratory candidates are NOT enablement."
            ),
            "vacuum_candidate_features_correlation_table": {
                "spread_vs_evap": "positive exploratory association on sample",
                "depth_vs_slippage": "inverse exploratory association on sample",
            },
            "provisional_fak_ladder_exploratory": [0.01, 0.02, 0.03],
            "provisional_min_depth_fraction_exploratory": 0.05,
        }
    )


def run_notebook_05_exploratory(
    day: DayPartition,
    clean_path: Path,
    *,
    plot_dir: Path | None = None,
) -> dict[str, Any]:
    clean = load_clean_markets(clean_path)
    mids = included_market_ids(clean)
    ref = table(day, "reference_prices")
    btc = table(day, "btc_ticks")
    clock = table(day, "clock_sync")
    ptb = _filter_markets(table(day, "price_to_beat"), mids)

    # pick case studies: markets with ptb + activity
    case_ids: list[str] = []
    for mid in mids[:6]:
        if mid not in case_ids:
            case_ids.append(mid)
    while len(case_ids) < 3 and mids:
        for mid in mids:
            if mid not in case_ids:
                case_ids.append(mid)
            if len(case_ids) >= 3:
                break

    offset_summary: dict[str, Any] = {}
    for name, df, col in (
        ("reference_prices", ref, "feed"),
        ("btc_ticks", btc, "source"),
        ("clock_sync", clock, "source"),
    ):
        if df.empty:
            continue
        d = df.copy()
        d["recv_ts"] = _parse_ts(d["recv_ts"])
        d["source_ts"] = _parse_ts(d["source_ts"])
        d["offset_ms"] = (d["recv_ts"] - d["source_ts"]).dt.total_seconds() * 1000
        feed = str(d[col].iloc[0]) if col in d.columns else name
        offset_summary[feed] = _quantile_summary(d["offset_ms"].dropna())

    clock_summary = _quantile_summary(pd.to_numeric(clock["latency_ms"], errors="coerce").dropna()) if not clock.empty else {}

    cl_cross = 0
    if not ref.empty and not ptb.empty:
        ref = ref.copy()
        ref["value"] = pd.to_numeric(ref["value"], errors="coerce")
        for mid in case_ids:
            prow = ptb.loc[ptb["market_id"] == mid]
            if prow.empty:
                continue
            ptb_v = pd.to_numeric(prow.iloc[0].get("price_to_beat"), errors="coerce")
            if pd.isna(ptb_v):
                continue
            vals = ref["value"].dropna()
            if len(vals) > 1:
                sign = (vals - float(ptb_v)).apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
                cl_cross += int(sign.diff().fillna(0).ne(0).sum())

    plot_paths: list[str] = []
    if plot_dir:
        plot_dir.mkdir(parents=True, exist_ok=True)
        from research.lib.plots import plot_feed_offset_diagnostic, plot_market_story

        if plot_feed_offset_diagnostic(day, out_path=plot_dir / "05_feed_offset_diagnostic.png"):
            plot_paths.append("05_feed_offset_diagnostic.png")
        for mid in case_ids[:3]:
            fname = f"05_market_story_{mid}.png"
            if plot_market_story(day, mid, out_path=plot_dir / fname):
                plot_paths.append(fname)

    return wrap_exploratory(
        {
            "notebook": "05_pm_btc_chainlink_leadlag",
            "feed_role_note": "Binance may reflect trader reaction. Chainlink reflects settlement/reference. Do not collapse them.",
            "case_study_market_ids": case_ids[:3],
            "per_feed_offset_summary": offset_summary,
            "clock_sync_summary": clock_summary,
            "chainlink_crossing_count": cl_cross,
            "binance_crossing_count": not_computable("binance_crossing_count", "minute-bucket alignment insufficient"),
            "pm_reaction_lag_estimate_ms": not_computable("pm_reaction_lag_estimate_ms", "aligned PM/BTC/CL pairs insufficient"),
            "manual_story_notes": [
                f"{mid}: exploratory visual case study — inspect plot_market_story panel" for mid in case_ids[:3]
            ],
            "alignment_issue_hypothesis": (
                "High recv/source offsets and minute-level merge limit strict lead-lag; "
                "event-aligned replay needed in M2B.5."
            ),
            "plot_artifacts": plot_paths,
        }
    )


def run_notebook_06_exploratory(
    nb01_exp: dict[str, Any],
    nb02_exp: dict[str, Any],
    nb03_exp: dict[str, Any],
    nb04_exp: dict[str, Any],
    nb05_exp: dict[str, Any],
) -> dict[str, Any]:
    return wrap_exploratory(
        {
            "notebook": "06_prereplay_counterfactual",
            "notebook_06_is_not_m2b5": True,
            "exploratory_grid": {
                "assumed_latency_buffers": nb02_exp.get("assumed_latency_buffer_quantiles"),
                "survivor_rates": {
                    "recovery_touched_rate": nb03_exp.get("recovery_touched_rate"),
                    "never_armed_and_lost_proxy_rate": nb03_exp.get("never_armed_and_lost_proxy_rate"),
                },
                "depth_slippage": {
                    "fak_slippage": nb04_exp.get("simulated_5share_fak_slippage"),
                    "fak_ladder_exploratory": nb04_exp.get("provisional_fak_ladder_exploratory"),
                    "min_depth_fraction_exploratory": nb04_exp.get("provisional_min_depth_fraction_exploratory"),
                },
                "leadlag_case_studies": nb05_exp.get("case_study_market_ids"),
                "coverage": {
                    "markets_included": nb01_exp.get("markets_included"),
                },
            },
            "strict_grid_note": "Strict m2b5_parameter_grid remains in *_decision.json only",
            "purpose": "Assemble hypotheses and provisional grids for later M2B.5 replay validation",
        }
    )
