"""Command-line entrypoint for the unified trading runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
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
            key: str(value) if key == "minimum_exit_price" else value
            for key, value in config.lifecycle.__dict__.items()
        },
        "state_directory": str(config.state_directory),
        "report_directory": str(config.report_directory),
    }


def _run(args: argparse.Namespace) -> int:
    try:
        config = load_trading_run_config(args.config)
    except (RunConfigError, OSError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.validate_config:
        print(f"config ok strategy={config.strategy_kind} run_name={config.run_name}")
        return 0
    if args.show_config:
        print(json.dumps(_config_payload(config), indent=2, default=str))
        return 0
    if not args.live:
        print(
            "LIVE submission is disabled. Re-run with --live after reviewing the config.",
            file=sys.stderr,
        )
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or config.report_directory / f"{config.run_name}_{stamp}"
    from tyrex_pm.runtime.trading_runtime import run_trading_runtime

    try:
        result = asyncio.run(
            run_trading_runtime(
                config=config,
                output_directory=out_dir,
                dotenv=args.dotenv if args.dotenv.exists() else None,
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
                "live_requested": True,
                "mutation_attempts": result.mutation_attempts,
                "runtime": "unified_trading_runtime",
            },
            indent=2,
        )
    )
    print(f"report={result.report_path}", file=sys.stderr)
    return 0 if result.ok else 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "run":
        return _run(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
