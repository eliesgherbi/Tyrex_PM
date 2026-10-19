"""Read-only explorer for saved settlement_value summaries (notebook 03).

Loads a saved ``summaries*.parquet`` once, then filters the cached frame. Nothing here
reruns the strategy, opens a feed or places an order. Metrics are defined in
:func:`summarize`; they deliberately differ from ``analysis.trade_economics`` in one
respect: a filled trade without a realized P&L stays unknown instead of counting as zero.

Datasets are kept apart (:data:`DATASETS`); nothing combines them.
"""

from __future__ import annotations

import hashlib
import json
import math
import warnings
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from . import timelines
from .experiments import REPO
from .plots import BLUE, GRID, INK, MUTED, ORANGE

S2 = REPO / "var" / "research" / "strategy_runs" / "s2"
STAGED = REPO / "var" / "research" / "strategy_runs" / "staged"
STAGED_PREFIX = "staged:"
NEUTRAL = "#8a8985"
SMALL_SAMPLE = 10  # resolved fills below this are drawn faded and flagged
DOT_PLOT_BELOW = 8  # fewer resolved fills than this -> individual dots, no histogram


@dataclass(frozen=True)
class DatasetSpec:
    key: str
    title: str
    path: Path
    scope: str
    limitations: tuple[str, ...]
    traces: str


CLOCK_V1_LIMITATION = (
    "Produced with replay v1, whose clock mixed in whole-second clob_time samples: about "
    "60–80% of evaluations were host-skipped (future_binance_rejected), so entries were "
    "only possible in a fraction of seconds. Replay v2 (Binance time only) fixes this; "
    "rerun as a new experiment for corrected results."
)

EXECUTION_V2_LIMITATION = (
    "Produced with replay v2: orders were sent as the strategy's intended gross shares "
    "with no pre-send final gate and no execution feedback to the strategy. Replay v3 "
    "follows the live order contract (planner debit cap, fee reserve, amount-denominated "
    "FAK BUY, dispatch_check at the gate time) and supports retry, exits and re-entry; "
    "rerun as a new experiment to compare like with like."
)

DATASETS: dict[str, DatasetSpec] = {
    "validation_seed": DatasetSpec(
        key="validation_seed",
        title="Corrected model comparison (validation_seed)",
        path=S2 / "validation_seed" / "summaries.parquet",
        scope="Two model backends compared on the development windows, after the clock "
        "correction, with a one-tick limit buffer.",
        limitations=(
            "Only the model backend (and the recorded limit buffer) varies; it is not a "
            "parameter search.",
            "Settings not listed as overrides were inherited from the code defaults when "
            "the run was made; those defaults are not recorded here.",
            CLOCK_V1_LIMITATION,
        ),
        traces="This explorer reads one summary row per run. The bundle also holds a "
        "compact decisions.parquet, which this explorer does not load.",
    ),
    "grid_partial": DatasetSpec(
        key="grid_partial",
        title="Partial parameter grid (grid/summaries_partial.parquet)",
        path=S2 / "grid" / "summaries_partial.parquet",
        scope="A parameter grid stopped early: only the windows whose groups finished "
        "are included.",
        limitations=(
            "Predates any clock correction (raw collector clock): on 6 Oct most "
            "evaluations were host-skipped; entry times differ from current code.",
            "No limit buffer: under friction, many taker orders did not fill.",
            "Incomplete grid: few windows, so every configuration has few fills.",
            "Settings not listed as overrides were inherited from the code defaults when "
            "the run was made; those defaults are not recorded here.",
        ),
        traces="Only one summary row per run was saved. No per-decision timeline exists "
        "for this grid.",
    ),
}

COMMON_LIMITATIONS = (
    "Outcomes are proxy labels (label_type proxy_outcome), not qualified settlements.",
    "K, the opening threshold, is PROVISIONAL.",
    "Execution follows the uncalibrated favorable / friction scenarios; favorable is not "
    "a profitability bound.",
    "These are development windows. Nothing here validates profitability.",
)

BACKENDS = {"twap_proxy_diffusion": "TWAP", "endpoint_diffusion": "Endpoint"}

METRICS = {
    "net_pnl": "Total simulated net P&L (USDC)",
    "win_rate": "Win rate (profitable ÷ resolved fills)",
    "avg_pnl_per_fill": "Average net P&L per resolved fill (USDC)",
    "return_on_spending": "Net P&L ÷ acquisition spending",
}

STATUS_TEXT = {
    "win": "profitable",
    "loss": "losing",
    "breakeven": "break-even",
    "unfilled": "entry requested, nothing filled",
    "skipped": "skipped (no entry request)",
    "unresolved": "filled, outcome or P&L missing",
}


# --------------------------------------------------------------------------- labels


