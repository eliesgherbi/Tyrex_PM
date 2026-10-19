"""Per-run probability-vs-book timelines for the results explorer.

Where a trace comes from, in order:

1. **Original evidence**: the compact per-evaluation series saved in the same bundle as
   the selected summary row (``decisions.parquet``), matched on window, scenario and the
   exact saved configuration string. Nothing is borrowed from another configuration.
2. **Cached diagnostic replay**: a trace generated earlier for exactly this run
   (configuration hash, scenario, collection, window) under
   ``var/research/strategy_replay/timelines/``.
3. Only on an explicit call to :func:`generate`: a replay of one window × one
   configuration × one scenario through ``experiments.Lab.run`` (real driver, recorded
   clock handling), after identity checks. Its decision digest is compared with the
   saved run's; a different digest makes it a *new diagnostic replay*, not the original.

Original summaries and P&L are never modified.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .experiments import REPO
from .plots import BLUE, GRID, INK, MUTED

TIMELINE_CACHE = REPO / "var" / "research" / "strategy_replay" / "timelines"
TRACE_COLUMNS = (
    "at_ns",
    "action",
    "reason",
    "tau_s",
    "p_up_point",
    "p_up_low",
    "p_up_high",
    "up_bid",
    "up_ask",
    "down_bid",
    "down_ask",
    "chosen_side",
    "window_end_ns",
)
AQUA = "#1baf7a"
RED = "#d03b3b"  # book midpoint: a market price, not a model output
WINDOW_NS = 300 * 10**9
# Status strip: action -> (label, fill colour, hatch). Hatches keep categories apart
# without colour.
STATUS_STYLE = {
    "HOST_SKIPPED": ("host skipped (inputs unavailable)", "#d9d8d4", "////"),
    "WAIT": ("WAIT", "#cfe0f5", ""),
    "BLOCKED": ("BLOCKED", "#f3d2c4", "xx"),
    "SKIP": ("SKIP (entry window closed)", "#ece3c8", ".."),
    "ENTER": ("ENTER", INK, ""),
    "HOLD": ("HOLD (position held)", "#cdeee0", "\\\\"),
}


@dataclass
class Trace:
    frame: pd.DataFrame  # one row per evaluation, TRACE_COLUMNS
    source: str  # "original" | "diagnostic"
    provenance: str  # one line for captions
    orders: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    has_event_times: bool = False
    matches_original: bool | None = None  # diagnostic only: digest equality

    @property
    def is_original_execution(self) -> bool:
        """Markers describe the saved run (original, or a replay with an equal digest)."""
        return self.source == "original" or bool(self.matches_original)


# --------------------------------------------------------------------------- lookup


def _row_value(row: pd.Series, key: str, default=None):
    value = row.get(key, default)
    if isinstance(value, float) and math.isnan(value):
        return default
    return value


def _is_staged(dataset) -> bool:
    return getattr(dataset, "stages", None) is not None


def _kind(dataset) -> str:
    return getattr(dataset, "kind", "fixed")


def _bundle_dir(dataset, row: pd.Series) -> Path:
    """Batch experiments: the run's own scenario folder (or the experiment folder)."""
    folder = _row_value(row, "bundle_dir")
    return Path(folder) if folder else Path(dataset.spec.path)


def decisions_file(dataset, row: pd.Series) -> Path:
    """The decisions file that belongs to the selected run's own bundle."""
    if _is_staged(dataset):
        return Path(dataset.spec.path) / f"stage_{row['stage']}" / "decisions.parquet"
    if _kind(dataset) == "batch":
        return _bundle_dir(dataset, row) / "decisions.parquet"
    return Path(dataset.spec.path).parent / "decisions.parquet"


def load_study_trace(row: pd.Series) -> Trace | None:
    """A preserved notebook 06 study bundle's own decisions (saved, not regenerated)."""
    bundle = _row_value(row, "bundle_path")
    if not bundle or not Path(bundle).exists():
        return None
    result = json.loads(Path(bundle).read_text(encoding="utf-8"))
    decisions = result.get("decisions") or []
    if not decisions:
        return None
    frame = pd.DataFrame([{k: d.get(k) for k in TRACE_COLUMNS} for d in decisions])
    frame["window_end_ns"] = result["window"]["window_end_ns"]
    for column in frame.columns:
        if column not in TEXT_COLUMNS:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    orders, events = saved_execution(row)
    return Trace(
        frame=frame.sort_values("at_ns").reset_index(drop=True),
        source="original",
        provenance=f"saved study trace ({Path(bundle).parent.name}/{Path(bundle).name}; "
        "recorded with the former study hooks)",
        orders=orders,
        events=events,
        has_event_times=bool(events),
    )


