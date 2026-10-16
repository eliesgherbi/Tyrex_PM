"""Offline teaching tools for expiry, alignment, probabilities and honest ML evaluation.

No settlement rule, fee schedule or execution outcome is inferred by these tools.
Feature builders do not read resolution labels or retrospective quality scores.
"""

from __future__ import annotations

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

import numpy as np
import pandas as pd

from tyrex_pm.data_warehouse.contracts import decode

from .analysis import BookStudy, btc_grid, observed_ofi_grid, strict_forward_move
from .data import Sample

FEATURES = (
    "mid",
    "spread_cents",
    "imbalance",
    "seconds_left",
    "quote_age_s",
    "btc_return_1s_bps",
    "btc_volatility_bps",
)


def reference_grid(
    sample: Sample, times, feed_id: str | None = None, max_age_s: float = 5
) -> pd.DataFrame:
    """Align one feed on receipt time; reject stale and observed-disconnected states."""
    refs = sample.frame("reference_observations")
    ids = refs.feed_id.dropna().unique()
    if feed_id is None:
        if len(ids) != 1:
            raise ValueError("Select one reference feed explicitly; absent feeds stay absent.")
        feed_id = ids[0]
    refs = refs[refs.feed_id == feed_id].copy()
    if refs.empty:
        raise ValueError("The selected reference feed has no observations.")
    refs["reference_value"] = refs.value.map(float)
    refs = refs.rename(
        columns={"received_at": "reference_at", "source_time": "reference_source_at"}
    )
    out = pd.merge_asof(
        pd.DataFrame({"time": pd.DatetimeIndex(times)}),
        refs.sort_values("reference_at", kind="stable"),
        left_on="time",
        right_on="reference_at",
        direction="backward",
    )
    out["reference_age_s"] = (out.time - out.reference_at).dt.total_seconds()
    out["reference_source_age_s"] = (out.time - out.reference_source_at).dt.total_seconds()
    out["reference_eligible"] = out.reference_age_s.between(0, max_age_s)
    sources = set(refs.source_id.dropna())
    # A remembered price must not survive a known transport/session interruption.
    outages = []
    transport_changes = []
    for row in sample.records("operational_events"):
        event = decode(row["details"])
        kind = event.get("type")
        relevant = event.get("source") in sources or kind in (
            "session_started",
            "session_interrupted",
            "session_stopped",
        )
        if relevant and kind in (
            "disconnected",
            "sequence_gap",
            "session_started",
            "session_interrupted",
            "session_stopped",
        ):
            outages.append(row["received_at"])
            transport_changes.append((row["received_at"], False))
        elif event.get("source") in sources and kind in ("connected", "feed_evidence"):
            transport_changes.append((row["received_at"], True))
    if outages:
        outages = np.sort(np.array(outages, dtype=np.int64))
        times_ns = np.array([t.value for t in out.time])
        ref_ns = np.array(
            [t.value if pd.notna(t) else np.iinfo(np.int64).min for t in out.reference_at]
        )
        since_observation = np.searchsorted(outages, times_ns, side="right") - np.searchsorted(
            outages, ref_ns, side="right"
        )
        out["reference_eligible"] &= since_observation == 0
    if transport_changes:
        transport_changes.sort(key=lambda item: item[0])
        at = np.array([item[0] for item in transport_changes], dtype=np.int64)
        indices = np.searchsorted(at, [t.value for t in out.time], side="right") - 1
        connected = np.array([index < 0 or transport_changes[index][1] for index in indices])
        out["reference_eligible"] &= connected
    out.loc[~out.reference_eligible, "reference_value"] = np.nan
    return out


