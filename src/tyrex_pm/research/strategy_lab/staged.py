"""Staged settlement_value experiments (notebook 04): plan, run and record stages.

Orchestration only. Every run goes through ``batch.run_batch`` and so through the real
plugin driver; nothing here decides anything. Layout::

    var/research/strategy_runs/staged/<experiment-id>/
        experiment.json          design, frozen windows, split snapshot, fingerprints
        stage_A/ stage_B/ stage_C/
            stage.json           configurations (full effective), scenario, status, attempts
            parts/               resumable per-window part files (batch.run_batch)
            summaries.parquet    written when the stage completes
            manifest.json        batch manifest (specs, cache keys, scenarios, code, timing)

Stages: A = primary backend entry search under the primary scenario, B = one benchmark
configuration under the same scenario, C = up to two manually chosen A configurations,
identical except for the scenario. Holdout windows are refused everywhere.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from tyrex_pm.research.strategy_replay.cache import window_cache_key
from tyrex_pm.research.strategy_replay.loader import LOADER_VERSION
from tyrex_pm.research.strategy_replay.run import RUNNER_VERSION
from tyrex_pm.strategies.settlement_value.schema import (
    settlement_value_config_as_parameters,
    settlement_value_config_from_parameters,
)

from . import batch
from .batch import Collection, RunSpec
from .experiments import REPO, RESEARCH_BASE_PARAMETERS, SCENARIOS, Lab, merge

STAGED = REPO / "var" / "research" / "strategy_runs" / "staged"
SPLIT_FILE = REPO / "var" / "research" / "strategy_runs" / "s2" / "split" / "split.json"
TIMING_REFERENCE = REPO / "var" / "research" / "strategy_runs" / "s2" / "validation_seed"
STAGES = ("A", "B", "C")
MAX_FINALISTS = 2
BACKEND_NAMES = {"twap_proxy_diffusion": "TWAP", "endpoint_diffusion": "Endpoint"}
# Source trees whose edits can change a replay result. Hashed (not just git HEAD) so an
# uncommitted change is detected too.
CODE_TREES = (
    "src/tyrex_pm/strategies",
    "src/tyrex_pm/research/strategy_replay",
    "src/tyrex_pm/research/replay",
    "src/tyrex_pm/runtime",
    "src/tyrex_pm/indicators",
    "src/tyrex_pm/core",
    # v3: the replay uses the live planner, dispatch gate, outcome table, exit
    # arbitration and protection triggers; they are result-affecting sources.
    "src/tyrex_pm/execution",
    "src/tyrex_pm/protection",
    "src/tyrex_pm/market_data",
    "src/tyrex_pm/data_warehouse/reconstruction",
    "config/research/scenarios",
)
# Evidence-labelled settings: shown with their provenance, never search knobs.
LOCKED = {
    "model.averaging_window_s": "ASSUMED from provider metadata twapLookbackSeconds = 60; "
    "boundary and weights unverified",
    "settlement.reference_series": "ASSUMED: replayed PolyBolt TWAP feed, not the live RTDS stream",
    "settlement.comparison": "ASSUMED tie rule: UP wins at or above K",
    "settlement.provenance_status": "ASSUMED (not VERIFIED)",
    "fees.rate": "SCENARIO_ASSUMPTION_NOT_VERIFIED",
    "fees.exponent": "SCENARIO_ASSUMPTION_NOT_VERIFIED",
    "quality.accepted_k_qualities": "K replayed without attestation is PROVISIONAL; research "
    "runs accept it explicitly",
}
EDITABLE = {
    "model": {"sigma_band", "reference_band_bps", "spot_input", "tau_floor_s"},
    "volatility": {
        "half_life_s",
        "sigma_floor",
        "sigma_scale",
        "min_samples_s",
        "jump_threshold_sigma",
        "variance_time_policy",
    },
    "entry": {
        "tau_min_s",
        "max_entry_price",
        "target_mode",
        "target_profit_usdc",
        "limit_buffer_ticks",
    },
    "sizing": {"max_debit_usdc", "max_loss_usdc", "depth_participation"},
}


class IncompatibleStage(RuntimeError):
    """Saved results were produced from different inputs; they are never reused."""


def _digest(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


# --------------------------------------------------------------------------- design


@dataclass(frozen=True)
class Design:
    """Everything fixed for an experiment, plus the small entry search.

    ``baseline`` holds the fixed model, volatility, entry and sizing settings (shared
    by every configuration of every stage). Only ``edges`` and ``taus`` vary in stage A.
    """

    baseline: dict = field(default_factory=dict)
    primary_backend: str = "twap_proxy_diffusion"
    benchmark_backend: str = "endpoint_diffusion"
    edges: tuple = ("0.02", "0.05", "0.08")
    taus: tuple = (240.0, 150.0)
    benchmark_edge: str = "0.05"
    benchmark_tau: float = 240.0
    primary_scenario: str = "friction"
    finalist_scenario: str = "favorable"

    def validate(self) -> None:
        for section, values in self.baseline.items():
            if section not in EDITABLE:
                raise ValueError(f"baseline section {section!r} is not editable here")
            extra = set(values) - EDITABLE[section]
            if extra:
                locked = [f"{section}.{k}" for k in extra if f"{section}.{k}" in LOCKED]
                raise ValueError(
                    f"not editable: {sorted(locked or extra)}"
                    + (" (evidence-labelled; see LOCKED)" if locked else "")
                )
        if self.primary_backend == self.benchmark_backend:
            raise ValueError("benchmark backend must differ from the primary backend")
        if not self.edges or not self.taus:
            raise ValueError("entry search needs at least one edge and one tau_max_s")
        if len(set(map(str, self.edges))) != len(self.edges) or len(set(self.taus)) != len(
            self.taus
        ):
            raise ValueError("duplicate values in the entry search lists")
        for name in (self.primary_scenario, self.finalist_scenario):
            if not (SCENARIOS / f"{name}.yaml").exists():
                raise ValueError(f"unknown scenario {name!r}")
        for params in self.stage_parameters("A") + self.stage_parameters("B"):
            effective_parameters(params)  # raises ConfigError on invalid settings

    def parameters(self, backend: str, edge: Any, tau: float) -> dict:
        params = merge(self.baseline, {})
        params.setdefault("model", {})["backend"] = backend
        entry = params.setdefault("entry", {})
        entry["min_edge_per_share"] = str(edge)
        entry["tau_max_s"] = float(tau)
        return params

    def stage_parameters(self, stage: str) -> list[dict]:
        if stage == "A":
            return [
                self.parameters(self.primary_backend, e, t) for e in self.edges for t in self.taus
            ]
        if stage == "B":
            return [
                self.parameters(self.benchmark_backend, self.benchmark_edge, self.benchmark_tau)
            ]
        raise ValueError("stage C parameters come from chosen stage A configurations")

    def as_dict(self) -> dict:
        out = dataclasses.asdict(self)
        out["edges"] = [str(e) for e in self.edges]
        out["taus"] = [float(t) for t in self.taus]
        return out


def effective_parameters(params: dict) -> dict:
    """The complete settings the driver will use, inherited defaults included."""
    cfg = settlement_value_config_from_parameters(
        merge(RESEARCH_BASE_PARAMETERS, params), file="staged_experiment"
    )
    return settlement_value_config_as_parameters(cfg)


def config_label(params: dict) -> str:
    entry, model = params.get("entry", {}), params.get("model", {})
    edge = Decimal(str(entry.get("min_edge_per_share"))) * 100
    return (
        f"{BACKEND_NAMES.get(model.get('backend'), model.get('backend'))} | "
        f"edge {format(edge.normalize(), 'f')}¢ | "
        f"entry from {float(entry.get('tau_max_s')):g}s remaining"
    )


def describe_configs(params_list: list[dict]) -> pd.DataFrame:
    rows = []
    for params in params_list:
        eff = effective_parameters(params)
        rows.append(
            {
                "label": config_label(params),
                "backend": eff["model"]["backend"],
                "min_edge_per_share": eff["entry"]["min_edge_per_share"],
                "tau_max_s": eff["entry"]["tau_max_s"],
                "tau_min_s": eff["entry"]["tau_min_s"],
                "limit_buffer_ticks": eff["entry"]["limit_buffer_ticks"],
                "max_debit_usdc": eff["sizing"]["max_debit_usdc"],
                "config_id": config_id(params),
            }
        )
    return pd.DataFrame(rows)


def config_id(params: dict) -> str:
    """Same value as the replay's ``config_hash`` (strategy kind + full effective config)."""
    from tyrex_pm.research.strategy_replay.results import config_hash

    cfg = settlement_value_config_from_parameters(merge(RESEARCH_BASE_PARAMETERS, params))
    return config_hash("settlement_value", cfg)