def saved_execution(row: pd.Series) -> tuple[list[dict], list[dict]]:
    raw = _row_value(row, "execution_events")
    if not isinstance(raw, str) or not raw:
        return [], []
    data = json.loads(raw)
    return data.get("orders", []), data.get("events", [])


def load_original(dataset, row: pd.Series) -> Trace | None:
    if _kind(dataset) == "study":
        return load_study_trace(row)
    path = decisions_file(dataset, row)
    if not path.exists():
        return None
    filters = [
        ("market_id", "=", row["market_id"]),
        ("scenario_name", "=", row["scenario_name"]),
        ("config", "=", row["config"]),
    ]
    frame = pd.read_parquet(path, filters=filters)
    if frame.empty:
        return None
    frame = frame[[c for c in TRACE_COLUMNS if c in frame]].sort_values("at_ns")
    orders, events = saved_execution(row)
    return Trace(
        frame=frame.reset_index(drop=True),
        source="original",
        provenance=f"original saved trace ({path.parent.name}/{path.name})",
        orders=orders,
        events=events,
        has_event_times=bool(events),
    )


def cache_key(row: pd.Series) -> str:
    """Run identity plus the saved run's digest: runs of the same configuration, scenario
    and window made under different replay versions never share a cached timeline."""
    payload = [
        row["config_hash"],
        row["scenario_name"],
        row["collection"],
        row["market_id"],
        _row_value(row, "digest"),
    ]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:24]


def load_cached(row: pd.Series, root: Path = TIMELINE_CACHE) -> Trace | None:
    key = cache_key(row)
    meta_path, frame_path = root / f"{key}.json", root / f"{key}.parquet"
    if not (meta_path.exists() and frame_path.exists()):
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return _diagnostic_trace(pd.read_parquet(frame_path), meta, row)


def _diagnostic_trace(frame: pd.DataFrame, meta: dict, row: pd.Series) -> Trace:
    saved = _row_value(row, "digest")
    matches = None if not saved else meta["decision_digest"] == saved
    if matches is None:
        text = "diagnostic replay (no saved digest to compare)"
    elif matches:
        text = "diagnostic replay, decision digest identical to the saved run"
    else:
        text = "NEW diagnostic replay: decision digest differs from the saved run"
    created = meta.get("created_utc", "")[:16]
    return Trace(
        frame=frame,
        source="diagnostic",
        provenance=f"{text} (generated {created} UTC)",
        orders=meta.get("orders", []),
        events=meta.get("events", []),
        has_event_times=True,
        matches_original=matches,
    )


def available_windows(dataset, runs: pd.DataFrame, root: Path = TIMELINE_CACHE) -> set[str]:
    """Windows of these runs (one configuration × scenario) with an original or cached trace.

    Reads only the window column of the bundle's decisions file; never replays.
    """
    if runs.empty:
        return set()
    first = runs.iloc[0]
    found: set[str] = set()
    if _kind(dataset) == "study":
        return {r["market_id"] for _, r in runs.iterrows()
                if _row_value(r, "trace_level") in ("decisions", "full")}
    path = decisions_file(dataset, first)
    if path.exists():
        filters = [("scenario_name", "=", first["scenario_name"]), ("config", "=", first["config"])]
        found |= set(pd.read_parquet(path, columns=["market_id"], filters=filters)["market_id"])
    for _, row in runs.iterrows():
        if (root / f"{cache_key(row)}.json").exists():
            found.add(row["market_id"])
    return found


def get_trace(dataset, row: pd.Series, root: Path = TIMELINE_CACHE) -> Trace | None:
    """Original or cached trace for exactly this run. Never replays."""
    return load_original(dataset, row) or load_cached(row, root)


# --------------------------------------------------------------------------- replay


def _manifest_path(dataset, row: pd.Series) -> Path:
    if _kind(dataset) == "batch":
        return _bundle_dir(dataset, row) / "manifest.json"
    return Path(dataset.spec.path).parent / "manifest.json"


def _saved_scenario(dataset, row: pd.Series) -> dict | None:
    if _is_staged(dataset):
        path = Path(dataset.spec.path) / f"stage_{row['stage']}" / "stage.json"
        return json.loads(path.read_text(encoding="utf-8")).get("scenario_effective")
    if _kind(dataset) == "study":
        bundle = _row_value(row, "bundle_path")
        if not bundle or not Path(bundle).exists():
            return None
        return json.loads(Path(bundle).read_text(encoding="utf-8")).get("scenario")
    manifest = _manifest_path(dataset, row)
    if manifest.exists():
        return (
            json.loads(manifest.read_text(encoding="utf-8"))
            .get("scenarios", {})
            .get(row["scenario_name"])
        )
    return None