def digital_probability(log_distance, return_variance, mean_log_return=0):
    """Illustrative endpoint event probability for a normal remaining log return.

    This is a conditional physical-distribution toy, not a qualified PM valuation
    or a risk-neutral option price. Zero variance follows the explicit >= tie rule.
    Missing inputs stay NaN. Variance is integrated over the remaining interval.
    """
    distance, variance, mean = np.broadcast_arrays(
        np.asarray(log_distance, dtype=float),
        np.asarray(return_variance, dtype=float),
        np.asarray(mean_log_return, dtype=float),
    )
    if np.any(variance < 0):
        raise ValueError("Return variance cannot be negative.")
    result = np.full(distance.shape, np.nan)
    present = np.isfinite(distance) & np.isfinite(variance) & np.isfinite(mean)
    positive = present & (variance > 0)
    z = (distance[positive] + mean[positive]) / np.sqrt(variance[positive])
    result[positive] = np.array([0.5 * math.erfc(-float(v) / math.sqrt(2)) for v in z])
    zero = present & (variance == 0)
    result[zero] = ((distance + mean)[zero] >= 0).astype(float)
    return float(result) if result.ndim == 0 else result


def ev_size(
    probability: float,
    price: float,
    fee_usd_per_share: float = 0,
    target_usd: float = 3,
    cash_cap_usd: float = 100,
) -> dict:
    """Hypothetical $1 binary payout and USD fees; all inputs are scenario assumptions."""
    values = (probability, price, fee_usd_per_share, target_usd, cash_cap_usd)
    if not all(math.isfinite(v) for v in values) or not (
        0 <= probability <= 1
        and 0 < price < 1
        and fee_usd_per_share >= 0
        and target_usd > 0
        and cash_cap_usd > 0
    ):
        raise ValueError("Invalid hypothetical sizing inputs.")
    # Scenario cents must not become an extra share through float roundoff.
    with localcontext() as context:
        context.prec = 60
        exact_cost = Decimal(str(price)) + Decimal(str(fee_usd_per_share))
        exact_edge = Decimal(str(probability)) - exact_cost
        required = (
            int((Decimal(str(target_usd)) / exact_edge).to_integral_value(rounding=ROUND_CEILING))
            if exact_edge > 0
            else None
        )
        cap = int((Decimal(str(cash_cap_usd)) / exact_cost).to_integral_value(rounding=ROUND_FLOOR))
    cost, edge = float(exact_cost), float(exact_edge)
    shares = min(required, cap) if required else 0
    return {
        "net_edge_per_share_USD": edge,
        "shares_for_target_EV": required,
        "shares_with_cash_cap": shares,
        "capital_USD": shares * cost,
        "expected_profit_USD": shares * edge,
        "target_feasible_under_cap": required is not None and required <= cap,
        "profit_if_win_USD": shares * (1 - cost),
        "profit_if_lose_USD": -shares * cost,
        # Shares of the same binary contract share one outcome: risk scales with N, not sqrt(N).
        "pnl_standard_deviation_USD": shares * math.sqrt(probability * (1 - probability)),
    }


def feature_dataset(
    sample: Sample, study: BookStudy | None = None, lookback_s: int = 20, horizon_s: int = 5
) -> pd.DataFrame:
    """Causal one-second Up features plus a retrospective next-midpoint-move label.

    No outcome/settlement data is read. Price labels allow fresh usable snapshots
    on the same socket and reject recorded gaps. Missing labels remain missing.
    """
    if lookback_s < 2 or horizon_s < 1:
        raise ValueError("Use a lookback >= 2 and a positive horizon.")
    study = study or BookStudy(sample)
    iid = next((i for i, label in sample.labels.items() if str(label).lower() == "up"), None)
    if iid is None:
        raise ValueError("A recorded Up instrument is required.")
    up = study.grid()
    up = up[up.instrument_id == iid].reset_index(drop=True)
    events = study.tape[study.tape.instrument_id == iid]
    btc = btc_grid(sample)
    frame = up.copy()
    frame["market_id"] = sample.manifest["market_id"]
    frame["sample_manifest_sha256"] = sample.manifest_sha256
    frame["window_start"] = pd.Timestamp(sample.start, tz="UTC")
    frame["window_end"] = pd.Timestamp(sample.end, tz="UTC")
    frame["spread_cents"] = frame.spread * 100
    frame["seconds_left"] = (pd.Timestamp(sample.end, tz="UTC") - frame.time).dt.total_seconds()
    frame["quote_age_s"] = frame.age_s
    frame["btc_return_1s_bps"] = btc.log_return_bps.to_numpy()
    frame["btc_volatility_bps"] = (
        btc.log_return_bps.rolling(lookback_s, min_periods=lookback_s).std().to_numpy()
    )
    frame["observed_ofi_shares"] = observed_ofi_grid(events, up)
    frame["feature_start"] = frame.time - pd.Timedelta(seconds=lookback_s)
    frame["label_end"] = frame.time + pd.Timedelta(seconds=horizon_s)
    frame["forward_move_cents"] = (
        strict_forward_move(up, horizon_s, events=events, require_same_seed=False) * 100
    )
    # NaN must not silently become class zero through a >0 comparison.
    frame["target_rise"] = pd.array(
        [int(v > 0) if pd.notna(v) else None for v in frame.forward_move_cents], dtype="Int64"
    )
    frame["features_ready"] = np.isfinite(frame[list(FEATURES)].to_numpy(dtype=float)).all(axis=1)
    frame["research_row_eligible"] = frame.features_ready & frame.target_rise.notna()
    frame.attrs.update(
        {
            "target": "next_midpoint_rises_not_terminal_Up",
            "lookback_s": lookback_s,
            "horizon_s": horizon_s,
            "settlement_labels_used": False,
        }
    )
    return frame


