"""Read-only result views, explicit comparison saves, and evaluation guard records."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.execution.process_lock import StateDirectoryBusy
from tyrex_pm.research.strategy_replay.engine_version import ENGINE_BEFORE_VERSIONING, REPLAY_ENGINE

from .artifacts import acquire, now_iso, plan_digest, read, slice_page, write_json
from .contracts import WorkflowError
from .jobs import _job, part_path

# Prepared result populations, keyed by job directory and the manifest's stat.
# A new committed unit rewrites the manifest, so a stale entry is never served.
_POPULATIONS: dict[str, tuple[tuple[int, int], list[dict]]] = {}


def _manifest_stamp(job_dir: Path) -> tuple[int, int] | None:
    path = job_dir / "parts" / "manifest.json"
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def _part_rows(job_dir: Path) -> list[dict]:
    manifest_path = job_dir / "parts" / "manifest.json"
    if not manifest_path.exists():
        return []
    manifest = read(manifest_path)
    rows = []
    for unit_id in sorted((manifest.get("parts") or {})):
        rows.append(read(part_path(job_dir / "parts", unit_id)))
    return rows


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def population_summary(rows: list[dict]) -> dict[str, Any]:
    """Totals for one configuration. Callers pass the full population, never a ledger page."""
    qualified = [row for row in rows if row.get("qualified") is True]
    unqualified = [row for row in rows if row.get("qualified") is False]
    known = [row for row in qualified if _number(row.get("realized_pnl")) is not None]
    outcomes = []
    for row in rows:
        for attempt in row.get("attempt_summary") or []:
            if not isinstance(attempt, dict):
                continue
            outcomes.append(
                {
                    "market_id": row.get("market_id"),
                    "unit_id": row.get("unit_id"),
                    "window_start_ns": row.get("window_start_ns"),
                    "role": attempt.get("role"),
                    "outcome": attempt.get("outcome"),
                    "outcome_reason": attempt.get("outcome_reason"),
                    "side": attempt.get("side"),
                    "limitations": row.get("limitations") or [],
                    "qualified": row.get("qualified"),
                }
            )
    return {
        "windows": len(rows),
        "qualified": len(qualified),
        "unqualified": len(unqualified),
        "unknown_qualification": len(rows) - len(qualified) - len(unqualified),
        "attempts": sum(1 for row in rows if row.get("entered") is True),
        "qualified_attempts": sum(1 for row in qualified if row.get("entered") is True),
        "fills": sum(1 for row in rows if row.get("filled") is True),
        "qualified_fills": sum(1 for row in qualified if row.get("filled") is True),
        "no_trade": sum(1 for row in qualified if row.get("entered") is not True),
        "qualified_pnl": (
            sum(_number(row.get("realized_pnl")) or 0 for row in known) if known else None
        ),
        "qualified_pnl_rows": len(known),
        "pnl_per_fill": pnl_per_fill(
            [
                float(_number(row.get("realized_pnl")) or 0)
                for row in known
                if row.get("filled") is True
            ]
        ),
        "attempt_outcomes": outcomes,
        "execution_states": _state_counts(rows),
        "qualified_with_notes": sum(
            1 for row in qualified if row.get("qualification") == "qualified_with_notes"
        ),
        "k_checks": _count(rows, "k_check"),
        "qualified_requested_windows": sum(1 for row in qualified if row.get("entered") is True),
        "missing_pnl_is_zero": False,
    }


# Two-sided 95% Student t quantiles; 1.96 beyond the table.
_T95 = {
    1: 12.71,
    2: 4.30,
    3: 3.18,
    4: 2.78,
    5: 2.57,
    6: 2.45,
    7: 2.36,
    8: 2.31,
    9: 2.26,
    10: 2.23,
    12: 2.18,
    15: 2.13,
    20: 2.09,
    25: 2.06,
    30: 2.04,
    40: 2.02,
    60: 2.00,
}
TOO_SMALL_FILLS = 10
SMALL_FILLS = 30


def pnl_per_fill(values: list[float]) -> dict[str, Any]:
    """Mean qualified P&L per filled window with a 95% interval and a size verdict."""
    n = len(values)
    verdict = (
        "none"
        if n == 0
        else "too_small"
        if n < TOO_SMALL_FILLS
        else ("small" if n < SMALL_FILLS else "comparable")
    )
    if n == 0:
        return {"n": 0, "mean": None, "sd": None, "ci95": None, "verdict": verdict}
    mean = sum(values) / n
    if n < 2:
        return {"n": n, "mean": mean, "sd": None, "ci95": None, "verdict": verdict}
    sd = (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5
    df = n - 1
    t = next((_T95[k] for k in sorted(_T95) if k >= df), 1.96)
    half = t * sd / n**0.5
    return {"n": n, "mean": mean, "sd": sd, "ci95": [mean - half, mean + half], "verdict": verdict}


def _count(rows: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "not_stored")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _state_counts(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        state = str(row.get("execution_state") or execution_state(row))
        counts[state] = counts.get(state, 0) + 1
    return counts


def _membership(root: Path, selection_id: str | None) -> dict[str, dict]:
    if not selection_id:
        return {}
    path = root / "selections" / f"{selection_id}.json"
    if not path.is_file():
        return {}
    saved = read(path)
    return {
        str(row["market_id"]): row for row in saved.get("included") or [] if row.get("market_id")
    }


def _membership_times(root: Path, selection_id: str | None) -> dict[str, tuple[Any, Any]]:
    return {
        market: (row.get("window_start_ns"), row.get("window_end_ns"))
        for market, row in _membership(root, selection_id).items()
    }


def k_check(k_value: Any, provider_value: Any) -> str:
    """Compare the K the strategy used with the provider's later price-to-beat."""
    if k_value is None:
        return "not_stored"
    if provider_value is None:
        return "provider_not_published"
    try:
        same = abs(Decimal(str(k_value)) - Decimal(str(provider_value))) <= Decimal("0.000001")
    except Exception:  # noqa: BLE001 - unparseable values are reported, not guessed
        return "not_comparable"
    return "confirmed_by_provider" if same else "differs_from_provider"


