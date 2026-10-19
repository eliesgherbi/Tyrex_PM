"""Notebook 06: deliberately selected development cases, never a parameter search.

Historical live decisions support evidence review only. Recorded warehouse windows
use the real closed-loop replay, with optional experimental volatility / entry veto.
Nothing here enables live orders or edits the two frozen parameter dictionaries.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import pandas as pd

from tyrex_pm.indicators.graph import IndicatorGraph
from tyrex_pm.research.strategy_replay.cache import window_cache_key
from tyrex_pm.research.strategy_replay.results import jsonable
from tyrex_pm.research.strategy_replay.run import ReplayExecutionPolicy, run_window
from tyrex_pm.strategies.settlement_value.schema import settlement_value_config_from_parameters

from .experiments import REPO, RESEARCH_BASE_PARAMETERS, Lab, merge
from .frames import summary
from .staged import code_identity
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


def gap_reason(row: dict, threshold_pp: float | None) -> str | None:
    """Causal decision-time veto: point probability vs valid two-sided UP midpoint."""
    if threshold_pp is None:
        return None
    try:
        bid, ask, p = (float(row[k]) for k in ("up_bid", "up_ask", "p_up_point"))
    except (KeyError, TypeError, ValueError):
        return "STUDY_COMPARISON_UNAVAILABLE"
    if not all(math.isfinite(x) for x in (bid, ask, p)) or not 0 <= bid <= ask <= 1:
        return "STUDY_COMPARISON_UNAVAILABLE"
    if not 0 <= p <= 1:
        return "STUDY_COMPARISON_UNAVAILABLE"
    return "STUDY_DISAGREEMENT_VETO" if 100 * abs(p - (bid + ask) / 2) > threshold_pp else None


class StudyIndicatorGraph(IndicatorGraph):
    """Keep the chosen time policy even when reconnects reset an estimator.