def _num(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def config_label(config: dict) -> str:
    """Readable label from the saved overrides (only what was recorded)."""
    flat = flatten(config)
    parts: list[str] = []
    backend = flat.pop("model.backend", None)
    if backend is not None:
        parts.append(BACKENDS.get(backend, str(backend)))
    edge = flat.pop("entry.min_edge_per_share", None)
    if edge is not None:
        d = _num(edge)
        parts.append(f"edge {_plain(d * 100)}¢" if d is not None else f"edge {edge}")
    tau = flat.pop("entry.tau_max_s", None)
    if tau is not None:
        d = _num(tau)
        parts.append(f"entry from {_plain(d) if d is not None else tau}s remaining")
    cap = flat.pop("sizing.max_debit_usdc", None)
    if cap is not None:
        parts.append(f"cap {cap}")
    buffer = flat.pop("entry.limit_buffer_ticks", None)
    if buffer is not None:
        parts.append(f"limit +{buffer} tick" + ("" if str(buffer) == "1" else "s"))
    parts.extend(f"{key}={value}" for key, value in sorted(flat.items()))
    return " | ".join(parts) if parts else "(no recorded overrides)"


def flatten(config: dict) -> dict:
    out = {}
    for section, values in config.items():
        if isinstance(values, dict):
            for key, value in values.items():
                out[f"{section}.{key}"] = value
        else:
            out[section] = values
    return out


# --------------------------------------------------------------------------- loading


@dataclass
class Dataset:
    spec: DatasetSpec
    runs: pd.DataFrame
    configs: pd.DataFrame  # one row per config_id: config (JSON), label, overrides
    stages: pd.DataFrame | None = None  # staged experiments: every stage and its status
    # "fixed" | "staged" | "batch" (v3 verification/feature runs, case runs; one batch
    # bundle per scenario folder) | "study" (preserved notebook 06 study bundles).
    kind: str = "fixed"
    root: Path | None = None

    @property
    def counts(self) -> dict:
        runs = self.runs
        return {
            "runs": len(runs),
            "configurations": runs["config_id"].nunique(),
            "scenarios": runs["scenario_name"].nunique(),
            "windows": runs["market_id"].nunique(),
            "collections": runs["collection"].nunique() if "collection" in runs else 0,
            "runs_per_cell": runs.groupby(["config_id", "scenario_name"]).size(),
        }

    @property
    def scenarios(self) -> list[str]:
        order = {"favorable": 0, "friction": 1}
        return sorted(
            self.runs["scenario_name"].dropna().unique(), key=lambda s: (order.get(s, 9), s)
        )


_CACHE: dict[tuple[str, float], Dataset] = {}


BATCH_PREFIX = "batch:"
STUDY_PREFIX = "study:"
# Experiment records whose results are batch bundles (one per scenario folder), not stages.
BATCH_KINDS = frozenset({"v3_verification", "v3_feature", "v3_shared_account"})


def _kind(exp_file: Path) -> str | None:
    try:
        return json.loads(exp_file.read_text(encoding="utf-8")).get("kind")
    except (OSError, ValueError):
        return None


def batch_experiments() -> list[tuple[str, Path]]:
    """v3 verification / feature / shared-account runs and case runs, with results."""
    from .case_runs import CASE_RUNS
    from .v3 import FEATURE_ROOT, SHARED_ROOT

    found: list[tuple[str, Path]] = []
    for root in (STAGED, FEATURE_ROOT, SHARED_ROOT):
        for exp_file in sorted(root.glob("*/experiment.json")):
            kind = _kind(exp_file)
            if kind in BATCH_KINDS and _batch_dirs(exp_file.parent):
                title = {"v3_verification": "v3 verification", "v3_feature": "v3 feature run",
                         "v3_shared_account": "v3 shared account"}[kind]
                found.append((f"{title} {exp_file.parent.name}", exp_file.parent))
    for summaries in sorted(CASE_RUNS.glob("cases-*/summaries.parquet")):
        found.append((f"Case run {summaries.parent.name}", summaries.parent))
    return found


def study_bundles() -> list[tuple[str, Path]]:
    """Preserved notebook 06 study results (saved with the former study hooks)."""
    from .case_runs import LEGACY_CASES

    return [(f"Notebook 06 study {p.parent.name} (preserved)", p.parent)
            for p in sorted(LEGACY_CASES.glob("*/experiment.json"))
            if any(p.parent.glob("btc-*.json"))]


def _relative_key(prefix: str, path: Path) -> str:
    try:
        return prefix + str(path.resolve().relative_to(REPO)).replace("\\", "/")
    except ValueError:
        return prefix + str(path)


def available_datasets(root: Path = STAGED) -> list[tuple[str, str]]:
    """(title, key): fixed datasets, staged experiments, batch experiments (v3
    verification/feature runs, case runs) and preserved notebook 06 study bundles."""
    out = [(spec.title, key) for key, spec in DATASETS.items()]
    for exp_file in sorted(root.glob("*/experiment.json")):
        if _kind(exp_file) in BATCH_KINDS:
            continue
        out.append(
            (f"Staged experiment {exp_file.parent.name}", STAGED_PREFIX + exp_file.parent.name)
        )
    out += [(title, _relative_key(BATCH_PREFIX, path)) for title, path in batch_experiments()]
    out += [(title, _relative_key(STUDY_PREFIX, path)) for title, path in study_bundles()]
    return out


def _key_path(key: str, prefix: str) -> Path:
    raw = Path(key[len(prefix):])
    return raw if raw.is_absolute() else REPO / raw


def load_dataset(key: str) -> Dataset:
    """Load a saved summaries file once per (path, modification time)."""
    if key.startswith(STAGED_PREFIX):
        return load_experiment(STAGED / key[len(STAGED_PREFIX) :])
    if key.startswith(BATCH_PREFIX):
        return load_batch(_key_path(key, BATCH_PREFIX))
    if key.startswith(STUDY_PREFIX):
        return load_study(_key_path(key, STUDY_PREFIX))
    spec = DATASETS[key]
    if not spec.path.exists():
        raise FileNotFoundError(f"{spec.title}: file not found at {spec.path}")
    cache_key = (str(spec.path), spec.path.stat().st_mtime)
    if cache_key not in _CACHE:
        _CACHE[cache_key] = prepare(pd.read_parquet(spec.path), spec)
    return _CACHE[cache_key]


def load_experiment(path: Path | str) -> Dataset:
    """A staged experiment directory: every *completed* stage, scenarios kept apart.

    Unfinished stages are listed in ``Dataset.stages`` but their partial part files are
    not loaded. Raises FileNotFoundError when no stage has completed yet.
    """
    path = Path(path)
    exp_file = path / "experiment.json"
    if not exp_file.exists():
        raise FileNotFoundError(f"no experiment.json in {path}")
    stage_files = sorted(path.glob("stage_*/stage.json"))
    watched = [exp_file, *stage_files, *sorted(path.glob("stage_*/summaries.parquet"))]
    cache_key = (str(path), sum(f.stat().st_mtime for f in watched))
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    record = json.loads(exp_file.read_text(encoding="utf-8"))
    frames, labels, stages = [], {}, []
    for stage_file in stage_files:
        stage = json.loads(stage_file.read_text(encoding="utf-8"))
        summaries = stage_file.parent / "summaries.parquet"
        done = stage["status"] == "completed" and summaries.exists()
        stages.append(
            {
                "stage": stage["stage"],
                "scenario": stage["scenario"],
                "status": stage["status"],
                "configurations": len(stage["configurations"]),
                "runs_planned": len(stage["configurations"]) * len(stage["windows"]),
                "loaded": done,
            }
        )
        suffix = " (benchmark)" if stage["stage"] == "B" else ""
        for c in stage["configurations"]:
            labels[c["config_id"]] = c["label"] + suffix
        if done:
            frames.append(pd.read_parquet(summaries).assign(stage=stage["stage"]))
    stage_table = pd.DataFrame(stages)
    if not frames:
        raise FileNotFoundError(
            f"experiment {record['experiment_id']}: no completed stage yet "
            f"({', '.join(st['stage'] + '=' + st['status'] for st in stages) or 'none started'})"
        )
    with warnings.catch_warnings():  # a stage with no fills has all-NA trade columns
        warnings.simplefilter("ignore", FutureWarning)
        frame = pd.concat(frames, ignore_index=True)
    if frame.duplicated(["config_hash", "scenario_name", "market_id"]).any():
        raise ValueError("the same configuration, scenario and window appear in two stages")
    design = record["design"]
    spec = DatasetSpec(
        key=STAGED_PREFIX + record["experiment_id"],
        title=f"Staged experiment {record['experiment_id']}",
        path=path,
        scope=f"{record['kind'].replace('_', ' ')}: {len(record['windows'])} frozen "
        f"development windows; primary scenario {design['primary_scenario']}, finalists "
        f"under {design['finalist_scenario']}. Model, volatility, budget and limit buffer "
        "are fixed across all stages; only edge and entry timing vary."
        + (
            " SMOKE RUN: a tiny subset, for checking the pipeline only."
            if record["kind"] == "smoke"
            else ""
        ),
        limitations=(
            "Only completed stages are loaded; unfinished stages are listed, not shown.",
            "Full effective settings (inherited defaults included) are saved in each "
            "stage.json; the overrides table shows only the settings that vary.",
            *(
                (CLOCK_V1_LIMITATION,)
                if (record.get("replay") or {}).get("runner", "strategy_closed_loop_v1")
                == "strategy_closed_loop_v1"
                else ()
            ),
            *(
                (EXECUTION_V2_LIMITATION,)
                if (record.get("replay") or {}).get("runner", "strategy_closed_loop_v1")
                in ("strategy_closed_loop_v1", "strategy_closed_loop_v2")
                else ()
            ),
        ),
        traces="Only one summary row per run was saved (no per-decision timeline).",
    )
    dataset = prepare(frame, spec, labels=labels)
    dataset.stages = stage_table
    dataset.kind, dataset.root = "staged", path
    _CACHE[cache_key] = dataset
    return dataset


def _batch_dirs(path: Path) -> list[Path]:
    """Batch bundles of an experiment: one per scenario folder, or the folder itself."""
    if (path / "summaries.parquet").exists():
        return [path]
    return sorted(p.parent for p in path.glob("*/summaries.parquet"))


def _record_policy(record: dict) -> dict:
    """Execution policy a whole experiment ran under (older tables lack the column).

    The contract is stated explicitly so a regenerated timeline uses exactly it."""
    policy = dict(record.get("execution_policy") or {})
    contract = (record.get("replay") or {}).get("execution_contract")
    if contract and "execution_contract" not in policy:
        policy["execution_contract"] = contract
    return policy


def _candidate_labels(record: dict) -> dict[str, str]:
    from .staged import config_id

    labels = {}
    for name, params in (record.get("candidates") or {}).items():
        try:
            labels[config_id(params)] = name
        except Exception:  # noqa: BLE001 - a label only
            continue
    if record.get("kind") == "v3_feature" and record.get("parameters"):
        try:
            labels[config_id(record["parameters"])] = (
                f"{record.get('candidate')} + {record.get('name')}")
        except Exception:  # noqa: BLE001
            pass
    return labels


def load_batch(path: Path | str) -> Dataset:
    """A batch experiment: v3 verification / feature run (``experiment.json`` + scenario
    folders) or a case run (``case_runs``). Every row keeps its exact parameters,
    scenario, execution contract and reference-availability policy."""
    path = Path(path)
    dirs = _batch_dirs(path)
    if not dirs:
        raise FileNotFoundError(f"no summaries.parquet under {path}")
    watched = [d / "summaries.parquet" for d in dirs]
    cache_key = (str(path), sum(f.stat().st_mtime for f in watched))
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    exp_file = path / "experiment.json"
    record = json.loads(exp_file.read_text(encoding="utf-8")) if exp_file.exists() else {}
    fallback = json.dumps(_record_policy(record), sort_keys=True) if record else None
    frames = []
    for folder in dirs:
        frame = pd.read_parquet(folder / "summaries.parquet")
        frame["bundle_dir"] = str(folder)
        if "execution_policy" not in frame:
            frame["execution_policy"] = fallback
        elif fallback is not None:
            empty = frame["execution_policy"].isna() | frame["execution_policy"].isin(["", "{}"])
            frame.loc[empty, "execution_policy"] = fallback
        frames.append(frame)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        frame = pd.concat(frames, ignore_index=True)
    labels = _candidate_labels(record)
    case_run = not record and "tag" in frame
    if case_run:
        for (cfg_hash, tag) in frame[["config_hash", "tag"]].drop_duplicates().itertuples(
                index=False):
            parts = str(tag).split("--")
            if len(parts) == 3:
                labels[str(cfg_hash)] = f"{parts[1]} · {parts[2]}"
    policies = sorted(set(frame["execution_policy"].dropna()))
    kind = record.get("kind", "case_run" if case_run else "batch")
    spec = DatasetSpec(
        key=_relative_key(BATCH_PREFIX, path),
        title=f"{kind.replace('_', ' ')} {path.name}",
        path=path,
        scope=(f"{kind.replace('_', ' ')}: {frame['market_id'].nunique()} windows, "
               f"{frame['config_hash'].nunique()} configurations, scenarios "
               f"{', '.join(sorted(frame['scenario_name'].unique()))}. Execution policy: "
               + ("; ".join(policies) if policies else "replay defaults") + "."),
        limitations=(
            "Every timeline (saved or regenerated) uses this run's exact parameters, "
            "scenario and execution policy; a regenerated one is compared with the saved "
            "decision digest.",
            *(("Case runs vary strategy settings (variance time policy, disagreement "
               "veto) on deliberately selected development cases; not a parameter search "
               "and not evaluation evidence.",) if case_run else ()),
        ),
        traces=("Saved per-decision timelines exist for this run"
                if any((d / "decisions.parquet").exists() for d in dirs)
                else "Only summary rows were saved; timelines must be regenerated")
        + (" and indicator explanations (recording level explain)."
           if (path / "explanations.parquet").exists() else "."),
    )
    dataset = prepare(frame, spec, labels=labels)
    dataset.kind, dataset.root = "batch", path
    _CACHE[cache_key] = dataset
    return dataset


STUDY_INDEX = REPO / "var" / "research" / "case_runs" / "study_index"


def load_study(path: Path | str) -> Dataset:
    """Preserved notebook 06 study bundles (read only; indexed once into a side cache).

    Saved traces are the study's own decisions. A regenerated timeline replays the
    variant as strategy settings on the production path (``case_runs.variant_overlays``),
    which the migration check proved trading-equal (entries, fills, economics);
    decision labels of the veto variant differ (study vetoed every evaluation)."""
    from tyrex_pm.research.strategy_replay.results import decision_digest

    from .batch import _reason_counts, execution_events
    from .case_runs import variant_overlays
    from .experiments import label as params_label
    from .experiments import merge
    from .staged import config_id
    from .v3 import frozen_windows, load_candidates

    path = Path(path)
    exp_file = path / "experiment.json"
    record = json.loads(exp_file.read_text(encoding="utf-8"))
    bundles = sorted(path.glob("btc-*.json"))
    stamp = hashlib.sha256(json.dumps(
        [exp_file.stat().st_mtime, [(b.name, b.stat().st_size) for b in bundles]]
    ).encode()).hexdigest()[:12]
    cache_key = (str(path), stamp)
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    index = STUDY_INDEX / f"{path.name}-{stamp}.parquet"
    if index.exists():
        frame = pd.read_parquet(index)
    else:
        candidates = load_candidates()
        variants = record.get("variants") or {}
        threshold = float((variants.get("time_corrected_gap_veto") or {}).get(
            "disagreement_pp", 9.0))
        overlays = variant_overlays(threshold)
        windows = frozen_windows().set_index("slug")
        policy = {**{k: v for k, v in (record.get("execution") or {}).items()
                     if k == "reference_availability"},
                  "execution_contract": "replay_execution_contract_v1"}
        rows = []
        for bundle in bundles:
            result = json.loads(bundle.read_text(encoding="utf-8"))
            study = result["case_study"]
            params = merge(candidates[study["candidate"]].parameters, overlays[study["variant"]])
            slug = result["window"]["slug"]
            rows.append({
                **result["case_summary"],
                "collection": str(windows.loc[slug].collection),
                "market_id": result["window"]["market_id"],
                "window_start_ns": result["window"]["window_start_ns"],
                "config": params_label(params), "config_hash": config_id(params),
                "scenario_name": result["scenario_name"], "tag": bundle.stem,
                "candidate": study["candidate"], "variant": study["variant"],
                "digest": decision_digest(result),
                "execution_events": json.dumps(execution_events(result)),
                "entry_reason_counts": json.dumps(_reason_counts(result)),
                "execution_policy": json.dumps(policy, sort_keys=True),
                "bundle_path": str(bundle), "trace_level": study.get("trace_level"),
            })
            del result
        frame = pd.DataFrame(rows)
        STUDY_INDEX.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(index, index=False)
    labels = {h: f"{c} · {v} (study)" for h, c, v in
              frame[["config_hash", "candidate", "variant"]].drop_duplicates().itertuples(
                  index=False)}
    spec = DatasetSpec(
        key=_relative_key(STUDY_PREFIX, path),
        title=f"Notebook 06 study {path.name} (preserved)",
        path=path,
        scope=(f"Preserved notebook 06 study: {frame['market_id'].nunique()} deliberately "
               f"selected development cases × {frame['candidate'].nunique()} candidates × "
               f"{frame['variant'].nunique()} variants, scenario "
               f"{', '.join(sorted(frame['scenario_name'].unique()))}, "
               f"{(record.get('execution') or {}).get('reference_availability')}, "
               "execution contract v1."),
        limitations=(
            "Recorded with the former study hooks (removed after the migration check). "
            "Saved timelines show the study's decision labels: its veto marked every "
            "evaluation over the gap BLOCKED, also outside the entry band.",
            "A regenerated timeline replays the variant as strategy settings on the "
            "production path: trading-equal by the migration check, decision labels may "
            "differ; its digest is therefore compared and reported.",
            "Development cases, not evaluation evidence.",
        ),
        traces="Saved per-decision study traces exist for every run.",
    )
    dataset = prepare(frame, spec, labels=labels)
    dataset.kind, dataset.root = "study", path
    _CACHE[cache_key] = dataset
    return dataset


def prepare(frame: pd.DataFrame, spec: DatasetSpec, labels: dict | None = None) -> Dataset:
    runs = frame.copy()
    if "config_hash" in runs and runs["config_hash"].notna().all():
        runs["config_id"] = runs["config_hash"].astype(str)
    else:
        runs["config_id"] = runs["config"].map(lambda c: hashlib.sha256(c.encode()).hexdigest())
    pairs = runs[["config_id", "config"]].drop_duplicates()
    if pairs["config_id"].duplicated().any():
        raise ValueError("one config_hash maps to several saved configurations")
    # The same saved overrides under two hashes stay two configurations (labels get a
    # hash suffix below); grouping always uses config_id.
    for column in ("entered", "filled"):
        runs[column] = runs[column].fillna(False).astype(bool)
    for column in (
        "realized_pnl",
        "debit",
        "ev_point",
        "ev_conservative",
        "p_conservative",
        "entry_tau_s",
        "limit_price",
        "vwap",
        "fill_ratio",
    ):
        if column in runs:
            runs[column] = pd.to_numeric(runs[column], errors="coerce")
    runs["status"] = window_status(runs)
    caps = {
        c: _num(flatten(json.loads(c)).get("sizing.max_debit_usdc"))
        for c in runs["config"].unique()
    }
    runs["budget_cap"] = runs["config"].map(lambda c: float(caps[c]) if caps[c] else math.nan)
    runs["over_budget"] = runs["filled"] & (runs["debit"] > runs["budget_cap"] + 1e-9)
    start = pd.to_datetime(runs["window_start_ns"], unit="ns", utc=True)
    runs["window_utc"] = start
    runs["window_label"] = start.dt.strftime("%m-%d %H:%M")

    given = labels or {}
    labels = {
        cid: given.get(cid) or config_label(json.loads(cfg))
        for cid, cfg in pairs.itertuples(index=False)
    }
    configs = pairs.assign(
        label=pairs["config_id"].map(labels),
        overrides=pairs["config"].map(lambda c: flatten(json.loads(c))),
    )
    clash = configs["label"].duplicated(keep=False)
    configs.loc[clash, "label"] = configs.loc[clash].apply(
        lambda r: f"{r['label']} [{r['config_id'][:8]}]", axis=1
    )
    configs = configs.sort_values("label").reset_index(drop=True)
    runs["label"] = runs["config_id"].map(dict(zip(configs["config_id"], configs["label"])))
    return Dataset(spec=spec, runs=runs, configs=configs)


def window_status(runs: pd.DataFrame) -> pd.Series:
    pnl = runs["realized_pnl"]
    status = pd.Series("skipped", index=runs.index)
    status[runs["entered"] & ~runs["filled"]] = "unfilled"
    filled = runs["filled"]
    status[filled & pnl.isna()] = "unresolved"
    status[filled & (pnl > 0)] = "win"
    status[filled & (pnl < 0)] = "loss"
    status[filled & (pnl == 0)] = "breakeven"
    return status


# --------------------------------------------------------------------------- metrics


def _metrics(group: pd.DataFrame) -> dict:
    filled = group[group["filled"]]
    resolved = filled[filled["realized_pnl"].notna()]
    pnl = resolved["realized_pnl"]
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    entries = group[group["entered"]]
    spend_resolved = float(resolved["debit"].sum())
    if len(filled) == 0:
        net = 0.0  # no trade: nothing gained or lost
    elif len(resolved) == 0:
        net = math.nan  # trades exist but their outcome is unknown
    else:
        net = float(pnl.sum())
    n_res = len(resolved)
    return {
        "windows": len(group),
        "entry_requests": int(group["entered"].sum()),
        "fills": len(filled),
        "unfilled": int((group["entered"] & ~group["filled"]).sum()),
        "skipped": int((~group["entered"]).sum()),
        "resolved_fills": n_res,
        "unresolved_fills": len(filled) - n_res,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": int((pnl == 0).sum()),
        "win_rate": len(wins) / n_res if n_res else math.nan,
        "spending": float(filled["debit"].sum()),
        "spending_resolved": spend_resolved,
        "net_pnl": net,
        "avg_pnl_per_fill": float(pnl.mean()) if n_res else math.nan,
        "return_on_spending": float(pnl.sum()) / spend_resolved if spend_resolved else math.nan,
        "avg_win": float(wins.mean()) if len(wins) else math.nan,
        "avg_loss": float(losses.mean()) if len(losses) else math.nan,
        "largest_loss": float(losses.min()) if len(losses) else math.nan,
        "over_budget": int(group["over_budget"].sum()) if "over_budget" in group else 0,
        "budget_cap": float(group["budget_cap"].max()) if "budget_cap" in group else math.nan,
        "max_spend": float(filled["debit"].max()) if len(filled) else math.nan,
        "mean_ev_point": float(entries["ev_point"].mean()) if len(entries) else math.nan,
        "mean_ev_conservative": float(entries["ev_conservative"].mean())
        if len(entries)
        else math.nan,
        "mean_p_conservative": float(entries["p_conservative"].mean())
        if len(entries)
        else math.nan,
    }


def summarize(runs: pd.DataFrame) -> pd.DataFrame:
    """One row per (configuration, scenario), grouped by full configuration identity.

    * win rate = profitable fills ÷ fills with a known realized P&L;
    * skipped windows and unfilled requests are counted, never treated as losses;
    * a fill without a realized P&L is ``unresolved_fills`` and excluded from P&L sums
      (not counted as zero); ``net_pnl`` is NaN when no fill is resolved;
    * return on spending = net P&L ÷ spending on resolved fills (not annualized, not
      a return on the account);
    * mean EV and probability are averages over entry requests, recorded for the
      intended purchase at the decision.
    """
    rows = []
    for (config_id, scenario), group in runs.groupby(["config_id", "scenario_name"], sort=False):
        record = {
            "config_id": config_id,
            "scenario_name": scenario,
            "label": group["label"].iloc[0] if "label" in group else config_id,
        }
        record.update(_metrics(group))
        rows.append(record)
    return pd.DataFrame(rows)


def comparison(dataset: Dataset, scenario: str, min_fills: int = 0) -> pd.DataFrame:
    table = summarize(dataset.runs[dataset.runs["scenario_name"] == scenario])
    if table.empty:
        return table
    return table[table["resolved_fills"] >= min_fills].reset_index(drop=True)


def windows_for(dataset: Dataset, config_id: str, scenario: str) -> pd.DataFrame:
    runs = dataset.runs
    rows = runs[(runs["config_id"] == config_id) & (runs["scenario_name"] == scenario)]
    return rows.sort_values("window_start_ns").reset_index(drop=True)


# --------------------------------------------------------------------------- tables


def _usd(value: float) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"−${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def _pct(value: float) -> str:
    return "n/a" if value is None or math.isnan(value) else f"{value:.0%}"


def _num_text(value, digits=3) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value:.{digits}f}"


