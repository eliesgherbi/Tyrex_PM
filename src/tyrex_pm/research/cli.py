from __future__ import annotations

import argparse
import platform
from dataclasses import asdict
from pathlib import Path

import pyarrow.parquet as pq

from tyrex_pm.data_warehouse.capture.files import atomic_json, read_json, within
from tyrex_pm.data_warehouse.config import strict
from tyrex_pm.data_warehouse.contracts import canonical, digest
from tyrex_pm.data_warehouse.quality.readiness import readiness
from tyrex_pm.data_warehouse.query.export import load_export
from tyrex_pm.data_warehouse.query.reader import exact_rows, order

from .replay.config import Intent, load_scenario
from .replay.engine import Engine


def run_replay(dataset: Path, scenario_path: Path, output: Path) -> dict:
    scenario = load_scenario(scenario_path)
    if scenario.adapter != "scripted_v1":
        raise ValueError("tyrex-research replay runs scripted_v1 scenarios only")
    selection = read_json(dataset)
    allowed = {
        "schema_version",
        "exports",
        "selection_policy",
        "expected_windows",
        "discovered_windows",
    }
    if set(selection) - allowed:
        raise ValueError("unknown dataset selection fields")
    if selection.get("schema_version") != 1 or not selection.get("exports"):
        raise ValueError("dataset requires nonempty immutable export set")
    events = []
    seen = set()
    manifests = []
    quality = []
    for item in selection["exports"]:
        if set(item) != {"path", "manifest_sha256"}:
            raise ValueError("each export needs path and pinned manifest checksum")
        root = (dataset.parent / item["path"]).resolve()
        if digest((root / "manifest.json").read_bytes()) != item["manifest_sha256"]:
            raise ValueError("dataset export manifest changed")
        manifest = load_export(root)
        manifests.append(manifest)
        report = read_json(root / "quality.json")
        quality.append(report)
        if scenario.quality_predicate == "strategy_ready" and not readiness(report)["ready"]:
            raise ValueError("strategy-ready quality predicate failed")
        for file in manifest["files"]:
            if file["dataset"] in (
                "resolution_observations",
                "processing_results",
                "boundary_observations",
                "book_epochs",
                "resolution_assessments",
                "coverage_intervals",
                "market_coverage",
            ):
                continue
            for row in exact_rows(pq.ParquetFile(within(root, file["path"])).read()):
                key = file["dataset"], row["record_id"]
                if key not in seen:
                    seen.add(key)
                    events.append((file["dataset"], row))
    events.sort(key=lambda item: order(item[1]))
    intents_path = (scenario_path.parent / scenario.intents_file).resolve()
    if not scenario.intents_file:
        raise ValueError("scripted replay requires intents_file")
    intents = [strict(Intent, row) for row in read_json(intents_path)]
    if any(
        not any(m["received_from"] <= i.at_ns <= m["received_until"] for m in manifests)
        for i in intents
    ):
        raise ValueError("intent outside exported decision intervals")
    if scenario.label_policy_file:
        raise ValueError(
            "external unverified labels are not accepted; qualify resolution contract first"
        )
    labels = [
        {
            "instrument_id": row["instrument_id"],
            "status": row["status"],
            "payout": row["payout"],
            "available_at": row["available_at"],
        }
        for report in quality
        for row in report.get("scoring_labels") or []
    ]
    result = Engine(scenario).run(events, intents, labels)
    fills = [row for row in result["ledger"] if row["event"] == "fill"]
    market_of = {
        row["instrument_id"]: row["market_id"] for name, row in events if name == "instruments"
    }
    result["dataset_counts"] = {
        "expected": selection.get("expected_windows"),
        "discovered": selection.get("discovered_windows"),
        "exported": len(manifests),
        "eligible": sum(readiness(r)["ready"] for r in quality),
        "excluded": sum(not readiness(r)["ready"] for r in quality),
        # Windows (markets) with at least one fill; orders and fills are counted separately.
        "traded": len({market_of.get(row["instrument_id"], row["instrument_id"]) for row in fills}),
        "filled_orders": len({row["order_id"] for row in fills}),
        "fills": len(fills),
        "unresolved": sum(r.get("settlement_readiness") != "PROVIDER_TERMINAL" for r in quality),
    }
    result["quality_reports"] = quality
    result["selection_policy"] = selection.get("selection_policy")
    result["reproducibility"] = {
        "dataset_sha256": digest(dataset.read_bytes()),
        "scenario_sha256": digest(canonical(asdict(scenario))),
        "intents_sha256": digest(intents_path.read_bytes()),
        "python": platform.python_version(),
        "engine_version": "1",
        "code_files_sha256": {
            p.name: digest(p.read_bytes())
            for p in Path(__file__).parent.joinpath("replay").glob("*.py")
        },
    }
    if output.exists():
        raise ValueError("immutable experiment destination exists")
    output.mkdir(parents=True)
    atomic_json(output / "result.json", result)
    atomic_json(
        output / "manifest.json",
        {
            "schema_version": 1,
            "result_sha256": digest((output / "result.json").read_bytes()),
            "reproducibility": result["reproducibility"],
        },
    )
    return {
        "output": str(output),
        "qualified": result["qualified"],
        "limitations": result["limitations"],
        "dataset_counts": result["dataset_counts"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tyrex-research")
    sub = parser.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay")
    replay.add_argument("--dataset", type=Path, required=True)
    replay.add_argument("--scenario", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(canonical(run_replay(args.dataset, args.scenario, args.output)))
        return 0
    except (ValueError, OSError, ImportError) as exc:
        print(canonical({"error": type(exc).__name__, "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
