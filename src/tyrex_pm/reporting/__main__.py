"""CLI: ``python -m tyrex_pm.reporting build_db|summarize``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tyrex_pm.reporting.etl.jsonl_to_sqlite import build_sqlite_from_jsonl
from tyrex_pm.reporting.summarize import build_summary, write_summary_artifacts


def _require_run_artifacts(run_dir: Path) -> None:
    """Post-run tools expect a single run directory (manifest + facts), not repo root."""
    man = run_dir / "manifest.json"
    facts = run_dir / "facts.jsonl"
    if not man.is_file() and not facts.is_file():
        print(
            "ERROR: --run-dir must be the Tyrex run folder that contains manifest.json "
            "and/or facts.jsonl (e.g. var/reporting/runs/<uuid>), not the repo root.\n"
            f"Given: {run_dir.resolve()}",
            file=sys.stderr,
        )
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tyrex_pm.reporting")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_db = sub.add_parser("build_db", help="facts.jsonl → run.sqlite (REC-05)")
    p_db.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Directory for one run (contains manifest.json / facts.jsonl), e.g. var/reporting/runs/<uuid>",
    )

    p_sum = sub.add_parser("summarize", help="summary.json + summary.md (RPT-01)")
    p_sum.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Same as build_db: the run folder under var/reporting/runs/<uuid>, not repo root.",
    )
    p_sum.add_argument(
        "--also-build-db",
        action="store_true",
        help="Build run.sqlite first if missing (recommended before summarize).",
    )
    p_sum.add_argument(
        "--fail-on-dq",
        action="store_true",
        help="Exit non-zero when manifest data_quality shows order_events_sparse (framework).",
    )

    args = ap.parse_args(argv)
    run_dir: Path = args.run_dir.resolve()

    if args.cmd == "build_db":
        _require_run_artifacts(run_dir)
        out = build_sqlite_from_jsonl(run_dir)
        print(out)
        return 0

    if args.cmd == "summarize":
        _require_run_artifacts(run_dir)
        if args.also_build_db:
            db = run_dir / "run.sqlite"
            if not db.is_file():
                build_sqlite_from_jsonl(run_dir)
        js, md = write_summary_artifacts(run_dir)
        print(js)
        print(md)
        if args.fail_on_dq:
            summary = build_summary(run_dir)
            dq = summary.get("data_quality_flags") or {}
            if dq.get("order_events_sparse"):
                msg = (
                    "ERROR: data_quality.order_events_sparse — "
                    "framework execution truth incomplete."
                )
                print(msg, file=sys.stderr)
                return 2
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