def combine_datasets(frames: list[pd.DataFrame]) -> pd.DataFrame:
    if not frames:
        raise ValueError("At least one prepared sample is required.")
    ids = [f.market_id.iloc[0] for f in frames]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate market samples would falsely increase sample size.")
    return pd.concat(frames, ignore_index=True).sort_values(["time", "market_id"], kind="stable")


def chronological_split(
    frame: pd.DataFrame, train_fraction: float = 0.65, mode: str = "markets", embargo_s: int = 0
) -> dict:
    """Hold out later markets, or explicitly run a within-window teaching demo.

    Purge training labels that touch the earliest held-out feature lookback. No
    row-level shuffle is used. Independent test windows are reported explicitly.
    """
    if not 0 < train_fraction < 1 or embargo_s < 0:
        raise ValueError("Invalid split fraction or embargo.")
    ready = frame[frame.research_row_eligible].sort_values("time", kind="stable").copy()
    if len(ready) < 4:
        raise ValueError("Not enough eligible labeled rows for a split.")
    markets = ready.groupby("market_id").window_start.min().sort_values()
    if mode == "markets":
        if len(markets) < 2:
            raise ValueError("A cross-market holdout requires at least two different markets.")
        cut = min(len(markets) - 1, max(1, int(len(markets) * train_fraction)))
        train = ready[ready.market_id.isin(markets.index[:cut])]
        test = ready[ready.market_id.isin(markets.index[cut:])]
    elif mode == "within_window_demo":
        if len(markets) != 1:
            raise ValueError("The within-window demonstration uses exactly one market.")
        cut = min(len(ready) - 1, max(1, int(len(ready) * train_fraction)))
        train, test = ready.iloc[:cut], ready.iloc[cut:]
    else:
        raise ValueError("Use markets or within_window_demo.")
    cutoff = test.feature_start.min() - pd.Timedelta(seconds=embargo_s)
    original = len(train)
    train = train[train.label_end < cutoff].copy()
    if train.empty or test.empty:
        raise ValueError("No usable split survives label/lookback purging.")
    return {
        "train": train,
        "test": test.copy(),
        "mode": mode,
        "purged_rows": original - len(train),
        "purge_before": cutoff,
        "training_markets": train.market_id.nunique(),
        "heldout_markets": test.market_id.nunique(),
        "independent_new_market_holdout": mode == "markets",
    }


