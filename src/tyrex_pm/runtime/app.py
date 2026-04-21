from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx

from tyrex_pm.core.enums import ExecutionMode
from tyrex_pm.core.ids import RunId
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.execution.live_oms import LiveOMS
from tyrex_pm.execution.oms import SingleWriterOMS
from tyrex_pm.ingestion.guru_stream import poll_guru_incremental, process_fixture_signals
from tyrex_pm.ingestion.user_stream import run_user_ws_ingest
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_GURU_POLL, FACT_TYPE_HEALTH
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import load_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.live_supervisor import (
    provisional_repair_probe_loop,
    supervised_heartbeat_loop,
    user_ws_staleness_loop,
    venue_refresh_loop,
)
from tyrex_pm.runtime.pipeline import _reconcile_kw, process_new_guru_signals, reconcile_coordinator
from tyrex_pm.execution.order_lifecycle import sync_local_open_orders_from_venue_wallet
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.reconcile import reconcile_open_orders
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.strategy_store import load_strategy_store, save_strategy_store
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.guru_follow.strategy import GuruFollowStrategy
from tyrex_pm.venue.polymarket.clob_bridge import PyClobBridge
from tyrex_pm.venue.polymarket.clob_env import (
    DEFAULT_CLOB_HOST_V2,
    resolve_positions_wallet_address,
    try_create_clob_client,
)
from tyrex_pm.venue.polymarket.clob_wallet_sync import refresh_wallet_from_clob
from tyrex_pm.venue.polymarket.data_api_client import DEFAULT_DATA_API_BASE, DataApiClient
from tyrex_pm.venue.polymarket.gamma_client import GammaClient
from tyrex_pm.venue.polymarket.market_info import MarketInfoCache
from tyrex_pm.venue.polymarket.positions_sync import refresh_positions_from_data_api

log = logging.getLogger(__name__)

_PLACEHOLDER_GURU = "0x0000000000000000000000000000000000000000"


def _maybe_load_dotenv(repo_root: Path) -> None:
    """Load `.env` into os.environ if python-dotenv is installed.

    Prefers `./.env` (cwd) so runs from the project root work with editable or global installs;
    falls back to `<repo_root>/.env` when that matches the packaged layout (e.g. src checkout).

    Uses ``override=True`` so the on-disk ``.env`` always wins over stale shell env vars left
    over from a previous ``set -a && source .env && set +a``. Without override, editing ``.env``
    has no effect on a process whose parent shell pre-loaded the old values.
    """
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


def _guru_wallet_configured(wallet: str) -> bool:
    w = (wallet or "").strip().lower()
    return bool(w) and w != _PLACEHOLDER_GURU.lower()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _git_sha() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=_repo_root(),
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def _safe_run_dir_label(name: str) -> str:
    """User-supplied --run-name segment for the run directory (no path separators)."""
    s = (name or "").strip()
    if not s:
        return ""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s)
    s = re.sub(r"\s+", "_", s).strip("._")
    return s[:120] or ""


