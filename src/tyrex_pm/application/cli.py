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

    shadow = sub.add_parser(
        "shadow",
        help="Run R5 ShadowOMS path (public data only; no real orders)",
    )
    shadow.add_argument("--config", type=Path, required=True, help="Observe+shadow JSON config")
    shadow.add_argument("--event-slug", type=str, help="Polymarket event slug")
    shadow.add_argument("--duration-s", type=float, help="Live runtime seconds")
    shadow.add_argument(
        "--btc-window",
        choices=["current", "next"],
        help="Resolve BTC Up/Down 5m slug",
    )
    shadow.add_argument("--output", type=Path, help="Facts JSONL output path")

    discover = sub.add_parser(
        "discover-btc-window",
        help="Print current/next BTC Up/Down slug (operations helper; no second runtime)",
    )
    discover.add_argument("--which", choices=["current", "next"], default="next")

    preflight = sub.add_parser(
        "live-preflight",
        help=(
            "R6C mutation-impossible account observation preflight "
            "(no submit/cancel/heartbeat; no enable-mutations flag)"
        ),
    )
    preflight.add_argument(
        "--output",
        type=Path,
        default=Path("var/reporting/r6/live_preflight.json"),
        help="Sanitized artifact path",
    )
    preflight.add_argument(
        "--dotenv",
        type=Path,
        default=Path(".env"),
        help="Optional .env path (values never printed)",
    )
    preflight.add_argument(
        "--user-stream-s",
        type=float,
        default=0.0,
        help="Bounded user-WS observe seconds (0=skip; use on target host)",
    )
    preflight.add_argument(
        "--skip-auth",
        action="store_true",
        help="Public connectivity only (no L2 authenticated reads)",
    )

    r7a = sub.add_parser(
        "r7a-prepare",
        help="R7A: dry mutation validation + read-only proposal (no real orders)",
    )
    r7a.add_argument(
        "--output-dir",
        type=Path,
        default=Path("var/reporting/r7"),
        help="Directory for r7a_report.json and approval artifact",
    )
    r7a.add_argument(
        "--preflight",
        type=Path,
        default=Path("var/reporting/r6/live_preflight_r6d.json"),
        help="Prior live-preflight artifact (read-only evidence)",
    )
    r7a.add_argument(
        "--run-preflight",
        action="store_true",
        help="Run live-preflight with user-stream before preparing proposal",
    )

    r7a2 = sub.add_parser(
        "r7a2-prepare",
        help="R7A.2: record position acknowledgment + draft session (no R7B)",
    )
    r7a2.add_argument(
        "--output-dir",
        type=Path,
        default=Path("var/reporting/r7"),
        help="Directory for acknowledgment and draft session artifacts",
    )
    r7a2.add_argument(
        "--preflight",
        type=Path,
        default=Path("var/reporting/r7/live_preflight.json"),
        help="Prior live-preflight artifact (read-only evidence)",
    )
    r7a2.add_argument(
        "--run-preflight",
        action="store_true",
        help="Run live-preflight with user-stream before recording ack",
    )

    live_once = sub.add_parser(
        "live-once",
        help="Legacy R7B path (refuses; use r7b-live-once)",
    )
    live_once.add_argument(
        "--approval",
        type=Path,
        default=None,
        help="Legacy path to R7B approval artifact",
    )
    live_once.add_argument(
        "--session",
        type=Path,
        default=None,
        help="Path to user-authorized R7B session envelope",
    )
    live_once.add_argument(
        "--i-authorize-r7b",
        action="store_true",
        help="Legacy second authorization flag (superseded by r7b-live-once --execute-live)",
    )

    r7b = sub.add_parser(
        "r7b-live-once",
        help=(
            "Operator-owned R7B one-shot: default dry/read-only; "
            "mutations only with explicit --execute-live"
        ),
    )
    r7b.add_argument(
        "--strategy",
        type=str,
        default="reference-momentum",
        help="Must be reference-momentum (ReferenceMomentumStrategy)",
    )
    r7b.add_argument(
        "--market-family",
        type=str,
        default="btc_updown_5m",
        help="Must be btc_updown_5m",
    )
    r7b.add_argument(
        "--max-windows",
        type=int,
        default=3,
        help="Max BTC 5m windows to observe (1-3)",
    )
    r7b.add_argument(
        "--max-buy-collateral",
        type=str,
        default="5.00",
        help="Max BUY amount + entry fee in USDC (≤ 5.00)",
    )
    mode = r7b.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Read-only preview/validation (default when --execute-live omitted)",
    )
    mode.add_argument(
        "--execute-live",
        action="store_true",
        default=False,
        help=(
            "Operator-only: enable mutations for this one process / one lifecycle. "
            "No verbatim statement, session nonce, or chat artifact required. "
            "Absence of this flag remains read-only. Agent must never pass this flag."
        ),
    )
    r7b.add_argument(
        "--output-dir",
        type=Path,
        default=Path("var/reporting/r7b"),
        help="Directory for JSON report + JSONL facts",
    )
    r7b.add_argument(
        "--acknowledgment-path",
        "--acknowledgment",
        dest="acknowledgment",
        type=Path,
        default=Path("var/state/r7/position_acknowledgment.json"),
        help=(
            "Durable acknowledgment state path (required). "
            "Missing/invalid artifact fails closed for dry and live."
        ),
    )
    r7b.add_argument(
        "--allow-dirty-worktree",
        action="store_true",
        help=(
            "Dry-run only: permit dirty worktree. "
            "Ignored/forbidden for --execute-live (clean worktree required)."
        ),
    )

    r7c = sub.add_parser(
        "r7c-recon",
        help="R7C read-only incident / FLAT reconciliation (never mutates)",
    )
    r7c.add_argument(
        "--output",
        type=Path,
        default=Path("var/reporting/r7c/incident_recon.json"),
        help="Sanitized recon report path",
    )
    r7c.add_argument(
        "--buy-order-id",
        type=str,
        default="0x68efa63a23abb0ab55042204683f48f4303ed2db3e9d955317bc41add43e71db",
    )
    r7c.add_argument(
        "--condition-id",
        type=str,
        default="0x32204a5cffff255df6155b69105aded512770c4597ccb4bef721dfa0ab526401",
    )
    r7c.add_argument(
        "--token-id",
        type=str,
        default=(
            "1038082852808592687103030316298741805143710778541582307413776216436336466979"
        ),
    )
    r7c.add_argument(
        "--market-slug",
        type=str,
        default="btc-updown-5m-1784303100",
    )

    ack_regen = sub.add_parser(
        "r7-ack-regenerate",
        help=(
            "R7D.2: regenerate durable acknowledgment from sealed "
            "config/r7/acknowledgment_policy.json identities + read-only inventory "
            "(zero mutations; never acks every resolved position)"
        ),
    )
    ack_regen.add_argument(
        "--output",
        type=Path,
        default=Path("var/state/r7/position_acknowledgment.json"),
        help="Durable acknowledgment state path (not under var/reporting/)",
    )
    ack_regen.add_argument(
        "--report",
        type=Path,
        default=Path("var/reporting/r7d/ack_regenerate_report.json"),
        help="Disposable regeneration report path",
    )

    n6_status = sub.add_parser(
        "n6-status",
        help="N6: print live config defaults and Scope A capability (read-only; no mutations)",
    )
    n6_preflight = sub.add_parser(
        "n6-preflight",
        help=(
            "N6: authenticated read-only preflight via live-preflight "
            "(no submit/cancel/redeem; mutations remain OFF)"
        ),
    )
    n6_preflight.add_argument(
        "--output",
        type=Path,
        default=Path("var/reporting/n6/live_preflight.json"),
    )
    n6_preflight.add_argument("--dotenv", type=Path, default=Path(".env"))
    n6_preflight.add_argument("--user-stream-s", type=float, default=2.0)
    n6_recon = sub.add_parser(
        "n6-recon",
        help="N6: authenticated read-only recon + restart (wrapper; mutations OFF)",
    )
    n6_recon.add_argument("--out-dir", type=Path, default=None)
    n6_recon.add_argument("--dotenv", type=Path, default=Path(".env"))
    n6_kill = sub.add_parser(
        "n6-kill-inspect",
        help="N6: inspect kill-switch / block-new-exposure semantics (no venue I/O)",
    )

    n7_status = sub.add_parser(
        "n7-status",
        help=(
            "N7: print sealed tiny-live defaults (mutations OFF; Scope A; "
            "no real venue mutation)"
        ),
    )
    n7_preflight = sub.add_parser(
        "n7-preflight",
        help=(
            "N7A: authenticated read-only preflight + authorization request "
            "(mutations remain OFF; does not approve or submit)"
        ),
    )
    n7_preflight.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Evidence directory under var/reporting/n7/",
    )
    n7_preflight.add_argument(
        "--config",
        type=Path,
        default=Path("config/n7_tiny_live.json"),
    )
    n7_preflight.add_argument("--dotenv", type=Path, default=Path(".env"))
    n7_preflight.add_argument("--user-stream-s", type=float, default=2.0)
    n7_preflight.add_argument(
        "--allow-dirty",
        action="store_true",
        help="N7A only: allow dirty worktree for read-only preflight",
    )
    n7_auth = sub.add_parser(
        "n7-auth-request",
        help=(
            "N7A: generate a single-use authorization *request* artifact. "
            "Does not approve. Operator must supply the exact phrase for N7B."
        ),
    )
    n7_auth.add_argument(
        "--config",
        type=Path,
        default=Path("config/n7_tiny_live.json"),
    )
    n7_auth.add_argument(
        "--output",
        type=Path,
        default=Path("var/reporting/n7/authorization_request.json"),
    )
    n7_auth.add_argument("--operator-label", type=str, default="operator")
    n7_auth.add_argument(
        "--valid-minutes",
        type=int,
        default=20,
        help="UTC validity window length for the next eligible BTC 5m window",
    )
    n7_oneshot = sub.add_parser(
        "n7-oneshot",
        help=(
            "N7B: operator-gated tiny live one-shot (Scope A). "
            "Requires --authorization-phrase matching the current request. "
            "Without the phrase this command refuses and does not mutate. "
            "N7A does not authorize real venue mutation."
        ),
    )
    n7_oneshot.add_argument(
        "--authorization-phrase",
        type=str,
        default=None,
        help="Exact one-use operator approval phrase (required for real mutation)",
    )
    n7_oneshot.add_argument(
        "--config",
        type=Path,
        default=Path("config/n7_tiny_live.json"),
    )
    n7_oneshot.add_argument(
        "--dry-run",
        action="store_true",
        help="Refuse real mutation; print ceremony requirements only",
    )
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
            risk=cfg.risk,
            shadow=cfg.shadow,
        )
    return cfg