Indicator instance ids and all other production feature calculations are unchanged.
"""

    def __init__(self, *args, time_policy: str, **kwargs):
        self.time_policy = time_policy
        super().__init__(*args, **kwargs)
        self._set_policy()

    def _set_policy(self):
        for estimator in self._ewma.values():
            estimator.config = replace(estimator.config, time_policy=self.time_policy)
            estimator.config.validate()

    def invalidate_source(self, source):
        super().invalidate_source(source)
        self._set_policy()


@dataclass
class ProbabilityStudy:
    time_policy: str = "legacy"
    disagreement_pp: float | None = None
    trace_level: str = "decisions"
    _reason: str | None = None

    def __post_init__(self):
        if self.trace_level not in ("summary", "decisions", "full"):
            raise ValueError("trace_level must be summary, decisions or full")
        if self.time_policy not in ("legacy", "elapsed_variance_v1"):
            raise ValueError("unknown time policy")
        if self.disagreement_pp is not None and not 0 < self.disagreement_pp <= 100:
            raise ValueError("disagreement_pp must be in (0, 100]")

    def identity(self):
        return {"time_policy": self.time_policy, "disagreement_pp": self.disagreement_pp,
                "trace_level": self.trace_level, "record_schema": "probability_explanation_v1"}

    def configure(self, runtime):
        graph = runtime.indicator_graph
        runtime.indicator_graph = StudyIndicatorGraph(
            graph.specs, max_trade_age_ms=graph.max_trade_age_ms, clock=graph._clock,
            time_policy=self.time_policy,
        )

    def before(self, agent, ready, context, features):
        self._reason = None
        if self.disagreement_pp is None or not context.entry_allowed:
            return context
        row: dict[str, Any] = {}
        # _observe computes diagnostics and candidates without advancing episode state.
        end = ready.snapshot.market.event_end
        tau = None if end is None else (end - ready.snapshot.observed_at).total_seconds()
        agent._driver()._observe(
            ready.snapshot, context, features, tau, row,
        )
        self._reason = gap_reason(row, self.disagreement_pp)
        return context if self._reason is None else replace(
            context, entry_allowed=False, entry_block_reason=self._reason,
        )

    def record(self, agent, ready, context, features, row):
        row["study_veto"] = self._reason
        row["study_time_policy"] = self.time_policy
        if self.trace_level != "full":
            return
        # This is optional research tracing, not a new global live logging default.
        row["probability_explanation"] = jsonable({
            "references": asdict(context.references),
            "features": features.values,
            "volatility_state": {
                key: {"config": asdict(est.config), "variance": est._var,
                      "previous_variance": est._previous_variance,
                      "last_return": est._last_return, "last_dt_s": est._last_dt_s,
                      "variance_units": "log_return_variance_per_second"
                      if self.time_policy != "legacy" else "log_return_variance",
                      "last_sample_ts": est._last_sample_ts,
                      "last_sample_price": est._last_price,
                      "sample_count": est._sample_count,
                      "effective_samples_s": est._effective_samples_s,
                      "last_update_ts": est._last_update_ts}
                for key, est in agent.runtime.indicator_graph._ewma.items()
            },
            "entry_allowed": context.entry_allowed,
            "entry_block_reason": context.entry_block_reason,
        })


def variants(threshold_pp: float = 9.0) -> dict[str, dict]:
    """One diagnostic hypothesis, not an optimization sweep."""
    return {
        "unchanged": {"time_policy": "legacy"},
        "time_corrected": {"time_policy": "elapsed_variance_v1"},
        "time_corrected_gap_veto": {
            "time_policy": "elapsed_variance_v1", "disagreement_pp": threshold_pp,
        },
    }


def replay_cases(*, window_ids: list[str] | None = None, trace_level: str = "decisions",
                 threshold_pp: float = 9.0, scenario: str = "friction",
                 variant_names: tuple[str, ...] | None = None) -> Path:
    """Explicit launch: both frozen configs on selected recorded development cases.

    Each window is released before loading the next. Identity includes code, helper,
    parameters, source hashes, execution policy and chosen hypothesis. Safe to resume.
    """
    inventory = case_inventory()
    inventory = inventory[inventory.kind.eq("warehouse replay")]
    requested = list(RESEARCH_CASES) if window_ids is None else list(window_ids)
    if len(requested) != len(set(requested)) or not set(requested) <= set(RESEARCH_CASES):
        raise ValueError("Choose unique replayable development case ids from the inventory")
    inventory = inventory.set_index("window_id").loc[requested].reset_index()
    choices = variants(threshold_pp)
    names = tuple(choices) if variant_names is None else variant_names
    if not names or not set(names) <= set(choices):
        raise ValueError("unknown or empty variant_names")
    candidates = load_candidates()
    execution = ReplayExecutionPolicy(reference_availability="reference_availability_v1")
    identity = {"cases": CASE_VERSION, "window_ids": requested, "scenario": scenario,
                "variants": {name: choices[name] for name in names}, "trace_level": trace_level,
                "candidates": {n: c.parameters for n, c in candidates.items()},
                "source_fingerprint": code_identity()["source_fingerprint"],
                "helper_sha256": file_hash(Path(__file__)), "execution": execution.identity(),
                "sources": []}
    # Fingerprint manifests, metadata and range without decompressing every window twice.
    for w in inventory.itertuples():
        lab = Lab(warehouse=Path(w.warehouse), collection=w.collection)
        identity["sources"].append({"slug": w.window_id,
                                    "cache_key": window_cache_key(
                                        lab.catalog, w.market_id, warmup_s=lab.warmup_s,
                                        after_s=30.0)})
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
    out = OUTPUT / f"{CASE_VERSION}-{digest[:12]}"
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "experiment.json"
    encoded = json.dumps(identity, indent=2, default=str)
    if manifest.exists() and manifest.read_text("utf-8") != encoded:
        raise ValueError("Saved case identity differs; never mix results")
    manifest.write_text(encoded, "utf-8")
    for w in inventory.itertuples():
        if (code_identity()["source_fingerprint"] != identity["source_fingerprint"]
                or file_hash(Path(__file__)) != identity["helper_sha256"]):
            raise RuntimeError("Source changed during case replay; stop without mixing versions")
        lab = Lab(warehouse=Path(w.warehouse), collection=w.collection)
        for candidate, config in candidates.items():
            for variant in names:
                path = out / f"{w.window_id}--{candidate}--{variant}.json"
                if path.exists():
                    continue
                window = lab.window(w.market_id)
                study = ProbabilityStudy(**choices[variant], trace_level=trace_level)
                result = run_window(
                    window,
                    strategy_config=settlement_value_config_from_parameters(
                        merge(RESEARCH_BASE_PARAMETERS, config.parameters)),
                    scenario=lab.scenario(scenario), execution_policy=execution,
                    clock_observations=lab.catalog.clock_observations_for(
                        window.range_start_ns, window.range_end_ns), research_study=study,
                )
                result["case_study"] = {**study.identity(), "candidate": candidate,
                                        "variant": variant, "purpose": w.purpose}
                result["case_summary"] = summary(result)
                result["case_summary"]["vetoed_evaluations"] = sum(
                    bool(d.get("study_veto")) for d in result["decisions"])
                if trace_level == "summary":
                    result["decisions"] = []
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps(result, default=str), "utf-8")
                temporary.replace(path)
                print(f"saved {w.window_id} / {candidate} / {variant}", flush=True)
        lab._windows.clear()
    return out


def saved_results(path: Path) -> pd.DataFrame:
    rows = []
    for p in sorted(path.glob("btc-*.json")):
        result = json.loads(p.read_text("utf-8"))
        rows.append({**result["case_summary"], **result["case_study"], "result_path": str(p)})
    return pd.DataFrame(rows)


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


def latest_case_run(*, trace_level="full", threshold_pp=9.0) -> Path | None:
    """Only the full chosen case set, matching policy and current source/helper."""
    helper = file_hash(Path(__file__))
    code = code_identity()["source_fingerprint"]
    matches = []
    for manifest in OUTPUT.glob(f"{CASE_VERSION}-*/experiment.json"):
        record = json.loads(manifest.read_text("utf-8"))
        if (record["window_ids"] == list(RESEARCH_CASES)
                and record["source_fingerprint"] == code
                and record["helper_sha256"] == helper
                and record["trace_level"] == trace_level
                and record["variants"] == variants(threshold_pp)
                and record["scenario"] == "friction"):
            matches.append(manifest.parent)
    return sorted(matches)[-1] if matches else None


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