def main() -> None:
    _maybe_load_dotenv(_repo_root())
    parser = argparse.ArgumentParser(prog="tyrex-pm")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="run bot (shadow by default)")
    p_run.add_argument("--strategy", default="config/strategies/guru_follow.yaml")
    p_run.add_argument("--scenario", default=None)
    p_run.add_argument("--repo-root", type=Path, default=None)
    p_run.add_argument("--state-dir", default="var/state", help="directory for guru watermark/dedup JSON")
    p_run.add_argument("--once", action="store_true", help="single poll iteration then exit")
    p_run.add_argument("--fixture", type=Path, default=None, help="replay Data API JSON from file (no HTTP)")
    p_run.add_argument("--max-iterations", type=int, default=None, help="stop after N poll loops (with --fixture, one pass)")
    p_run.add_argument(
        "--run-name",
        default=None,
        help="optional label; artifacts go under runs_dir/{sanitized_name} (facts run_id is still a UUID in each row)",
    )
    p_rs = sub.add_parser(
        "reset-state",
        help="clear local on-disk state (V2 cutover hygiene; never touches reporting/runs/)",
    )
    p_rs.add_argument(
        "--state-dir",
        default="var/state",
        help="directory whose documented state files will be deleted (default: var/state)",
    )
    p_rs.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="resolve --state-dir relative to this root (default: detected repo root)",
    )
    p_la = sub.add_parser(
        "live-attest",
        help="minimal live post+cancel via native OMS (designated wallet; not guru copy)",
    )
    p_la.add_argument("--repo-root", type=Path, default=None)
    p_la.add_argument("--strategy", default="config/strategies/guru_follow.yaml")
    p_la.add_argument("--scenario", default="live_attest")
    p_la.add_argument(
        "--token-id",
        default=os.environ.get("TYREX_SMOKE_TOKEN_ID"),
        help="numeric Polymarket CLOB outcome token id for the market "
             "(falls back to env TYREX_SMOKE_TOKEN_ID)",
    )
    p_la.add_argument(
        "--size",
        default=os.environ.get("TYREX_SMOKE_SIZE"),
        help="order size (falls back to env TYREX_SMOKE_SIZE)",
    )
    p_la.add_argument(
        "--price",
        default=os.environ.get("TYREX_SMOKE_PRICE"),
        help="limit price (falls back to env TYREX_SMOKE_PRICE)",
    )
    p_la.add_argument(
        "--side",
        default=os.environ.get("TYREX_SMOKE_SIDE", "BUY"),
        choices=("BUY", "SELL"),
        help="order side (falls back to env TYREX_SMOKE_SIDE, then BUY)",
    )
    p_la.add_argument("--readiness-timeout-s", type=float, default=120.0)
    args = parser.parse_args()
    if args.cmd == "live-attest":
        missing = [
            f"--{n}" for n, v in
            (("token-id", args.token_id), ("size", args.size), ("price", args.price))
            if not v
        ]
        if missing:
            parser.error(
                f"missing required value(s) for: {', '.join(missing)} "
                f"(set on the command line or in .env via TYREX_SMOKE_TOKEN_ID / "
                f"TYREX_SMOKE_SIZE / TYREX_SMOKE_PRICE)"
            )

    if args.cmd == "run":
        asyncio.run(cmd_run(args))
    elif args.cmd == "live-attest":
        from tyrex_pm.runtime.live_attest import cmd_live_attest

        raise SystemExit(asyncio.run(cmd_live_attest(args)))
    elif args.cmd == "reset-state":
        cmd_reset_state(args)


def cmd_reset_state(args: argparse.Namespace) -> None:
    """Clear documented local state files. Idempotent.

    See ``tyrex_pm.runtime.reset_state.reset_local_state`` for the file list.
    Reporting artifacts under ``var/reporting/`` are intentionally preserved.
    """
    from tyrex_pm.runtime.reset_state import reset_local_state, resettable_file_names

    root = args.repo_root or _repo_root()
    state_dir = Path(args.state_dir)
    if not state_dir.is_absolute():
        state_dir = (root / state_dir).resolve()
    removed = reset_local_state(state_dir)
    if removed:
        for p in removed:
            print(f"removed {p}")
    else:
        names = ", ".join(resettable_file_names())
        print(f"no state to clear under {state_dir} (looked for: {names})")


