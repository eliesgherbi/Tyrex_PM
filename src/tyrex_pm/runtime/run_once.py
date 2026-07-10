"""One-shot tyrex run (extracted from runtime/app.py)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from uuid import uuid4

import httpx

from tyrex_pm.core.enums import ExecutionMode
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.execution.live_oms import LiveOMS
from tyrex_pm.execution.oms import SingleWriterOMS
from tyrex_pm.execution.order_lifecycle import sync_local_open_orders_from_venue_wallet
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
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.live_supervisor import (
    provisional_repair_probe_loop,
    supervised_heartbeat_loop,
    user_ws_staleness_loop,
    venue_refresh_loop,
)
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
from tyrex_pm.runtime.paired_binary_recovery import recover_on_startup
from tyrex_pm.runtime.paired_binary_run import run_paired_binary_loop
from tyrex_pm.runtime.pipeline import (
    _reconcile_kw,
    process_new_guru_signals,
    reconcile_coordinator,
    scheduled_exit_demo_due_loop,
)
from tyrex_pm.runtime.protection_runtime import init_protection_monitor
from tyrex_pm.runtime.validation_harness_run import run_validation_harness_once
from tyrex_pm.state.allocation_ledger import load_allocation_ledger
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.reconcile import reconcile_open_orders
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.strategy_store import load_strategy_store, save_strategy_store
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.allocation_test.strategy import AllocationTestStrategy
from tyrex_pm.strategies.guru_follow.scheduled_exit_demo import try_arm_scheduled_exit_demos
from tyrex_pm.strategies.guru_follow.strategy import GuruFollowStrategy
from tyrex_pm.strategies.paired_binary.strategy import PairedBinaryStrategy
from tyrex_pm.strategies.sell_test.strategy import SellTestStrategy, try_arm_sell_test_pending
from tyrex_pm.strategies.simple_signal_test.strategy import SimpleSignalTestStrategy
from tyrex_pm.strategies.tp_sl_test.strategy import TpSlTestStrategy, try_arm_tp_sl_pending
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


async def execute_run(args: argparse.Namespace) -> int:
    from tyrex_pm.runtime.app import (
        _RUNTIME_WIRED_STRATEGY_KINDS,
        _PLACEHOLDER_GURU,
        _git_sha,
        _guru_wallet_configured,
        _repo_root,
        _run_allocation_test_loop,
        _run_sell_test_loop,
        _run_tp_sl_test_loop,
        _safe_run_dir_label,
        _wait_sell_test_live_readiness,
    )

    root = args.repo_root or _repo_root()
    app = load_app_config(
        repo_root=root,
        strategy_file=str(args.strategy),
        scenario_file=args.scenario,
    )
    logging.basicConfig(level=getattr(logging, app.runtime.log_level.upper(), logging.INFO))

    event_url = getattr(args, "event_url", None)
    resolved_event_meta = None
    if app.strategy_kind == STRATEGY_KIND_PAIRED_BINARY and (
        event_url or app.paired_binary is not None
    ):
        from tyrex_pm.runtime.paired_binary_metadata import resolve_and_apply_paired_binary_metadata
        from tyrex_pm.venue.polymarket.event_metadata import EventMetadataError, EventMetadataLookupError

        try:
            app, resolved_event_meta = resolve_and_apply_paired_binary_metadata(
                app,
                event_url=event_url,
            )
        except (EventMetadataError, EventMetadataLookupError) as exc:
            log.error("paired_binary event metadata resolution failed: %s", exc)
            return 2
        except httpx.HTTPError as exc:
            log.error("paired_binary event metadata HTTP error: %r", exc)
            return 2
        if resolved_event_meta is not None:
            log.info(
                "Resolved paired_binary metadata from %s: market_id=%s condition_id=%s "
                "yes=%s no=%s event_start_ts=%s event_end_ts=%s",
                event_url or "token_ids",
                app.paired_binary.market_id if app.paired_binary else "?",
                app.paired_binary.condition_id if app.paired_binary else "?",
                app.paired_binary.yes_token_id if app.paired_binary else "?",
                app.paired_binary.no_token_id if app.paired_binary else "?",
                app.paired_binary.event_start_ts if app.paired_binary else "?",
                app.paired_binary.event_end_ts if app.paired_binary else "?",
            )

    if app.strategy_kind == STRATEGY_KIND_Z_GAP and (event_url or app.z_gap is not None):
        from tyrex_pm.runtime.btc_5m_metadata import resolve_and_apply_btc_5m_metadata
        from tyrex_pm.venue.polymarket.event_metadata import EventMetadataError, EventMetadataLookupError

        try:
            app, resolved_z_gap_meta = resolve_and_apply_btc_5m_metadata(app, event_url=event_url)
        except (EventMetadataError, EventMetadataLookupError) as exc:
            log.error("z_gap btc_5m metadata resolution failed: %s", exc)
            return 2
        except httpx.HTTPError as exc:
            log.error("z_gap btc_5m metadata HTTP error: %r", exc)
            return 2
        if resolved_z_gap_meta is not None:
            log.info(
                "Resolved z_gap metadata from %s: market_id=%s condition_id=%s "
                "yes=%s no=%s event_start_ts=%s event_end_ts=%s",
                event_url or "token_ids",
                resolved_z_gap_meta.market_id,
                resolved_z_gap_meta.condition_id,
                resolved_z_gap_meta.yes_token_id,
                resolved_z_gap_meta.no_token_id,
                resolved_z_gap_meta.event_start_ts,
                resolved_z_gap_meta.event_end_ts,
            )

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
    allocation_ledger_path = (root / args.state_dir).resolve() / "allocation_ledger.json"
    allocation_ledger = load_allocation_ledger(allocation_ledger_path)
    strategy_kind = app.strategy_kind
    if strategy_kind not in _RUNTIME_WIRED_STRATEGY_KINDS:
        raise RuntimeError(
            f"strategy kind {strategy_kind!r} has no runtime loop wired in runtime/app.py "
            f"(supported: {sorted(_RUNTIME_WIRED_STRATEGY_KINDS)})"
        )
    sell_test_mode = strategy_kind == STRATEGY_KIND_SELL_TEST
    allocation_test_mode = strategy_kind == STRATEGY_KIND_ALLOCATION_TEST
    tp_sl_test_mode = strategy_kind == STRATEGY_KIND_TP_SL_TEST
    simple_signal_test_mode = strategy_kind == STRATEGY_KIND_SIMPLE_SIGNAL_TEST
    validation_harness_mode = strategy_kind == STRATEGY_KIND_VALIDATION_HARNESS
    paired_binary_mode = strategy_kind == STRATEGY_KIND_PAIRED_BINARY
    z_gap_mode = strategy_kind == STRATEGY_KIND_Z_GAP
    strat: (
        GuruFollowStrategy
        | SellTestStrategy
        | AllocationTestStrategy
        | TpSlTestStrategy
        | SimpleSignalTestStrategy
        | ValidationHarnessStrategy
        | PairedBinaryStrategy
    )
    if allocation_test_mode:
        assert app.allocation_test is not None
        strat = AllocationTestStrategy(app.allocation_test)
        log.info(
            "Loaded allocation_test strategy: token_id=%s owner_a=%s owner_b=%s run_once=%s",
            app.allocation_test.token_id,
            app.allocation_test.owner_a_id,
            app.allocation_test.owner_b_id,
            app.allocation_test.run_once,
        )
    elif tp_sl_test_mode:
        assert app.tp_sl_test is not None
        strat = TpSlTestStrategy(app.tp_sl_test)
        log.info(
            "Loaded tp_sl_test strategy: token_id=%s owner_id=%s run_once=%s",
            app.tp_sl_test.token_id,
            app.tp_sl_test.owner_id,
            app.tp_sl_test.run_once,
        )
    elif sell_test_mode:
        assert app.sell_test is not None  # for type checker
        strat = SellTestStrategy(app.sell_test)
        log.info(
            "Loaded sell_test strategy: token_id=%s buy.notional=%s buy.limit=%s sell.delay_s=%s run_once=%s",
            app.sell_test.token_id,
            app.sell_test.buy.notional_usd,
            app.sell_test.buy.limit_price,
            app.sell_test.sell.delay_s,
            app.sell_test.run_once,
        )
    elif simple_signal_test_mode:
        assert app.simple_signal_test is not None
        strat = SimpleSignalTestStrategy(owner_id=app.simple_signal_test.owner_id)
        log.info(
            "Loaded simple_signal_test harness: token_id=%s side=%s notional=%s limit=%s run_once=%s",
            app.simple_signal_test.token_id,
            app.simple_signal_test.side.value,
            app.simple_signal_test.notional_usd,
            app.simple_signal_test.limit_price,
            app.simple_signal_test.run_once,
        )
    elif validation_harness_mode:
        assert app.validation_harness is not None
        strat = ValidationHarnessStrategy(owner_id=app.validation_harness.owner_id)
        log.info(
            "Loaded validation_harness: mode=%s token_id=%s owner_id=%s",
            app.validation_harness.mode,
            app.validation_harness.token_id,
            app.validation_harness.owner_id,
        )
    elif paired_binary_mode:
        assert app.paired_binary is not None
        strat = PairedBinaryStrategy(app.paired_binary)
        log.info(
            "Loaded paired_binary: market_id=%s yes=%s no=%s owner_id=%s",
            app.paired_binary.market_id,
            app.paired_binary.yes_token_id,
            app.paired_binary.no_token_id,
            app.paired_binary.owner_id,
        )
    elif z_gap_mode:
        assert app.z_gap is not None
        strat = None
        log.info(
            "Loaded z_gap: entry_mode=%s market_id=%s owner_id=%s",
            app.z_gap.entry_mode,
            app.z_gap.market_id,
            app.z_gap.owner_id,
        )
    elif strategy_kind == STRATEGY_KIND_GURU_FOLLOW:
        strat = GuruFollowStrategy(app.strategy)
    else:
        raise RuntimeError(
            f"strategy kind {strategy_kind!r} has no runtime loop wired in runtime/app.py"
        )
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
            return 1
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

    coord.allocation_ledger = allocation_ledger
    coord.allocation_clamp_grace_s = float(app.runtime.allocation_ledger.clamp_grace_s_after_buy)

    if app.runtime.market_data.enabled:
        ensure_market_state_store(coord, app)
    if app.protection is not None and app.protection.enabled:
        init_protection_monitor(coord)

    if sell_test_mode:

        def _sell_test_try_arm(*, source="post_buy_ack"):
            try_arm_sell_test_pending(strat, coord, source=source)

        coord.scheduled_exit_demo_try_arm = _sell_test_try_arm
    elif tp_sl_test_mode:

        def _tp_sl_try_arm(*, source="post_buy_ack"):
            try_arm_tp_sl_pending(strat, coord, source=source)

        coord.scheduled_exit_demo_try_arm = _tp_sl_try_arm
    elif app.strategy.exits.demo_forced_exit_enabled:

        def _scheduled_exit_try_arm(*, source="post_buy_ack"):
            try_arm_scheduled_exit_demos(strat, coord, source=source)

        coord.scheduled_exit_demo_try_arm = _scheduled_exit_try_arm

    facts_path = runs_dir / "facts.jsonl"
    iterations = 0
    last_guru_poll: dict | None = None
    exit_code = 0
    signal_feed_state = None
    with JsonlSink(facts_path) as sink:
        coord.exit_lifecycle_run_id = str(run_id)
        coord.exit_lifecycle_sink = sink
        coord.allocation_ledger_run_id = str(run_id)
        coord.allocation_ledger_sink = sink
        try:
            sink.write(
                make_fact(
                    FACT_TYPE_HEALTH,
                    str(run_id),
                    {"status": "started", "mode": app.runtime.execution_mode.value},
                )
            )

            if z_gap_mode and app.runtime.execution_mode == ExecutionMode.LIVE:
                from tyrex_pm.runtime.time_authority import sample_offset

                coord.time_authority = sample_offset()
                log.info(
                    "z_gap TimeAuthority sampled before feeds: sync_status=%s uncertainty_ms=%s "
                    "samples_kept=%s source=%s",
                    coord.time_authority.sync_status,
                    coord.time_authority.uncertainty_ms,
                    coord.time_authority.samples_kept,
                    coord.time_authority.source,
                )

            if app.runtime.execution_mode == ExecutionMode.LIVE and live_bridge is not None and live_clob is not None:
                hb_interval = max(5.0, float(os.environ.get("TYREX_HEARTBEAT_INTERVAL_S", "8")))
                venue_interval = float(os.environ.get("TYREX_VENUE_REFRESH_S", str(app.runtime.reconcile_interval_s)))
                positions_addr = resolve_positions_wallet_address(live_clob)
                positions_data_client: DataApiClient | None = None
                coord.positions_wallet_address = positions_addr
                if positions_addr:
                    positions_data_client = DataApiClient(
                        os.environ.get("TYREX_DATA_API_BASE", DEFAULT_DATA_API_BASE)
                    )
                    coord.positions_client = positions_data_client
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
                    if coord.scheduled_exit_demo_try_arm is not None:
                        coord.scheduled_exit_demo_try_arm(source="periodic_refresh")
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

                if app.runtime.market_data.enabled and live_clob is not None:
                    md = app.runtime.market_data
                    ws_primary = ws_primary_enabled(app) or market_ws_primary_enabled(
                        config_flag=md.websocket.primary_enabled
                    )
                    auth_store = ensure_market_state_store(coord, app)
                    if ws_primary and app.paired_binary is not None:
                        if coord.market_update_coordinator is None:
                            coord.market_update_coordinator = MarketUpdateCoordinator(
                                debounce_ms=float(
                                    app.runtime.paired_binary.max_decision_rate_per_market_ms
                                ),
                            )
                        attach_coordinator_to_authoritative_store(
                            coord, coord.market_update_coordinator
                        )
                        ensure_market_readiness_tracker(coord, app, app.paired_binary)

                    if md.rest.bootstrap_on_startup:
                        try:
                            boot_n = await bootstrap_market_state(
                                coord, app, live_clob_client=live_clob
                            )
                            log.info("market_data REST bootstrap: %s token(s)", boot_n)
                            tracker = getattr(coord, "market_readiness_tracker", None)
                            if tracker is not None:
                                tracker.note_rest_bootstrapped()
                                emit_readiness_transitions(
                                    sink,
                                    run_id,
                                    tracker,
                                    correlation_id=getattr(app.paired_binary, "market_id", None),
                                )
                        except Exception:
                            log.exception("market_data REST bootstrap failed")

                    ws_conn_state = {"connected": False}
                    md_refresh_s = float(os.environ.get("TYREX_MARKET_DATA_REFRESH_S", "5"))

                    async def _rest_recovery_after_gap() -> None:
                        if not md.rest.recovery_on_reconnect:
                            return 1
                        await bootstrap_market_state(
                            coord,
                            app,
                            live_clob_client=live_clob,
                            source=BookSource.REST_RECOVERY,
                        )
                        tracker = getattr(coord, "market_readiness_tracker", None)
                        if tracker is not None:
                            tracker.note_rest_recovery()
                            emit_readiness_transitions(
                                sink,
                                run_id,
                                tracker,
                                correlation_id=getattr(app.paired_binary, "market_id", None),
                            )

                    async def _on_ws_connected() -> None:
                        ws_conn_state["connected"] = True
                        tracker = getattr(coord, "market_readiness_tracker", None)
                        if tracker is not None:
                            tracker.note_ws_connected()
                            emit_readiness_transitions(
                                sink,
                                run_id,
                                tracker,
                                correlation_id=getattr(app.paired_binary, "market_id", None),
                            )

                    async def _on_ws_disconnected() -> None:
                        ws_conn_state["connected"] = False
                        tracker = getattr(coord, "market_readiness_tracker", None)
                        if tracker is not None:
                            tracker.note_reconnect_gap()
                            emit_readiness_transitions(
                                sink,
                                run_id,
                                tracker,
                                correlation_id=getattr(app.paired_binary, "market_id", None),
                            )

                    async def _on_book_applied(token_id: TokenId) -> None:
                        tracker = getattr(coord, "market_readiness_tracker", None)
                        if tracker is not None:
                            tracker.note_ws_book(token_id, source_quality=SourceQuality.WS_PRIMARY)
                            if app.paired_binary is not None:
                                refresh_market_readiness(
                                    coord,
                                    app,
                                    app.paired_binary,
                                    sink=sink,
                                    run_id=run_id,
                                )

                    def _on_market_event_applied(event) -> None:
                        if app.runtime.observability.emit_event_correlation:
                            from tyrex_pm.strategies.paired_binary.facts import record_wake_market_event

                            record_wake_market_event(coord, event)

                    ws_tokens = resolve_market_token_ids(app)
                    ws_cfg = md.websocket
                    run_shadow = market_ws_shadow_enabled(config_flag=ws_cfg.shadow_enabled) and not ws_primary
                    run_primary = ws_primary

                    if md.rest.poll_enabled and not (
                        ws_primary and not rest_poll_should_run(app, ws_connected=False)
                    ):
                        live_tasks.append(
                            asyncio.create_task(
                                market_data_rest_refresh_loop(
                                    coord,
                                    app,
                                    live_clob,
                                    stop=stop_live,
                                    interval_s=md_refresh_s,
                                    ws_connected_ref=ws_conn_state,
                                )
                            )
                        )
                    elif ws_primary:
                        sink.write(
                            make_fact(
                                FACT_TYPE_REST_POLL_DISABLED,
                                str(run_id),
                                {"reason": "ws_primary_healthy_mode", "poll_enabled": False},
                            )
                        )

                    eb_cfg = md.event_backbone
                    eb_market_id = (
                        getattr(app.paired_binary, "market_id", None)
                        if app.paired_binary is not None
                        else None
                    )
                    if run_primary:
                        live_tasks.append(
                            asyncio.create_task(
                                run_market_ws_ingest(
                                    coord,
                                    ws_tokens,
                                    stop=stop_live,
                                    shadow_store=None,
                                    authoritative_store=auth_store,
                                    primary_mode=True,
                                    sink=sink,
                                    run_id=run_id,
                                    url=ws_cfg.url,
                                    reconnect_backoff_s=ws_cfg.reconnect_backoff_s,
                                    compare_interval_s=ws_cfg.compare_interval_s,
                                    on_gap_recovery=_rest_recovery_after_gap,
                                    on_ws_connected=_on_ws_connected,
                                    on_ws_disconnected=_on_ws_disconnected,
                                    on_book_applied=_on_book_applied,
                                    on_market_event_applied=_on_market_event_applied,
                                    event_backbone_config_flag=eb_cfg.enabled,
                                    event_backbone_market_id=eb_market_id,
                                    event_backbone_reorder_buffer_ms=eb_cfg.reorder_buffer_ms,
                                )
                            )
                        )
                        log.info(
                            "market WS primary ingest enabled (%s tokens); REST poll gated",
                            len(ws_tokens),
                        )
                    elif run_shadow:
                        shadow_store = ensure_market_state_shadow_store(coord, app)
                        live_tasks.append(
                            asyncio.create_task(
                                run_market_ws_ingest(
                                    coord,
                                    ws_tokens,
                                    stop=stop_live,
                                    shadow_store=shadow_store,
                                    authoritative_store=auth_store,
                                    primary_mode=False,
                                    sink=sink,
                                    run_id=run_id,
                                    url=ws_cfg.url,
                                    reconnect_backoff_s=ws_cfg.reconnect_backoff_s,
                                    compare_interval_s=ws_cfg.compare_interval_s,
                                    on_gap_recovery=_rest_recovery_after_gap,
                                    on_market_event_applied=_on_market_event_applied,
                                    event_backbone_config_flag=eb_cfg.enabled,
                                    event_backbone_market_id=eb_market_id,
                                    event_backbone_reorder_buffer_ms=eb_cfg.reorder_buffer_ms,
                                )
                            )
                        )
                        log.info(
                            "market WS shadow ingest enabled (%s tokens); REST remains authoritative",
                            len(ws_tokens),
                        )

            if sell_test_mode or app.strategy.exits.demo_forced_exit_enabled:
                live_tasks.append(
                    asyncio.create_task(
                        scheduled_exit_demo_due_loop(
                            strategy=strat,
                            app=app,
                            run_id=run_id,
                            coord=coord,
                            sink=sink,
                            oms=oms_backend,
                            apply_local_shadow_fill=apply_local_fill,
                            live_clob_client=live_clob,
                            stop=stop_live,
                        )
                    )
                )

            fixture_path: Path | None = args.fixture
            if allocation_test_mode:
                assert isinstance(strat, AllocationTestStrategy)
                iterations = await _run_allocation_test_loop(
                    args=args,
                    app=app,
                    run_id=run_id,
                    strat=strat,
                    coord=coord,
                    sink=sink,
                    oms_backend=oms_backend,
                    apply_local_fill=apply_local_fill,
                    live_clob=live_clob,
                    stop_live=stop_live,
                )
            elif tp_sl_test_mode:
                assert isinstance(strat, TpSlTestStrategy)
                iterations = await _run_tp_sl_test_loop(
                    args=args,
                    app=app,
                    run_id=run_id,
                    strat=strat,
                    coord=coord,
                    sink=sink,
                    oms_backend=oms_backend,
                    apply_local_fill=apply_local_fill,
                    live_clob=live_clob,
                    stop_live=stop_live,
                )
            elif sell_test_mode:
                assert isinstance(strat, SellTestStrategy)
                iterations = await _run_sell_test_loop(
                    args=args,
                    app=app,
                    run_id=run_id,
                    strat=strat,
                    coord=coord,
                    sink=sink,
                    oms_backend=oms_backend,
                    apply_local_fill=apply_local_fill,
                    live_clob=live_clob,
                    stop_live=stop_live,
                )
            elif simple_signal_test_mode:
                assert app.simple_signal_test is not None
                if app.runtime.execution_mode == ExecutionMode.LIVE:
                    readiness_timeout = float(
                        os.environ.get("TYREX_SIMPLE_SIGNAL_TEST_READINESS_S", "60")
                    )
                    ok_r, rsn = await _wait_sell_test_live_readiness(
                        coord, app, timeout_s=readiness_timeout
                    )
                    sink.write(
                        make_fact(
                            FACT_TYPE_HEALTH,
                            str(run_id),
                            {
                                "event": "simple_signal_test_readiness",
                                "ok": ok_r,
                                "detail": rsn,
                            },
                        )
                    )
                    if not ok_r:
                        log.error(
                            "simple_signal_test live readiness failed after %.1fs: %s "
                            "— aborting before BUY",
                            readiness_timeout,
                            rsn,
                        )
                        iterations = 0
                    else:
                        iterations = await run_fixture_signals_once(
                            app=app,
                            run_id=run_id,
                            coord=coord,
                            sink=sink,
                            oms=oms_backend,
                            cfg=app.simple_signal_test,
                            apply_local_shadow_fill=apply_local_fill,
                            live_clob_client=live_clob,
                        )
                else:
                    iterations = await run_fixture_signals_once(
                        app=app,
                        run_id=run_id,
                        coord=coord,
                        sink=sink,
                        oms=oms_backend,
                        cfg=app.simple_signal_test,
                        apply_local_shadow_fill=apply_local_fill,
                        live_clob_client=live_clob,
                    )
            elif validation_harness_mode:
                assert app.validation_harness is not None
                vh_cfg = app.validation_harness
                needs_live_readiness = (
                    app.runtime.execution_mode == ExecutionMode.LIVE
                    and vh_cfg.mode
                    not in (
                        VALIDATION_MODE_MARKET_DATA_READONLY,
                        VALIDATION_MODE_STALE_BOOK_DENY,
                        VALIDATION_MODE_PROTECTION_TP,
                        VALIDATION_MODE_PROTECTION_SL,
                        VALIDATION_MODE_PROTECTION_TRIGGER_LIVE,
                    )
                )
                if needs_live_readiness:
                    readiness_timeout = float(
                        os.environ.get("TYREX_VALIDATION_HARNESS_READINESS_S", "60")
                    )
                    ok_r, rsn = await _wait_sell_test_live_readiness(
                        coord, app, timeout_s=readiness_timeout
                    )
                    sink.write(
                        make_fact(
                            FACT_TYPE_HEALTH,
                            str(run_id),
                            {
                                "event": "validation_harness_readiness",
                                "ok": ok_r,
                                "detail": rsn,
                                "mode": vh_cfg.mode,
                            },
                        )
                    )
                    if not ok_r:
                        log.error(
                            "validation_harness live readiness failed after %.1fs: %s "
                            "— aborting mode=%s",
                            readiness_timeout,
                            rsn,
                            vh_cfg.mode,
                        )
                        iterations = 0
                    else:
                        iterations = await run_validation_harness_once(
                            app=app,
                            run_id=run_id,
                            coord=coord,
                            sink=sink,
                            oms=oms_backend,
                            cfg=vh_cfg,
                            apply_local_shadow_fill=apply_local_fill,
                            live_clob_client=live_clob,
                        )
                else:
                    iterations = await run_validation_harness_once(
                        app=app,
                        run_id=run_id,
                        coord=coord,
                        sink=sink,
                        oms=oms_backend,
                        cfg=vh_cfg,
                        apply_local_shadow_fill=apply_local_fill,
                        live_clob_client=live_clob,
                    )
            elif paired_binary_mode:
                assert app.paired_binary is not None
                pb_cfg = app.paired_binary
                state_dir = (root / args.state_dir).resolve()
                pb_state = recover_on_startup(
                    coord,
                    pb_cfg,
                    state_dir=state_dir,
                    sink=sink,
                    run_id=run_id,
                    pb_rt=app.runtime.paired_binary,
                    execution_mode=app.runtime.execution_mode,
                )
                iterations = await run_paired_binary_loop(
                    app=app,
                    run_id=run_id,
                    coord=coord,
                    sink=sink,
                    oms=oms_backend,
                    cfg=pb_cfg,
                    state=pb_state,
                    state_dir=state_dir,
                    apply_local_shadow_fill=apply_local_fill,
                    live_clob_client=live_clob,
                    stop=stop_live,
                )
                if pb_state.is_terminal():
                    from tyrex_pm.strategies.paired_binary import facts as pb_facts
                    from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase

                    one_shot = app.runtime.paired_binary.stop_background_tasks_after_strategy_done
                    msg = (
                        "Strategy terminal; background tasks stopping (one-shot)."
                        if one_shot
                        else "Strategy is DONE; operator may stop app safely."
                    )
                    pb_facts.emit_strategy_terminal_safe_to_stop(
                        sink,
                        run_id,
                        final_state=pb_state.phase.value,
                        message=msg,
                    )
                    if one_shot:
                        stop_live.set()
                        for task in list(live_tasks):
                            task.cancel()
                        if live_tasks:
                            await asyncio.gather(*live_tasks, return_exceptions=True)
                        live_tasks.clear()
                        exit_code = 0 if pb_state.phase == PairedBinaryPhase.DONE else 1
                        log.info(
                            "One-shot shutdown after paired_binary terminal (final_state=%s)",
                            pb_state.phase.value,
                        )
            elif z_gap_mode:
                import time as _time

                from tyrex_pm.runtime.signal_feed_runtime import (
                    signal_feeds_requested,
                    start_signal_feeds,
                    stop_signal_feeds,
                )
                from tyrex_pm.runtime.z_gap_run import (
                    ZGapStartupTimings,
                    run_z_gap_observe_loop,
                )
                from tyrex_pm.strategies.z_gap.facts import (
                    ZGapObserveRuntimeState,
                    handle_feed_health_callback,
                )

                startup_t0 = _time.monotonic()
                startup = ZGapStartupTimings()
                if coord.time_authority is None:
                    log.error("z_gap observe: TimeAuthority missing after pre-feed sync")

                zg_observe_state = ZGapObserveRuntimeState()
                signal_feed_state = None

                def _zg_on_health(payload: dict) -> None:
                    handle_feed_health_callback(sink, run_id, zg_observe_state, dict(payload))

                feeds_t0 = _time.monotonic()
                if signal_feeds_requested(app) and app.z_gap is not None:
                    signal_feed_state = await start_signal_feeds(
                        coord=coord,
                        app=app,
                        stop=stop_live,
                        market_id=app.z_gap.market_id,
                        event_start_ts=app.z_gap.event_start_ts,
                        event_end_ts=app.z_gap.event_end_ts,
                        on_health=_zg_on_health,
                    )
                    feeds_t1 = _time.monotonic()
                    startup.binance_connect_ms = round((feeds_t1 - feeds_t0) * 1000.0, 1)
                    startup.rtds_connect_ms = startup.binance_connect_ms
                    startup.signal_state_ready_ms = startup.binance_connect_ms
                    if signal_feed_state.price_to_beat_tracker is not None:
                        startup.ptb_tracker_registered_ms = startup.binance_connect_ms
                    log.info(
                        "z_gap signal feeds started (external_btc=%s reference_prices=%s)",
                        app.runtime.external_btc.enabled,
                        app.runtime.reference_prices.enabled,
                    )

                startup.total_startup_ms = round((_time.monotonic() - startup_t0) * 1000.0, 1)

                exit_code = await run_z_gap_observe_loop(
                    app=app,
                    run_id=run_id,
                    coord=coord,
                    sink=sink,
                    stop=stop_live,
                    signal_feed_state=signal_feed_state,
                    runtime_state=zg_observe_state,
                    time_authority=coord.time_authority,
                    startup_timings=startup,
                )
                if signal_feed_state is not None:
                    await stop_signal_feeds(signal_feed_state)
                iterations = 0
            elif fixture_path is not None:  # noqa: E701 — keep elif chain readable
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
                    if app.strategy.exits.demo_forced_exit_enabled:
                        await asyncio.sleep(float(app.strategy.exits.demo_forced_exit_delay_s) + 0.2)
                    save_strategy_store(state_path, strategy_store)
                    iterations = 1
            elif strategy_kind == STRATEGY_KIND_GURU_FOLLOW:
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

            else:
                raise RuntimeError(
                    f"strategy kind {strategy_kind!r} has no main-loop wired in runtime/app.py"
                )

            sink.write(
                make_fact(
                    FACT_TYPE_HEALTH,
                    str(run_id),
                    {"status": "stopped", "iterations": iterations},
                )
            )
        finally:
            stop_live.set()
            if signal_feed_state is not None:
                from tyrex_pm.runtime.signal_feed_runtime import stop_signal_feeds

                await stop_signal_feeds(signal_feed_state)
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
    return exit_code