def _saved_cache_key(dataset, row: pd.Series) -> str | None:
    if _is_staged(dataset):
        record = json.loads((Path(dataset.spec.path) / "experiment.json").read_text("utf-8"))
        return record.get("cache_keys", {}).get(row["market_id"])
    if _kind(dataset) == "study":
        record = json.loads((Path(dataset.spec.path) / "experiment.json").read_text("utf-8"))
        slug = _row_value(row, "slug")
        return next((s["cache_key"] for s in record.get("sources", [])
                     if s.get("slug") == slug), None)
    manifest = _manifest_path(dataset, row)
    if manifest.exists():
        keys = json.loads(manifest.read_text(encoding="utf-8")).get("cache_keys", {})
        return keys.get(f"{row['collection']}:{row['market_id']}")
    return None


def _collection(collection: str):
    from .batch import all_collections

    for c in all_collections():
        if c.collection == collection:
            return c
    return None


def _current_cache_key(collection: str, market_id: str) -> str:
    from tyrex_pm.research.strategy_replay.cache import window_cache_key

    from .batch import _lab

    lab = _lab(_collection(collection))
    return window_cache_key(lab.catalog, market_id, warmup_s=lab.warmup_s, after_s=30.0)


def _current_scenario(name: str) -> dict:
    from .experiments import Lab

    return dataclasses.asdict(Lab().scenario(name))


def _holdout() -> set[str]:
    from .staged import frozen_split

    try:
        return set(frozen_split()["holdout"])  # original split plus frozen extensions
    except FileNotFoundError:
        return set()


def replay_check(dataset, row: pd.Series) -> tuple[dict | None, list[str], list[str]]:
    """(parameters, problems, notes). Any problem means the replay is refused."""
    from .staged import config_id

    problems, notes = [], []
    params = json.loads(row["config"])
    try:
        current_id = config_id(params)
    except Exception as exc:  # noqa: BLE001 - surfaced to the reader as a refusal
        return None, [f"saved configuration no longer validates: {exc}"], notes
    if current_id != row["config_hash"]:
        problems.append(
            "the saved configuration resolves to different effective settings under the "
            "current code (inherited defaults changed and were not recorded); the "
            "historical run cannot be reproduced exactly"
        )
    if row["market_id"] in _holdout():
        problems.append("this window is in the holdout")
    if _collection(row["collection"]) is None:
        problems.append(f"recording {row['collection']} is not available locally")
        return params, problems, notes
    saved_key = _saved_cache_key(dataset, row)
    if saved_key is None:
        notes.append("data identity was not recorded with this run; current recording used")
    elif saved_key != _current_cache_key(row["collection"], row["market_id"]):
        problems.append("the recorded window data changed since the run (cache key differs)")
    if _kind(dataset) == "study":
        notes.append("study variant replayed as strategy settings on the production path "
                     "(trading-equal by the migration check; decision labels of the veto "
                     "variant differ, so the digest comparison is expected to differ there)")
    policy = _row_policy(row)
    notes.append("execution policy: " + (json.dumps(policy, sort_keys=True) if policy
                                         else "replay defaults (none recorded)"))
    saved_sc = _saved_scenario(dataset, row)
    current_sc = _current_scenario(row["scenario_name"])
    if saved_sc is None:
        notes.append("scenario settings were not recorded with this run; current file used")
    elif json.dumps(saved_sc, sort_keys=True, default=str) != json.dumps(
        current_sc, sort_keys=True, default=str
    ):
        problems.append("the scenario file changed since the run")
    return params, problems, notes


def _row_policy(row: Any) -> dict | None:
    """The run's replay execution policy (batch column), or None for older tables."""
    raw = row.get("execution_policy") if hasattr(row, "get") else None
    if raw is None or (isinstance(raw, float) and raw != raw):
        return None
    policy = json.loads(raw) if isinstance(raw, str) else dict(raw)
    return policy or None


def _replay(collection: str, market_id: str, params: dict, scenario: str,
            execution_policy: dict | None = None) -> dict:
    from .batch import _lab

    lab = _lab(_collection(collection))
    try:
        return lab.run(market_id, parameters=params, scenario=scenario,
                       execution_policy=execution_policy)
    finally:
        # A prepared window holds ~1 GB of rows; Lab keeps it for reuse, which in a
        # notebook kernel would accumulate with every generated timeline.
        lab._windows.pop(market_id, None)


TEXT_COLUMNS = ("action", "reason", "chosen_side", "up_reason", "down_reason", "twap_reason")