async def cmd_run(args: argparse.Namespace) -> None:
    root = args.repo_root or _repo_root()
    app = load_app_config(
        repo_root=root,
        strategy_file=str(args.strategy),
        scenario_file=args.scenario,
    )
    logging.basicConfig(level=getattr(logging, app.runtime.log_level.upper(), logging.INFO))

    run_id = RunId(str(uuid4()))
    name_seg = _safe_run_dir_label(args.run_name) if args.run_name else ""
    run_dir_name = name_seg if name_seg else str(run_id)
    runs_dir = root / app.runtime.reporting.runs_dir / run_dir_name
    runs_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": str(run_id),
        "schema_version": 2,
        "git_sha": _git_sha(),
        "execution_mode": app.runtime.execution_mode.value,
        "run_kind": "tyrex_run",
        "run_name": args.run_name,
        "run_dir": run_dir_name,
    }
    (runs_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    state_path = (root / args.state_dir).resolve() / "guru_strategy_store.json"
    strategy_store = load_strategy_store(state_path)
    strat = GuruFollowStrategy(app.strategy)
    shadow_oms = ShadowOMS()

    coord: RuntimeCoordinator | None = None
    oms_backend = shadow_oms
    apply_local_fill = True
    live_tasks: list[asyncio.Task[None]] = []
    stop_live = asyncio.Event()
    live_clob = None
    live_bridge: PyClobBridge | None = None
    live_oms_writer: SingleWriterOMS | None = None
    gamma = GammaClient()

    if app.runtime.execution_mode == ExecutionMode.LIVE:
        live_clob = try_create_clob_client()
        if live_clob is None:
            log.error("Live mode needs TYREX_PRIVATE_KEY and `pip install tyrex-pm[live]` (py-clob-client-v2)")
            return
        live_bridge = PyClobBridge(live_clob)
        live_oms_writer = SingleWriterOMS(LiveOMS(live_bridge))
        live_oms_writer.start()
        oms_backend = live_oms_writer
        apply_local_fill = False
        clob_host = os.environ.get("TYREX_CLOB_HOST", DEFAULT_CLOB_HOST_V2)
        coord = RuntimeCoordinator(
            wallet=WalletStore(),
            orders=OrderStore(),
            health=HealthRuntime(),
            submit_grace_s=float(app.runtime.submit_grace_s),
            provisional_unknown_terminal_timeout_s=float(app.runtime.provisional_unknown_terminal_timeout_s),
            venue_confirm_provisional_timeout_s=float(app.runtime.provisional_unknown_terminal_timeout_s),
            adoption_grace_s=float(app.runtime.adoption_grace_s),
            market_info_cache=MarketInfoCache(live_clob, host=clob_host),
        )
        res0 = reconcile_open_orders(coord.wallet, coord.orders, **_reconcile_kw(coord))
        coord.health.apply_reconcile(res0)
    else:
        wallet = WalletStore()
        if app.runtime.shadow_bootstrap is not None:
            apply_shadow_bootstrap(wallet, app.runtime.shadow_bootstrap)
        else:
            log.warning(
                "Shadow mode without runtime.shadow_bootstrap: wallet unset until venue sync is implemented"
            )
        coord = RuntimeCoordinator(
            wallet=wallet,
            orders=OrderStore(),
            health=HealthRuntime(),
            submit_grace_s=float(app.runtime.submit_grace_s),
            provisional_unknown_terminal_timeout_s=float(app.runtime.provisional_unknown_terminal_timeout_s),
            venue_confirm_provisional_timeout_s=float(app.runtime.provisional_unknown_terminal_timeout_s),
            adoption_grace_s=float(app.runtime.adoption_grace_s),
        )
        res0 = reconcile_open_orders(coord.wallet, coord.orders, **_reconcile_kw(coord))
        coord.health.apply_reconcile(res0)

    assert coord is not None

    facts_path = runs_dir / "facts.jsonl"
    iterations = 0
    last_guru_poll: dict | None = None
    with JsonlSink(facts_path) as sink:
        try:
            sink.write(
                make_fact(
                    FACT_TYPE_HEALTH,
                    str(run_id),
                    {"status": "started", "mode": app.runtime.execution_mode.value},
                )
            )

            if app.runtime.execution_mode == ExecutionMode.LIVE and live_bridge is not None and live_clob is not None:
                hb_interval = max(5.0, float(os.environ.get("TYREX_HEARTBEAT_INTERVAL_S", "8")))
                venue_interval = float(os.environ.get("TYREX_VENUE_REFRESH_S", str(app.runtime.reconcile_interval_s)))
                positions_addr = resolve_positions_wallet_address(live_clob)
                positions_data_client: DataApiClient | None = None
                if positions_addr:
                    positions_data_client = DataApiClient(
                        os.environ.get("TYREX_DATA_API_BASE", DEFAULT_DATA_API_BASE)
                    )
                else:
                    log.warning(
                        "positions REST safety net disabled: no funder/EOA wallet address resolved; "
                        "wallet.positions will rely on user-WS CONFIRMED trade events alone"
                    )
                try:
                    await refresh_wallet_from_clob(coord.wallet, live_clob)
                    if positions_data_client is not None and positions_addr:
                        await refresh_positions_from_data_api(
                            coord.wallet, positions_data_client, positions_addr
                        )
                    sync_local_open_orders_from_venue_wallet(coord.orders, coord.wallet)
                    # V2 cutover hygiene: open the new-order risk gate as soon
                    # as the first live venue truth rebuild succeeds. Until
                    # this flips, ``check_aggressive_readiness`` denies with
                    # ``bootstrap_not_complete`` (see runtime/health_runtime).
                    coord.health.mark_first_v2_sync_complete()
                except Exception:
                    log.exception("initial live bootstrap (wallet sync) failed")
                    coord.health.mark_heartbeat(ok=False)

                live_tasks.append(
                    asyncio.create_task(
                        supervised_heartbeat_loop(
                            coord.health,
                            live_bridge,
                            hb_interval,
                            sink,
                            run_id=str(run_id),
                            stop=stop_live,
                        )
                    )
                )
                live_tasks.append(
                    asyncio.create_task(
                        venue_refresh_loop(
                            coord,
                            live_clob,
                            venue_interval,
                            sink,
                            str(run_id),
                            stop_live,
                            positions_client=positions_data_client,
                            positions_wallet_address=positions_addr,
                        )
                    )
                )
                live_tasks.append(
                    asyncio.create_task(
                        provisional_repair_probe_loop(
                            coord,
                            live_clob,
                            sink,
                            str(run_id),
                            stop_live,
                        )
                    )
                )
                ws_disable = os.environ.get("TYREX_USER_WS_DISABLE", "").strip() == "1"
                if ws_disable:
                    log.warning(
                        "TYREX_USER_WS_DISABLE=1: user WebSocket off; "
                        "readiness uses REST-only venue policy (require_user_ws_live should be false)"
                    )
                    coord.health.user_ws_rest_only = True
                elif getattr(live_clob, "creds", None) is None:
                    log.error("CLOB client has no API creds; user WebSocket disabled")
                    coord.health.user_ws_rest_only = True
                else:
                    c = live_clob.creds
                    live_tasks.append(
                        asyncio.create_task(
                            run_user_ws_ingest(
                                coord,
                                api_key=c.api_key,
                                secret=c.api_secret,
                                passphrase=c.api_passphrase,
                                stop=stop_live,
                            )
                        )
                    )
                ws_threshold = float(os.environ.get("TYREX_USER_WS_STALE_S", "45"))
                ws_grace = float(os.environ.get("TYREX_USER_WS_GRACE_S", "20"))
                live_tasks.append(
                    asyncio.create_task(
                        user_ws_staleness_loop(
                            coord.health,
                            app,
                            threshold_s=ws_threshold,
                            grace_s=ws_grace,
                            sink=sink,
                            run_id=str(run_id),
                            stop=stop_live,
                        )
                    )
                )

            fixture_path: Path | None = args.fixture
            if fixture_path is not None:
                if app.runtime.execution_mode == ExecutionMode.LIVE:
                    log.error("Fixture replay is only supported in shadow mode")
                else:
                    fixture_path = (
                        fixture_path if fixture_path.is_absolute() else (root / fixture_path).resolve()
                    )
                    text = fixture_path.read_text(encoding="utf-8")
                    sigs = DataApiClient.parse_activity_json(text, app.strategy.guru.wallet)
                    new = process_fixture_signals(sigs, strategy_store)
                    last_guru_poll = {
                        "source": "fixture",
                        "fixture_path": str(fixture_path),
                        "parsed_rows": len(sigs),
                        "new_signals_after_ingest": len(new),
                        "guru_wallet_configured": _guru_wallet_configured(app.strategy.guru.wallet),
                    }
                    sink.write(
                        make_fact(
                            FACT_TYPE_GURU_POLL,
                            str(run_id),
                            last_guru_poll,
                        )
                    )
                    await process_new_guru_signals(
                        new,
                        app=app,
                        run_id=run_id,
                        strategy=strat,
                        coord=coord,
                        sink=sink,
                        oms=oms_backend,
                        apply_local_shadow_fill=apply_local_fill,
                        http_client=None,
                        gamma_client=gamma,
                    )
                    save_strategy_store(state_path, strategy_store)
                    iterations = 1
            else:
                if not app.strategy.guru.wallet or app.strategy.guru.wallet == _PLACEHOLDER_GURU:
                    log.warning(
                        "guru.wallet is unset or placeholder; set it in strategy YAML for live polling "
                        "or use --fixture for replay"
                    )

                if app.runtime.execution_mode == ExecutionMode.LIVE:
                    next_reconcile_at = monotonic_s()
                    async with httpx.AsyncClient(base_url=DEFAULT_DATA_API_BASE, timeout=30.0) as http:
                        client = DataApiClient(client=http)
                        while True:
                            now = monotonic_s()
                            if now >= next_reconcile_at:
                                reconcile_coordinator(coord, sink, str(run_id))
                                next_reconcile_at = now + float(app.runtime.reconcile_interval_s)
                            res = await poll_guru_incremental(
                                client=client,
                                guru_wallet=app.strategy.guru.wallet,
                                limit=app.strategy.guru.data_api_limit,
                                max_pages=app.strategy.guru.data_api_max_pages_per_poll,
                                store=strategy_store,
                            )
                            last_guru_poll = {
                                "source": "data_api",
                                "new_signals": len(res.new_signals),
                                "raw_rows": res.raw_rows,
                                "normalized_candidates": res.normalized_candidates,
                                "pages_fetched": res.pages_fetched,
                                "guru_wallet_configured": _guru_wallet_configured(app.strategy.guru.wallet),
                            }
                            sink.write(
                                make_fact(
                                    FACT_TYPE_GURU_POLL,
                                    str(run_id),
                                    last_guru_poll,
                                )
                            )
                            await process_new_guru_signals(
                                res.new_signals,
                                app=app,
                                run_id=run_id,
                                strategy=strat,
                                coord=coord,
                                sink=sink,
                                oms=oms_backend,
                                apply_local_shadow_fill=apply_local_fill,
                                http_client=http,
                                gamma_client=gamma,
                                live_clob_client=live_clob,
                            )
                            save_strategy_store(state_path, strategy_store)
                            iterations += 1
                            if args.once or args.max_iterations is not None and iterations >= args.max_iterations:
                                break
                            await asyncio.sleep(app.strategy.guru.data_api_poll_interval_s)
                else:
                    next_reconcile_at = monotonic_s()
                    async with httpx.AsyncClient(base_url=DEFAULT_DATA_API_BASE, timeout=30.0) as http:
                        client = DataApiClient(client=http)
                        while True:
                            now = monotonic_s()
                            if now >= next_reconcile_at:
                                reconcile_coordinator(coord, sink, str(run_id))
                                next_reconcile_at = now + float(app.runtime.reconcile_interval_s)
                            res = await poll_guru_incremental(
                                client=client,
                                guru_wallet=app.strategy.guru.wallet,
                                limit=app.strategy.guru.data_api_limit,
                                max_pages=app.strategy.guru.data_api_max_pages_per_poll,
                                store=strategy_store,
                            )
                            last_guru_poll = {
                                "source": "data_api",
                                "new_signals": len(res.new_signals),
                                "raw_rows": res.raw_rows,
                                "normalized_candidates": res.normalized_candidates,
                                "pages_fetched": res.pages_fetched,
                                "guru_wallet_configured": _guru_wallet_configured(app.strategy.guru.wallet),
                            }
                            sink.write(
                                make_fact(
                                    FACT_TYPE_GURU_POLL,
                                    str(run_id),
                                    last_guru_poll,
                                )
                            )
                            await process_new_guru_signals(
                                res.new_signals,
                                app=app,
                                run_id=run_id,
                                strategy=strat,
                                coord=coord,
                                sink=sink,
                                oms=oms_backend,
                                apply_local_shadow_fill=apply_local_fill,
                                http_client=http,
                                gamma_client=gamma,
                            )
                            save_strategy_store(state_path, strategy_store)
                            iterations += 1
                            if args.once or args.max_iterations is not None and iterations >= args.max_iterations:
                                break
                            await asyncio.sleep(app.strategy.guru.data_api_poll_interval_s)

            sink.write(
                make_fact(
                    FACT_TYPE_HEALTH,
                    str(run_id),
                    {"status": "stopped", "iterations": iterations},
                )
            )
        finally:
            stop_live.set()
            if live_tasks:
                await asyncio.gather(*live_tasks, return_exceptions=True)
            if live_oms_writer is not None:
                await live_oms_writer.stop()

    run_summary = {
        "run_kind": "tyrex_run",
        "execution_mode": app.runtime.execution_mode.value,
        "iterations": iterations,
        "last_guru_poll": last_guru_poll,
    }
    (runs_dir / "run_summary.json").write_text(json.dumps(run_summary, indent=2), encoding="utf-8")

    log.info("Wrote run to %s", runs_dir)


if __name__ == "__main__":
    main()