def _build_shadow_config(args: argparse.Namespace) -> ObserveConfig:
    import json

    raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.output:
        raw["output_path"] = str(args.output)
    if args.duration_s is not None:
        raw["runtime_duration_s"] = args.duration_s
    if args.event_slug:
        raw["event_slug"] = args.event_slug
    elif args.btc_window:
        raw["event_slug"] = (
            current_btc_updown_slug()
            if args.btc_window == "current"
            else next_btc_updown_slug()
        )
    cfg = observe_config_from_mapping(raw)
    if cfg.shadow is None or not cfg.shadow.enable_oms:
        raise SystemExit("shadow command requires shadow.enable_oms=true in config")
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
    if args.command == "live-preflight":
        from tyrex_pm.runtime.live_preflight import run_live_preflight

        result = run_live_preflight(
            output_path=args.output,
            dotenv_path=args.dotenv if args.dotenv.exists() else None,
            user_stream_observe_s=args.user_stream_s,
            skip_auth=args.skip_auth,
        )
        print(
            f"live-preflight complete ok={result.ok} "
            f"cloudflare={result.payload.get('cloudflare_blocked')} "
            f"path={result.artifact_path}"
        )
        # Exit 0 when structurally safe; 1 when account evidence incomplete.
        return 0 if result.payload.get("mutations_attempted") is False else 3
    if args.command == "r7a-prepare":
        from tyrex_pm.runtime.r7a_proposal import prepare_r7a_artifacts

        preflight_path = args.preflight
        if args.run_preflight:
            from tyrex_pm.runtime.live_preflight import run_live_preflight

            pf = run_live_preflight(
                output_path=args.output_dir / "live_preflight.json",
                dotenv_path=Path(".env") if Path(".env").exists() else None,
                user_stream_observe_s=5.0,
                skip_auth=False,
            )
            preflight_path = pf.artifact_path
            print(f"preflight ok={pf.ok} path={preflight_path}")
        report = prepare_r7a_artifacts(
            repo_root=Path.cwd(),
            output_dir=args.output_dir,
            preflight_path=preflight_path if preflight_path.exists() else None,
            issue_approval=False,
        )
        blockers = (report.get("r7_readiness") or {}).get("blockers") or []
        print(
            f"r7a-prepare complete phase={report.get('phase')} "
            f"ready_for_proposal={(report.get('r7_readiness') or {}).get('ready_for_r7b_proposal')} "
            f"blockers={blockers} "
            f"approval={report.get('approval_artifact_id')} "
            f"mutations_enabled={report.get('mutations_enabled')}"
        )
        return 0
    if args.command == "r7a2-prepare":
        from tyrex_pm.runtime.r7a2_prepare import prepare_r7a2

        preflight_path = args.preflight
        if args.run_preflight:
            from tyrex_pm.runtime.live_preflight import run_live_preflight

            pf = run_live_preflight(
                output_path=args.output_dir / "live_preflight.json",
                dotenv_path=Path(".env") if Path(".env").exists() else None,
                user_stream_observe_s=5.0,
                skip_auth=False,
            )
            preflight_path = pf.artifact_path
            print(f"preflight ok={pf.ok} path={preflight_path}")
        report = prepare_r7a2(
            repo_root=Path.cwd(),
            output_dir=args.output_dir,
            preflight_path=preflight_path if preflight_path.exists() else None,
        )
        after = report.get("readiness_after_ack") or {}
        print(
            f"r7a2-prepare complete ack={report.get('acknowledgment', {}).get('id')} "
            f"ack_ok={(report.get('acknowledgment') or {}).get('validation', {}).get('ok')} "
            f"blockers_after={after.get('blockers')} "
            f"session_draft={report.get('session_draft', {}).get('session_id')} "
            f"user_auth={report.get('session_draft', {}).get('user_authorization_present')} "
            f"mutations_enabled={report.get('mutations_enabled')}"
        )
        return 0
    if args.command == "live-once":
        print(
            "R7B BLOCKED: legacy live-once is retired. "
            "Use: tyrex-pm r7b-live-once --dry-run | --execute-live. "
            "Mutations remain disabled."
        )
        return 2
    if args.command == "r7-ack-regenerate":
        from tyrex_pm.runtime.r7_ack_regenerate import regenerate_acknowledgment

        report = regenerate_acknowledgment(
            repo_root=Path.cwd(),
            output_path=args.output,
            write_dust_state=True,
        )
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            __import__("json").dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"r7-ack-regenerate ok={report.get('ok')} "
            f"id={report.get('artifact_id')} "
            f"hash={report.get('content_hash')} "
            f"path={report.get('path')} "
            f"mutations_attempted={report.get('mutations_attempted')} "
            f"report={args.report}"
        )
        return 0 if report.get("ok") else 2
    if args.command == "r7c-recon":
        from tyrex_pm.runtime.r7_paths import DEFAULT_ACKNOWLEDGMENT_PATH
        from tyrex_pm.runtime.r7c_incident_recon import run_incident_recon

        ack_path = DEFAULT_ACKNOWLEDGMENT_PATH
        report = run_incident_recon(
            buy_order_id=args.buy_order_id,
            condition_id=args.condition_id,
            token_id=args.token_id,
            market_slug=args.market_slug,
            acknowledgment_path=ack_path if ack_path.exists() else None,
            output_path=args.output,
            repo_root=Path.cwd(),
        )
        flatness = report.get("flat_classification") or {}
        print(
            f"r7c-recon terminal={report.get('terminal')} "
            f"balance={flatness.get('balance')} "
            f"open_orders_zero={report.get('selected_market_open_orders_zero')} "
            f"buy_acq={((report.get('buy_fill_summary') or {}).get('acquired_quantity'))} "
            f"sell_qty={((report.get('sell_fill_summary') or {}).get('sold_quantity'))} "
            f"ack_ok={((report.get('acknowledgment') or {}).get('ok'))} "
            f"path={args.output}"
        )
        print(f"mutations_attempted={report.get('mutations_attempted')}")
        # Non-tradable dust is acceptable terminal; tradable residual / unknown → nonzero
        term = str(report.get("terminal") or "")
        return 0 if term in {"FLAT", "FLAT_WITH_DUST", "FLAT_EXTERNAL_ACTION"} else 3
    if args.command == "r7b-live-once":
        from decimal import Decimal

        from tyrex_pm.runtime.r7b_live_once import R7BLiveOnceArgs, run_r7b_live_once

        execute_live = bool(args.execute_live)
        # Default dry when neither flag set; --dry-run explicit; --execute-live live
        dry_run = not execute_live
        result = run_r7b_live_once(
            R7BLiveOnceArgs(
                strategy=args.strategy,
                market_family=args.market_family,
                max_windows=int(args.max_windows),
                max_buy_collateral=Decimal(str(args.max_buy_collateral)),
                dry_run=dry_run,
                execute_live=execute_live,
                output_dir=args.output_dir,
                acknowledgment_path=args.acknowledgment,
                repo_root=Path.cwd(),
                allow_dirty_worktree=bool(args.allow_dirty_worktree),
            )
        )
        report = result.report
        pre = report.get("pre_submit") or {}
        print(
            f"r7b-live-once mode={report.get('mode')} terminal={result.outcome.value} "
            f"exit={result.exit_code}"
        )
        if pre:
            print(
                "bound_market="
                f"{pre.get('market_slug')} token_suffix="
                f"{str(pre.get('token_id') or '')[-8:]} side={pre.get('side')} "
                f"qty={pre.get('quantity_max_estimated_shares')} "
                f"limit={pre.get('limit_price')} "
                f"max_fee={pre.get('estimated_max_entry_fee')} "
                f"max_collateral={pre.get('max_collateral')} "
                f"entry_deadline={pre.get('entry_deadline')} "
                f"flatten_deadline={pre.get('flatten_deadline')}"
            )
        if report.get("blockers"):
            print(f"blockers={report.get('blockers')}")
        print(f"report={result.report_path}")
        print(f"facts={result.facts_path}")
        print(f"mutations_attempted={len(report.get('mutations_attempted') or [])}")
        return int(result.exit_code)
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

    if args.command == "shadow":
        cfg = _build_shadow_config(args)
        if cfg.mode is SourceMode.FIXTURE:
            from tyrex_pm.runtime.shadow_host import ShadowHost

            clock = FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc))
            host = ShadowHost(cfg, clock=clock)
            try:
                result = host.run_fixture()
            finally:
                host.close()
            print(
                f"fixture shadow complete decisions={len(result.decisions)} "
                f"intents={len(result.intents)} commands={len(host.commands)} "
                f"lifecycle={host.lifecycle.state.value} facts={result.fact_count} "
                f"path={result.facts_path}"
            )
            return 0
        from tyrex_pm.runtime.live_shadow import run_live_shadow

        result = asyncio.run(run_live_shadow(cfg))
        print(
            f"live shadow complete decisions={len(result.decisions)} "
            f"intents={len(result.intents)} facts={result.fact_count} "
            f"path={result.facts_path}"
        )
        return 0

    if args.command == "n6-status":
        from tyrex_pm.runtime.live_config import LiveConfig
        from tyrex_pm.runtime.scope_a_ladder import PRODUCTION_TIMING_VALUES_STATUS

        cfg = LiveConfig()
        print(
            __import__("json").dumps(
                {
                    "live": cfg.to_dict(),
                    "production_timing_values": PRODUCTION_TIMING_VALUES_STATUS,
                    "mutations_default": False,
                    "zgap_execute_live": False,
                    "scope_b_available": False,
                    "n7_authorized": False,
                },
                indent=2,
            )
        )
        return 0
    if args.command == "n6-preflight":
        from tyrex_pm.runtime.live_preflight import run_live_preflight

        result = run_live_preflight(
            output_path=args.output,
            dotenv_path=args.dotenv if args.dotenv.exists() else None,
            user_stream_observe_s=args.user_stream_s,
            skip_auth=False,
        )
        print(
            f"n6-preflight complete ok={result.ok} "
            f"mutations_attempted={result.payload.get('mutations_attempted')} "
            f"path={result.artifact_path}"
        )
        return 0 if result.payload.get("mutations_attempted") is False else 3
    if args.command == "n6-recon":
        import subprocess

        cmd = [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "tools" / "n6_live" / "run_n6_readonly_recon.py"),
        ]
        if args.out_dir is not None:
            cmd.extend(["--out-dir", str(args.out_dir)])
        if args.dotenv is not None:
            cmd.extend(["--dotenv", str(args.dotenv)])
        return subprocess.call(cmd)
    if args.command == "n6-kill-inspect":
        print(
            __import__("json").dumps(
                {
                    "kill_blocks_new_exposure": True,
                    "kill_exits_confirmed_qty_only": True,
                    "heartbeat_cancel_all": False,
                    "venue_io": False,
                },
                indent=2,
            )
        )
        return 0

    if args.command == "n7-status":
        from tyrex_pm.runtime.n7_sealed import default_n7_sealed_config, load_n7_sealed_config
        from tyrex_pm.runtime.n7_timing import PRODUCTION_TIMING_VALUES_STATUS

        cfg_path = Path("config/n7_tiny_live.json")
        sealed = (
            load_n7_sealed_config(cfg_path) if cfg_path.exists() else default_n7_sealed_config()
        )
        print(
            __import__("json").dumps(
                {
                    "n7": sealed.to_dict(),
                    "production_timing_values": PRODUCTION_TIMING_VALUES_STATUS,
                    "mutations_default": False,
                    "n7b_authorized": False,
                    "scope_b_available": False,
                    "real_venue_mutation": False,
                    "ceremony": (
                        "n7-auth-request -> operator phrase -> n7-oneshot "
                        "--authorization-phrase ..."
                    ),
                },
                indent=2,
            )
        )
        return 0

    if args.command == "n7-preflight":
        import subprocess

        cmd = [
            sys.executable,
            str(
                Path(__file__).resolve().parents[2]
                / "tools"
                / "n7_live"
                / "run_n7_readonly_preflight.py"
            ),
            "--config",
            str(args.config),
        ]
        if args.out_dir is not None:
            cmd.extend(["--out-dir", str(args.out_dir)])
        if args.dotenv is not None:
            cmd.extend(["--dotenv", str(args.dotenv)])
        cmd.extend(["--user-stream-s", str(args.user_stream_s)])
        if args.allow_dirty:
            cmd.append("--allow-dirty")
        return subprocess.call(cmd)

    if args.command == "n7-auth-request":
        from datetime import timedelta

        from tyrex_pm.runtime.n7_authorization import (
            create_authorization_request,
            write_authorization_request,
        )
        from tyrex_pm.runtime.n7_git import inspect_git
        from tyrex_pm.runtime.n7_sealed import load_n7_sealed_config

        repo = Path(__file__).resolve().parents[2]
        git = inspect_git(repo)
        sealed = load_n7_sealed_config(args.config)
        req, _env = create_authorization_request(
            sealed=sealed,
            git_head=git.head,
            operator_label=args.operator_label,
            valid_for=timedelta(minutes=int(args.valid_minutes)),
        )
        write_authorization_request(args.output, req)
        print(
            __import__("json").dumps(
                {
                    "written": str(args.output),
                    "envelope_id": req.envelope_id,
                    "git_head": req.git_head,
                    "config_fingerprint": req.config_fingerprint,
                    "valid_until_utc": req.valid_until_utc,
                    "approval_phrase_template": req.approval_phrase_template,
                    "approved": False,
                    "note": (
                        "This is a request only. N7B requires the operator to "
                        "provide this phrase verbatim; the CLI does not auto-approve."
                    ),
                },
                indent=2,
            )
        )
        return 0

    if args.command == "n7-oneshot":
        # N7A / safety: never silently mutate. Phrase required for any arming.
        if not args.authorization_phrase or args.dry_run:
            print(
                __import__("json").dumps(
                    {
                        "refused": True,
                        "reason": "authorization_phrase_required",
                        "n7b_authorized": False,
                        "mutations_enabled": False,
                        "real_venue_mutations": 0,
                        "help": (
                            "1) Commit N7A and ensure clean worktree. "
                            "2) Run n7-preflight / n7-auth-request. "
                            "3) Operator supplies the exact approval phrase. "
                            "4) Re-run: n7-oneshot --authorization-phrase '...'"
                        ),
                    },
                    indent=2,
                )
            )
            return 2
        print(
            __import__("json").dumps(
                {
                    "refused": True,
                    "reason": "n7b_not_enabled_in_this_task",
                    "note": (
                        "Phrase received by CLI but N7B live mutation path is "
                        "gated behind an explicit post-N7A operator go. "
                        "Complete N7A acceptance first; do not infer approval."
                    ),
                    "mutations_enabled": False,
                    "real_venue_mutations": 0,
                },
                indent=2,
            )
        )
        return 3

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