def summary_table(metrics: dict) -> pd.DataFrame:
    """Selected configuration + scenario as a readable Metric / Value / Meaning table."""
    m = metrics
    rows = [
        ("Windows tested", m["windows"], "Market windows replayed with this configuration."),
        ("Entry requests", m["entry_requests"], "Windows where the strategy sent an order."),
        ("Filled trades", m["fills"], "Entry requests with at least some fill."),
        ("Unfilled entry requests", m["unfilled"], "Order sent, nothing filled. Not a loss."),
        ("Skipped windows", m["skipped"], "No order sent. Not a loss."),
        ("Fills with unknown P&L", m["unresolved_fills"], "Excluded from P&L; not zero."),
        (
            "Profitable / losing / break-even",
            f"{m['wins']} / {m['losses']} / {m['breakeven']}",
            "Among fills with known P&L.",
        ),
        ("Win rate", _pct(m["win_rate"]), "Profitable ÷ fills with known P&L."),
        ("Acquisition spending", _usd(m["spending"]), "Sum of filled quantity × fill price."),
        (
            "Fills spending above the configured cap",
            "cap not recorded"
            if math.isnan(m.get("budget_cap", math.nan))
            else f"{m['over_budget']} (largest {_usd(m['max_spend'])} vs cap "
            f"{_usd(m['budget_cap'])})",
            "Sized at walked prices; adverse ticks and the limit buffer can fill above.",
        ),
        ("Total simulated net P&L", _usd(m["net_pnl"]), "After fees, proxy outcome labels."),
        (
            "Net P&L ÷ spending",
            _pct(m["return_on_spending"]),
            "On resolved fills. Not annualized; not a return on the account.",
        ),
        ("Average win", _usd(m["avg_win"]), "Mean P&L of profitable fills."),
        ("Average loss", _usd(m["avg_loss"]), "Mean P&L of losing fills."),
        (
            "Mean expected profit at entry (point)",
            _usd(m["mean_ev_point"]),
            "Model estimate for the intended purchase, per entry request.",
        ),
        (
            "Mean expected profit at entry (conservative)",
            _usd(m["mean_ev_conservative"]),
            "Same, using the conservative probability bound.",
        ),
        (
            "Mean conservative entry probability",
            _num_text(m["mean_p_conservative"]),
            "Model estimate for the chosen side, not a calibrated win rate.",
        ),
    ]
    return pd.DataFrame(rows, columns=["Metric", "Value", "Meaning"]).set_index("Metric")