def _saved_trace(job_dir: Path, unit_id: str) -> dict | None:
    if not unit_id:
        return None
    path = part_path(job_dir / "parts", unit_id, ".decisions.json")
    return read(path) if path.is_file() else None


def _attempts_from_trace(trace: dict | None) -> list[dict]:
    return [
        {
            "role": item.get("role"),
            "outcome": item.get("outcome"),
            "outcome_reason": item.get("outcome_reason"),
            "side": item.get("side"),
            "decided_at_ns": item.get("decided_at_ns"),
        }
        for item in ((trace or {}).get("attempts") or [])
        if isinstance(item, dict)
    ]


def _position_from_trace(trace: dict | None) -> dict | None:
    """Rebuild size, side and price from a saved engine ledger. Nothing is estimated."""
    if not trace or not trace.get("ledger"):
        return None
    sides = {
        str(order.get("order_id")): order.get("side")
        for order in trace.get("orders") or []
        if isinstance(order, dict)
    }
    gross = net = debit = fees = sold = Decimal(0)
    side = None
    for event in trace["ledger"]:
        if not isinstance(event, dict) or event.get("event") != "fill":
            continue
        quantity = Decimal(str(event.get("quantity") or 0))
        price = Decimal(str(event.get("price") or 0))
        fees += Decimal(str(event.get("fee") or 0))
        if event.get("side", "BUY") == "BUY":
            gross += quantity
            net += Decimal(str(event.get("net_quantity") or quantity))
            debit += quantity * price
            side = side or event.get("leg") or sides.get(str(event.get("order_id")))
        else:
            sold += quantity
    if gross == 0 and sold == 0:
        return None
    return {
        "side": side,
        "filled_gross_shares": str(gross),
        "filled_net_shares": str(net),
        "debit": str(debit),
        "vwap": str(debit / gross) if gross else None,
        "fee_usdc": str(fees),
        "sold_shares": str(sold),
    }