def replay_compact(collection: str, market_id: str, params: dict, scenario: str,
                   execution_policy: dict | None = None) -> dict:
    """Replay one run and keep only what the chart needs (picklable; runs in workers)."""
    from .batch import DECISION_COLUMNS, execution_events

    result = _replay(collection, market_id, params, scenario, execution_policy)
    frame = pd.DataFrame([{k: d.get(k) for k in DECISION_COLUMNS} for d in result["decisions"]])
    for column in frame.columns:
        if column not in TEXT_COLUMNS:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["window_end_ns"] = result["window"]["window_end_ns"]
    frame = frame[[c for c in TRACE_COLUMNS if c in frame]].sort_values("at_ns")
    return {
        "frame": frame.reset_index(drop=True),
        "execution": execution_events(result),
        "decision_digest": result["decision_digest"],
        "config_hash": result["config_hash"],
        "clock_correction": result["provenance"].get("clock_correction"),
    }


def _save(row: pd.Series, params: dict, notes: list[str], payload: dict, root: Path) -> Trace:
    from .staged import code_identity

    meta = {
        "run": {
            k: _row_value(row, k)
            for k in ("config_hash", "scenario_name", "collection", "market_id", "slug")
        },
        "parameters": params,
        "decision_digest": payload["decision_digest"],
        "saved_digest_at_generation": _row_value(row, "digest"),
        "execution_policy": _row_policy(row),
        "config_hash_replayed": payload["config_hash"],
        "notes": notes,
        "clock_correction": payload["clock_correction"],
        "code": code_identity(),
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **payload["execution"],
    }
    root.mkdir(parents=True, exist_ok=True)
    key = cache_key(row)
    payload["frame"].to_parquet(root / f"{key}.parquet", index=False)
    (root / f"{key}.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    return _diagnostic_trace(payload["frame"], meta, row)


def generate(dataset, row: pd.Series, root: Path = TIMELINE_CACHE) -> Trace:
    """Replay ONE window × configuration × scenario and cache its compact timeline."""
    params, problems, notes = replay_check(dataset, row)
    if problems:
        raise RuntimeError("replay refused: " + "; ".join(problems))
    payload = replay_compact(row["collection"], row["market_id"], params, row["scenario_name"],
                             _row_policy(row))
    return _save(row, params, notes, payload, root)


def generate_many(
    dataset, runs: pd.DataFrame, *, workers: int = 4, root: Path = TIMELINE_CACHE
) -> pd.DataFrame:
    """Timelines for many runs (e.g. one configuration × scenario across its windows).

    Explicit only. Runs that already have an original or cached trace are skipped;
    each remaining run passes the same identity checks as :func:`generate` and is
    replayed on its own (one window per worker, ~2.5 GB RAM each). Interrupting keeps
    every timeline already saved; calling again continues with the rest. Returns one
    status row per run.
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed

    have = available_windows(dataset, runs, root)
    report, todo = [], []
    for _, row in runs.iterrows():
        status = {"window": row.get("window_label"), "market_id": row["market_id"]}
        if row["market_id"] in have:
            report.append({**status, "result": "already available"})
            continue
        params, problems, notes = replay_check(dataset, row)
        if problems:
            report.append({**status, "result": "refused: " + "; ".join(problems)})
        else:
            todo.append((row, params, notes))
    print(f"{len(todo)} to replay, {len(runs) - len(todo)} skipped", flush=True)

    def record(row, params, notes, payload):
        trace = _save(row, params, notes, payload, root)
        verdict = "matches saved run" if trace.matches_original else "DIFFERS from saved run"
        report.append(
            {
                "window": row.get("window_label"),
                "market_id": row["market_id"],
                "result": f"generated, {verdict}",
            }
        )

    started = datetime.now(timezone.utc)
    if workers <= 1:
        for i, (row, params, notes) in enumerate(todo, 1):
            args = (row["collection"], row["market_id"], params, row["scenario_name"],
                    _row_policy(row))
            record(row, params, notes, replay_compact(*args))
            print(f"timelines {i}/{len(todo)}", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    replay_compact,
                    row["collection"],
                    row["market_id"],
                    params,
                    row["scenario_name"],
                    _row_policy(row),
                ): (row, params, notes)
                for row, params, notes in todo
            }
            try:
                for i, future in enumerate(as_completed(futures), 1):
                    row, params, notes = futures[future]
                    try:
                        record(row, params, notes, future.result())
                    except Exception as exc:  # noqa: BLE001 - one bad window, keep going
                        report.append(
                            {
                                "window": row.get("window_label"),
                                "market_id": row["market_id"],
                                "result": f"failed: {type(exc).__name__}: {exc}",
                            }
                        )
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                    print(f"timelines {i}/{len(todo)} {elapsed:.0f}s", flush=True)
            except BaseException:
                for future in futures:
                    future.cancel()  # queued windows dropped; saved ones are kept
                raise
    return pd.DataFrame(report)


def explanations_for(dataset, row: pd.Series) -> pd.DataFrame:
    """Indicator explanations recorded for exactly this run (recording level explain).

    Only batch experiments run with ``recording: {level: explain}`` have them (case runs
    write ``explanations.parquet``). Never replays."""
    if _kind(dataset) != "batch" or dataset.root is None:
        return pd.DataFrame()
    path = Path(dataset.root) / "explanations.parquet"
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_parquet(path, filters=[("market_id", "=", row["market_id"]),
                                           ("scenario_name", "=", row["scenario_name"])])
    if "tag" in frame and _row_value(row, "tag"):
        frame = frame[frame["tag"].eq(row["tag"])]
    return frame.sort_values("at_ns").reset_index(drop=True)


def explanation_table(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per explained evaluation and indicator: the calculated state behind it."""
    rows = []
    for item in frame.itertuples():
        indicators = json.loads(item.indicators) if isinstance(item.indicators, str) else {}
        for name, state in (indicators or {}).items():
            rows.append({
                "time_utc": _utc(item.at_ns).strftime("%H:%M:%S"),
                "tau_s": item.tau_s, "action": item.action, "reason": item.reason,
                "context_for": None if pd.isna(getattr(item, "context_for_at_ns", None))
                else _utc(item.context_for_at_ns).strftime("%H:%M:%S"),
                "indicator": name.split(":")[-1][:24],
                "sigma": state.get("sigma"), "variance": state.get("variance"),
                "units": state.get("variance_units"), "last_return": state.get("last_return"),
                "last_dt_s": state.get("last_dt_s"), "samples": state.get("sample_count"),
                "tape_age_ms": state.get("tape_age_ms"),
                "jump_guard": state.get("jump_guard_tripped"),
            })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- curves


def curves(frame: pd.DataFrame) -> pd.DataFrame:
    """Up/Down point, conservative and book-midpoint series; NaN where unavailable.

    Down conservative is 1 - p_up_high (the cautious bound for Down), not 1 - p_up_low.
    Each book midpoint uses its own book; Down is never derived from Up.
    """
    out = pd.DataFrame({"at_ns": frame["at_ns"].to_numpy()})
    point, low, high = (
        frame[c].to_numpy(dtype=float) for c in ("p_up_point", "p_up_low", "p_up_high")
    )
    out["up_point"], out["up_conservative"] = point, low
    out["down_point"], out["down_conservative"] = 1 - point, 1 - high
    out["up_mid"] = (
        frame["up_bid"].to_numpy(dtype=float) + frame["up_ask"].to_numpy(dtype=float)
    ) / 2
    out["down_mid"] = (
        frame["down_bid"].to_numpy(dtype=float) + frame["down_ask"].to_numpy(dtype=float)
    ) / 2
    return out


def break_gaps(series: pd.DataFrame, *, max_gap_s: float = 2.5) -> pd.DataFrame:
    """Insert NaN rows where evaluations are missing so lines never bridge a gap."""
    if len(series) < 2:
        return series
    t = series["at_ns"].to_numpy()
    gaps = np.flatnonzero(np.diff(t) > max_gap_s * 1e9)
    if not len(gaps):
        return series
    fillers = pd.DataFrame({"at_ns": t[gaps] + 1})
    return pd.concat([series, fillers], ignore_index=True).sort_values("at_ns", ignore_index=True)


# --------------------------------------------------------------------------- markers


def markers(trace: Trace, row: pd.Series) -> list[dict]:
    """Decision and execution markers with their meaning; times only where recorded.

    kind: requested | arrived | fill | partial | unfilled | rejected | cancelled | status.
    ``y`` is a model probability for ``requested`` and an execution price otherwise.
    """
    frame = trace.frame
    out: list[dict] = []
    enters = frame[frame["action"] == "ENTER"]
    for r in enters.itertuples():
        side = r.chosen_side or _row_value(row, "side")
        if side == "UP":
            y = r.p_up_low
        elif side == "DOWN":
            y = 1 - r.p_up_high
        else:
            y = math.nan
        out.append(
            {
                "kind": "requested",
                "at_ns": int(r.at_ns),
                "side": side,
                "y": y,
                "y_meaning": "conservative model probability at the decision",
                "label": f"ENTER requested ({side})"
                + ("" if trace.is_original_execution else " in this diagnostic replay"),
            }
        )
    if not trace.is_original_execution:
        # A different replay's fills are not the saved run's execution: state the saved
        # outcome instead, without a time.
        out.append(
            {
                "kind": "status",
                "at_ns": None,
                "side": _row_value(row, "side"),
                "y": math.nan,
                "y_meaning": "",
                "label": f"saved run: {summary_status(row)}; replay fills not shown",
            }
        )
        return out
    orders = {o.get("order_id"): o for o in trace.orders}
    filled: dict[str, float] = {}
    for e in trace.events:
        order = orders.get(e.get("order_id"), {})
        side = e.get("leg") or order.get("side") or _row_value(row, "side")
        kind = {
            "fill": "fill",
            "unfilled_cancelled": "unfilled",
            "rejected": "rejected",
            "cancelled": "cancelled",
            "arrived": "arrived",
        }.get(e["event"])
        if kind is None:
            continue
        if kind == "fill":
            q = float(e.get("quantity", 0))
            filled[e.get("order_id")] = filled.get(e.get("order_id"), 0.0) + q
            intended = float(order.get("quantity") or 0)
            partial = intended and filled[e["order_id"]] < intended - 1e-9
            out.append(
                {
                    "kind": "partial" if partial else "fill",
                    "at_ns": int(e["at_ns"]),
                    "side": side,
                    "y": float(e["price"]),
                    "y_meaning": "fill price per share",
                    "label": f"{'partial fill' if partial else 'fill'} {q:g} sh @ "
                    f"{float(e['price']):.2f}",
                }
            )
        elif kind in ("unfilled", "rejected", "cancelled"):
            limit = order.get("limit_price")
            qty = e.get("quantity") or e.get("unfilled")
            text = {
                "unfilled": "unfilled remainder cancelled",
                "rejected": "rejected",
                "cancelled": "cancelled",
            }[kind]
            if qty:
                text += f" ({float(qty):g} sh)"
            if e.get("reason"):
                text += f": {e['reason']}"
            out.append(
                {
                    "kind": kind,
                    "at_ns": int(e["at_ns"]),
                    "side": side,
                    "y": float(limit) if limit else math.nan,
                    "y_meaning": "order limit price",
                    "label": text,
                }
            )
        else:
            out.append(
                {
                    "kind": "arrived",
                    "at_ns": int(e["at_ns"]),
                    "side": side,
                    "y": math.nan,
                    "y_meaning": "",
                    "label": "order reached venue",
                }
            )
    if not trace.events:
        out.append(
            {
                "kind": "status",
                "at_ns": None,
                "side": _row_value(row, "side"),
                "y": math.nan,
                "y_meaning": "",
                "label": summary_status(row),
            }
        )
    return out


def summary_status(row: pd.Series) -> str:
    """Execution outcome from the summary row alone (no event timestamps saved)."""
    if not row.get("entered"):
        return "no entry requested in this window"
    if not row.get("filled"):
        return "entry requested; nothing filled (no event time saved)"
    ratio = _row_value(row, "fill_ratio")
    vwap = _row_value(row, "vwap")
    part = "" if ratio is None else f"{ratio:.0%} of intended shares"
    price = "" if vwap is None else f" at avg {vwap:.3f}"
    return f"filled {part}{price} (summary only; no fill timestamp saved)"


# --------------------------------------------------------------------------- plot


def _utc(ns) -> pd.Timestamp:
    return pd.to_datetime(ns, unit="ns", utc=True)


def _segments(frame: pd.DataFrame) -> list[tuple[str, int, int]]:
    actions, times = frame["action"].fillna("?").to_numpy(), frame["at_ns"].to_numpy()
    out, start = [], 0
    for i in range(1, len(actions) + 1):
        if i == len(actions) or actions[i] != actions[start]:
            end = times[i] if i < len(actions) else times[i - 1] + 10**9
            out.append((actions[start], int(times[start]), int(end)))
            start = i
    return out


# Lines join valid evaluations at most this far apart. In validation_seed, 12% of steps
# between valid evaluations exceed 3 s but only 3% exceed 10 s: 10 s keeps lines readable
# and still breaks them at real outages.
JOIN_GAP_S = 10.0


def line_series(values: pd.DataFrame, column: str, *, join_gap_s: float) -> pd.DataFrame:
    """One curve's valid points, with a break wherever they are > join_gap_s apart.

    The host often skips every other second (collector-clock gap), so joining only
    adjacent seconds would leave nothing but isolated dots. Joining points up to a few
    seconds apart draws a readable line between two real evaluations; longer
    unavailable periods still break it (and stay visible in the action strip).
    """
    valid = values[["at_ns", column]].dropna()
    return break_gaps(valid, max_gap_s=join_gap_s)


def plot_timeline(
    trace: Trace,
    row: pd.Series,
    *,
    warmup: bool = False,
    title: str = "",
    join_gap_s: float = JOIN_GAP_S,
):
    frame = trace.frame
    start = int(row["window_start_ns"])
    ends = frame["window_end_ns"].dropna() if "window_end_ns" in frame else pd.Series([])
    end = int(ends.iloc[0]) if len(ends) else start + WINDOW_NS
    lo = start - (WINDOW_NS if warmup else 0)
    hi = end + 30 * 10**9
    view = frame[(frame["at_ns"] >= lo) & (frame["at_ns"] <= hi)].reset_index(drop=True)
    series = curves(view)

    fig, axes = plt.subplots(
        3, 1, figsize=(11, 7.4), sharex=True, gridspec_kw={"height_ratios": [3, 3, 0.55]}
    )
    side = _row_value(row, "side")
    for ax, leg in ((axes[0], "UP"), (axes[1], "DOWN")):
        key = leg.lower()
        for column, color, lw, ls, name in (
            ("point", BLUE, 1.6, "-", "model probability (point)"),
            ("conservative", AQUA, 1.4, "--", "model probability\n(conservative)"),
            ("mid", RED, 1.3, "-", f"{leg.title()} book midpoint\n(token price)"),
        ):
            name_col = f"{key}_{column}"
            line = line_series(series, name_col, join_gap_s=join_gap_s)
            ax.plot(_utc(line["at_ns"]), line[name_col], color=color, lw=lw, ls=ls, label=name)
        ax.set_ylim(0, 1)
        ax.set_ylabel(f"{leg.title()}: probability\nor price (0–1)", fontsize=9, color=MUTED)
        ax.grid(True, color=GRID, lw=0.6)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        ax.tick_params(colors=MUTED, labelsize=8)
        for t, text in ((start, "window opens"), (end, "window closes")):
            ax.axvline(_utc(t), color=MUTED, lw=1)
            if leg == "UP":
                ax.annotate(
                    text,
                    (_utc(t), 1.0),
                    xytext=(3, -10),
                    textcoords="offset points",
                    fontsize=8,
                    color=MUTED,
                )
        if side and leg != side:
            ax.text(
                0.995,
                0.03,
                f"no order on this leg (selected side: {side})",
                transform=ax.transAxes,
                ha="right",
                fontsize=8,
                color=MUTED,
                bbox=dict(facecolor="white", edgecolor="none", alpha=0.85, pad=1.5),
            )
    axes[0].legend(fontsize=8, frameon=False, loc="upper left", bbox_to_anchor=(1.0, 1.0))

    styles = {
        "requested": dict(marker="^", ms=9, color=INK, mfc="white", mew=1.5),
        "fill": dict(marker="o", ms=8, color=INK, mfc=INK),
        "partial": dict(marker="o", ms=8, color=INK, mfc="white", mew=1.5),
        "unfilled": dict(marker="x", ms=9, color=INK, mew=2),
        "rejected": dict(marker="X", ms=9, color=INK),
        "cancelled": dict(marker="x", ms=9, color=MUTED, mew=2),
        "arrived": dict(marker="|", ms=12, color=MUTED, mew=1.5),
    }
    seen = set()
    status_lines = []
    for m in markers(trace, row):
        if m["at_ns"] is None:
            status_lines.append(m["label"])
            continue
        t = _utc(m["at_ns"])
        if m["kind"] == "requested":
            for ax in axes[:2]:
                ax.axvline(t, color=INK, lw=0.9, ls=(0, (4, 3)))
        ax = axes[0] if m["side"] == "UP" else axes[1]
        y = m["y"] if not (isinstance(m["y"], float) and math.isnan(m["y"])) else 0.04
        ax.plot([t], [y], ls="", **styles[m["kind"]])
        seen.add(m["kind"])
        if m["kind"] != "arrived":
            ax.annotate(
                m["label"],
                (t, y),
                # decision labels above the point, execution labels below: they often
                # share the same second
                xytext=(6, 6) if m["kind"] == "requested" else (6, -14),
                textcoords="offset points",
                fontsize=8,
                color=INK,
            )
    if seen:
        from matplotlib.lines import Line2D

        names = {
            "requested": "ENTER requested\n(y = conservative\nmodel probability)",
            "fill": "fill (y = fill price)",
            "partial": "partial fill\n(y = fill price)",
            "unfilled": "unfilled remainder\n(y = limit price)",
            "rejected": "rejected\n(y = limit price)",
            "cancelled": "cancelled\n(y = limit price)",
            "arrived": "order reached venue",
        }
        handles = [Line2D([], [], ls="", label=names[k], **styles[k]) for k in styles if k in seen]
        axes[1].legend(
            handles=handles, fontsize=8, frameon=False, loc="upper left", bbox_to_anchor=(1.0, 1.0)
        )
    if not trace.is_original_execution:
        axes[0].text(
            0.005,
            0.03,
            "diagnostic replay differs from the saved run: execution markers omitted",
            transform=axes[0].transAxes,
            fontsize=8,
            color=MUTED,
        )

    strip = axes[2]
    counts: dict[str, int] = {}
    for action, a, b in _segments(view):
        name, color, hatch = STATUS_STYLE.get(action, (action, "#ffffff", ""))
        strip.broken_barh(
            [(_utc(a), _utc(b) - _utc(a))],
            (0, 1),
            facecolors=color,
            hatch=hatch,
            edgecolor="white",
            lw=0,
        )
        counts[action] = counts.get(action, 0) + 1
    strip.set_yticks([])
    strip.set_ylabel("action", fontsize=8, color=MUTED, rotation=0, ha="right", va="center")
    for spine in ("top", "right", "left"):
        strip.spines[spine].set_visible(False)
    from matplotlib.patches import Patch

    present = [a for a in STATUS_STYLE if a in set(view["action"])]
    strip.legend(
        handles=[
            Patch(
                facecolor=STATUS_STYLE[a][1],
                hatch=STATUS_STYLE[a][2],
                edgecolor=MUTED,
                label=STATUS_STYLE[a][0],
            )
            for a in present
        ],
        fontsize=7,
        frameon=False,
        ncol=3,
        loc="upper left",
        bbox_to_anchor=(0.0, -0.9),
    )
    strip.xaxis.set_major_formatter(plt.matplotlib.dates.DateFormatter("%H:%M:%S"))
    strip.set_xlabel("time (UTC)", fontsize=9, color=MUTED)
    strip.tick_params(colors=MUTED, labelsize=8)
    axes[0].set_xlim(_utc(lo), _utc(hi))
    skipped = int((view["action"] == "HOST_SKIPPED").sum())
    caption = (
        f"{title}\ntrace: {trace.provenance}\nlines join evaluations ≤ {join_gap_s:g} s apart; "
        f"{skipped} of {len(view)} seconds shown were host-skipped (hatched in the strip)"
    )
    if status_lines:
        caption += f"\nexecution ({side or 'no'} leg): " + "; ".join(status_lines)
    fig.suptitle(caption, x=0.01, ha="left", fontsize=10, color=INK)
    fig.subplots_adjust(left=0.09, right=0.78, top=0.83, bottom=0.17, hspace=0.12)
    return fig


def reason_counts(trace: Trace, row: pd.Series, top: int = 6) -> pd.Series:
    """Most frequent decision reasons inside the five-minute window."""
    start = int(row["window_start_ns"])
    frame = trace.frame
    inside = frame[(frame["at_ns"] >= start) & (frame["at_ns"] <= start + WINDOW_NS)]
    keys = inside["action"].fillna("?") + ": " + inside["reason"].fillna("")
    return keys.value_counts().head(top).rename("evaluations")


READING_GUIDE = """
<b>How to read the probability-vs-book chart</b>
<ul style='margin:2px 0'>
<li><b>Three curves per panel.</b> Solid blue: the model's point probability that this
side wins. Dashed green: the model's <i>conservative</i> probability (the cautious bound
the strategy trades on). Red line: that side's own book midpoint, (bid + ask) / 2, a
market <i>price</i> for a token paying 1 if the side wins, not a verified probability.</li>
<li><b>Up and Down.</b> Point estimates mirror each other (Down = 1 − Up). Conservative
estimates do not: Up uses the low bound, Down uses 1 − the high bound, so the two
conservative values add up to less than 1. The gap is the model's uncertainty band.</li>
<li><b>Midpoint vs ask vs fill.</b> A buyer pays the ask (above the midpoint), possibly
worse after delays and adverse ticks; the fill marker shows the price actually paid.</li>
<li><b>Markers.</b> ▲ ENTER requested is the strategy's recommendation, not a fill or
venue acceptance. ● is a simulated fill, ○ a partial fill, × an unfilled remainder.
When the file holds no event times, the outcome is written in a box instead of drawn.</li>
<li><b>After entry.</b> The model keeps updating, but the strategy holds to settlement
(HOLD) and allows one entry per window, so later changes trigger no purchase.</li>
<li><b>Grey hatched strip.</b> Seconds where the host skipped evaluation (inputs
unavailable). Lines join real evaluations up to 10 s apart (the title says so) and
break at longer outages; a lone value between two long outages is not drawn. Tick
"Strict lines" to break at every skipped second.</li>
<li><b>Provenance.</b> The title says whether this is the original saved trace or a
regenerated diagnostic replay, and whether that replay matches the saved run.</li>
</ul>
"""