def flat(effective: dict) -> pd.DataFrame:
    rows = [
        (f"{s}.{k}", json.dumps(v) if isinstance(v, list) else v, LOCKED.get(f"{s}.{k}", ""))
        for s, values in effective.items()
        for k, v in values.items()
    ]
    return pd.DataFrame(rows, columns=["setting", "value", "provenance"]).set_index("setting")


def scenario_table(*names: str) -> pd.DataFrame:
    lab = Lab()
    keys = [
        ("feed_delay_ms", "information delay [ms]"),
        ("decision_delay_ms", "decision delay [ms]"),
        ("order_delay_ms", "order delay [ms]"),
        ("feedback_delay_ms", "feedback delay [ms]"),
        ("adverse_price_ticks", "adverse ticks per fill"),
        ("participation", "share of displayed depth available"),
        ("maker_model", "maker fills"),
        ("gap_policy", "data-gap policy"),
        ("tie_policy", "event tie policy"),
    ]
    data = {}
    for name in names:
        sc = dataclasses.asdict(lab.scenario(name))
        col = {label: sc.get(key) for key, label in keys}
        fee = sc.get("fee_policy") or {}
        col["fee rate · exponent · unit"] = (
            f"{fee.get('rate')} · {fee.get('exponent')} · {fee.get('buy_fee_unit')}"
        )
        col["fee provenance"] = fee.get("provenance")
        data[name] = col
    return pd.DataFrame(data)


