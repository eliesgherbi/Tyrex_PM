"""Independent job worker. Run as ``python -m tyrex_pm.research.workflows.worker``."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any

from .artifacts import acquire, file_sha256, json_bytes, plan_digest, read, write_json
from .contracts import WorkflowError
from .jobs import part_path, stop_applies
from .replay_run import run_closed_loop_fixture

PARTS = "parts"


class StopRequested(Exception):
    pass


def _publish(job_dir: Path, status: dict) -> None:
    status["heartbeat_at"] = time.time_ns()
    status["pid"] = os.getpid()
    write_json(job_dir / "status.json", status)


def _stop_requested(job_dir: Path, generation: int) -> bool:
    path = job_dir / "stop.json"
    if not path.exists():
        return False
    return stop_applies(read(path), generation)


def _commit(job_dir: Path, unit_id: str, payload: dict) -> None:
    parts = job_dir / PARTS
    parts.mkdir(parents=True, exist_ok=True)
    target = part_path(parts, unit_id)
    temporary = part_path(parts, unit_id, ".json.tmp")
    marker = job_dir / "crash_once"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == unit_id:
        marker.unlink()
        temporary.write_bytes(b"partial")
        os._exit(3)
    trace = payload.pop("trace", None)
    job = read(job_dir / "job.json")
    unit = next(item for item in job["units"] if item["id"] == unit_id)
    record = {
        **payload,
        "schema": "workflow_part_v1",
        "source_fingerprint": job["source_fingerprint"],
        "plan_digest": job["plan_digest"],
        "pins_digest": plan_digest(job.get("pins") or {}),
        "scenario": unit.get("scenario"),
        "recording": unit.get("recording"),
        "config_digest": plan_digest(unit.get("parameters") or {}),
    }
    body = json_bytes(record)
    from tyrex_pm.data_warehouse.capture.files import atomic_bytes

    atomic_bytes(target, body)
    temporary.unlink(missing_ok=True)
    published: dict[str, str] = {"sha256": file_sha256(target)}
    if trace is not None:
        trace_path = part_path(parts, unit_id, ".decisions.json")
        write_json(trace_path, trace)
        published["trace_sha256"] = file_sha256(trace_path)
    manifest_path = parts / "manifest.json"
    manifest = read(manifest_path) if manifest_path.exists() else {"parts": {}}
    manifest["parts"][unit_id] = published
    write_json(manifest_path, manifest)


def execute_unit(unit: dict, job_dir: Path, generation: int) -> dict:
    kind = unit["kind"]
    if kind == "digest":
        return {"unit_id": unit["id"], "value": unit["value"]}
    if kind == "gate":
        deadline = time.time() + float(unit.get("timeout_s") or 30)
        while time.time() < deadline:
            if _stop_requested(job_dir, generation):
                raise StopRequested(unit["id"])
            if (job_dir / "proceed").exists():
                return {"unit_id": unit["id"], "value": unit["value"]}
            time.sleep(0.05)
        raise WorkflowError("unit_timeout", "gated unit was not released", {"unit_id": unit["id"]})
    if kind == "stale_heartbeat":
        return {"unit_id": unit["id"], "value": "held"}
    if kind == "replay_engine":
        return run_closed_loop_fixture(unit)
    if kind == "strategy_replay":
        return _strategy_replay(unit)
    raise WorkflowError("unknown_unit", "unit kind is not supported", {"kind": kind})


_CATALOGS: dict[tuple, Any] = {}
_WINDOWS: dict[tuple, Any] = {}


def _strategy_replay(unit: dict) -> dict:
    from tyrex_pm.data_warehouse.query.reader import Reader
    from tyrex_pm.facts import ids as F
    from tyrex_pm.protection import protection_spec_from_mapping
    from tyrex_pm.research.replay.config import load_scenario
    from tyrex_pm.research.strategy_replay.loader import WindowCatalog, load_window
    from tyrex_pm.research.strategy_replay.results import decision_digest
    from tyrex_pm.research.strategy_replay.run import ReplayExecutionPolicy, run_window
    from tyrex_pm.runtime.composition import starts_adapter
    from tyrex_pm.strategies.registry import get_strategy_plugin

    kind = str(unit.get("strategy_kind") or "settlement_value")
    plugin = get_strategy_plugin(kind)
    # Binance depth is rebuilt only for strategies whose contract reads it (as live).
    spot_depth = starts_adapter(plugin.input_contract, F.BINANCE_SPOT_L2)

    # One catalog per pinned build set and the last loaded window are kept for the life of
    # the process: configurations of the same window, and later windows of the same
    # recording, no longer re-read what is already in memory. run_window leaves both intact.
    catalog_key = (
        unit["warehouse"],
        unit["collection"],
        plan_digest([item.get("build_id") for item in unit["manifests"]]),
    )
    catalog = _CATALOGS.get(catalog_key)
    if catalog is None:
        _CATALOGS.clear()
        reader = Reader(
            Path(unit["warehouse"]),
            unit["collection"],
            manifests=list(unit["manifests"]),
        )
        catalog = WindowCatalog(reader)
        _CATALOGS[catalog_key] = catalog
    window_key = (catalog_key, unit["market_id"], spot_depth)
    window = _WINDOWS.get(window_key)
    if window is None:
        _WINDOWS.clear()
        window = load_window(catalog, unit["market_id"], spot_depth=spot_depth)
        _WINDOWS[window_key] = window
    scenario = load_scenario(Path(unit["scenario_path"]))
    config = plugin.load_config(unit.get("parameters") or {}, "workflow")
    # The scenario's book rule applies through run_window (one setting).
    policy = None
    if unit.get("protection"):
        policy = ReplayExecutionPolicy(
            protection=protection_spec_from_mapping(dict(unit["protection"]), where="protection")
        )
    result = run_window(
        window,
        strategy_config=config,
        scenario=scenario,
        execution_policy=policy,
        strategy_kind=kind,
        clock_observations=catalog.clock_observations_for(
            window.range_start_ns, window.range_end_ns
        ),
    )
    economics = result["economics"]
    legs = {window.up_instrument_id: "UP", window.down_instrument_id: "DOWN"}
    sealed = next(
        (
            d
            for d in result.get("decisions") or []
            if isinstance(d, dict) and d.get("k") is not None
        ),
        {},
    )
    return {
        "unit_id": unit["id"],
        "engine": "strategy_replay.run_window",
        "market_id": unit["market_id"],
        "scenario_name": result["scenario_name"],
        "configuration_name": unit.get("configuration_name"),
        "configuration_fingerprint": unit.get("configuration_fingerprint"),
        "decision_digest": decision_digest(result),
        "entered": economics.get("entered"),
        "filled": economics.get("filled"),
        "realized_pnl": economics.get("realized_pnl"),
        "no_trade": economics.get("entered") is not True,
        # The market's own five minutes. The loaded range adds warm-up before and
        # settlement time after; it is kept separately and never used as a label.
        "window_start_ns": int(window.ref.window_start_ns),
        "window_end_ns": int(window.ref.window_end_ns),
        "range_start_ns": int(window.range_start_ns),
        "range_end_ns": int(window.range_end_ns),
        "window_clock_contract": "market_interval_v1",
        "position": position_summary(economics),
        # K as the strategy saw it once sealed; checked against the provider later.
        "k_value": _jsonable(sealed.get("k")),
        "k_quality": sealed.get("k_quality"),
        "attempt_summary": _attempt_summary(result.get("attempts") or []),
        "evaluations": result["timing"]["evaluations"],
        "trace": _compact_trace(
            result,
            window_start_ns=int(window.ref.window_start_ns),
            window_end_ns=int(window.ref.window_end_ns),
            range_start_ns=int(window.range_start_ns),
            range_end_ns=int(window.range_end_ns),
            legs=legs,
        ),
        **replay_result_fields(result),
    }


_POSITION_KEYS = (
    "side",
    "filled_gross_shares",
    "filled_net_shares",
    "fee_shares",
    "debit",
    "vwap",
    "sold_shares",
    "exit_proceeds_net",
    "final_net_shares",
    "payout",
    "fill_ratio",
    "intended_quantity",
    "limit_price",
    "spend_amount",
    "entry_attempts",
    "entry_not_sent",
    "entry_zero_fill",
    "entry_posts",
    "exit_attempts",
    "filled_episodes",
)


def position_summary(economics: dict) -> dict:
    """Engine economics the ledger needs: size, side, price, fees and exits."""
    return {key: _jsonable(economics.get(key)) for key in _POSITION_KEYS}


def _jsonable(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


_TRACE_KEYS = (
    "at_ns",
    "action",
    "reason",
    "chosen_side",
    "up_ask",
    "down_ask",
    "up_bid",
    "down_bid",
    "p_up_point",
    "p_up_low",
    "p_up_high",
    "reference_price",
    "observed_at",
    "k",
    "k_quality",
    "sigma",
)


def _attempt_summary(attempts: list) -> list[dict]:
    rows = []
    for item in attempts:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "role": item.get("role"),
                "outcome": item.get("outcome"),
                "outcome_reason": item.get("outcome_reason"),
                "side": item.get("side"),
                "decided_at_ns": item.get("decided_at_ns"),
            }
        )
    return rows


def _compact_trace(
    result: dict,
    *,
    window_start_ns: int | None = None,
    window_end_ns: int | None = None,
    range_start_ns: int | None = None,
    range_end_ns: int | None = None,
    legs: dict[str, str] | None = None,
) -> dict:
    decisions = []
    for row in result.get("decisions") or []:
        if not isinstance(row, dict):
            continue
        kept = {key: _jsonable(row[key]) for key in _TRACE_KEYS if key in row}
        decisions.append(kept)
    truncated = len(decisions) > 4000
    engine = result.get("engine") or {}
    ledger = []
    for event in engine.get("ledger") or []:
        row = _jsonable(event)
        if isinstance(row, dict) and legs:
            instrument = row.get("instrument_id") or (row.get("intent") or {}).get(
                "instrument_id"
            )
            if instrument in legs:
                row["leg"] = legs[instrument]
        ledger.append(row)
    return {
        "schema": "replay_trace_v3",
        "source": "strategy_replay.run_window",
        "window_start_ns": window_start_ns,
        "window_end_ns": window_end_ns,
        "range_start_ns": range_start_ns,
        "range_end_ns": range_end_ns,
        "window_clock_contract": "market_interval_v1",
        "truncated": truncated,
        "decisions": decisions[:4000],
        "orders": _jsonable(result.get("orders") or []),
        "attempts": _jsonable(result.get("attempts") or []),
        "ledger": ledger,
        "holds": _jsonable(result.get("holds") or []),
        "protection_events": _jsonable(result.get("protection_events") or []),
        "omitted": "Fields absent from a decision row were not stored. Nothing is filled in.",
    }


def replay_result_fields(result: dict) -> dict:
    """Keep the engine verdict. A P&L figure does not make the window qualified."""
    engine = result.get("engine") or {}
    label = result.get("label") or {}
    qualified = engine.get("qualified") is True
    limitations = engine.get("limitations") or []
    if not isinstance(limitations, list):
        limitations = [limitations]
    level = engine.get("qualification_level") or ("qualified" if qualified else "unqualified")
    return {
        "qualified": qualified,
        "qualification": level,
        "qualification_notes": _jsonable(engine.get("qualification_notes") or {}),
        "gap_blips_ignored": engine.get("gap_blips_ignored"),
        "limitations": limitations,
        "fee_provenance": engine.get("fee_provenance"),
        "label_method": label.get("method"),
        "label_winner": label.get("winner"),
        "label_reason": label.get("reason"),
        "payout_unit": label.get("payout_unit"),
    }


def _with_manifests(unit: dict, pins: dict | None) -> dict:
    if unit.get("kind") != "strategy_replay" or unit.get("manifests"):
        return unit
    builds = [
        build
        for build in (pins or {}).get("manifests") or []
        if build.get("collection_id") in {None, unit.get("collection")}
    ]
    attached = builds or list((pins or {}).get("manifests") or [])
    return {**unit, "manifests": attached}


def run_job(job_dir: Path) -> int:
    body = read(job_dir / "job.json")
    status = read(job_dir / "status.json")
    generation = int(status["generation"])
    lock = acquire(job_dir)
    try:
        status["state"] = "running"
        status["owner_acknowledged"] = True
        status["generation"] = generation
        _publish(job_dir, status)
        time.sleep(0.3)
        if status.get("stale_heartbeat_hold"):
            status["heartbeat_at"] = 0
            write_json(job_dir / "status.json", status)
        parallel = replay_workers() > 1 and body["units"] and all(
            unit["kind"] == "strategy_replay" for unit in body["units"]
        )
        if parallel:
            outcome = _run_parallel(job_dir, body, status, generation)
            status["state"] = outcome
            status["active_unit"] = None
            if outcome == "completed":
                status["completed"] = len(body["units"])
            _publish(job_dir, status)
            return 0
        for unit in body["units"]:
            if unit["kind"] == "stale_heartbeat":
                status["state"] = "running"
                status["owner_acknowledged"] = True
                status["heartbeat_at"] = 0
                status["active_unit"] = unit["id"]
                status["pid"] = os.getpid()
                write_json(job_dir / "status.json", status)
                deadline = time.time() + float(unit.get("timeout_s") or 30)
                while time.time() < deadline:
                    if _stop_requested(job_dir, generation):
                        raise StopRequested(unit["id"])
                    if (job_dir / "proceed").exists():
                        break
                    time.sleep(0.05)
                else:
                    raise WorkflowError("unit_timeout", "stale holder was not released", {})
            if _stop_requested(job_dir, generation):
                status["state"] = "interrupted"
                _publish(job_dir, status)
                return 0
            if unit["id"] in _done(job_dir):
                continue
            status["active_unit"] = unit["id"]
            _publish(job_dir, status)
            try:
                payload = execute_unit(
                    _with_manifests(unit, body.get("pins")), job_dir, generation
                )
            except StopRequested:
                status["state"] = "interrupted"
                status["active_unit"] = None
                _publish(job_dir, status)
                return 0
            _commit(job_dir, unit["id"], payload)
            done = _done(job_dir)
            status["completed"] = len(done)
            _publish(job_dir, status)
        status["state"] = "completed"
        status["active_unit"] = None
        status["completed"] = len(body["units"])
        _publish(job_dir, status)
        return 0
    except WorkflowError as exc:
        status = read(job_dir / "status.json")
        status["state"] = "failed"
        status["error"] = {"code": exc.code, "reason": exc.reason, "fields": exc.fields}
        _publish(job_dir, status)
        return 1
    except Exception as exc:  # noqa: BLE001 - worker failures stay on the job record
        status = read(job_dir / "status.json")
        status["state"] = "failed"
        status["error"] = {
            "code": "worker_failed",
            "reason": str(exc),
            "fields": {"error_type": type(exc).__name__},
        }
        _publish(job_dir, status)
        return 1
    finally:
        lock.release()


def replay_workers() -> int:
    try:
        return max(1, int(os.environ.get("TYREX_REPLAY_WORKERS", "3")))
    except ValueError:
        return 3


def _window_group_key(unit: dict) -> tuple:
    return (unit.get("warehouse"), unit.get("collection"), unit.get("market_id"))


def run_window_group(units: list[dict]) -> list[tuple[str, dict]]:
    """Pool task: every configuration of one window, sharing one loaded window."""
    return [(unit["id"], _strategy_replay(unit)) for unit in units]


def _run_parallel(job_dir: Path, body: dict, status: dict, generation: int) -> str:
    """Replay pending strategy units in a process pool. The parent alone commits parts."""
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
    from concurrent.futures.process import BrokenProcessPool

    done = _done(job_dir)
    groups: dict[tuple, list[dict]] = {}
    for unit in body["units"]:
        if unit["id"] in done:
            continue
        groups.setdefault(_window_group_key(unit), []).append(
            _with_manifests(unit, body.get("pins"))
        )
    pending_groups = list(groups.values())
    if not pending_groups:
        return "completed"
    workers = min(replay_workers(), len(pending_groups))
    status["workers"] = workers
    executor = ProcessPoolExecutor(max_workers=workers)
    try:
        futures = {}
        queue = list(pending_groups)
        while queue and len(futures) < workers:
            group = queue.pop(0)
            futures[executor.submit(run_window_group, group)] = group
        while futures:
            finished, _ = wait(list(futures), timeout=5, return_when=FIRST_COMPLETED)
            for future in finished:
                group = futures.pop(future)
                try:
                    results = future.result()
                except BrokenProcessPool as exc:
                    raise WorkflowError(
                        "replay_worker_ended",
                        "a replay worker process ended before finishing its window (closed "
                        "or out of memory); finished windows are kept, resume to replay "
                        "the rest",
                        {"windows": [str(unit.get("market_id")) for unit in group]},
                    ) from exc
                for unit_id, payload in results:
                    _commit(job_dir, unit_id, payload)
                status["completed"] = len(_done(job_dir))
            if _stop_requested(job_dir, generation):
                for future in futures:
                    future.cancel()
                status["active_unit"] = None
                _publish(job_dir, status)
                return "interrupted"
            while queue and len(futures) < workers:
                group = queue.pop(0)
                futures[executor.submit(run_window_group, group)] = group
            status["active_unit"] = ", ".join(
                str(group[0].get("market_id")) for group in futures.values()
            )
            _publish(job_dir, status)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return "completed"


def _done(job_dir: Path) -> set[str]:
    from .jobs import verified_unit_ids

    return verified_unit_ids(job_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tyrex-workflow-worker")
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args(argv)
    return run_job(args.job)


if __name__ == "__main__":
    raise SystemExit(main())
