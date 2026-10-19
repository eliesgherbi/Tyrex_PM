"""Notebook 06: deliberately selected development cases, never a parameter search.

Historical live decisions support evidence review only. Recorded warehouse windows are
replayed as strategy configuration through ``case_runs`` (variance time policy and the
disagreement veto are strategy settings; the former study hooks were removed after the
migration check). Nothing here enables live orders or edits the frozen parameters.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from .experiments import REPO
from .v3 import frozen_windows, load_candidates

AUDIT = REPO / "var/research/probability_audit_20261019"
OUTPUT = REPO / "var/research/settlement_extreme_cases"
CASE_VERSION = "development_cases_v1"
# Explicit cases, rather than choosing whichever cases improve after changing a rule.
RESEARCH_CASES = {
    "btc-updown-5m-1790870400": "ordinary winner / Oct 1 control",
    "btc-updown-5m-1791302100": "ordinary winner / Oct 6 control",
    "btc-updown-5m-1792077000": "ordinary winner / Oct 15 control",
    "btc-updown-5m-1791303300": "large gap; direction correct (not necessarily filled)",
    "btc-updown-5m-1792085400": "large disagreement; profitable baseline trade",
    "btc-updown-5m-1790870700": "ordinary baseline loss / Oct 1",
    "btc-updown-5m-1792078800": "ordinary baseline loss / Oct 15",
}
LIVE_CASES = {
    "btc-updown-5m-1792263300": "large disagreement / actual live loss",
    "btc-updown-5m-1792266900": "large disagreement / actual live loss",
    "btc-updown-5m-1792262700": "smaller-gap live control",
    "btc-updown-5m-1792273500": "smaller-gap live control",
}


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def case_inventory() -> pd.DataFrame:
    windows = frozen_windows().set_index("slug")
    rows = []
    for slug, purpose in RESEARCH_CASES.items():
        w = windows.loc[slug]
        rows.append({"window_id": slug, "kind": "warehouse replay", "purpose": purpose,
                     "collection": w.collection, "market_id": w.market_id,
                     "warehouse": w.warehouse})
    for slug, purpose in LIVE_CASES.items():
        rows.append({"window_id": slug, "kind": "live evidence only", "purpose": purpose})
    result = pd.DataFrame(rows)
    result["start_paris"] = pd.to_datetime(
        result.window_id.str.rsplit("-", n=1).str[-1].astype(int), unit="s", utc=True
    ).dt.tz_convert("Europe/Paris")
    return result


def samples() -> pd.DataFrame:
    path = AUDIT / "decision_samples.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Saved audit input missing: {path}")
    df = pd.read_parquet(path)
    df = df[df.dataset.isin(["v3_research", "corrected_live"])].copy()
    df["gap_pp"] = 100 * (df.p_up_point - df.book_implied_up).abs()
    df["paris"] = pd.to_datetime(df.at_ns, unit="ns", utc=True).dt.tz_convert("Europe/Paris")
    return df


def case_metrics(df: pd.DataFrame, tau_min: float = 31, tau_max: float = 150) -> pd.DataFrame:
    """Descriptive window means; these are NOT a per-decision trading rule."""
    selected = set(RESEARCH_CASES) | set(LIVE_CASES)
    df = df[df.window_id.isin(selected) & df.tau_s.between(tau_min, tau_max)
            & df.two_sided_book & df.gap_pp.notna()]
    return df.groupby(["dataset", "window_id"]).agg(
        seconds=("gap_pp", "size"), mean_gap_pp=("gap_pp", "mean"),
        max_gap_pp=("gap_pp", "max"),
        share_above_9pp=("gap_pp", lambda s: (s > 9).mean()),
        share_above_15pp=("gap_pp", lambda s: (s > 15).mean()),
    ).reset_index()


def entry_shadow(threshold_pp: float = 9.0) -> pd.DataFrame:
    """Gap at actual live ENTER, not the larger peak gap seen after buying.

An ENTER record is an intent, not proof of a POST or fill. No hypothetical P&L.
"""
    df = pd.read_csv(AUDIT / "live_entry_probabilities.csv")
    df = df[df.dataset.eq("corrected_live")].copy()
    df["gap_at_intent_pp"] = 100 * (df.p_up_point - df.book_implied_up).abs()
    df["would_veto_intent"] = df.gap_at_intent_pp > threshold_pp
    df["threshold_pp"] = threshold_pp
    return df[["window_id", "run", "chosen_side", "tau_s", "p_up_point", "p_up_low",
               "p_up_high", "book_implied_up", "gap_at_intent_pp", "would_veto_intent",
               "threshold_pp"]]


def previous_comparison(results: pd.DataFrame) -> pd.DataFrame:
    """Trade-level regression against saved notebook 05; includes both candidates."""
    from .v3 import VERIFICATION_ROOT

    previous = pd.read_parquet(
        VERIFICATION_ROOT / "v3_verification-ref_v1-141434f36a/friction/summaries.parquet")
    configs = load_candidates()
    id_to_candidate = {c.config_id: name for name, c in configs.items()}
    if "config_id" in previous:
        previous["candidate"] = previous.config_id.map(id_to_candidate)
    else:
        # Batch tables encode complete parameters as canonical JSON in `config`.
        previous["candidate"] = previous.config.map(lambda raw: "default" if
            float(json.loads(raw)["entry"]["tau_max_s"]) == 150 else "comparison")
    old = previous[["slug", "candidate", "filled", "side", "debit", "realized_pnl"]]
    return results[results.variant.eq("unchanged")].merge(
        old, on=["slug", "candidate"], how="left", suffixes=("_now", "_previous"),
        validate="one_to_one",
    )


def plot_case(df: pd.DataFrame, window_id: str):
    import matplotlib.pyplot as plt

    rows = df[df.window_id.eq(window_id)].sort_values("at_ns")
    if rows.empty:
        raise ValueError("No saved samples for this case")
    fig, axes = plt.subplots(3, 1, figsize=(12, 8), sharex=True)
    axes[0].plot(rows.paris, rows.p_up_point, label="Model P(UP)")
    valid = rows[rows.two_sided_book]
    axes[0].plot(valid.paris, valid.book_implied_up, label="UP quote midpoint")
    axes[0].fill_between(rows.paris, rows.p_up_low, rows.p_up_high, alpha=.15,
                         label="Model sensitivity band (not calibrated confidence)")
    axes[0].legend(loc="best")
    axes[0].set_ylim(0, 1)
    axes[1].plot(valid.paris, valid.gap_pp)
    axes[1].set_ylabel("Absolute gap (pp)")
    axes[2].plot(rows.paris, rows.sigma)
    axes[2].set_ylabel("Sigma / sqrt(second)")
    axes[2].set_xlabel("Paris time")
    fig.suptitle(window_id + " — saved evidence, not a counterfactual execution")
    fig.tight_layout()
    return fig