def execution_state(row: dict) -> str:
    """One label per window. A request is not a fill and a fill is not an exit."""
    attempts = [item for item in row.get("attempt_summary") or [] if isinstance(item, dict)]
    entries = [item for item in attempts if item.get("role") in (None, "ENTRY")]
    position = row.get("position") or {}
    if row.get("entered") is not True:
        return "no_entry"
    if entries and all(item.get("outcome") == "NOT_SENT" for item in entries):
        return "not_sent"
    if row.get("filled") is not True:
        return "no_fill"
    if _number(position.get("sold_shares")):
        return "exited"
    if any(item.get("outcome") == "PARTIAL" for item in entries):
        return "partial"
    return "filled"


def _batch_for_experiment(root: Path, experiment_id: str) -> dict:
    folder = root / "replay_batches"
    if not folder.is_dir():
        return {}
    for path in folder.glob("*.json"):
        batch = read(path)
        if experiment_id in (batch.get("experiments") or []):
            return batch
    return {}


def _prepare_population(
    rows: list[dict], job_dir: Path, root: Path, selection_id: str | None
) -> list[dict]:
    members = _membership(root, selection_id)
    times = {
        market: (row.get("window_start_ns"), row.get("window_end_ns"))
        for market, row in members.items()
    }
    prepared = []
    for row in rows:
        item = dict(row)
        span = times.get(str(item.get("market_id") or ""))
        if item.get("window_clock_contract") == "market_interval_v1":
            item["window_clock"] = "part"
        else:
            # Earlier parts stored the loaded range (warm-up included) under the
            # window keys. Keep it as the range and label the market from the set.
            if item.get("window_start_ns") is not None:
                item["range_start_ns"] = item.get("window_start_ns")
                item["range_end_ns"] = item.get("window_end_ns")
            if span and span[0] is not None:
                item["window_start_ns"], item["window_end_ns"] = span
                item["window_clock"] = "saved_selection"
            else:
                item["window_start_ns"] = item["window_end_ns"] = None
                item["window_clock"] = "unavailable"
        needs_trace = item.get("entered") is True and (
            not item.get("attempt_summary") or not item.get("position")
        )
        trace = _saved_trace(job_dir, str(item.get("unit_id") or "")) if needs_trace else None
        if not item.get("attempt_summary") and item.get("entered") is True:
            summary = _attempts_from_trace(trace)
            item["attempt_summary"] = summary
            item["attempt_summary_source"] = "saved_trace" if summary else "not_stored"
        if not item.get("position") and item.get("filled") is True:
            rebuilt = _position_from_trace(trace)
            item["position"] = rebuilt
            item["position_source"] = "saved_trace_ledger" if rebuilt else "not_stored"
        elif item.get("position"):
            item["position_source"] = item.get("position_source") or "part"
        item["execution_state"] = execution_state(item)
        member = members.get(str(item.get("market_id") or "")) or {}
        item["k_check"] = k_check(item.get("k_value"), member.get("price_to_beat_value"))
        if member.get("book_profile"):
            item["book_profile"] = member["book_profile"]
        if item["k_check"] == "differs_from_provider":
            # The threshold the strategy traded against is not the settled one.
            limitations = {*(item.get("limitations") or []), "k_differs_from_provider"}
            item["limitations"] = sorted(limitations)
            item["qualified"] = False
            item["qualification"] = "unqualified"
        if not item.get("qualification") or item["qualification"] not in (
            "qualified",
            "qualified_with_notes",
            "unqualified",
        ):
            item["qualification"] = "qualified" if item.get("qualified") is True else "unqualified"
        prepared.append(item)
    prepared.sort(
        key=lambda item: (
            item.get("window_start_ns") is None,
            item.get("window_start_ns") or 0,
            str(item.get("market_id") or ""),
        )
    )
    return prepared


def _ledger(rows: list[dict], ledger: str) -> list[dict]:
    if ledger == "qualified":
        return [row for row in rows if row.get("qualified") is True]
    if ledger == "unqualified":
        return [row for row in rows if row.get("qualified") is False]
    if ledger == "entered":
        return [row for row in rows if row.get("entered") is True]
    if ledger == "nofill":
        return [row for row in rows if row.get("filled") is not True]
    if ledger == "filled":
        return [row for row in rows if row.get("filled") is True]
    return rows


