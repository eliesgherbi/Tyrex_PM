"""Run one preparation owner. Stop is observed only before the owner call."""

from __future__ import annotations

import argparse
from pathlib import Path

from .artifacts import read, write_json
from .preparation import role_held


def run_preparation(folder: Path) -> int:
    plan = read(folder / "plan.json")
    status = read(folder / "status.json")
    stop_path = folder / "stop.json"
    if stop_path.exists():
        stop = read(stop_path)
        if int(stop.get("generation", -1)) == int(status.get("generation", -2)):
            status["state"] = "interrupted"
            status["stop_requested"] = True
            status["safe_stop"] = "unsupported"
            status["owner_interrupted"] = False
            status["reason"] = "stop_before_owner"
            write_json(folder / "status.json", status)
            return 0
    warehouse = Path(plan["warehouse"])
    busy = [role for role in plan.get("lock_roles") or [] if role_held(warehouse, role)]
    if busy:
        status["state"] = "failed"
        status["error"] = {
            "code": "owner_busy",
            "reason": "owner lock is held",
            "fields": {"roles": busy},
        }
        write_json(folder / "status.json", status)
        return 1
    try:
        kind = plan["kind"]
        collection = plan["collection"]
        if kind == "normalize":
            from tyrex_pm.data_warehouse.normalize.worker import normalize_once

            result = normalize_once(warehouse, collection)
        elif kind == "quality":
            from tyrex_pm.data_warehouse.quality.publish import publish_quality

            result = publish_quality(warehouse, collection)
        else:
            from tyrex_pm.data_warehouse.query.export import verify

            result = verify(warehouse, collection)
    except ValueError as exc:
        status["state"] = "failed"
        code = "owner_busy" if "already owns" in str(exc) else "preparation_failed"
        status["error"] = {"code": code, "reason": str(exc), "fields": {}}
        write_json(folder / "status.json", status)
        return 1
    status = read(folder / "status.json")
    status["state"] = "completed"
    status["owner_result"] = _public(result)
    status["safe_stop"] = "unsupported"
    status["selection_updated"] = False
    status["qualification_granted"] = False
    write_json(folder / "status.json", status)
    return 0


def _public(result: dict) -> dict:
    if not isinstance(result, dict):
        return {"value": str(result)}
    kept = {}
    for key in (
        "new_builds",
        "records",
        "rejected",
        "publication_id",
        "markets",
        "normalization_complete",
    ):
        if key in result:
            kept[key] = result[key]
    if not kept:
        kept["keys"] = sorted(result)[:12]
    return kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tyrex-preparation-worker")
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args(argv)
    return run_preparation(args.job)


if __name__ == "__main__":
    raise SystemExit(main())
