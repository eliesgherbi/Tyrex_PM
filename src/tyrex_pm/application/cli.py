"""Command-line entrypoint for the unified trading runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm import __version__
from tyrex_pm.runtime.run_config import RunConfigError, load_trading_run_config

DEFAULT_CONFIG = Path("config/runs/z_gap_tiny_live.yaml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tyrex-pm",
        description="Tyrex_PM event-driven strategy execution framework",
    )
    parser.add_argument("--version", action="version", version=f"tyrex-pm {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("help", help="Print command help")
    commands.add_parser("version", help="Print package version")
    run = commands.add_parser("run", help="Run one configured production lifecycle")
    run.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Single run configuration (default: {DEFAULT_CONFIG})",
    )
    run.add_argument(
        "--live",
        action="store_true",
        help="Authorize venue order submission for this process",
    )
    run.add_argument(
        "--paper",
        action="store_true",
        help="Run a venue: paper config (live public data, simulated account; no signing)",
    )
    run.add_argument("--dotenv", type=Path, default=Path(".env"))
    run.add_argument("--out-dir", type=Path, default=None)
    run.add_argument(
        "--validate-config",
        action="store_true",
        help="Validate configuration and exit without network access",
    )
    run.add_argument(
        "--show-config",
        action="store_true",
        help="Print the typed effective configuration and exit",
    )
    run.add_argument(
        "--check-account",
        action="store_true",
        help="Inspect the account and write startup_report.json without trading",
    )
    ev = commands.add_parser("eval", help="Replay evaluate() on a lab fixture (no venue)")
    ev.add_argument("--config", type=Path, required=True, help="Run YAML (kind + parameters)")
    ev.add_argument("--fixture", type=Path, required=True, help="lab_evaluate_v1 JSON pack")
    ev.add_argument(
        "--live",
        action="store_true",
        help="Rejected: eval never submits orders",
    )
    ev.add_argument("--out", type=Path, default=None, help="Optional JSON output path")
    obs = commands.add_parser(
        "observe", help="Signal-only live observation over successive windows (no orders)"
    )
    obs.add_argument("--config", type=Path, required=True, help="Observation YAML (mode: observe)")
    obs.add_argument("--out-dir", type=Path, default=None)
    obs.add_argument(
        "--validate-config", action="store_true", help="Validate and exit without network access"
    )
    obs.add_argument(
        "--connect-public-feeds",
        action="store_true",
        help="Required to start: connects to public market-data feeds (never submits orders)",
    )
    obs.add_argument("--live", action="store_true", help="Rejected: observe never submits orders")
    from tyrex_pm.application.settlement_cli import add_parser as add_settlement_parser

    add_settlement_parser(commands)
    from tyrex_pm.application.watch_run import add_parser as add_watch_parser

    add_watch_parser(commands)
    return parser


def _config_payload(config) -> dict:  # noqa: ANN001 - dataclass projection
    return {
        "schema_version": config.schema_version,
        "run_name": config.run_name,
        "strategy_kind": config.strategy_kind,
        "market": config.market.__dict__,
        "risk": {
            key: value if isinstance(value, bool) else str(value)
            for key, value in config.risk.__dict__.items()
        },
        "lifecycle": {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in config.lifecycle.__dict__.items()
        },
        "state_directory": str(config.state_directory),
        "report_directory": str(config.report_directory),
        "lifecycle_mode": config.lifecycle_mode,
        "venue": config.venue,
        "run_mode": config.market.run_mode,
        "run_limits": None
        if config.settlement is None or config.settlement.run_limits is None
        else {k: str(v) for k, v in config.settlement.run_limits.__dict__.items()},
    }


def _run(args: argparse.Namespace) -> int:
    try:
        config = load_trading_run_config(args.config)
    except (RunConfigError, OSError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.validate_config:
        print(
            f"config ok strategy={config.strategy_kind} run_name={config.run_name} "
            f"venue={config.venue} run_mode={config.market.run_mode} "
            f"lifecycle={config.lifecycle_mode}"
        )
        return 0
    if args.show_config:
        print(json.dumps(_config_payload(config), indent=2, default=str))
        return 0
    if args.check_account and args.live:
        print("refusing --check-account together with --live", file=sys.stderr)
        return 2
    if args.check_account:
        return _check_account(args, config)
    if config.venue == "paper":
        if args.live:
            print("refusing --live with a venue: paper config", file=sys.stderr)
            return 2
        if not args.paper:
            print("paper run: re-run with --paper after reviewing the config.", file=sys.stderr)
            return 2
    elif args.paper:
        print("--paper requires a config with venue: paper", file=sys.stderr)
        return 2
    elif not args.live:
        print(
            "LIVE submission is disabled. Re-run with --live after reviewing the config.",
            file=sys.stderr,
        )
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or config.report_directory / f"{config.run_name}_{stamp}"
    existing_report = next(
        (
            path
            for path in (
                out_dir / "run_summary.json",
                out_dir / "startup_report.json",
            )
            if path.exists()
        ),
        None,
    )
    if existing_report is not None:
        print(f"refusing to overwrite existing report: {existing_report}", file=sys.stderr)
        return 2
    from tyrex_pm.runtime.trading_runtime import run_trading_runtime

    try:
        result = asyncio.run(
            run_trading_runtime(
                config=config,
                output_directory=out_dir,
                dotenv=args.dotenv if args.dotenv.exists() else None,
                config_path=args.config,
            )
        )
    except KeyboardInterrupt:
        print("run interrupted by operator", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"runtime error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "outcome": result.outcome,
                "ok": result.ok,
                "report": str(result.report_path).replace("\\", "/"),
                "live_requested": config.venue == "live",
                "venue": config.venue,
                "mutation_attempts": result.mutation_attempts,
                "runtime": "unified_trading_runtime",
            },
            indent=2,
        )
    )
    startup_report = out_dir / "startup_report.json"
    if startup_report.exists():
        payload = json.loads(startup_report.read_text(encoding="utf-8"))
        if payload.get("summary"):
            print(payload["summary"], file=sys.stderr)
    print(f"report={result.report_path}", file=sys.stderr)
    return 0 if result.ok else 2


def _check_account(args: argparse.Namespace, config) -> int:  # noqa: ANN001
    """Online inspection only. This process cannot sign, post, cancel, or redeem."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or config.report_directory / f"{config.run_name}_check_{stamp}"
    existing = out_dir / "startup_report.json"
    if existing.exists():
        print(f"refusing to overwrite existing report: {existing}", file=sys.stderr)
        return 2
    from tyrex_pm.runtime.trading_runtime import run_account_inspection

    try:
        status = asyncio.run(
            run_account_inspection(
                config=config,
                output_directory=out_dir,
                dotenv=args.dotenv if args.dotenv.exists() else None,
            )
        )
    except KeyboardInterrupt:
        print("account check interrupted by operator", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"account check error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    report_path = out_dir / "startup_report.json"
    if report_path.exists():
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        print(payload.get("summary") or status)
        for proposal in payload.get("proposals") or ():
            print(
                f"Proposal {proposal.get('issue_id')}: {proposal.get('action')} "
                f"up to {proposal.get('max_shares')} shares."
            )
        print(f"live_requested={str(payload.get('live_requested')).lower()}")
    else:
        print(status)
    return 0 if status == "READY" else 2


def _eval(args: argparse.Namespace) -> int:
    if args.live:
        print("eval refuses --live; it never submits orders.", file=sys.stderr)
        return 2
    from tyrex_pm.lab.evaluate_replay import evaluation_as_record, replay_configured_run

    try:
        results = replay_configured_run(config_path=args.config, fixture_path=args.fixture)
    except (RunConfigError, OSError, ValueError) as exc:
        print(f"eval error: {exc}", file=sys.stderr)
        return 2
    payload = [evaluation_as_record(item) for item in results]
    text = json.dumps(payload, indent=2, default=str)
    print(text)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


def _observe(args: argparse.Namespace) -> int:
    if args.live:
        print("observe refuses --live; it never plans, signs or submits orders.", file=sys.stderr)
        return 2
    from tyrex_pm.runtime.observation import (
        ObservationConfigError,
        load_observation_config,
        run_observation_sync,
    )

    try:
        config = load_observation_config(args.config)
    except (ObservationConfigError, OSError, ValueError) as exc:
        print(f"observe config error: {exc}", file=sys.stderr)
        return 2
    if args.validate_config:
        print(
            f"observe config ok strategy={config.strategy_kind} run_name={config.run_name} "
            f"max_windows={config.max_windows}"
        )
        return 0
    if not args.connect_public_feeds:
        print(
            "observe connects to public feeds; re-run with --connect-public-feeds to start.",
            file=sys.stderr,
        )
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or config.output_directory / f"{config.run_name}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = run_observation_sync(config, out_dir=out_dir)
    print(json.dumps({k: summary[k] for k in ("evaluations", "recommendations", "rollovers")}))
    print(f"records: {out_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "run":
        return _run(args)
    if args.command == "eval":
        return _eval(args)
    if args.command == "observe":
        return _observe(args)
    if args.command == "settlement":
        from tyrex_pm.application.settlement_cli import run as run_settlement

        return run_settlement(args)
    if args.command == "watch-run":
        from tyrex_pm.application.watch_run import run as run_watch

        return run_watch(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