def _series_row(row: dict) -> dict[str, Any]:
    return {
        "unit_id": row.get("unit_id"),
        "market_id": row.get("market_id"),
        "window_start_ns": row.get("window_start_ns"),
        "window_end_ns": row.get("window_end_ns"),
        "qualified": row.get("qualified"),
        "entered": row.get("entered"),
        "filled": row.get("filled"),
        "realized_pnl": row.get("realized_pnl"),
        "label_winner": row.get("label_winner"),
        "configuration_name": row.get("configuration_name"),
        "attempt_summary": row.get("attempt_summary") or [],
        "execution_state": row.get("execution_state"),
        "qualification": row.get("qualification"),
        "qualification_notes": row.get("qualification_notes") or {},
        "k_check": row.get("k_check"),
        "position": row.get("position"),
        "window_clock": row.get("window_clock"),
        "limitations": row.get("limitations") or [],
    }


def results_open(request: dict, root: Path) -> dict[str, Any]:
    job_dir, body = _job(root, request["job_id"])
    stamp = _manifest_stamp(job_dir)
    cached = _POPULATIONS.get(str(job_dir))
    if stamp is not None and cached is not None and cached[0] == stamp:
        rows = cached[1]
    else:
        rows = _part_rows(job_dir)
        if stamp is not None:
            _POPULATIONS[str(job_dir)] = (stamp, rows)
    experiment_path = root / "experiments" / f"{body['experiment_id']}.json"
    experiment = read(experiment_path) if experiment_path.is_file() else {}
    status_path = job_dir / "status.json"
    status = read(status_path) if status_path.is_file() else {}
    names: list[str] = [
        str(item.get("name")) for item in experiment.get("configurations") or [] if item.get("name")
    ]
    for row in rows:
        name = str(row.get("configuration_name") or "Replay")
        if name not in names:
            names.append(name)
    if request.get("all_configurations"):
        selected = None
        chosen = rows
    else:
        requested = request.get("configuration_name")
        selected = str(requested or (names[0] if names else ""))
        chosen = (
            [row for row in rows if str(row.get("configuration_name") or "Replay") == selected]
            if selected
            else rows
        )
    population = _prepare_population(chosen, job_dir, root, experiment.get("selection_id"))
    summary = population_summary(population)
    filtered = _ledger(population, str(request.get("ledger") or "all"))
    windows, next_cursor = slice_page(filtered, request)
    limitation = experiment.get("limitation")
    if experiment.get("outcome_conditioned"):
        limitation = limitation or "outcome_conditioned"
    batch = _batch_for_experiment(root, str(body["experiment_id"]))
    selection_id = experiment.get("selection_id")
    selection_path = root / "selections" / f"{selection_id}.json" if selection_id else None
    selection = read(selection_path) if selection_path and selection_path.is_file() else {}
    error = status.get("error")
    return {
        "job_id": request["job_id"],
        "experiment_id": body["experiment_id"],
        "plan_digest": body["plan_digest"],
        "windows": windows,
        "window_total": len(population),
        "ledger_total": len(filtered),
        "job_window_total": len(rows),
        "next_cursor": next_cursor,
        "missing_pnl_is_zero": False,
        "limitation": limitation,
        "traces": "saved_parts_only",
        "qualified_windows": summary["qualified"],
        "unqualified_windows": summary["unqualified"],
        "job_completion_is_qualification": False,
        "mode": experiment.get("mode"),
        "selection_id": selection_id,
        "selection_name": selection.get("name") or batch.get("name"),
        "batch_name": batch.get("name"),
        "research_policy": experiment.get("research_policy"),
        "scenario_name": experiment.get("scenario_name"),
        "window_count": experiment.get("window_count") or len(rows),
        "configurations": experiment.get("configurations") or [],
        "execution": experiment.get("execution") or {},
        "strategy_kind": experiment.get("strategy_kind"),
        "engine": experiment.get("engine") or ENGINE_BEFORE_VERSIONING,
        "engine_current": (experiment.get("engine") or ENGINE_BEFORE_VERSIONING) == REPLAY_ENGINE,
        "batch_id": batch.get("batch_id"),
        "created_at": body.get("created_at"),
        "selection_window_count": len(selection.get("included") or []) or None,
        "recordings": len(selection.get("block_ids") or []) or None,
        "configuration_names": names,
        "configuration_name": selected,
        "summary": summary,
        "series": [_series_row(row) for row in population],
        "holdout": experiment.get("holdout"),
        "job_state": status.get("state"),
        "expected": status.get("expected"),
        "completed": status.get("completed"),
        "error": error.get("reason") if isinstance(error, dict) else error,
    }