# --------------------------------------------------------------------------- windows


def frozen_split(path: Path = SPLIT_FILE, extensions: Path | None = None) -> dict:
    """The saved development/holdout split: the original S2 split plus one frozen
    extension per later recording (``split/extensions/<collection>.json``).

    Never recomputed here: recomputing on a larger inventory would move the chronological
    boundary into the holdout. A recording without an extension stays excluded.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing; create it once with "
            "`python -m tyrex_pm.research.strategy_lab.s2_experiments split`"
        )
    split = json.loads(path.read_text(encoding="utf-8"))
    split["development"] = list(split["development"])
    split["holdout"] = list(split["holdout"])
    split["extensions"] = []
    folder = extensions if extensions is not None else path.parent / "extensions"
    for file in sorted(folder.glob("*.json")) if folder.exists() else []:
        ext = json.loads(file.read_text(encoding="utf-8"))
        overlap = set(ext["development"] + ext["holdout"]) & set(
            split["development"] + split["holdout"]
        )
        if overlap:
            raise ValueError(
                f"split extension {file.name} overlaps the split: {sorted(overlap)[:2]}"
            )
        split["development"] += ext["development"]
        split["holdout"] += ext["holdout"]
        split["extensions"].append(
            {
                "file": file.name,
                "collection": ext["collection"],
                "development": len(ext["development"]),
                "holdout": len(ext["holdout"]),
                "created_utc": ext.get("created_utc"),
            }
        )
    return split


def extend_split(
    collection: str,
    *,
    holdout_fraction: float = 0.3,
    path: Path = SPLIT_FILE,
    inventory: pd.DataFrame | None = None,
) -> dict:
    """Freeze the split for one new recording: its earlier windows become development,
    its latest ``holdout_fraction`` holdout (the S2 rule). Written once, never changed;
    the original split and earlier extensions are left untouched."""
    folder = path.parent / "extensions"
    target = folder / f"{collection}.json"
    if target.exists():
        raise IncompatibleStage(f"{target} already exists; a frozen split is never changed")
    current = frozen_split(path)
    known = set(current["development"] + current["holdout"])
    inventory = batch.window_inventory() if inventory is None else inventory
    usable = inventory[
        (inventory["collection"] == collection)
        & inventory["has_resolution"]
        & ~inventory["duplicate_rejected"]
        & ~inventory["market_id"].isin(known)
    ]
    if len(usable) < 2:
        raise ValueError(f"collection {collection}: {len(usable)} new usable windows; need 2+")
    part = batch.chronological_split(usable, holdout_fraction=holdout_fraction)
    record = {
        "collection": collection,
        "policy": part["policy"],
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "development": part["development"],
        "holdout": part["holdout"],
        "boundary_window_start_ns": part["boundary_window_start_ns"],
        "excluded_without_resolution": int(
            ((inventory["collection"] == collection) & ~inventory["has_resolution"]).sum()
        ),
    }
    folder.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=1), encoding="utf-8")
    return record


def window_table(split: dict | None = None) -> pd.DataFrame:
    """Current inventory labelled development / holdout / not in the frozen split."""
    split = split or frozen_split()
    inventory = batch.window_inventory()
    dev, hold = set(split["development"]), set(split["holdout"])
    inventory["role"] = [
        "development"
        if m in dev
        else "holdout (excluded)"
        if m in hold
        else "not in frozen split (excluded)"
        for m in inventory["market_id"]
    ]
    inventory["window_utc"] = pd.to_datetime(inventory["window_start_ns"], unit="ns", utc=True)
    return inventory


def collections_summary(windows: pd.DataFrame) -> pd.DataFrame:
    return (
        windows.groupby(["collection", "role"])
        .agg(windows=("market_id", "size"), first=("window_utc", "min"), last=("window_utc", "max"))
        .reset_index()
    )


def select_windows(
    windows: pd.DataFrame, *, collections: list[str] | None = None, smoke_windows: int | None = None
) -> pd.DataFrame:
    chosen = windows[windows["role"] == "development"]
    if collections:
        chosen = chosen[chosen["collection"].isin(collections)]
    chosen = chosen.sort_values("window_start_ns")
    if smoke_windows:
        chosen = chosen.head(smoke_windows)
    return chosen.reset_index(drop=True)


# --------------------------------------------------------------------------- experiment


def _collection(row) -> Collection:
    return Collection(str(row["warehouse"]), str(row["collection"]))


def _cache_keys(windows: list[dict]) -> dict[str, str]:
    out = {}
    for w in windows:
        lab = batch._lab(_collection(w))
        out[w["market_id"]] = window_cache_key(
            lab.catalog, w["market_id"], warmup_s=lab.warmup_s, after_s=30.0
        )
    return out


def code_identity() -> dict:
    digest = hashlib.sha256()
    files = 0
    for tree in CODE_TREES:
        for path in sorted((REPO / tree).rglob("*")):
            if path.suffix in (".py", ".yaml") and path.is_file():
                digest.update(str(path.relative_to(REPO)).replace("\\", "/").encode())
                digest.update(path.read_bytes())
                files += 1
    return {
        **batch._code_state(),
        "source_fingerprint": digest.hexdigest(),
        "source_files": files,
        "source_trees": list(CODE_TREES),
        "versions": {
            "loader": LOADER_VERSION,
            "runner": RUNNER_VERSION,
            "part": batch.PART_VERSION,
        },
    }


@dataclass
class Experiment:
    path: Path
    record: dict

    @property
    def id(self) -> str:
        return self.record["experiment_id"]

    @property
    def design(self) -> Design:
        d = dict(self.record["design"])
        d["edges"], d["taus"] = tuple(d["edges"]), tuple(d["taus"])
        return Design(**d)

    @property
    def windows(self) -> list[dict]:
        return self.record["windows"]

    @property
    def saved(self) -> bool:
        return (self.path / "experiment.json").exists()

    def save(self) -> None:
        if not self.saved:
            self.path.mkdir(parents=True, exist_ok=True)
            (self.path / "experiment.json").write_text(
                json.dumps(self.record, indent=1, default=str), encoding="utf-8"
            )

    def stage_dir(self, stage: str) -> Path:
        return self.path / f"stage_{stage}"

    def stage_record(self, stage: str) -> dict | None:
        path = self.stage_dir(stage) / "stage.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def replay_identity() -> dict:
    """Replay semantics an experiment is frozen under (runner, clock policy and, from v3,
    the execution contract). Older records carry only the first two fields."""
    from tyrex_pm.research.strategy_replay.clock_sync import CLOCK_POLICY
    from tyrex_pm.research.strategy_replay.run import EXECUTION_CONTRACT_VERSION

    return {
        "runner": RUNNER_VERSION,
        "clock_policy": CLOCK_POLICY,
        "execution_contract": EXECUTION_CONTRACT_VERSION,
    }


def experiment_replay(exp: "Experiment") -> dict:
    # Experiments created before the replay identity was recorded ran under runner v1.
    return exp.record.get("replay") or {
        "runner": "strategy_closed_loop_v1",
        "clock_policy": "all_sources_last_wins",
    }


def experiment_id(design: Design, windows: pd.DataFrame, kind: str) -> str:
    """Deterministic: the same design, windows and replay semantics reopen the same
    experiment; a new replay version (e.g. a clock fix) starts a new one."""
    key = _digest(
        {
            "design": design.as_dict(),
            "windows": list(windows["market_id"]),
            "kind": kind,
            "replay": replay_identity(),
        }
    )
    return f"{kind}-{key[:10]}"


def create_or_open(
    design: Design,
    windows: pd.DataFrame,
    *,
    kind: str = "full_development",
    name: str | None = None,
    root: Path = STAGED,
    write: bool = True,
) -> Experiment:
    """Freeze design and window identities; reopen if an identical experiment exists.

    With ``write=False`` nothing is written: the record is saved by the first
    ``run_stage``, so previewing settings never leaves experiment directories behind.
    """
    if kind not in ("full_development", "smoke"):
        raise ValueError("kind must be 'full_development' or 'smoke'")
    if windows.empty:
        raise ValueError("no development windows selected")
    design.validate()
    split = frozen_split()
    leaked = set(windows["market_id"]) & set(split["holdout"])
    if leaked or (windows["role"] != "development").any():
        raise ValueError(f"holdout or unsplit windows selected: {sorted(leaked)[:3]}")
    exp_id = name or experiment_id(design, windows, kind)
    path = root / exp_id
    frozen = [
        {
            "market_id": r.market_id,
            "collection": r.collection,
            "warehouse": r.warehouse,
            "slug": r.slug,
            "window_start_ns": int(r.window_start_ns),
        }
        for r in windows.itertuples()
    ]
    record = {
        "experiment_id": exp_id,
        "kind": kind,
        "design": design.as_dict(),
        "windows": frozen,
        "replay": replay_identity(),
        "split": {
            "file": str(SPLIT_FILE),
            "sha256": _digest(split),
            "policy": split["policy"],
            "development": len(split["development"]),
            "holdout": len(split["holdout"]),
            "extensions": split.get("extensions", []),
        },
    }
    existing = path / "experiment.json"
    if existing.exists():
        saved = json.loads(existing.read_text(encoding="utf-8"))
        # The split may have grown (extensions for later recordings) without touching
        # this experiment: its own windows were checked against the current split above.
        same = {k: saved.get(k) for k in record if k != "split"} == {
            k: v for k, v in record.items() if k != "split"
        }
        if not same:
            raise IncompatibleStage(
                f"{path} already holds an experiment with a different design, window set or "
                "split. Choose another name; saved results are never mixed."
            )
        return Experiment(path, saved)
    record.update(
        created_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        code_at_creation=code_identity(),
        cache_keys=_cache_keys(frozen),
    )
    experiment = Experiment(path, record)
    if write:
        experiment.save()
    return experiment


def open_experiment(path: Path | str) -> Experiment:
    path = Path(path)
    record = json.loads((path / "experiment.json").read_text(encoding="utf-8"))
    return Experiment(path, record)


def list_experiments(root: Path = STAGED) -> pd.DataFrame:
    rows = []
    for exp_file in sorted(root.glob("*/experiment.json")):
        exp = open_experiment(exp_file.parent)
        row = {
            "experiment": exp.id,
            "kind": exp.record["kind"],
            "windows": len(exp.windows),
            "created_utc": exp.record.get("created_utc"),
            "path": str(exp.path),
        }
        for stage in STAGES:
            rec = exp.stage_record(stage)
            row[f"stage_{stage}"] = rec["status"] if rec else "not started"
        rows.append(row)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- stages


def stage_parameters(exp: Experiment, stage: str, finalists: list[str] | None = None):
    design = exp.design
    if stage in ("A", "B"):
        return design.stage_parameters(stage), design.primary_scenario
    if stage != "C":
        raise ValueError(f"unknown stage {stage!r}")
    finalists = list(finalists or [])
    if not finalists:
        raise ValueError(
            "stage C needs one or two chosen stage A configurations "
            "(or skip it: choosing none is a valid outcome)"
        )
    if len(finalists) > MAX_FINALISTS or len(set(finalists)) != len(finalists):
        raise ValueError(f"choose at most {MAX_FINALISTS} distinct configurations")
    a = exp.stage_record("A")
    if not a or a["status"] != "completed":
        raise ValueError("stage C requires a completed stage A to choose from")
    by_id = {c["config_id"]: c["parameters"] for c in a["configurations"]}
    by_label = {c["label"]: c["parameters"] for c in a["configurations"]}
    chosen = []
    for key in finalists:
        params = by_id.get(key) or by_label.get(key)
        if params is None:
            raise ValueError(f"{key!r} is not a stage A configuration")
        chosen.append(params)
    return chosen, design.finalist_scenario


def build_specs(exp: Experiment, stage: str, params_list: list[dict], scenario: str):
    split = frozen_split()
    holdout = set(split["holdout"])
    tag = f"staged:{exp.id}:{stage}"
    specs = []
    for w in exp.windows:  # one group per window; fixed order -> stable part digests
        if w["market_id"] in holdout:
            raise ValueError(f"holdout window {w['market_id']} in experiment; refusing")
        for params in params_list:
            specs.append(
                RunSpec(
                    collection=_collection(w),
                    market_id=w["market_id"],
                    parameters=effective_parameters(params),
                    scenario=scenario,
                    tag=tag,
                )
            )
    return specs


def _same_inputs(record: dict, inputs: dict) -> bool:
    """Same configurations, scenario and windows (code versions and the per-configuration
    ``runs_saved`` bookkeeping added on completion aside)."""
    keys = [k for k in inputs if k != "versions"]

    def comparable(values: dict) -> dict:
        out = {k: values.get(k) for k in keys}
        out["configurations"] = [
            {k: v for k, v in c.items() if k != "runs_saved"}
            for c in out.get("configurations") or []
        ]
        return out

    return _digest(comparable(record)) == _digest(comparable(inputs))


def _stage_inputs(exp: Experiment, stage: str, params_list, scenario) -> dict:
    lab = Lab()
    return {
        "experiment_id": exp.id,
        "stage": stage,
        "scenario": scenario,
        "scenario_effective": dataclasses.asdict(lab.scenario(scenario)),
        "configurations": [
            {
                "label": config_label(p),
                "config_id": config_id(p),
                "parameters": p,
                "effective": effective_parameters(p),
            }
            for p in params_list
        ],
        "windows": [w["market_id"] for w in exp.windows],
        "cache_keys": exp.record["cache_keys"],
        "versions": code_identity()["versions"],
    }


def _groups(specs: list[RunSpec]) -> dict[str, list[RunSpec]]:
    groups: dict[str, list[RunSpec]] = {}
    for spec in specs:
        groups.setdefault(spec.market_id, []).append(spec)
    return groups


def completed_runs(exp: Experiment, stage: str, specs: list[RunSpec], keep: bool = False) -> int:
    """Runs already saved as part files for exactly these specs (resumable work)."""
    parts = exp.stage_dir(stage) / "parts"
    done = 0
    for group in _groups(specs).values():
        if (parts / f"{batch._group_digest(group, keep)}.summaries.parquet").exists():
            done += len(group)
    return done


def seconds_per_run(scenario: str) -> tuple[float, str]:
    """Median recorded wall time per run, and where that number comes from."""
    summaries = TIMING_REFERENCE / "summaries.parquet"
    if summaries.exists():
        frame = pd.read_parquet(summaries, columns=["scenario_name", "wall_seconds"])
        values = frame.loc[frame["scenario_name"] == scenario, "wall_seconds"]
        if len(values):
            return float(values.median()), (
                f"median wall time per run under {scenario} in s2/validation_seed "
                f"({len(values)} runs on this machine)"
            )
    return 35.0, "fallback guess of 35 s per run (no timing reference found)"


def preview(
    exp: Experiment,
    stage: str,
    *,
    workers: int,
    finalists=None,
    keep_timelines: bool | None = None,
) -> pd.DataFrame:
    params_list, scenario = stage_parameters(exp, stage, finalists)
    specs = build_specs(exp, stage, params_list, scenario)
    record = exp.stage_record(stage)
    keep = bool(record.get("keep_timelines", False)) if record else bool(keep_timelines)
    done = completed_runs(exp, stage, specs, keep)
    per_run, source = seconds_per_run(scenario)
    groups = len(_groups(specs))
    remaining = len(specs) - done
    parallel = max(1, min(workers, groups))
    estimate = remaining * per_run / parallel
    status = record["status"] if record else "not started"
    inputs = _stage_inputs(exp, stage, params_list, scenario)
    replay = experiment_replay(exp)
    if record and not _same_inputs(record, inputs):
        status += " — INCOMPATIBLE with current inputs (will refuse to resume)"
    elif replay["runner"] != RUNNER_VERSION:
        status += (
            f" — frozen under replay {replay['runner']}; will not run "
            f"(new experiment needed for {RUNNER_VERSION})"
        )
    rows = [
        ("experiment", f"{exp.id} ({exp.record['kind']})"),
        ("replay", f"{replay['runner']} · clock: {replay['clock_policy']}"),
        ("stage", stage),
        ("status", status),
        ("configurations", len(params_list)),
        ("windows", len(exp.windows)),
        ("scenarios", scenario),
        ("total simulations", len(specs)),
        ("already completed (compatible part files)", done),
        ("remaining", remaining),
        ("workers", workers),
        (
            "timelines saved",
            "yes (decisions.parquet)"
            if keep
            else "no (summaries only; single runs can be regenerated in the explorer)",
        ),
        (
            "estimated duration",
            f"~{estimate / 60:.0f} min" if estimate >= 60 else f"~{estimate:.0f} s",
        ),
        (
            "estimate method",
            f"remaining × {per_run:.0f} s ÷ {parallel} parallel windows; "
            f"{source}. First loads of uncached windows add time; ~2.5 GB RAM per worker.",
        ),
    ]
    return pd.DataFrame(rows, columns=["item", "value"]).set_index("item")


def _write_stage(exp: Experiment, stage: str, record: dict) -> None:
    path = exp.stage_dir(stage) / "stage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def run_stage(
    exp: Experiment,
    stage: str,
    *,
    workers: int,
    finalists=None,
    allow_code_change: bool = False,
    keep_timelines: bool | None = None,
) -> pd.DataFrame:
    """Run (or resume) one stage. Refuses when saved results came from other inputs.

    ``keep_timelines=True`` also saves the compact per-evaluation series
    (``stage_X/decisions.parquet``: probabilities, book quotes, actions, reasons) for the
    explorer's probability-vs-book chart. It is fixed when a stage starts: part files are
    keyed by it, so a resume must use the same value. Completed stages never rerun.
    """
    params_list, scenario = stage_parameters(exp, stage, finalists)
    specs = build_specs(exp, stage, params_list, scenario)
    inputs = _stage_inputs(exp, stage, params_list, scenario)
    fingerprint = _digest(inputs)
    current_cache = _cache_keys(exp.windows)
    if current_cache != exp.record["cache_keys"]:
        raise IncompatibleStage(
            "the recorded windows changed since the experiment was "
            "frozen (cache keys differ); start a new experiment"
        )
    code = code_identity()
    exp.save()  # first launch freezes the experiment record on disk
    record = exp.stage_record(stage)
    frozen_replay = experiment_replay(exp)
    if record is not None and record["status"] == "completed" and _same_inputs(record, inputs):
        print(f"stage {stage} already completed; nothing to run")
        if frozen_replay["runner"] != RUNNER_VERSION:
            print(
                f"its results were produced with replay {frozen_replay['runner']} "
                f"({frozen_replay['clock_policy']}); start a new experiment to rerun "
                f"under {RUNNER_VERSION}"
            )
        if keep_timelines and not record.get("keep_timelines", False):
            print("its timelines were not saved; generate single runs in the explorer")
        return pd.read_parquet(exp.stage_dir(stage) / "summaries.parquet")
    if frozen_replay["runner"] != RUNNER_VERSION:
        raise IncompatibleStage(
            f"experiment {exp.id} was frozen under replay {frozen_replay['runner']} "
            f"({frozen_replay['clock_policy']}); running stage {stage} now would mix replay "
            f"versions. Start a new experiment (rerun the notebook: the id changes)."
        )
    if record is not None:
        if record["fingerprint"] != fingerprint:
            raise IncompatibleStage(
                f"stage {stage} in {exp.path} was run with different configurations, "
                "scenario or windows. Results are not reused; use a new experiment."
            )
        recorded = bool(record.get("keep_timelines", False))
        if (
            record["code"]["source_fingerprint"] != code["source_fingerprint"]
            and not allow_code_change
        ):
            raise IncompatibleStage(
                f"strategy/replay source changed since stage {stage} started. Resuming would "
                "mix code versions. Pass allow_code_change=True to accept (recorded), or "
                "start a new experiment."
            )
        if keep_timelines is not None and keep_timelines != recorded:
            raise IncompatibleStage(
                f"stage {stage} started with keep_timelines={recorded}; resume with the same "
                "value (finished windows are keyed by it and would otherwise rerun)."
            )
        keep = recorded
    else:
        keep = bool(keep_timelines)
        record = {
            **inputs,
            "fingerprint": fingerprint,
            "code": code,
            "keep_timelines": keep,
            "attempts": [],
        }
    attempt = {
        "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "workers": workers,
        "runs_planned": len(specs),
        "runs_already_done": completed_runs(exp, stage, specs, keep),
        "source_fingerprint": code["source_fingerprint"],
    }
    record["attempts"].append(attempt)
    record["status"] = "running"
    _write_stage(exp, stage, record)
    started = time.perf_counter()
    try:
        summaries, _ = batch.run_batch(
            specs,
            out=exp.stage_dir(stage),
            workers=workers,
            keep_decisions=keep,
            split=frozen_split(),
        )
    except BaseException as exc:
        attempt["ended"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        attempt["error"] = f"{type(exc).__name__}: {exc}"
        attempt["wall_seconds"] = time.perf_counter() - started
        attempt["runs_done_after"] = completed_runs(exp, stage, specs, keep)
        record["status"] = attempt["ended"]
        _write_stage(exp, stage, record)
        raise
    attempt.update(
        ended="completed",
        wall_seconds=time.perf_counter() - started,
        runs_done_after=len(summaries),
    )
    per_config = summaries.groupby("config_hash").size().to_dict()
    for c in record["configurations"]:
        c["runs_saved"] = int(per_config.get(c["config_id"], 0))
    record["status"] = "completed"
    _write_stage(exp, stage, record)
    return summaries


def candidate_table(exp: Experiment) -> pd.DataFrame:
    """Stage A results for manual finalist choice: counts beside performance, no ranking."""
    from . import explorer

    rec = exp.stage_record("A")
    if not rec or rec["status"] != "completed":
        return pd.DataFrame()
    ds = explorer.load_experiment(exp.path)
    table = explorer.comparison(ds, exp.design.primary_scenario)
    table = table[table["config_id"].isin([c["config_id"] for c in rec["configurations"]])]
    columns = [
        "label",
        "windows",
        "entry_requests",
        "fills",
        "unfilled",
        "skipped",
        "wins",
        "losses",
        "win_rate",
        "avg_win",
        "avg_loss",
        "spending",
        "net_pnl",
        "over_budget",
        "config_id",
    ]
    out = table[columns].sort_values("label").reset_index(drop=True)
    out["sample_note"] = [
        "no fills" if f == 0 else f"small sample ({f} fills)" if f < explorer.SMALL_SAMPLE else ""
        for f in out["fills"]
    ]
    return out