def window_detail(row: pd.Series) -> tuple[pd.DataFrame, pd.Series]:
    """Fields for one window, plus its saved decision-reason counts."""

    def val(column, fmt=lambda v: v):
        v = row.get(column)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return "not recorded"
        return fmt(v)

    fields = [
        ("Window (UTC start)", row["window_utc"].strftime("%Y-%m-%d %H:%M")),
        ("Market", row.get("slug") or row["market_id"]),
        ("Market id", row["market_id"]),
        ("Result", STATUS_TEXT[row["status"]]),
        ("Selected side", val("side")),
        ("Entry decision, seconds remaining", val("entry_tau_s", lambda v: f"{v:.0f} s")),
        ("Conservative probability (model estimate)", val("p_conservative", lambda v: f"{v:.3f}")),
        ("Book-implied UP at entry", val("book_implied_up_at_entry", lambda v: f"{v:.3f}")),
        ("Limit price", val("limit_price", lambda v: f"{v:.3f}")),
        ("Average fill price", val("vwap", lambda v: f"{v:.4f}")),
        ("Fill ratio (filled ÷ intended shares)", val("fill_ratio", lambda v: f"{v:.0%}")),
        ("Acquisition spending", val("debit", _usd) if row["filled"] else "nothing filled"),
        ("Expected profit at decision (point, intended)", val("ev_point", _usd)),
        ("Expected profit at decision (conservative, intended)", val("ev_conservative", _usd)),
        ("Proxy winner", val("winner")),
        ("Realized simulated P&L", val("realized_pnl", _usd) if row["filled"] else "no trade"),
        ("Recorded limitations", row.get("limitations") or "none recorded"),
    ]
    reasons = pd.Series(dtype=int, name="evaluations")
    raw = row.get("entry_reason_counts")
    if isinstance(raw, str) and raw:
        reasons = pd.Series(json.loads(raw), name="evaluations").sort_values(ascending=False)
    reasons.index.name = "decision reason"
    return pd.DataFrame(fields, columns=["Field", "Value"]).set_index("Field"), reasons