def results_compare(request: dict, root: Path) -> dict[str, Any]:
    left = results_open(
        {"job_id": request["left_job_id"], "all_configurations": True, "limit": 100000}, root
    )
    right = results_open(
        {"job_id": request["right_job_id"], "all_configurations": True, "limit": 100000}, root
    )
    left_ids = {row.get("market_id") for row in left["windows"] if row.get("market_id")}
    right_ids = {row.get("market_id") for row in right["windows"] if row.get("market_id")}
    common = sorted(left_ids & right_ids)
    paired = bool(common) and request.get("pairing") == "common_markets"
    return {
        "left_job_id": request["left_job_id"],
        "right_job_id": request["right_job_id"],
        "common_markets": common,
        "pairing": "common_markets" if paired else "unpaired",
        "denominator": "common_markets" if paired else "not_paired",
        "left_windows": len(left["windows"]),
        "right_windows": len(right["windows"]),
        "unknown_pnl": "omitted",
        "limitations": [item for item in (left.get("limitation"), right.get("limitation")) if item],
    }


def results_trace(request: dict, root: Path) -> dict[str, Any]:
    job_dir, _body = _job(root, request["job_id"])
    trace = part_path(job_dir / "parts", str(request["unit_id"]), ".decisions.json")
    if not trace.is_file():
        return {"available": False, "unit_id": request["unit_id"], "reason": "trace_not_saved"}
    return {"available": True, "unit_id": request["unit_id"], "trace": read(trace)}


def comparison_save(request: dict, root: Path) -> dict[str, Any]:
    compared = results_compare(request, root)
    comparison_id = request.get("comparison_id") or uuid.uuid4().hex
    path = root / "comparisons" / f"{comparison_id}.json"
    if path.exists():
        raise WorkflowError(
            "comparison_exists", "a saved comparison is immutable", {"comparison_id": comparison_id}
        )
    body = {
        "schema": "workflow_comparison_v1",
        "comparison_id": comparison_id,
        "created_at": now_iso(),
        **compared,
    }
    body["digest"] = plan_digest(compared)
    write_json(path, body)
    return {"comparison_id": comparison_id, "digest": body["digest"], "path": str(path)}


def _research_root(request: dict, root: Path) -> Path:
    return Path(request["research_root"]) if request.get("research_root") else root


def _split_file(request: dict) -> Path | None:
    raw = request.get("split_file")
    return Path(raw) if raw else None


def _hold(request: dict, root: Path) -> dict:
    from tyrex_pm.research.strategy_lab.qualification import holdout_untouched

    split = _split_file(request)
    return holdout_untouched(research_root=_research_root(request, root), split_file=split)


def _write_usage(request: dict, root: Path, purpose: str) -> dict[str, Any]:
    research = _research_root(request, root)
    guard = research / "guard"
    guard.mkdir(parents=True, exist_ok=True)
    try:
        lock = acquire(guard)
    except StateDirectoryBusy as exc:
        raise WorkflowError("guard_busy", "another evaluation check holds the guard", {}) from exc
    try:
        markets = [str(item) for item in request.get("market_ids") or []]
        check = _hold(request, root)
        from tyrex_pm.research.strategy_lab.staged import frozen_split

        split = _split_file(request)
        hold = set(frozen_split(path=split)["holdout"] if split else frozen_split()["holdout"])
        if purpose == "evaluation" and (set(markets) & hold) and not check["untouched"]:
            raise WorkflowError(
                "holdout_exposed",
                "reserved markets already appear in saved usage or results",
                {"market_ids": sorted(set(markets) & hold)},
            )
        usage_id = uuid.uuid4().hex
        payload = {
            "schema": "workflow_usage_v1",
            "usage_id": usage_id,
            "purpose": purpose,
            "market_ids": markets,
            "partial": True,
            "created_at": now_iso(),
            "job_id": request.get("job_id"),
        }
        write_json(research / "usages" / usage_id / "workflow_usage.json", payload)
        return {"usage_id": usage_id, "holdout_check": check, "purpose": purpose}
    finally:
        lock.release()


