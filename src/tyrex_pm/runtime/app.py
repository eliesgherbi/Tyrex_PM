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
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.execution.live_oms import LiveOMS
from tyrex_pm.execution.oms import SingleWriterOMS
from tyrex_pm.ingestion.guru_stream import poll_guru_incremental, process_fixture_signals
from tyrex_pm.ingestion.market_ws_ingest import (
    market_ws_primary_enabled,
    market_ws_shadow_enabled,
    run_market_ws_ingest,
)
from tyrex_pm.ingestion.user_stream import run_user_ws_ingest
from tyrex_pm.market_data.decision_gate import rest_poll_should_run, ws_primary_enabled
from tyrex_pm.market_data.models import BookSource, SourceQuality
from tyrex_pm.market_data.readiness_runtime import (
    ensure_market_readiness_tracker,
    emit_readiness_transitions,
    refresh_market_readiness,
)
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_GURU_POLL,
    FACT_TYPE_HEALTH,
    FACT_TYPE_REST_POLL_DISABLED,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import (
    STRATEGY_KIND_ALLOCATION_TEST,
    STRATEGY_KIND_GURU_FOLLOW,
    STRATEGY_KIND_PAIRED_BINARY,
    STRATEGY_KIND_Z_GAP,
    STRATEGY_KIND_SELL_TEST,
    STRATEGY_KIND_SIMPLE_SIGNAL_TEST,
    STRATEGY_KIND_TP_SL_TEST,
    STRATEGY_KIND_VALIDATION_HARNESS,
    VALIDATION_MODE_MARKET_DATA_READONLY,
    VALIDATION_MODE_PROTECTION_SL,
    VALIDATION_MODE_PROTECTION_TP,
    VALIDATION_MODE_PROTECTION_TRIGGER_LIVE,
    VALIDATION_MODE_STALE_BOOK_DENY,
    load_app_config,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.fixture_signal_run import run_fixture_signals_once
from tyrex_pm.runtime.paired_binary_recovery import recover_on_startup
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.runtime.market_data_runtime import (
    bootstrap_market_state,
    ensure_market_state_shadow_store,
    ensure_market_state_store,
    market_data_rest_refresh_loop,
    resolve_market_token_ids,
)
from tyrex_pm.runtime.market_update_coordinator import (
    MarketUpdateCoordinator,
    attach_coordinator_to_authoritative_store,
)
from tyrex_pm.runtime.protection_runtime import init_protection_monitor
from tyrex_pm.runtime.validation_harness_run import run_validation_harness_once
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.live_supervisor import (
    provisional_repair_probe_loop,
    supervised_heartbeat_loop,
    user_ws_staleness_loop,
    venue_refresh_loop,
)
from tyrex_pm.runtime.config import SELL_TEST_PRICING_AUTO
from tyrex_pm.strategies.guru_follow.scheduled_exit_demo import try_arm_scheduled_exit_demos
from tyrex_pm.strategies.sell_test.pricing import resolve_marketable_price_via_client
from tyrex_pm.strategies.sell_test.strategy import (
    SellTestStrategy,
    try_arm_sell_test_pending,
)
from tyrex_pm.strategies.tp_sl_test.strategy import (
    TpSlTestStrategy,
    try_arm_tp_sl_pending,
)
from tyrex_pm.runtime.pipeline import (
    _reconcile_kw,
    process_intent_work_unit,
    process_new_guru_signals,
    process_scheduled_exit_demo_due,
    reconcile_coordinator,
    scheduled_exit_demo_due_loop,
)
from tyrex_pm.execution.order_lifecycle import sync_local_open_orders_from_venue_wallet
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.reconcile import reconcile_open_orders
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.strategy_store import load_strategy_store, save_strategy_store
from tyrex_pm.state.allocation_ledger import load_allocation_ledger
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.allocation_test.strategy import AllocationTestStrategy
from tyrex_pm.strategies.guru_follow.strategy import GuruFollowStrategy
from tyrex_pm.strategies.paired_binary.strategy import PairedBinaryStrategy
from tyrex_pm.strategies.simple_signal_test.strategy import SimpleSignalTestStrategy
from tyrex_pm.strategies.validation_harness.strategy import ValidationHarnessStrategy
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

_RUNTIME_WIRED_STRATEGY_KINDS = frozenset(
    {
        STRATEGY_KIND_GURU_FOLLOW,
        STRATEGY_KIND_SELL_TEST,
        STRATEGY_KIND_ALLOCATION_TEST,
        STRATEGY_KIND_TP_SL_TEST,
        STRATEGY_KIND_SIMPLE_SIGNAL_TEST,
        STRATEGY_KIND_VALIDATION_HARNESS,
        STRATEGY_KIND_PAIRED_BINARY,
        STRATEGY_KIND_Z_GAP,
    }
)


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
    p_run.add_argument(
        "--event-url",
        default=None,
        metavar="URL_OR_SLUG",
        help=(
            "Polymarket event URL or slug for paired_binary live runs. "
            "Resolves market_id, condition_id, token ids, and event timestamps "
            "from Gamma and fills only missing/placeholder scenario fields."
        ),
    )
    p_rc = sub.add_parser(
        "run_continue",
        help="continuous paired_binary across consecutive BTC Up/Down 5m windows",
    )
    p_rc.add_argument("--strategy", required=True, help="strategy YAML (paired_binary)")
    p_rc.add_argument("--scenario", required=True, help="scenario YAML overlay")
    p_rc.add_argument("--repo-root", type=Path, default=None)
    p_rc.add_argument(
        "--state-dir",
        default="var/state",
        help="directory for paired_binary / allocation state (default: var/state)",
    )
    p_rc.add_argument(
        "--run-name",
        required=True,
        help="session label; per-window runs go under runs_dir/{session}__{market_id}",
    )
    p_rc.add_argument(
        "--event-url",
        required=True,
        metavar="URL_OR_SLUG",
        help=(
            "Reference BTC Up/Down 5m Polymarket URL (may be old/current). "
            "Used to validate the event family; the next full window is traded."
        ),
    )
    p_rc.add_argument(
        "--prestart-seconds",
        type=float,
        default=30.0,
        help="start each window run this many seconds before window open (default: 30)",
    )
    p_rc.add_argument(
        "--entry-grace-seconds",
        type=float,
        default=15.0,
        help="skip window if Gamma metadata still unavailable this long after open (default: 15)",
    )
    p_rc.add_argument(
        "--sleep-granularity",
        type=float,
        default=1.0,
        help="interruptible wait poll interval in seconds (default: 1.0)",
    )
    p_rc.add_argument(
        "--max-windows",
        type=int,
        default=None,
        help="stop after N completed window runs (default: unlimited until CTRL+C)",
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
    p_rs.add_argument(
        "--paired-binary",
        action="store_true",
        help="also remove var/state/paired_binary/ persistence (does not cancel venue orders)",
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
    p_rec = sub.add_parser("record", help="Record market events (no trading)")
    p_rec.add_argument("--scenario", required=True)
    p_rec.add_argument("--repo-root", type=Path, default=None)
    p_rec.add_argument(
        "--event-url",
        default=None,
        metavar="URL_OR_SLUG",
        help="Optional Polymarket event URL/slug to resolve token ids for recording",
    )
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
        raise SystemExit(asyncio.run(cmd_run(args)) or 0)
    elif args.cmd == "run_continue":
        from tyrex_pm.runtime.run_continue import cmd_run_continue

        try:
            raise SystemExit(asyncio.run(cmd_run_continue(args)) or 0)
        except KeyboardInterrupt:
            raise SystemExit(0) from None
    elif args.cmd == "live-attest":
        from tyrex_pm.runtime.live_attest import cmd_live_attest

        raise SystemExit(asyncio.run(cmd_live_attest(args)))
    elif args.cmd == "reset-state":
        cmd_reset_state(args)
    elif args.cmd == "record":
        from tyrex_pm.runtime.record_run import cmd_record

        try:
            raise SystemExit(asyncio.run(cmd_record(args)) or 0)
        except KeyboardInterrupt:
            raise SystemExit(0) from None


def cmd_reset_state(args: argparse.Namespace) -> None:
    """Clear documented local state files. Idempotent.

    See ``tyrex_pm.runtime.reset_state.reset_local_state`` for the file list.
    Reporting artifacts under ``var/reporting/`` are intentionally preserved.
    """
    from tyrex_pm.runtime.reset_state import reset_local_state, resettable_file_names, venue_orders_warning

    root = args.repo_root or _repo_root()
    state_dir = Path(args.state_dir)
    if not state_dir.is_absolute():
        state_dir = (root / state_dir).resolve()
    removed = reset_local_state(state_dir, paired_binary=bool(getattr(args, "paired_binary", False)))
    if removed:
        for p in removed:
            print(f"removed {p}")
    else:
        names = ", ".join(resettable_file_names())
        print(f"no state to clear under {state_dir} (looked for: {names})")
    print(f"warning: {venue_orders_warning()}")


async def _wait_sell_test_live_readiness(
    coord: RuntimeCoordinator,
    app,
    *,
    timeout_s: float,
) -> tuple[bool, str]:
    """Block until ``check_aggressive_readiness`` + heartbeat + CLOB session all flip green.

    The sell_test loop emits its single BUY immediately after live bootstrap, before
    the background heartbeat task has had a chance to send its first ping. Without
    this wait, the BUY's ``risk_decision`` is denied with ``heartbeat_failed`` (see
    var/reporting/runs/sell_test_live_1). Mirrors :func:`live_attest._wait_aggressive_readiness`.
    """
    from tyrex_pm.risk.health import check_aggressive_readiness

    deadline = monotonic_s() + timeout_s
    last_reason = "timeout"
    while monotonic_s() < deadline:
        ctx = coord.build_risk_context(app)
        ok, reason = check_aggressive_readiness(
            ctx, runtime=app.runtime, readiness=app.risk.readiness
        )
        if ok and ctx.heartbeat_ok and ctx.clob_session_ok:
            return True, "ok"
        last_reason = reason or "not_ready"
        await asyncio.sleep(0.5)
    return False, last_reason or "readiness_timeout"


async def _run_sell_test_loop(
    *,
    args: argparse.Namespace,
    app,
    run_id: RunId,
    strat: SellTestStrategy,
    coord: RuntimeCoordinator,
    sink: JsonlSink,
    oms_backend,
    apply_local_fill: bool,
    live_clob,
    stop_live: asyncio.Event,
) -> int:
    """Drive the standalone SELL test: emit one BUY, wait for the SELL to drain.

    The loop re-uses :func:`process_intent_work_unit` for the BUY and lets the
    background ``scheduled_exit_demo_due_loop`` task drain the SELL once the
    sell_test state arms it. In live mode, waits for aggressive readiness
    (heartbeat + CLOB session + first venue truth rebuild) before the initial
    BUY so risk does not deny it with ``heartbeat_failed`` on a cold start.
    Exits when ``run_once=True`` and the strategy is done, when ``--once`` is
    passed, or when ``--max-iterations`` is reached.
    """
    iterations = 0
    if app.runtime.execution_mode == ExecutionMode.LIVE:
        readiness_timeout = float(os.environ.get("TYREX_SELL_TEST_READINESS_S", "60"))
        ok_r, rsn = await _wait_sell_test_live_readiness(
            coord, app, timeout_s=readiness_timeout
        )
        sink.write(
            make_fact(
                FACT_TYPE_HEALTH,
                str(run_id),
                {"event": "sell_test_readiness", "ok": ok_r, "detail": rsn},
            )
        )
        if not ok_r:
            log.error(
                "sell_test live readiness failed after %.1fs: %s — aborting before BUY",
                readiness_timeout,
                rsn,
            )
            return iterations
    # Auto-pricing: if buy.pricing_mode=auto and we are live, fetch the venue
    # book once and override the BUY limit_price with a marketable value
    # (best_ask + aggression_ticks * tick). On failure / guardrail trip we
    # fall back to cfg.buy.limit_price so the strategy still tries to place
    # an order; both outcomes emit a fact so the operator can audit it.
    buy_cfg = strat.cfg.buy
    if (
        buy_cfg.enabled
        and buy_cfg.pricing_mode == SELL_TEST_PRICING_AUTO
        and app.runtime.execution_mode == ExecutionMode.LIVE
        and live_clob is not None
    ):
        market_info = None
        if coord.market_info_cache is not None:
            try:
                market_info = await coord.market_info_cache.get(strat.cfg.token_id)
            except Exception:  # noqa: BLE001 — pricing falls back regardless
                market_info = None
        resolved = await resolve_marketable_price_via_client(
            client=live_clob,
            market_info=market_info,
            token_id=strat.cfg.token_id,
            side="BUY",
            aggression_ticks=buy_cfg.aggression_ticks,
            fallback_price=buy_cfg.limit_price,
            max_price=buy_cfg.max_price,
        )
        evidence = resolved.to_evidence()
        sink.write(
            make_fact(
                FACT_TYPE_HEALTH,
                str(run_id),
                {
                    "event": "sell_test_pricing",
                    "side": "BUY",
                    "token_id": strat.cfg.token_id,
                    **evidence,
                },
            )
        )
        if resolved.source == "auto_book" and resolved.price > 0:
            strat.set_resolved_buy_price(resolved.price, evidence=evidence)
            log.info(
                "sell_test auto-pricing BUY: best_ask=%s tick=%s aggression=%s -> price=%s",
                resolved.best_ask,
                resolved.tick_size,
                resolved.aggression_ticks,
                resolved.price,
            )
        else:
            log.warning(
                "sell_test auto-pricing BUY fell back: %s (using cfg.buy.limit_price=%s)",
                resolved.error,
                buy_cfg.limit_price,
            )
    for wu in strat.initial_buy_work_units():
        await process_intent_work_unit(
            wu,
            app=app,
            run_id=run_id,
            strategy=strat,
            coord=coord,
            sink=sink,
            oms=oms_backend,
            apply_local_shadow_fill=apply_local_fill,
            live_clob_client=live_clob,
        )
        iterations += 1
    if args.once:
        return iterations
    if not strat.cfg.sell.enabled:
        return iterations
    completion_timeout = float(os.environ.get("TYREX_SELL_TEST_COMPLETION_TIMEOUT_S", "120"))
    inventory_timeout = float(os.environ.get("TYREX_SELL_TEST_INVENTORY_TIMEOUT_S", "90"))
    deadline = monotonic_s() + completion_timeout
    inventory_deadline = (
        monotonic_s() + inventory_timeout if strat.buy_submit_succeeded else deadline
    )
    next_reconcile_at = monotonic_s()
    while not stop_live.is_set() and monotonic_s() < deadline:
        if strat.is_done():
            log.info("sell_test strategy reports is_done; exiting run loop")
            break
        if strat.has_pending_inventory_wait() and monotonic_s() >= inventory_deadline:
            strat.sell_test_state.emit_timeout_waiting_for_inventory(coord)
            log.error("sell_test timed out waiting for sellable inventory")
            break
        now = monotonic_s()
        if now >= next_reconcile_at:
            reconcile_coordinator(coord, sink, str(run_id))
            next_reconcile_at = now + float(app.runtime.reconcile_interval_s)
        try:
            await asyncio.wait_for(stop_live.wait(), timeout=0.5)
            break
        except asyncio.TimeoutError:
            iterations += 1
    grace_s = float(os.environ.get("TYREX_SELL_TEST_SCHEDULER_GRACE_S", "2"))
    await asyncio.sleep(grace_s)
    return iterations


async def _run_tp_sl_test_loop(
    *,
    args: argparse.Namespace,
    app,
    run_id: RunId,
    strat: TpSlTestStrategy,
    coord: RuntimeCoordinator,
    sink: JsonlSink,
    oms_backend,
    apply_local_fill: bool,
    live_clob,
    stop_live: asyncio.Event,
) -> int:
    """Drive tp_sl_test: one BUY, monitor price, emit SELL on TP/SL trigger."""
    iterations = 0
    if app.runtime.execution_mode == ExecutionMode.LIVE:
        readiness_timeout = float(os.environ.get("TYREX_TP_SL_TEST_READINESS_S", "60"))
        ok_r, rsn = await _wait_sell_test_live_readiness(
            coord, app, timeout_s=readiness_timeout
        )
        sink.write(
            make_fact(
                FACT_TYPE_HEALTH,
                str(run_id),
                {"event": "tp_sl_test_readiness", "ok": ok_r, "detail": rsn},
            )
        )
        if not ok_r:
            log.error(
                "tp_sl_test live readiness failed after %.1fs: %s — aborting before BUY",
                readiness_timeout,
                rsn,
            )
            return iterations
    buy_cfg = strat.cfg.buy
    if (
        buy_cfg.enabled
        and buy_cfg.pricing_mode == SELL_TEST_PRICING_AUTO
        and app.runtime.execution_mode == ExecutionMode.LIVE
        and live_clob is not None
    ):
        market_info = None
        if coord.market_info_cache is not None:
            try:
                market_info = await coord.market_info_cache.get(strat.cfg.token_id)
            except Exception:  # noqa: BLE001
                market_info = None
        resolved = await resolve_marketable_price_via_client(
            client=live_clob,
            market_info=market_info,
            token_id=strat.cfg.token_id,
            side="BUY",
            aggression_ticks=buy_cfg.aggression_ticks,
            fallback_price=buy_cfg.limit_price,
            max_price=buy_cfg.max_price,
        )
        evidence = resolved.to_evidence()
        sink.write(
            make_fact(
                FACT_TYPE_HEALTH,
                str(run_id),
                {
                    "event": "tp_sl_test_pricing",
                    "side": "BUY",
                    "token_id": strat.cfg.token_id,
                    **evidence,
                },
            )
        )
        if resolved.source == "auto_book" and resolved.price > 0:
            strat.set_resolved_buy_price(resolved.price, evidence=evidence)
    for wu in strat.initial_buy_work_units():
        await process_intent_work_unit(
            wu,
            app=app,
            run_id=run_id,
            strategy=strat,
            coord=coord,
            sink=sink,
            oms=oms_backend,
            apply_local_shadow_fill=apply_local_fill,
            live_clob_client=live_clob,
        )
        iterations += 1
    if args.once:
        return iterations
    if not strat.cfg.exit.enabled:
        return iterations
    timeouts = strat.cfg.timeouts
    completion_deadline = monotonic_s() + timeouts.completion_timeout_s
    inventory_deadline = (
        monotonic_s() + timeouts.inventory_timeout_s if strat.buy_submit_succeeded else completion_deadline
    )
    trigger_deadline = monotonic_s() + timeouts.trigger_timeout_s
    poll_s = float(strat.cfg.monitor.poll_interval_s)
    next_reconcile_at = monotonic_s()
    while not stop_live.is_set() and monotonic_s() < completion_deadline:
        if strat.is_done():
            log.info("tp_sl_test strategy reports is_done; exiting run loop")
            break
        st = strat.tp_sl_state
        if strat.has_pending_inventory_wait() and monotonic_s() >= inventory_deadline:
            st.emit_timeout_waiting_for_inventory(coord)
            log.error("tp_sl_test timed out waiting for sellable inventory")
            break
        if (
            st._monitoring is not None
            and not st._monitoring.triggered
            and st._monitor_started_mono is not None
            and monotonic_s() >= trigger_deadline
        ):
            st.emit_timeout_waiting_for_trigger(coord)
            log.error("tp_sl_test timed out waiting for TP/SL trigger")
            break
        if coord.scheduled_exit_demo_try_arm is not None:
            coord.scheduled_exit_demo_try_arm(source="periodic_refresh")
        await st.tick_monitor(coord, live_clob_client=live_clob)
        work = await st.resolve_triggered_work_units(
            coord=coord,
            live_clob_client=live_clob,
        )
        for wu in work:
            await process_intent_work_unit(
                wu,
                app=app,
                run_id=run_id,
                strategy=strat,
                coord=coord,
                sink=sink,
                oms=oms_backend,
                apply_local_shadow_fill=apply_local_fill,
                live_clob_client=live_clob,
            )
            iterations += 1
        now = monotonic_s()
        if now >= next_reconcile_at:
            reconcile_coordinator(coord, sink, str(run_id))
            next_reconcile_at = now + float(app.runtime.reconcile_interval_s)
        try:
            await asyncio.wait_for(stop_live.wait(), timeout=poll_s)
            break
        except asyncio.TimeoutError:
            iterations += 1
    grace_s = float(os.environ.get("TYREX_TP_SL_TEST_GRACE_S", "2"))
    await asyncio.sleep(grace_s)
    return iterations


async def _run_allocation_test_loop(
    *,
    args: argparse.Namespace,
    app,
    run_id: RunId,
    strat: AllocationTestStrategy,
    coord: RuntimeCoordinator,
    sink: JsonlSink,
    oms_backend,
    apply_local_fill: bool,
    live_clob,
    stop_live: asyncio.Event,
) -> int:
    """Drive allocation_test: Owner A BUY → Owner B block → Owner A SELL."""
    from decimal import Decimal

    from tyrex_pm.core.ids import TokenId
    from tyrex_pm.runtime.exit_lifecycle import inventory_snapshot

    iterations = 0
    cfg = strat.cfg
    rid = str(run_id)

    if app.runtime.execution_mode == ExecutionMode.LIVE:
        readiness_timeout = float(os.environ.get("TYREX_ALLOCATION_TEST_READINESS_S", "60"))
        ok_r, rsn = await _wait_sell_test_live_readiness(coord, app, timeout_s=readiness_timeout)
        sink.write(
            make_fact(
                FACT_TYPE_HEALTH,
                rid,
                {"event": "allocation_test_readiness", "ok": ok_r, "detail": rsn},
            )
        )
        if not ok_r:
            log.error(
                "allocation_test live readiness failed after %.1fs: %s — aborting before BUY",
                readiness_timeout,
                rsn,
            )
            return iterations

    for wu in strat.owner_a_buy_work_units():
        await process_intent_work_unit(
            wu,
            app=app,
            run_id=run_id,
            strategy=strat,
            coord=coord,
            sink=sink,
            oms=oms_backend,
            apply_local_shadow_fill=apply_local_fill,
            live_clob_client=live_clob,
        )
        iterations += 1

    if strat.is_done():
        return iterations

    if not strat.buy_submit_succeeded:
        return iterations

    pos_deadline = monotonic_s() + cfg.timeouts.position_visible_s
    prerequisites_visible = False
    tid = TokenId(cfg.token_id)
    last_pos_refresh = 0.0
    while monotonic_s() < pos_deadline and not stop_live.is_set():
        if (
            app.runtime.execution_mode == ExecutionMode.LIVE
            and live_clob is not None
            and coord.positions_client is not None
            and coord.positions_wallet_address
            and monotonic_s() - last_pos_refresh >= 0.5
        ):
            await refresh_positions_from_data_api(
                coord.wallet,
                coord.positions_client,
                coord.positions_wallet_address,
            )
            last_pos_refresh = monotonic_s()
        if strat.check_owner_b_prerequisites_visible(coord):
            prerequisites_visible = True
            strat.mark_owner_a_allocation_visible()
            break
        await asyncio.sleep(0.05 if apply_local_fill else 0.5)

    if not prerequisites_visible:
        if strat.check_owner_b_prerequisites_visible(coord):
            strat.mark_owner_a_allocation_visible()
        else:
            strat.emit_timeout_position_visible(sink, rid)
            return iterations

    if strat.is_done():
        return iterations

    strat.attempt_owner_b_unauthorized_sell(coord, sink, rid)
    if strat.is_done():
        return iterations

    if cfg.owner_a_sell.delay_s > 0:
        await asyncio.sleep(float(cfg.owner_a_sell.delay_s))

    if app.runtime.execution_mode == ExecutionMode.LIVE:
        pos_deadline = monotonic_s() + cfg.timeouts.position_visible_s
        tid = TokenId(cfg.token_id)
        while monotonic_s() < pos_deadline and not stop_live.is_set():
            snap = inventory_snapshot(coord, tid)
            if Decimal(snap["available_to_sell"]) > 0:
                break
            await asyncio.sleep(0.5)
        else:
            strat.mark_timeout_position_visible()
            return iterations

    sell_cfg = cfg.owner_a_sell
    if (
        sell_cfg.enabled
        and sell_cfg.pricing_mode == SELL_TEST_PRICING_AUTO
        and app.runtime.execution_mode == ExecutionMode.LIVE
        and live_clob is not None
    ):
        market_info = None
        if coord.market_info_cache is not None:
            try:
                market_info = await coord.market_info_cache.get(cfg.token_id)
            except Exception:  # noqa: BLE001 — pricing falls back regardless
                market_info = None
        fallback = sell_cfg.limit_price or cfg.buy.limit_price
        resolved = await resolve_marketable_price_via_client(
            client=live_clob,
            market_info=market_info,
            token_id=cfg.token_id,
            side="SELL",
            aggression_ticks=sell_cfg.aggression_ticks,
            fallback_price=fallback,
            min_price=sell_cfg.min_price,
        )
        evidence = resolved.to_evidence()
        sink.write(
            make_fact(
                FACT_TYPE_HEALTH,
                rid,
                {
                    "event": "allocation_test_pricing",
                    "side": "SELL",
                    "token_id": cfg.token_id,
                    **evidence,
                },
            )
        )
        if resolved.source == "auto_book" and resolved.price > 0:
            strat.set_resolved_sell_price(resolved.price, evidence=evidence)
            log.info(
                "allocation_test auto-pricing SELL: best_bid=%s tick=%s aggression=%s -> price=%s",
                resolved.best_bid,
                resolved.tick_size,
                resolved.aggression_ticks,
                resolved.price,
            )
        elif fallback is not None and fallback > 0:
            log.warning(
                "allocation_test auto-pricing SELL fell back: %s (using limit_price=%s)",
                resolved.error,
                fallback,
            )
            strat.set_resolved_sell_price(
                fallback,
                evidence={**evidence, "fallback_used": True},
            )
        else:
            log.error(
                "allocation_test auto-pricing SELL failed with no fallback: %s",
                resolved.error,
            )
            strat.emit_sell_pricing_failed(sink, rid, error=resolved.error)
            return iterations

    wu = strat.build_owner_a_sell_work_unit(coord)
    if wu is None:
        strat.mark_timeout_owner_a_exit()
        return iterations

    await process_intent_work_unit(
        wu,
        app=app,
        run_id=run_id,
        strategy=strat,
        coord=coord,
        sink=sink,
        oms=oms_backend,
        apply_local_shadow_fill=apply_local_fill,
        live_clob_client=live_clob,
    )
    iterations += 1

    exit_deadline = monotonic_s() + cfg.timeouts.owner_a_exit_timeout_s
    while monotonic_s() < exit_deadline and not stop_live.is_set():
        if strat.is_done():
            break
        await asyncio.sleep(0.05)
    if not strat.is_done():
        strat.mark_timeout_owner_a_exit()

    strat.verify_final_ledger(coord)
    complete_payload: dict = {
        "event": "allocation_test_complete",
        "phase": strat.phase,
        "done": strat.is_done(),
        "sell_outcome": strat.sell_outcome,
    }
    complete_payload.update(strat.ledger_snapshot(coord))
    sink.write(
        make_fact(
            FACT_TYPE_HEALTH,
            rid,
            complete_payload,
        )
    )
    return iterations


async def cmd_run(args: argparse.Namespace) -> int:
    from tyrex_pm.runtime.run_once import execute_run

    return await execute_run(args)


if __name__ == "__main__":
    main()