def scope_html(dataset: Dataset) -> str:
    c = dataset.counts
    per_cell = c["runs_per_cell"]
    cell = (
        f"{per_cell.min()}"
        if per_cell.min() == per_cell.max()
        else f"{per_cell.min()}–{per_cell.max()}"
    )
    items = "".join(f"<li>{t}</li>" for t in (*dataset.spec.limitations, *COMMON_LIMITATIONS))
    return (
        f"<div style='border-left:4px solid {MUTED};padding:6px 12px;margin:4px 0'>"
        f"<b>{dataset.spec.title}</b><br>{dataset.spec.scope}<br>"
        + (
            f"<b>{c['configurations']}</b> configurations × <b>{c['scenarios']}</b> scenarios "
            f"× <b>{c['windows']}</b> windows = <b>{c['runs']}</b> runs "
            f"({cell} windows per configuration and scenario; {c['collections']} collections)."
            if dataset.stages is None
            else f"<b>{c['runs']}</b> runs loaded on <b>{c['windows']}</b> windows "
            f"({cell} windows per configuration and scenario). Stages:"
            + dataset.stages.to_html(index=False, border=0)
        )
        + f"<i>One configuration tested across many windows and two scenarios is still one "
        f"configuration. Its runs share the same market history, so they are not "
        f"independent new market outcomes.</i>"
        f"<br><b>Limitations</b><ul style='margin:2px 0'>{items}</ul>"
        f"{dataset.spec.traces}</div>"
    )


def overrides_table(dataset: Dataset, config_id: str) -> pd.DataFrame:
    """Saved overrides; for staged experiments (full settings saved) only those that vary."""
    row = dataset.configs[dataset.configs["config_id"] == config_id].iloc[0]
    items = row["overrides"]
    if dataset.stages is not None:
        all_keys = {k for o in dataset.configs["overrides"] for k in o}
        varying = {
            k
            for k in all_keys
            if len({json.dumps(o.get(k), default=str) for o in dataset.configs["overrides"]}) > 1
        }
        items = {k: v for k, v in items.items() if k in varying}
    table = pd.DataFrame(sorted(items.items()), columns=["Saved override", "Value"])
    return table.set_index("Saved override")


# --------------------------------------------------------------------------- charts


def _style(ax) -> None:
    ax.grid(True, axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=8)


def _metric_text(metric: str, value: float) -> str:
    if value is None or math.isnan(value):
        return "n/a"
    if metric in ("win_rate", "return_on_spending"):
        return f"{value:.0%}"
    return _usd(value)


def plot_comparison(
    table: pd.DataFrame, metric: str = "net_pnl", *, selected: str | None = None, title: str = ""
):
    """Horizontal bars, one per configuration, with fills printed beside each bar."""
    if table.empty:
        return None
    # Configurations without resolved fills sit at the bottom; the best value at the top.
    data = (
        table.assign(_has=table["resolved_fills"] > 0)
        .sort_values(["_has", metric], na_position="first")
        .reset_index(drop=True)
    )
    height = max(3.2, 0.42 * len(data) + 1.4)
    fig, ax = plt.subplots(figsize=(11, height))
    y = np.arange(len(data))
    values = data[metric].to_numpy(dtype=float)
    for i, (value, n) in enumerate(zip(values, data["resolved_fills"])):
        if n == 0 or math.isnan(value):
            continue  # no bar; the note column says "no trades"
        small = n < SMALL_SAMPLE
        color = BLUE if value > 0 else ORANGE if value < 0 else NEUTRAL
        ax.barh(
            i,
            value,
            height=0.62,
            color=color,
            alpha=0.45 if small else 1.0,
            hatch="//" if value < 0 else None,
            edgecolor="white",
            linewidth=0.8,
        )
    ax.axvline(0, color=INK, linewidth=1)
    labels = [
        "▶ " + lab if cid == selected else lab for lab, cid in zip(data["label"], data["config_id"])
    ]
    ax.set_yticks(y, labels, fontsize=8)
    for tick, cid in zip(ax.get_yticklabels(), data["config_id"]):
        if cid == selected:
            tick.set_fontweight("bold")
            tick.set_color(INK)
    for i, row in data.iterrows():
        shown = _metric_text(metric, row[metric]) if row["resolved_fills"] else "—"
        note = f"{shown}   {row['resolved_fills']} fills"
        if row["unresolved_fills"]:
            note += f" (+{row['unresolved_fills']} unknown)"
        if row["resolved_fills"] == 0:
            note += " · no trades to compare"
        elif row["resolved_fills"] < SMALL_SAMPLE:
            note += " · small sample"
        ax.annotate(
            note,
            (1.01, i),
            xycoords=("axes fraction", "data"),
            va="center",
            fontsize=8,
            color=MUTED,
            annotation_clip=False,
        )
    if metric == "win_rate":
        ax.set_xlim(0, 1)
        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    elif metric == "return_on_spending":
        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    lo, hi = np.nanmin(np.append(values, 0)), np.nanmax(np.append(values, 0))
    if metric != "win_rate":
        pad = 0.08 * max(hi - lo, 1e-9)
        ax.set_xlim(lo - pad, hi + pad)
    ax.set_ylim(-0.7, len(data) - 0.3)
    ax.set_xlabel(METRICS[metric], fontsize=9, color=MUTED)
    ax.set_title(title, loc="left", fontsize=10, color=INK)
    _style(ax)
    fig.subplots_adjust(left=0.36, right=0.72, top=1 - 0.45 / height, bottom=0.8 / height)
    return fig


