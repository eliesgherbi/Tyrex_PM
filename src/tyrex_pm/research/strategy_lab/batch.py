"""Parallel multi-window, multi-configuration runs with reproducible bundles.

Work is grouped by window so each worker loads a prepared window once (from the disk
cache) and replays every configuration on it. Every run is the real driver through
``run_window``; nothing here decides anything.

Bundle layout (``<out>/``):

* ``manifest.json`` — specs, collections, scenario definitions, cache keys, code state,
  data split, hardware and timings.
* ``summaries.parquet`` — one row per (window, configuration, scenario).
* ``decisions.parquet`` — compact per-evaluation series (only when requested).
* ``explanations.parquet`` — indicator explanations, when a spec sets ``recording``
  to level ``explain`` (selected by the same trigger as live).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from tyrex_pm.research.strategy_replay.cache import window_cache_key

from .experiments import Lab, label
from .frames import NUMERIC, summary

PART_VERSION = "batch_part_v2"
DECISION_COLUMNS = (
    "at_ns",
    "action",
    "reason",
    *NUMERIC,
    "chosen_side",
    "up_reason",
    "down_reason",
    "twap_reason",
    # Strategy disagreement veto (absent from parts written before it existed).
    "disagreement_gap_pp",
    "disagreement_reason",
)


@dataclass(frozen=True)
class Collection:
    warehouse: str
    collection: str


@dataclass(frozen=True)
class RunSpec:
    collection: Collection
    market_id: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    scenario: str = "favorable"
    scenario_overrides: Mapping[str, Any] = field(default_factory=dict)
    tag: str = ""
    # Replay execution policy (v3): e.g. a protection SL/TP block. Empty = defaults.
    execution_policy: Mapping[str, Any] = field(default_factory=dict)
    # Framework recording block (e.g. {"level": "explain"}); explanations are written to
    # ``explanations.parquet``. Empty = no recording (bundle unchanged).
    recording: Mapping[str, Any] = field(default_factory=dict)

    @property
    def config_label(self) -> str:
        return label(self.parameters)


_LABS: dict[tuple[str, str, str | None], Lab] = {}


def _lab(collection: Collection, parser: str | None = None) -> Lab:
    key = (collection.warehouse, collection.collection, parser)
    if key not in _LABS:
        extra = {} if parser is None else {"parser": parser}
        _LABS[key] = Lab(
            warehouse=Path(collection.warehouse), collection=collection.collection, **extra
        )
    return _LABS[key]


def _compact(result: dict) -> list[dict]:
    rows = []
    for row in result["decisions"]:
        compact = {k: row.get(k) for k in DECISION_COLUMNS}
        for key in ("held_side", "held_p_point", "exit_hold_value", "exit_feasible",
                    "exit_reason", "exit_sell_shares", "exit_trend"):
            if key in row:
                value = row[key]
                compact[key] = json.dumps(value) if isinstance(value, dict) else value
        rows.append(compact)
    return rows


def _spec_identity(spec: RunSpec) -> dict:
    """Spec fields for the part digest; an empty ``recording`` keeps pre-P4 digests."""
    identity = {**asdict(spec), "collection": asdict(spec.collection)}
    if not identity.get("recording"):
        identity.pop("recording", None)
    return identity


def _group_digest(specs: list[RunSpec], keep_decisions: bool) -> str:
    from tyrex_pm.research.strategy_replay.loader import LOADER_VERSION
    from tyrex_pm.research.strategy_replay.run import RUNNER_VERSION

    payload = json.dumps(
        {
            "specs": [_spec_identity(s) for s in specs],
            "keep": keep_decisions,
            "versions": [LOADER_VERSION, RUNNER_VERSION, PART_VERSION],
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def _typed(frame: pd.DataFrame, numeric: Iterable[str]) -> pd.DataFrame:
    """Stable parquet types: known numeric columns as floats, everything else as text."""
    numeric = set(numeric)
    for column in frame.columns:
        if column in numeric:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        elif frame[column].dtype == object:
            frame[column] = frame[column].map(lambda v: None if v is None else str(v))
    return frame


def _run_group(specs: list[RunSpec], keep_decisions: bool, parts: str) -> tuple[str, str | None]:
    """Worker: one window, many configurations; results go to part files, not memory."""
    parts_dir = Path(parts)
    digest = _group_digest(specs, keep_decisions)
    summary_path = parts_dir / f"{digest}.summaries.parquet"
    decision_path = parts_dir / f"{digest}.decisions.parquet" if keep_decisions else None
    if summary_path.exists() and (decision_path is None or decision_path.exists()):
        return str(summary_path), None if decision_path is None else str(decision_path)
    summaries_rows: list[dict] = []
    decision_rows: list[dict] = []
    explain_rows: list[dict] = []
    for spec in specs:
        lab = _lab(spec.collection)
        started = time.perf_counter()
        result = lab.run(
            spec.market_id,
            parameters=spec.parameters,
            scenario=spec.scenario,
            scenario_overrides=dict(spec.scenario_overrides) or None,
            execution_policy=dict(spec.execution_policy) or None,
            recording=dict(spec.recording) or None,
        )
        row = summary(result)
        row.update(
            collection=spec.collection.collection,
            market_id=spec.market_id,
            window_start_ns=result["window"]["window_start_ns"],
            config=spec.config_label,
            config_hash=result["config_hash"],
            scenario_name=result["scenario_name"],
            tag=spec.tag,
            decisions_evaluated=result["timing"]["evaluations"],
            wall_seconds=time.perf_counter() - started,
            orders=len(result["orders"]),
            entry_reason_counts=json.dumps(_reason_counts(result)),
            execution_events=json.dumps(execution_events(result)),
            # Exact replay execution policy, so a timeline regenerates the same run.
            execution_policy=json.dumps(dict(spec.execution_policy), sort_keys=True,
                                        default=str),
        )
        summaries_rows.append(row)
        if keep_decisions:
            for d in _compact(result):
                decision_rows.append(
                    {
                        **d,
                        "market_id": spec.market_id,
                        "tag": spec.tag,
                        "config": spec.config_label,
                        "scenario_name": result["scenario_name"],
                        "winner": result["label"]["winner"],
                        "label_type": result["label"]["label_type"],
                        "window_end_ns": result["window"]["window_end_ns"],
                    }
                )
        for e in result.get("explanations", ()):
            explain_rows.append({
                "market_id": spec.market_id, "config": spec.config_label,
                "scenario_name": result["scenario_name"], "tag": spec.tag,
                "at_ns": e.get("at_ns"), "tau_s": e.get("tau_s"), "action": e.get("action"),
                "reason": e.get("reason"), "context_for_at_ns": e.get("context_for_at_ns"),
                "indicators": json.dumps(e.get("indicators"), sort_keys=True, default=str),
            })
        del result
    for spec in specs:  # one window per group: release its ~1 GB of rows now
        _lab(spec.collection)._windows.pop(spec.market_id, None)
    parts_dir.mkdir(parents=True, exist_ok=True)
    if decision_path is not None:
        numeric = (*NUMERIC, "at_ns", "window_end_ns", "disagreement_gap_pp")
        frame = _typed(pd.DataFrame(decision_rows), numeric)
        frame.to_parquet(decision_path, index=False)
    if explain_rows:
        pd.DataFrame(explain_rows).to_parquet(_explain_path(summary_path), index=False)
    tmp = summary_path.with_suffix(".tmp")
    pd.DataFrame(summaries_rows).to_parquet(tmp, index=False)
    os.replace(tmp, summary_path)  # summary last: its presence marks a complete part
    return str(summary_path), None if decision_path is None else str(decision_path)


def _explain_path(summary_path: Path | str) -> Path:
    path = Path(summary_path)
    return path.with_name(path.name.replace(".summaries.parquet", ".explanations.parquet"))


EXECUTION_EVENTS = (
    "arrived",
    "fill",
    "unfilled_cancelled",
    "resting",
    "rejected",
    "cancelled",
    "cancel_no_active_order",
)
EVENT_FIELDS = ("order_id", "quantity", "net_quantity", "price", "fee", "reason", "unfilled")


def execution_events(result: dict) -> dict:
    """Compact orders and their ledger events (simulated timestamps), a few per run."""
    up = result["window"].get("up_instrument_id")
    orders = [
        {
            k: o.get(k)
            for k in (
                "order_id",
                "decided_at_ns",
                "arrival_ns",
                "side",
                "quantity",
                "limit_price",
                "action",
                "spend_amount",
            )
        }
        for o in result.get("orders", [])
    ]
    events = []
    for entry in result.get("engine", {}).get("ledger", []):
        if entry.get("event") not in EXECUTION_EVENTS:
            continue
        event = {"at_ns": entry.get("at_ns"), "event": entry["event"]}
        event.update({k: entry[k] for k in EVENT_FIELDS if entry.get(k) is not None})
        if entry.get("instrument_id"):
            event["leg"] = "UP" if entry["instrument_id"] == up else "DOWN"
        events.append(event)
    return {"orders": orders, "events": events}


def _reason_counts(result: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in result["decisions"]:
        key = f"{d.get('action')}:{d.get('reason')}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _code_state() -> dict:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "src", "config"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        return {"git_head": None, "worktree_dirty": None}
    return {"git_head": head or None, "worktree_dirty": bool(dirty.strip())}


def run_batch(
    specs: Iterable[RunSpec],
    *,
    out: Path | None = None,
    workers: int | None = None,
    keep_decisions: bool = False,
    split: Mapping[str, Any] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    specs = list(specs)
    groups: dict[tuple[str, str], list[RunSpec]] = {}
    for spec in specs:
        groups.setdefault((spec.collection.collection, spec.market_id), []).append(spec)
    started = time.perf_counter()
    # Each worker holds one prepared window (~1 GB of Python rows) at a time.
    workers = workers or max(1, min(len(groups), 8, (os.cpu_count() or 2) - 2))
    parts_dir = (out or Path(tempfile.mkdtemp(prefix="sv_batch_"))) / "parts"
    paths: list[tuple[str, str | None]] = []
    if workers == 1:
        for group in groups.values():
            paths.append(_run_group(group, keep_decisions, str(parts_dir)))
    else:
        # No max_tasks_per_child: on CPython 3.12 worker recycling can deadlock the pool
        # (observed: all workers exit after their quota and the parent waits forever).
        # Memory stays bounded because each group releases its window.
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_run_group, g, keep_decisions, str(parts_dir)) for g in groups.values()
            ]
            try:
                for done, future in enumerate(as_completed(futures), start=1):
                    paths.append(future.result())
                    print(
                        f"batch progress {done}/{len(futures)} groups "
                        f"{time.perf_counter() - started:.0f}s",
                        flush=True,
                    )
            except BaseException:
                # Interrupt or failure: drop queued groups; running groups finish and keep
                # their part files, so a rerun with the same specs resumes from them.
                for future in futures:
                    future.cancel()
                raise
    elapsed = time.perf_counter() - started
    summaries = pd.concat([pd.read_parquet(p) for p, _ in paths], ignore_index=True)
    summaries = summaries.sort_values(["window_start_ns", "config", "scenario_name"])
    summaries = summaries.reset_index(drop=True)
    decisions = (
        pd.concat([pd.read_parquet(d) for _, d in paths if d], ignore_index=True)
        if keep_decisions
        else None
    )
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        summaries.to_parquet(out / "summaries.parquet", index=False)
        if decisions is not None:
            decisions.to_parquet(out / "decisions.parquet", index=False)
        explain_parts = [_explain_path(p) for p, _ in paths if _explain_path(p).exists()]
        if explain_parts:
            pd.concat([pd.read_parquet(x) for x in explain_parts], ignore_index=True
                      ).to_parquet(out / "explanations.parquet", index=False)
        labs = {(s.collection.warehouse, s.collection.collection) for s in specs}
        manifest = {
            "created_by": "tyrex_pm.research.strategy_lab.batch.run_batch",
            "code": _code_state(),
            "specs": [{**asdict(s), "collection": asdict(s.collection)} for s in specs],
            "collections": sorted(c for _, c in labs),
            "cache_keys": {
                f"{s.collection.collection}:{s.market_id}": window_cache_key(
                    _lab(s.collection).catalog,
                    s.market_id,
                    warmup_s=_lab(s.collection).warmup_s,
                    after_s=30.0,
                )
                for s in {(sp.collection, sp.market_id): sp for sp in specs}.values()
            },
            "scenarios": {
                name: asdict(_lab(specs[0].collection).scenario(name))
                for name in sorted({s.scenario for s in specs})
            },
            "split": dict(split or {}),
            "hardware": {
                "processor": platform.processor(),
                "cpu_count": os.cpu_count(),
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "timing": {"wall_seconds": elapsed, "workers": workers, "runs": len(specs)},
        }
        (out / "manifest.json").write_text(
            json.dumps(manifest, indent=1, sort_keys=True, default=str), encoding="utf-8"
        )
    return summaries, decisions


def all_collections() -> list[Collection]:
    """Every recorded collection found locally under ``var/warehouse*/collections/``.

    Discovered rather than listed, so a new recording is picked up without code changes.
    Which of its windows an experiment may use is decided by the frozen split
    (``staged.frozen_split``): a new recording stays excluded until its split is frozen.
    """
    from .experiments import REPO

    found = []
    for collections in sorted((REPO / "var").glob("warehouse*/collections")):
        for path in sorted(p for p in collections.iterdir() if p.is_dir()):
            found.append(Collection(str(collections.parent), path.name))
    return found


def window_inventory(collections: Iterable[Collection] | None = None) -> pd.DataFrame:
    """All usable windows across collections; a market seen twice is rejected."""
    rows = []
    for collection in collections or all_collections():
        lab = _lab(collection)
        for ref in lab.windows():
            resolved = any(
                r.get("provider_status") == "resolved"
                for r in lab.catalog.resolutions.get(ref.market_id, [])
            )
            rows.append(
                {
                    "collection": collection.collection,
                    "warehouse": collection.warehouse,
                    "market_id": ref.market_id,
                    "slug": ref.slug,
                    "window_start_ns": ref.window_start_ns,
                    "window_end_ns": ref.window_end_ns,
                    "has_resolution": resolved,
                }
            )
    frame = pd.DataFrame(rows).sort_values("window_start_ns").reset_index(drop=True)
    duplicated = frame.duplicated("market_id", keep="first")
    frame["duplicate_rejected"] = duplicated
    return frame


def chronological_split(inventory: pd.DataFrame, *, holdout_fraction: float = 0.3) -> dict:
    """Earlier windows for development, the latest ``holdout_fraction`` held out."""
    usable = inventory[~inventory["duplicate_rejected"] & inventory["has_resolution"]]
    usable = usable.sort_values("window_start_ns")
    n_hold = max(1, int(round(len(usable) * holdout_fraction)))
    dev, hold = usable.iloc[:-n_hold], usable.iloc[-n_hold:]
    return {
        "policy": f"chronological_last_{holdout_fraction:.0%}_holdout",
        "development": dev["market_id"].tolist(),
        "holdout": hold["market_id"].tolist(),
        "boundary_window_start_ns": int(hold["window_start_ns"].iloc[0]),
    }