def classifier_demo(split: dict, features: tuple = FEATURES) -> dict:
    """Training-only preprocessing, fixed model, and a training-prevalence baseline."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if not set(features).issubset(set(FEATURES) | {"observed_ofi_shares"}):
        raise ValueError("Only explicitly declared causal feature columns are allowed.")
    train, test = split["train"], split["test"]
    x_train = train[list(features)].to_numpy(dtype=float)
    x_test = test[list(features)].to_numpy(dtype=float)
    if not np.isfinite(x_train).all() or not np.isfinite(x_test).all():
        raise ValueError(
            "Filter unavailable features before fitting; never impute from future rows."
        )
    y_train = train.target_rise.to_numpy(dtype=int)
    y_test = test.target_rise.to_numpy(dtype=int)
    predictions = test[["time", "market_id", "target_rise", "forward_move_cents"]].copy()
    predictions["constant_training_prior"] = y_train.mean()
    model, status = None, "single_training_class_baseline_only"
    if len(np.unique(y_train)) == 2:
        model = make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=1000))
        model.fit(x_train, y_train)
        predictions["logistic_probability"] = model.predict_proba(x_test)[:, 1]
        status = "fixed_model_fitted_no_tuning"
    metrics = []
    for name in [
        c for c in predictions if c in ("constant_training_prior", "logistic_probability")
    ]:
        probability = predictions[name].to_numpy()
        clipped = np.clip(probability, 1e-12, 1 - 1e-12)
        metrics.append(
            {
                "model": name,
                "test_rows": len(test),
                "accuracy": np.mean((probability >= 0.5) == y_test),
                "brier_score": np.mean((probability - y_test) ** 2),
                "log_loss": -np.mean(y_test * np.log(clipped) + (1 - y_test) * np.log(1 - clipped)),
            }
        )
    return {
        "pipeline": model,
        "status": status,
        "predictions": predictions,
        "metrics": pd.DataFrame(metrics),
        "features": features,
        "probability_target": "next_midpoint_rise_not_settlement_or_profit",
    }


def local_block_means(
    series: pd.Series, block_length: int = 5, repetitions: int = 500, seed: int = 7
) -> np.ndarray:
    """Illustrate dependent resampling within one window; not a multi-market CI.

    Input retains a regular one-second clock and NaNs. Blocks crossing missing
    observations are rejected; dropping NaNs first would fabricate adjacency.
    """
    if block_length < 1 or repetitions < 1:
        raise ValueError("Use positive block length and repetitions.")
    if (
        not isinstance(series.index, pd.DatetimeIndex)
        or not series.index.to_series().diff().dropna().eq(pd.Timedelta(seconds=1)).all()
    ):
        raise ValueError("Keep the original regular one-second clock.")
    values = series.to_numpy(dtype=float)
    if len(values) < block_length:
        raise ValueError("Too few points for the block length.")
    blocks = np.lib.stride_tricks.sliding_window_view(values, block_length)
    blocks = blocks[np.isfinite(blocks).all(axis=1)]
    if not len(blocks):
        raise ValueError("No fully observed consecutive block is available.")
    rng = np.random.default_rng(seed)
    count = max(1, math.ceil(np.isfinite(values).sum() / block_length))
    selected = rng.integers(0, len(blocks), size=(repetitions, count))
    return blocks[selected].mean(axis=(1, 2))


def clustered_effect(
    frame: pd.DataFrame, threshold: float = 0.25, repetitions: int = 500, seed: int = 7
) -> dict:
    """High-minus-low imbalance mean forward move, resampling entire market IDs."""
    clean = frame[frame.imbalance.notna() & frame.forward_move_cents.notna()].copy()
    ids = clean.market_id.unique()

    def effect(rows):
        high = rows.loc[rows.imbalance >= threshold, "forward_move_cents"]
        low = rows.loc[rows.imbalance <= -threshold, "forward_move_cents"]
        return high.mean() - low.mean() if len(high) and len(low) else np.nan

    result = {
        "effect_cents": effect(clean),
        "market_count": len(ids),
        "interval": None,
        "status": "insufficient_independent_windows",
    }
    if len(ids) < 2:
        return result
    rng = np.random.default_rng(seed)
    groups = {market: clean[clean.market_id == market] for market in ids}
    effects = [
        effect(pd.concat([groups[mid] for mid in rng.choice(ids, len(ids), replace=True)]))
        for _ in range(repetitions)
    ]
    finite = np.array(effects)[np.isfinite(effects)]
    if len(finite):
        result.update(
            {
                "status": "exploratory_cluster_resampling_not_strategy_qualification",
                "interval": np.quantile(finite, [0.025, 0.975]).tolist(),
                "usable_resamples": len(finite),
            }
        )
    return result