def diagnostic_start(request: dict, root: Path) -> dict[str, Any]:
    recorded = _write_usage(request, root, "diagnostic")
    return {**recorded, "replay_started": False}


def evaluation_open(request: dict, root: Path) -> dict[str, Any]:
    if request.get("purpose") != "evaluation":
        raise WorkflowError(
            "explicit_purpose", "opening reserved evaluation requires purpose=evaluation", {}
        )
    return _write_usage(request, root, "evaluation")


def candidate_save(request: dict, root: Path) -> dict[str, Any]:
    candidate_id = request.get("candidate_id") or uuid.uuid4().hex
    path = root / "candidates" / f"{candidate_id}.json"
    if path.exists():
        raise WorkflowError(
            "candidate_exists", "a saved candidate is immutable", {"candidate_id": candidate_id}
        )
    body = {
        "schema": "workflow_candidate_v1",
        "candidate_id": candidate_id,
        "created_at": now_iso(),
        "strategy_kind": request["strategy_kind"],
        "parameters": request.get("parameters") or {},
        "rationale": request.get("rationale") or "",
        "result_ids": list(request.get("result_ids") or []),
        "frozen": False,
    }
    # Optional identity a result-backed candidate carries. Absent keys stay absent.
    for key in (
        "name",
        "configuration_name",
        "configuration_fingerprint",
        "scenario",
        "execution",
        "research_policy",
        "selection_id",
        "experiment_id",
        "origin",
    ):
        if request.get(key) is not None:
            body[key] = request[key]
    if body.get("origin") == "replay":
        body["holdout"] = "no_holdout_evaluation"
    body["digest"] = plan_digest(
        {
            "strategy_kind": body["strategy_kind"],
            "parameters": body["parameters"],
            "result_ids": body["result_ids"],
        }
    )
    write_json(path, body)
    return {"candidate_id": candidate_id, "digest": body["digest"], "frozen": False}


def candidate_freeze(request: dict, root: Path) -> dict[str, Any]:
    candidate_id = request["candidate_id"]
    path = root / "candidates" / f"{candidate_id}.json"
    if not path.exists():
        raise WorkflowError("not_found", "candidate is not saved", {"candidate_id": candidate_id})
    saved = read(path)
    freeze_path = root / "candidates" / f"{candidate_id}.freeze.json"
    if saved.get("frozen") or freeze_path.exists():
        raise WorkflowError(
            "already_frozen", "freeze is a one-time record", {"candidate_id": candidate_id}
        )
    frozen = {
        **saved,
        "frozen": True,
        "frozen_at": now_iso(),
        "freeze_rationale": request.get("rationale") or saved.get("rationale") or "",
    }
    write_json(freeze_path, frozen)
    return {"candidate_id": candidate_id, "frozen": True, "path": str(freeze_path)}


def candidate_list(request: dict, root: Path) -> dict[str, Any]:
    folder = root / "candidates"
    rows = []
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            if path.name.endswith(".freeze.json"):
                continue
            body = read(path)
            freeze = folder / f"{body.get('candidate_id')}.freeze.json"
            frozen = read(freeze) if freeze.exists() else None
            rows.append(
                {
                    **body,
                    "frozen": frozen is not None,
                    "frozen_at": (frozen or {}).get("frozen_at"),
                }
            )
    rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    page, cursor = slice_page(rows, request)
    return {"candidates": page, "next_cursor": cursor, "total": len(rows)}
