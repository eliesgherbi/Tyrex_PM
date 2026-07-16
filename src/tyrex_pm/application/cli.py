"""CLI for the clean-reset framework (R3 observe)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

from tyrex_pm import __version__
from tyrex_pm.core.clock import FakeClock
from tyrex_pm.operations import current_btc_updown_slug, next_btc_updown_slug
from tyrex_pm.runtime.config import ObserveConfig, SourceMode, load_observe_config, observe_config_from_mapping
from tyrex_pm.runtime.observe_host import ObserveHost


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tyrex-pm",
        description="Tyrex_PM - Polymarket-focused event-driven trading framework",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"tyrex-pm {__version__}",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("version", help="Print package version")
    sub.add_parser("help", help="Show help (same as -h)")

    observe = sub.add_parser("observe", help="Run read-only ReferenceMomentum observe path")
    observe.add_argument("--config", type=Path, help="Path to observe JSON config")
    observe.add_argument("--mode", choices=["fixture", "live"], help="Override mode")
    observe.add_argument("--fixture", type=Path, help="Fixture JSON path")
    observe.add_argument("--output", type=Path, help="Facts JSONL output path")
    observe.add_argument("--event-slug", type=str, help="Polymarket event slug")
    observe.add_argument("--event-url", type=str, help="Polymarket event URL")
    observe.add_argument("--duration-s", type=float, help="Live runtime seconds")
    observe.add_argument(
        "--btc-window",
        choices=["current", "next"],
        help="Resolve BTC Up/Down 5m slug (live mode helper)",
    )

    discover = sub.add_parser(
        "discover-btc-window",
        help="Print current/next BTC Up/Down slug (operations helper; no second runtime)",
    )
    discover.add_argument("--which", choices=["current", "next"], default="next")
    return parser


def _build_observe_config(args: argparse.Namespace) -> ObserveConfig:
    if args.config:
        cfg = load_observe_config(args.config)
        overrides: dict = {}
        if args.mode:
            overrides["mode"] = args.mode
        if args.fixture:
            overrides["fixture_path"] = str(args.fixture)
        if args.output:
            overrides["output_path"] = str(args.output)
        if args.event_slug:
            overrides["event_slug"] = args.event_slug
        if args.event_url:
            overrides["event_url"] = args.event_url
        if args.duration_s is not None:
            overrides["runtime_duration_s"] = args.duration_s
        if overrides:
            # Re-load via mapping merge
            import json

            raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
            raw.update(overrides)
            cfg = observe_config_from_mapping(raw)
    else:
        if not args.mode:
            raise SystemExit("observe requires --config or --mode")
        if args.mode == "fixture" and not args.fixture:
            raise SystemExit("fixture mode requires --fixture")
        if not args.output:
            raise SystemExit("observe without --config requires --output")
        raw = {
            "mode": args.mode,
            "fixture_path": None if args.fixture is None else str(args.fixture),
            "output_path": str(args.output),
            "binance_symbol": "BTCUSDT",
            "momentum_lookback_ms": 5000,
            "momentum_threshold": "0.001",
            "max_book_spread": "0.10",
            "freshness": {
                "book_threshold_ms": 60000,
                "reference_threshold_ms": 60000,
                "future_tolerance_ms": 500,
                "timestamp_basis": "EVENT_TIME",
            },
            "runtime_duration_s": args.duration_s,
            "event_slug": args.event_slug,
            "event_url": args.event_url,
        }
        cfg = observe_config_from_mapping(raw)

    if args.btc_window:
        if cfg.mode is not SourceMode.LIVE:
            raise SystemExit("--btc-window requires live mode")
        slug = (
            current_btc_updown_slug()
            if args.btc_window == "current"
            else next_btc_updown_slug()
        )
        cfg = ObserveConfig(
            mode=SourceMode.LIVE,
            output_path=cfg.output_path,
            binance_symbol=cfg.binance_symbol,
            momentum_lookback=cfg.momentum_lookback,
            momentum_threshold=cfg.momentum_threshold,
            max_book_spread=cfg.max_book_spread,
            freshness=cfg.freshness,
            runtime_duration=cfg.runtime_duration,
            fixture_path=None,
            event_slug=slug,
            event_url=None,
            condition_id=None,
            evaluate_on_reference=cfg.evaluate_on_reference,
            momentum_min_samples=cfg.momentum_min_samples,
        )
    return cfg


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in (None, "help"):
        parser.print_help()
        return 0
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "discover-btc-window":
        slug = (
            current_btc_updown_slug()
            if args.which == "current"
            else next_btc_updown_slug()
        )
        print(slug)
        return 0
    if args.command == "observe":
        cfg = _build_observe_config(args)
        if cfg.mode is SourceMode.FIXTURE:
            clock = FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc))
            host = ObserveHost(cfg, clock=clock)
            try:
                result = host.run_fixture()
            finally:
                host.close()
            print(
                f"fixture observe complete decisions={len(result.decisions)} "
                f"facts={result.fact_count} path={result.facts_path}"
            )
            return 0
        from tyrex_pm.runtime.live_observe import run_live_observe

        result = asyncio.run(run_live_observe(cfg))
        print(
            f"live observe complete decisions={len(result.decisions)} "
            f"facts={result.fact_count} path={result.facts_path}"
        )
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