def plot_windows(windows: pd.DataFrame, *, selected: str | None = None, title: str = ""):
    """One chronological bar per window; symbols for skipped / unfilled / unknown."""
    if windows.empty:
        return None
    fig, ax = plt.subplots(figsize=(11, 4.2))
    x = np.arange(len(windows))
    pnl = windows["realized_pnl"].to_numpy(dtype=float)
    status = windows["status"].to_numpy()
    span = np.nanmax(np.abs(pnl)) if np.isfinite(pnl).any() else 1.0
    span = span or 1.0
    for i, s in enumerate(status):
        if s == "win":
            ax.bar(i, pnl[i], width=0.7, color=BLUE, edgecolor="white")
        elif s == "loss":
            ax.bar(i, pnl[i], width=0.7, color=ORANGE, hatch="//", edgecolor="white")
        elif s == "breakeven":
            ax.plot(i, 0, marker="D", color=NEUTRAL, ms=7)
    marks = {
        "skipped": dict(marker="o", mfc="white", mec=NEUTRAL, ms=8, ls=""),
        "unfilled": dict(marker="x", color=INK, ms=8, mew=1.6, ls=""),
    }
    for s, style in marks.items():
        idx = x[status == s]
        if len(idx):
            ax.plot(idx, np.zeros(len(idx)), **style)
    for i in x[status == "unresolved"]:
        ax.text(i, 0, "?", ha="center", va="center", fontsize=12, fontweight="bold", color=INK)
    if selected is not None and (windows["market_id"] == selected).any():
        i = int(np.flatnonzero(windows["market_id"].to_numpy() == selected)[0])
        ax.axvspan(i - 0.5, i + 0.5, color=GRID, zorder=0)
    ax.axhline(0, color=INK, linewidth=1)
    ax.set_xticks(x, windows["window_label"], rotation=90, fontsize=7)
    ax.set_xlim(-0.8, len(windows) - 0.2)
    ax.set_ylim(-1.15 * span, 1.15 * span)
    ax.set_ylabel("Simulated net P&L (USDC)", fontsize=9, color=MUTED)
    ax.set_xlabel(
        "Window start (UTC, month-day hour:minute), chronological", fontsize=9, color=MUTED
    )
    ax.set_title(title, loc="left", fontsize=10, color=INK)
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    counts = windows["status"].value_counts()
    handles = [
        Patch(facecolor=BLUE, label=f"profitable fill ({counts.get('win', 0)})"),
        Patch(
            facecolor=ORANGE,
            hatch="//",
            edgecolor="white",
            label=f"losing fill ({counts.get('loss', 0)})",
        ),
        Line2D(
            [],
            [],
            marker="D",
            color=NEUTRAL,
            ls="",
            label=f"break-even ({counts.get('breakeven', 0)})",
        ),
        Line2D(
            [], [], **marks["unfilled"], label=f"× unfilled request ({counts.get('unfilled', 0)})"
        ),
        Line2D(
            [], [], **marks["skipped"], label=f"○ skipped, no request ({counts.get('skipped', 0)})"
        ),
        Line2D(
            [],
            [],
            marker="$?$",
            color=INK,
            ls="",
            ms=9,
            label=f"? fill, outcome unknown ({counts.get('unresolved', 0)})",
        ),
    ]
    ax.legend(
        handles=handles, fontsize=8, frameon=False, loc="upper left", bbox_to_anchor=(1.0, 1.0)
    )
    ax.grid(True, axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.tick_params(colors=MUTED)
    fig.subplots_adjust(left=0.07, right=0.76, bottom=0.3)
    return fig


def plot_distribution(windows: pd.DataFrame, *, title: str = ""):
    """Histogram of realized P&L per resolved fill; dots when the sample is small."""
    pnl = windows.loc[windows["filled"], "realized_pnl"].dropna().to_numpy(dtype=float)
    if len(pnl) == 0:
        return None
    fig, ax = plt.subplots(figsize=(9, 3.4))
    rng = np.random.default_rng(0)
    if len(pnl) < DOT_PLOT_BELOW:
        jitter = rng.uniform(-0.25, 0.25, len(pnl))
        colors = [BLUE if v > 0 else ORANGE if v < 0 else NEUTRAL for v in pnl]
        ax.scatter(pnl, jitter, s=60, c=colors, edgecolors="white", linewidths=1.5, zorder=3)
        ax.set_ylim(-1, 1)
        ax.set_yticks([])
        ax.set_ylabel("one dot = one trade", fontsize=9, color=MUTED)
        mode = "individual trades (too few for a histogram)"
    else:
        # Zero is always a bin edge, so no bar mixes wins and losses.
        width = max(pnl.max() - min(pnl.min(), 0), pnl.max() - pnl.min(), 1e-9) / 16
        bins = width * np.arange(math.floor(pnl.min() / width), math.ceil(pnl.max() / width) + 1)
        if len(bins) < 2:
            bins = np.array([bins[0] - width, bins[0] + width])
        counts, edges, patches = ax.hist(pnl, bins=bins, edgecolor="white", linewidth=1)
        for patch, left in zip(patches, edges[:-1]):
            centre = left + (edges[1] - edges[0]) / 2
            patch.set_facecolor(BLUE if centre > 0 else ORANGE)
            if centre < 0:
                patch.set_hatch("//")
        ax.plot(pnl, np.full(len(pnl), -0.04 * max(counts.max(), 1)), "|", color=INK, ms=10)
        ax.set_ylabel("Number of trades", fontsize=9, color=MUTED)
        mode = "histogram, ticks below = individual trades"
    ax.axvline(0, color=INK, linewidth=1)
    ax.set_xlabel("Realized net P&L per filled trade (USDC, proxy labels)", fontsize=9, color=MUTED)
    ax.set_title(
        f"{title}\n{len(pnl)} resolved filled trades · {mode}", loc="left", fontsize=10, color=INK
    )
    ax.grid(True, axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=8)
    fig.tight_layout()
    return fig


def distribution_note(metrics: dict) -> str:
    """One plain sentence on the win/loss shape, or why there is none."""
    n = metrics["resolved_fills"]
    if n == 0:
        return "No resolved filled trades: there is no distribution to describe."
    parts = [
        f"{metrics['wins']} of {n} resolved trades were profitable ({_pct(metrics['win_rate'])})."
    ]
    if metrics["wins"] and metrics["losses"]:
        ratio = abs(metrics["avg_loss"]) / metrics["avg_win"] if metrics["avg_win"] else math.nan
        parts.append(
            f"Average win {_usd(metrics['avg_win'])}, average loss "
            f"{_usd(metrics['avg_loss'])}: one average loss erases about "
            f"{ratio:.1f} average wins."
        )
        parts.append(f"Largest single loss: {_usd(metrics['largest_loss'])}.")
    if n < SMALL_SAMPLE:
        parts.append(f"With only {n} trades, one more win or loss can flip the total.")
    return " ".join(parts)


# --------------------------------------------------------------------------- widget app


@dataclass(frozen=True)
class _Html:
    value: str


def _part_widget(part):
    """A plain widget for one piece of section content (text, HTML, table or figure)."""
    import html as html_lib
    import io

    import ipywidgets as w

    if isinstance(part, str):
        return w.HTML(
            "<pre style='white-space:pre-wrap;margin:2px 0;font-size:12px'>"
            f"{html_lib.escape(part)}</pre>"
        )
    if isinstance(part, _Html):
        return w.HTML(part.value)
    if isinstance(part, plt.Figure):
        buffer = io.BytesIO()
        part.savefig(buffer, format="png", dpi=100, bbox_inches="tight")
        plt.close(part)  # rendered here once; never shown again by the inline backend
        return w.Image(
            value=buffer.getvalue(), format="png", layout=w.Layout(max_width="100%", height="auto")
        )
    if hasattr(part, "_repr_html_"):
        return w.HTML(part._repr_html_())
    return w.HTML(f"<pre>{html_lib.escape(repr(part))}</pre>")


class Explorer:
    """ipywidgets front end; every control filters cached data and redraws."""

    def __init__(
        self, dataset: str = "validation_seed", scenario: str = "friction", metric: str = "net_pnl"
    ):
        import ipywidgets as w
        from IPython.display import display

        self._display = display
        self.w_dataset = w.Dropdown(
            options=available_datasets(),
            value=dataset,
            description="Dataset",
            layout=w.Layout(width="560px"),
            style={"description_width": "90px"},
        )
        self.w_scenario = w.ToggleButtons(
            options=["favorable", "friction"],
            value=scenario,
            description="Scenario",
            style={"description_width": "90px"},
        )
        self.w_metric = w.Dropdown(
            options=[(v, k) for k, v in METRICS.items()],
            value=metric,
            description="Metric",
            layout=w.Layout(width="560px"),
            style={"description_width": "90px"},
        )
        self.w_min = w.IntSlider(
            value=0, min=0, max=20, description="Min fills", style={"description_width": "90px"}
        )
        self.w_config = w.Dropdown(
            description="Configuration",
            layout=w.Layout(width="760px"),
            style={"description_width": "90px"},
        )
        self.w_window = w.Dropdown(
            description="Window",
            layout=w.Layout(width="760px"),
            style={"description_width": "90px"},
        )
        # Section containers: content is replaced via ``children`` (see ``_show``).
        self.out_scope, self.out_compare, self.out_summary = w.VBox(), w.VBox(), w.VBox()
        self.out_windows, self.out_detail, self.out_dist = w.VBox(), w.VBox(), w.VBox()
        self.out_timeline = w.VBox()
        self.out_explain = w.VBox()
        self.w_warmup = w.Checkbox(value=False, description="Show 5-minute warm-up before open")
        self.w_strict = w.Checkbox(
            value=False, description="Strict lines: break at every skipped second"
        )
        self._timeline_count = (0, 0)
        # The only control that can start a replay, and only when clicked.
        self.w_generate = w.Button(
            description="Generate timeline for this selected run",
            layout=w.Layout(width="340px", display="none"),
        )
        guide = w.Accordion(children=[w.HTML(timelines.READING_GUIDE)])
        guide.set_title(0, "How to read this chart")
        guide.selected_index = None
        self.dataset: Dataset | None = None
        self._busy = False
        self._traces: dict[tuple, timelines.Trace | None] = {}
        self._checks: dict[tuple, tuple] = {}

        self.w_dataset.observe(lambda _: self._on_dataset(), "value")
        for widget in (self.w_scenario, self.w_metric, self.w_min):
            widget.observe(lambda _: self._on_config(), "value")
        self.w_config.observe(lambda _: self._on_config(), "value")
        self.w_window.observe(lambda _: self._render_detail(), "value")
        self.w_warmup.observe(lambda _: self._render_timeline(), "value")
        self.w_strict.observe(lambda _: self._render_timeline(), "value")
        self.w_generate.on_click(lambda _: self._on_generate())

        def head(text):
            return w.HTML(f"<h3 style='margin:14px 0 2px'>{text}</h3>")

        self.view = w.VBox(
            [
                head("1. Dataset"),
                self.w_dataset,
                self.out_scope,
                head("2. Execution scenario"),
                self.w_scenario,
                head("3. Compare configurations"),
                w.HBox([self.w_metric, self.w_min]),
                self.out_compare,
                head("4. Selected configuration"),
                self.w_config,
                self.out_summary,
                head("5. Its windows"),
                self.out_windows,
                self.w_window,
                self.out_detail,
                head("6. Model probability vs book in the selected window"),
                guide,
                w.HBox([self.w_warmup, self.w_strict]),
                self.w_generate,
                self.out_timeline,
                head("7. Indicator state behind its decisions (recorded explanations)"),
                self.out_explain,
                head("8. Its profit/loss distribution"),
                self.out_dist,
            ]
        )
        self._on_dataset()

    # -- state changes
    def _on_dataset(self) -> None:
        self._busy = True
        try:
            self.dataset = load_dataset(self.w_dataset.value)
        except FileNotFoundError as exc:
            self.dataset = None
            self._busy = False
            self._show(self.out_scope, html=f"<b style='color:{ORANGE}'>Unavailable:</b> {exc}")
            self.w_generate.layout.display = "none"
            for out in (
                self.out_compare,
                self.out_summary,
                self.out_windows,
                self.out_detail,
                self.out_timeline,
                self.out_explain,
                self.out_dist,
            ):
                out.children = ()
            return
        scenarios = self.dataset.scenarios
        keep = self.w_scenario.value
        self.w_scenario.options = scenarios
        self.w_scenario.value = keep if keep in scenarios else scenarios[0]
        configs = self.dataset.configs
        self.w_config.options = list(zip(configs["label"], configs["config_id"]))
        self.w_config.value = configs["config_id"].iloc[0]
        self._show(self.out_scope, html=scope_html(self.dataset))
        self._busy = False
        self._on_config()

    def _on_config(self) -> None:
        if self._busy or self.dataset is None:
            return
        self._busy = True
        rows = windows_for(self.dataset, self.w_config.value, self.w_scenario.value)
        options = self._window_options(rows)
        current = self.w_window.value
        self.w_window.options = options
        first_trade = rows.loc[rows["filled"], "market_id"]
        ids = [o[1] for o in options]
        if current in ids:
            self.w_window.value = current
        elif len(first_trade):
            self.w_window.value = first_trade.iloc[0]
        elif ids:
            self.w_window.value = ids[0]
        self._busy = False
        self.render()

    def _window_options(self, rows: pd.DataFrame) -> list[tuple[str, str]]:
        """Dropdown labels; windows whose probability timeline is ready are marked."""
        try:
            ready = timelines.available_windows(self.dataset, rows)
        except Exception:  # noqa: BLE001 - a marker only; never block the dropdown
            ready = set()
        self._timeline_count = (len(ready), len(rows))
        return [
            (
                f"{r.window_label}  ·  {STATUS_TEXT[r.status]}"
                + ("  ·  📈 timeline" if r.market_id in ready else ""),
                r.market_id,
            )
            for r in rows.itertuples()
        ]

    # -- drawing
    def _show(self, out, *, fig=None, html=None, obj=None, text=None, more=()) -> None:
        """Replace a section's whole content in one assignment.

        Sections are plain ``VBox`` containers, not ``Output`` widgets: VS Code did not
        reliably replace an Output widget's content (neither ``clear_output(wait=True)``
        nor assigning ``outputs``), so redraws piled up. Replacing a container's
        ``children`` is the most basic widget update and renders the same everywhere.
        """
        parts: list = []
        if html is not None:
            parts.append(_Html(html))
        if text is not None:
            parts.append(text)
        if obj is not None:
            parts.append(obj)
        if fig is not None:
            parts.append(fig)
        parts.extend(more)
        out.children = tuple(_part_widget(part) for part in parts)

    def render(self) -> None:
        ds, scenario, cid = self.dataset, self.w_scenario.value, self.w_config.value
        label = ds.configs.set_index("config_id").loc[cid, "label"]
        all_rows = comparison(ds, scenario)
        table = comparison(ds, scenario, self.w_min.value)
        note = (
            f"{len(table)} of {len(all_rows)} configurations have ≥ {self.w_min.value} "
            f"resolved fills under <b>{scenario}</b>."
        )
        if cid not in set(table["config_id"]):
            note += " The selected configuration is hidden by this filter."
        fig = plot_comparison(
            table, self.w_metric.value, selected=cid, title=f"{ds.spec.title} · {scenario}"
        )
        self._show(
            self.out_compare,
            html=note,
            fig=fig,
            text=None if fig is not None else "No configuration passes this filter.",
        )
        self._render_selected(label)
        self._render_detail()

    def _render_selected(self, label: str) -> None:
        ds, scenario, cid = self.dataset, self.w_scenario.value, self.w_config.value
        rows = windows_for(ds, cid, scenario)
        if rows.empty:
            for out in (self.out_summary, self.out_windows, self.out_dist):
                self._show(out, text=f"No runs for this configuration under {scenario}.")
            return
        metrics = _metrics(rows)
        self._show(
            self.out_summary,
            html=f"<b>{label}</b> · {scenario}",
            obj=overrides_table(ds, cid),
            more=(summary_table(metrics),),
        )
        self._show(
            self.out_windows,
            fig=plot_windows(rows, selected=self.w_window.value, title=f"{label} · {scenario}"),
        )
        fig = plot_distribution(rows, title=f"{label} · {scenario}")
        self._show(self.out_dist, fig=fig, text=distribution_note(metrics))

    def _render_detail(self) -> None:
        if self._busy or self.dataset is None:
            return
        rows = windows_for(self.dataset, self.w_config.value, self.w_scenario.value)
        hit = rows[rows["market_id"] == self.w_window.value]
        if hit.empty:
            self._show(self.out_detail, text="No window selected.")
            return
        fields, reasons = window_detail(hit.iloc[0])
        self._show(
            self.out_detail,
            obj=fields,
            more=(
                "No decision-reason counts were saved for this run."
                if reasons.empty
                else reasons.to_frame(),
                self.dataset.spec.traces,
            ),
        )
        # keep the highlight on the window chart in sync
        label = self.dataset.configs.set_index("config_id").loc[self.w_config.value, "label"]
        self._show(
            self.out_windows,
            fig=plot_windows(
                rows, selected=self.w_window.value, title=f"{label} · {self.w_scenario.value}"
            ),
        )
        self._render_timeline()

    # -- probability-vs-book timeline (section 6)
    def _selected_row(self) -> pd.Series | None:
        if self.dataset is None:
            return None
        rows = windows_for(self.dataset, self.w_config.value, self.w_scenario.value)
        hit = rows[rows["market_id"] == self.w_window.value]
        return None if hit.empty else hit.iloc[0]

    def _trace_key(self, row: pd.Series) -> tuple:
        return (self.dataset.spec.key, row["config_id"], row["scenario_name"], row["market_id"])

    def _render_timeline(self) -> None:
        """Show the selected run's trace if one exists. Never replays anything."""
        if self._busy:
            return
        row = self._selected_row()
        if row is None:
            self.w_generate.layout.display = "none"
            self._show(self.out_timeline, text="No window selected.")
            self._show(self.out_explain, text="No window selected.")
            return
        self._render_explanations(row)
        key = self._trace_key(row)
        if key not in self._traces:
            self._traces[key] = timelines.get_trace(self.dataset, row)
        trace = self._traces[key]
        label = self.dataset.configs.set_index("config_id").loc[row["config_id"], "label"]
        if trace is not None:
            self.w_generate.layout.display = "none"
            fig = timelines.plot_timeline(
                trace,
                row,
                warmup=self.w_warmup.value,
                title=f"{row['window_label']} UTC · {label} · {row['scenario_name']}",
                join_gap_s=1.5 if self.w_strict.value else timelines.JOIN_GAP_S,
            )
            self._show(
                self.out_timeline,
                fig=fig,
                more=(timelines.reason_counts(trace, row).to_frame(),),
            )
            return
        if key not in self._checks:
            try:
                self._checks[key] = timelines.replay_check(self.dataset, row)
            except Exception as exc:  # noqa: BLE001 - shown, never fatal
                self._checks[key] = (None, [f"could not check replay identity: {exc}"], [])
        _, problems, notes = self._checks[key]
        ready, total = self._timeline_count
        text = (
            "<b>No timeline exists yet for this window.</b> "
            f"Only {ready} of {total} windows of this configuration and scenario have one "
            "(marked 📈 in the Window list). "
            f"{self.dataset.spec.traces} The summary alone cannot reconstruct a curve, and "
            "another configuration's trace is never used. Nothing is replayed automatically.<br>"
        )
        if problems:
            self.w_generate.layout.display = "none"
            text += (
                "<b>A faithful replay is not possible:</b><ul>"
                + "".join(f"<li>{p}</li>" for p in problems)
                + "</ul>"
            )
        else:
            self.w_generate.layout.display = None
            self.w_generate.disabled = False
            text += (
                "The button replays only <i>this window × this exact configuration × this "
                "scenario</i> and saves the timeline in a separate diagnostic cache; the "
                "experiment's summaries and P&amp;L stay untouched. Its decision digest is "
                "compared with the saved run's. <b>It takes about 30–60 s, during which the "
                "kernel is busy and the controls do not respond.</b> Afterwards the window "
                "opens instantly."
            )
            if notes:
                text += "<ul>" + "".join(f"<li>{n}</li>" for n in notes) + "</ul>"
        self._show(self.out_timeline, html=text)

    def _render_explanations(self, row: pd.Series) -> None:
        """Recorded indicator explanations of exactly this run. Never replays."""
        try:
            frame = timelines.explanations_for(self.dataset, row)
        except Exception as exc:  # noqa: BLE001 - shown, never fatal
            self._show(self.out_explain, text=f"Explanations unreadable: {exc}")
            return
        if frame.empty:
            self._show(
                self.out_explain,
                html="No indicator explanations were recorded for this run. They exist only "
                "for runs made with <code>recording: {level: explain}</code> (case runs "
                "launched from notebook 06, observation and live runs at that level).",
            )
            return
        table = timelines.explanation_table(frame)
        entries = table[table["action"].eq("ENTER")]
        note = (
            f"{frame['at_ns'].nunique()} explained evaluations (entry band plus context "
            "before entries, vetoes and data faults). Rows marked <i>context_for</i> are "
            "the evaluations preceding that actionable one. Values are copies taken at "
            "decision time, before the estimator moved on."
        )
        shown = pd.concat([entries, table]).drop_duplicates().head(60)
        self._show(self.out_explain, html=note, obj=shown)

    def _on_generate(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        self.w_generate.disabled = True
        self._show(
            self.out_timeline,
            html="<b>Replaying this one run (about 30–60 s).</b> The controls are paused until "
            "it finishes; please don't click again. Other runs are not touched.",
        )
        try:
            self._traces[self._trace_key(row)] = timelines.generate(self.dataset, row)
        except Exception as exc:  # noqa: BLE001 - shown to the reader
            self.w_generate.disabled = False
            self._show(self.out_timeline, html=f"<b>Replay failed or refused:</b> {exc}")
            return
        # Mark the window as ready in the dropdown without triggering a redraw cascade.
        self._busy = True
        rows = windows_for(self.dataset, self.w_config.value, self.w_scenario.value)
        value = self.w_window.value
        self.w_window.options = self._window_options(rows)
        self.w_window.value = value
        self._busy = False
        self._render_timeline()

    def _ipython_display_(self) -> None:
        self._display(self.view)


def dataset_key(path: Path | str) -> str:
    """Explorer dataset key for a result folder: batch experiment, case run or study."""
    path = Path(path)
    if (path / "experiment.json").exists() and any(path.glob("btc-*.json")):
        return _relative_key(STUDY_PREFIX, path)
    if path.parent == STAGED and _kind(path / "experiment.json") not in BATCH_KINDS:
        return STAGED_PREFIX + path.name
    return _relative_key(BATCH_PREFIX, path)
