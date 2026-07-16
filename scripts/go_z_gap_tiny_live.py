#!/usr/bin/env python3
"""Single-command Z-Gap tiny live session orchestrator."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _maybe_load_dotenv(repo_root: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file():
        load_dotenv(cwd_env, override=True)
        return
    p = repo_root / ".env"
    if p.is_file():
        load_dotenv(p, override=True)


_maybe_load_dotenv(REPO)

from tyrex_pm.runtime.z_gap_session_orchestrator import (  # noqa: E402
    STATUS_NOT_READY,
    STATUS_PTB_READY,
    STATUS_READY_TO_WAIT,
    ZGapSessionOrchestrator,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Z-Gap single-session tiny live orchestrator")
    p.add_argument("--run-name", required=True, help="Run name for reporting")
    p.add_argument("--next-window", action="store_true", help="Auto-select next BTC 5m window")
    p.add_argument("--event-url", default=None, help="Explicit Polymarket event URL (overrides --next-window)")
    p.add_argument("--scenario", default="config/scenarios/live_z_gap_tiny.yaml")
    p.add_argument("--artifacts-dir", default="var/reporting/z_gap")
    p.add_argument(
        "--execute",
        action="store_true",
        help="Operator authorization to permit order submission (still gated)",
    )
    p.add_argument(
        "--non-interactive",
        action="store_true",
        help="Read-only validation / CI — never approves or executes",
    )
    p.add_argument(
        "--observe-only",
        action="store_true",
        help="Experimental observe session — live feeds, no orders, minimal blockers",
    )
    p.add_argument(
        "--experimental-live",
        action="store_true",
        help="Experimental tiny live — real OMS, max $5, one window/entry, interactive approval",
    )
    p.add_argument(
        "--max-usd",
        type=str,
        default="5",
        help="Maximum USD for --experimental-live (must be <= 5)",
    )
    p.add_argument(
        "--commission-ptb",
        action="store_true",
        help="Observe-only multi-window PTB commissioning (never submits orders)",
    )
    p.add_argument(
        "--windows",
        type=int,
        default=3,
        help="Usable windows required for --commission-ptb (default: 3)",
    )
    p.add_argument(
        "--max-attempts",
        type=int,
        default=6,
        help="Maximum window attempts for --commission-ptb (default: 6)",
    )
    return p


async def _async_main(args: argparse.Namespace) -> int:
    from tyrex_pm.runtime.z_gap_sidecar_supervisor import SidecarSupervisor

    orchestrator = ZGapSessionOrchestrator(
        repo_root=REPO,
        scenario_file=args.scenario,
        artifacts_dir=Path(args.artifacts_dir),
    )
    orchestrator.deps.sidecar = SidecarSupervisor()

    if args.commission_ptb:
        if args.execute:
            print("ERROR: --commission-ptb is observe-only and cannot be combined with --execute")
            return 2
        if not args.non_interactive:
            from scripts.preflight_binance_connectivity import run_check as binance_check
            from tyrex_pm.runtime.z_gap_clock_sanity import evaluate_clock_sanity
            from tyrex_pm.runtime.time_authority import sample_offset
            from datetime import datetime, timezone
            import httpx

            async def _binance():
                return await binance_check(
                    symbol="BTCUSDT",
                    streams=("bookTicker", "aggTrade"),
                    ws_base="wss://stream.binance.com:9443",
                    connect_timeout_s=15.0,
                    first_message_timeout_s=20.0,
                    test_reconnect=True,
                )

            def _clock():
                local_dt = datetime.now(timezone.utc)
                with httpx.Client(timeout=10.0) as client:
                    resp = client.get("https://api.binance.com/api/v3/time")
                    resp.raise_for_status()
                    server_ms = resp.json()["serverTime"]
                reference_dt = datetime.fromtimestamp(float(server_ms) / 1000.0, tz=timezone.utc)
                report = evaluate_clock_sanity(
                    local_dt=local_dt,
                    reference_dt=reference_dt,
                    reference_source="binance_rest_serverTime",
                    threshold_ms=500.0,
                    time_authority=sample_offset(require_feeds_not_started=False, timeout_s=10.0),
                )
                return report, None

            orchestrator.deps.run_binance_check = _binance
            orchestrator.deps.run_clock_check = _clock

            from tyrex_pm.runtime.z_gap_run import resolve_fee_model

            async def _fee_model(app_cfg, meta_obj):
                coord = type("C", (), {"market_info_cache": None})()
                zg = app_cfg.z_gap
                if zg is None:
                    raise RuntimeError("z_gap config missing")
                return await resolve_fee_model(coord, zg)

            orchestrator.deps.resolve_fee_model = _fee_model

        result = await orchestrator.run_commission_ptb(
            run_name=args.run_name,
            windows_required=args.windows,
            max_attempts=args.max_attempts,
        )
        print(f"PTB commissioning status: {result.status}")
        print(f"windows_usable: {result.windows_usable}/{result.windows_required}")
        print(f"windows_attempted: {result.windows_attempted}")
        if result.certificate_path:
            print(f"Certificate: {result.certificate_path}")
        if result.report_path:
            print(f"Report: {result.report_path}")
        for row in result.window_rows:
            print(
                f"  window {row.get('attempt')}: usable={row.get('usable')} "
                f"market={row.get('market_id')} error_bps={row.get('ptb_error_bps')} "
                f"exclude={row.get('exclude_reason')}"
            )
        if result.blockers:
            for b in result.blockers:
                print(f"BLOCKER: {b}")
        return 0 if result.status != STATUS_NOT_READY else 1

    if not args.non_interactive:
        from scripts.preflight_binance_connectivity import run_check as binance_check
        from tyrex_pm.runtime.z_gap_clock_sanity import evaluate_clock_sanity
        from tyrex_pm.runtime.time_authority import sample_offset
        from datetime import datetime, timezone
        import httpx

        async def _binance():
            return await binance_check(
                symbol="BTCUSDT",
                streams=("bookTicker", "aggTrade"),
                ws_base="wss://stream.binance.com:9443",
                connect_timeout_s=15.0,
                first_message_timeout_s=20.0,
                test_reconnect=True,
            )

        def _clock():
            local_dt = datetime.now(timezone.utc)
            with httpx.Client(timeout=10.0) as client:
                resp = client.get("https://api.binance.com/api/v3/time")
                resp.raise_for_status()
                server_ms = resp.json()["serverTime"]
            reference_dt = datetime.fromtimestamp(float(server_ms) / 1000.0, tz=timezone.utc)
            report = evaluate_clock_sanity(
                local_dt=local_dt,
                reference_dt=reference_dt,
                reference_source="binance_rest_serverTime",
                threshold_ms=500.0,
                time_authority=sample_offset(require_feeds_not_started=False, timeout_s=10.0),
            )
            return report, None

        orchestrator.deps.run_binance_check = _binance
        orchestrator.deps.run_clock_check = _clock

        from tyrex_pm.runtime.z_gap_run import resolve_fee_model

        async def _fee_model(app_cfg, meta_obj):
            coord = type("C", (), {"market_info_cache": None})()
            zg = app_cfg.z_gap
            if zg is None:
                raise RuntimeError("z_gap config missing")
            return await resolve_fee_model(coord, zg)

        orchestrator.deps.resolve_fee_model = _fee_model

    execute = bool(args.execute and not args.non_interactive and not args.observe_only and not args.experimental_live)
    interactive = not args.non_interactive

    from decimal import Decimal

    max_usd = Decimal(str(args.max_usd))
    if args.experimental_live and max_usd > Decimal("5"):
        print("ERROR: --max-usd cannot exceed 5 for experimental live")
        return 2

    result = await orchestrator.run_session(
        run_name=args.run_name,
        next_window=args.next_window or args.event_url is None,
        event_url=args.event_url,
        execute=execute or args.experimental_live,
        interactive=interactive,
        observe_only=args.observe_only or (args.non_interactive and not args.experimental_live),
        experimental_live=args.experimental_live and not args.non_interactive,
        max_usd=max_usd if args.experimental_live else None,
    )

    print(f"Z-Gap session status: {result.status}")
    print(f"market_id: {result.market_id}")
    if result.blockers:
        for b in result.blockers:
            print(f"BLOCKER: {b}")
    if result.warnings:
        for w in result.warnings:
            print(f"WARN: {w}")
    if result.report_path:
        print(f"Report: {result.report_path}")

    if result.status == STATUS_NOT_READY:
        return 1
    if result.status == STATUS_READY_TO_WAIT:
        return 0
    if result.status == STATUS_PTB_READY:
        return 0
    return 0 if not result.blockers else 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.non_interactive and args.execute:
        print("ERROR: --non-interactive cannot be combined with --execute")
        return 2
    if args.observe_only and args.experimental_live:
        print("ERROR: --observe-only and --experimental-live are mutually exclusive")
        return 2
    if args.experimental_live and args.commission_ptb:
        print("ERROR: --experimental-live cannot be combined with --commission-ptb")
        return 2
    if args.commission_ptb and args.non_interactive:
        print("ERROR: --commission-ptb requires live connectivity checks; omit --non-interactive")
        return 2
    if not args.next_window and not args.event_url and not args.commission_ptb:
        print("ERROR: specify --next-window, --event-url, or --commission-ptb")
        return 2
    if args.commission_ptb:
        args.next_window = True
        args.observe_only = True
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
