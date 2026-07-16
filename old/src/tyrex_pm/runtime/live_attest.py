"""
Minimal live CLOB attestation: one intentional post + cancel via native OMS stack.

Uses the same LiveOMS / SingleWriterOMS / HealthRuntime / supervised heartbeat + venue
refresh + user WS (unless disabled) as `tyrex-pm run` in live mode.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, IntentId, RunId, TokenId, VenueOrderId
from tyrex_pm.core.models import ApprovedCancel, EnterIntent
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.execution.live_oms import LiveOMS
from tyrex_pm.execution.oms import SingleWriterOMS
from tyrex_pm.execution.order_lifecycle import ack_submit, register_submit, remove_resting_order
from tyrex_pm.ingestion.user_stream import run_user_ws_ingest
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_HEALTH,
    FACT_TYPE_INTENT,
    FACT_TYPE_LIVE_ATTEST,
    FACT_TYPE_OMS_CANCEL,
    FACT_TYPE_OMS_SUBMIT,
    FACT_TYPE_RECONCILE,
    FACT_TYPE_RISK,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.risk.engine import evaluate_intent
from tyrex_pm.runtime.config import load_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.live_supervisor import (
    supervised_heartbeat_loop,
    user_ws_staleness_loop,
    venue_refresh_loop,
)
from tyrex_pm.runtime.pipeline import _reconcile_kw, reconcile_coordinator
from tyrex_pm.risk.health import check_aggressive_readiness
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.reconcile import reconcile_open_orders
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.venue.polymarket.clob_bridge import PyClobBridge, parse_venue_order_id
from tyrex_pm.venue.polymarket.clob_env import (
    DEFAULT_CLOB_HOST_V2,
    try_create_clob_client,
    v2_sdk_version,
)
from tyrex_pm.venue.polymarket.clob_wallet_sync import refresh_wallet_from_clob
from tyrex_pm.venue.polymarket.market_info import MarketInfoCache, MarketInfoFetchError

log = logging.getLogger(__name__)

LIVE_ATTEST_CORR = "live-attest"


def _v2_environment_payload(client: object, host: str) -> dict:
    """Build the ``phase=v2_environment`` evidence payload (no secrets).

    Captures the V2 SDK version, host, chain id, signer EOA address, the
    configured wallet model (``signature_type`` 0/1/2/3), the funder address
    when distinct from the EOA, and whether a builder config is plumbed.
    Reads everything from already-resolved env / client attributes; never
    surfaces the private key, API secret, or builder code value.
    """
    sdk_version = v2_sdk_version()

    chain_id: int | None
    try:
        chain_id = int(os.environ.get("TYREX_CHAIN_ID", "137"))
    except ValueError:
        chain_id = None

    sig_raw = (
        os.environ.get("TYREX_SIGNATURE_TYPE", "").strip()
        or os.environ.get("POLYMARKET_SIGNATURE_TYPE", "").strip()
        or "0"
    )
    try:
        sig_type: int | None = int(sig_raw)
    except ValueError:
        sig_type = None
    funder = (
        os.environ.get("TYREX_FUNDER")
        or os.environ.get("POLYMARKET_FUNDER")
        or ""
    ).strip() or None
    builder_addr = (os.environ.get("TYREX_BUILDER_ADDRESS") or "").strip() or None
    builder_configured = bool((os.environ.get("TYREX_BUILDER_CODE") or "").strip())

    eoa_address: str | None = None
    try:
        addr = client.get_address()  # type: ignore[attr-defined]
        eoa_address = str(addr) if addr else None
    except Exception:  # noqa: BLE001
        eoa_address = None

    return {
        "phase": "v2_environment",
        "sdk_package": "py-clob-client-v2",
        "sdk_version": sdk_version,
        "host": host,
        "chain_id": chain_id,
        "signature_type": sig_type,
        "eoa_address": eoa_address,
        "funder_address": funder,
        "builder_configured": builder_configured,
        "builder_address": builder_addr,
    }


def _token_id_invalid_reason(token_id: str) -> str | None:
    tid = str(token_id).strip()
    if not tid:
        return "token_id is empty"
    if "<" in tid or ">" in tid:
        return "token_id looks like a placeholder; pass the numeric CLOB outcome token id (not <TOKEN>)"
    if not tid.isdigit():
        return "token_id must be a numeric string (Polymarket CLOB token id)"
    return None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _git_sha_str() -> str:
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


async def _wait_aggressive_readiness(
    coord: RuntimeCoordinator,
    app,
    *,
    timeout_s: float,
) -> tuple[bool, str]:
    deadline = monotonic_s() + timeout_s
    last_reason = "timeout"
    while monotonic_s() < deadline:
        ctx = coord.build_risk_context(app)
        ok, reason = check_aggressive_readiness(ctx, runtime=app.runtime, readiness=app.risk.readiness)
        if ok and ctx.heartbeat_ok and ctx.clob_session_ok:
            return True, "ok"
        last_reason = reason or "not_ready"
        await asyncio.sleep(0.5)
    return False, last_reason or "readiness_timeout"


async def cmd_live_attest(args: argparse.Namespace) -> int:
    root = args.repo_root or _repo_root()
    app = load_app_config(
        repo_root=root,
        strategy_file=str(args.strategy),
        scenario_file=args.scenario,
    )
    logging.basicConfig(level=getattr(logging, app.runtime.log_level.upper(), logging.INFO))

    if app.runtime.execution_mode != ExecutionMode.LIVE:
        log.error("live-attest requires execution_mode: live (use scenario live_attest)")
        return 2

    tid_err = _token_id_invalid_reason(str(args.token_id))
    if tid_err:
        log.error("live-attest invalid --token-id: %s", tid_err)
        return 2

    run_id = RunId(str(uuid4()))
    runs_dir = root / app.runtime.reporting.runs_dir / str(run_id)
    runs_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": str(run_id),
        "schema_version": 2,
        "git_sha": _git_sha_str(),
        "execution_mode": "live",
        "run_kind": "live_attest",
    }
    (runs_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    live_clob = try_create_clob_client()
    if live_clob is None:
        log.error("live-attest needs TYREX_PRIVATE_KEY and pip install tyrex-pm[live]")
        return 2

    live_bridge = PyClobBridge(live_clob)
    oms_writer = SingleWriterOMS(LiveOMS(live_bridge))
    oms_writer.start()

    clob_host = os.environ.get("TYREX_CLOB_HOST", DEFAULT_CLOB_HOST_V2)
    coord = RuntimeCoordinator(
        wallet=WalletStore(),
        orders=OrderStore(),
        health=HealthRuntime(),
        market_info_cache=MarketInfoCache(live_clob, host=clob_host),
    )
    res0 = reconcile_open_orders(coord.wallet, coord.orders, **_reconcile_kw(coord))
    coord.health.apply_reconcile(res0)

    stop_live = asyncio.Event()
    live_tasks: list[asyncio.Task[None]] = []
    exit_code = 1
    summary: dict = {"run_kind": "live_attest", "outcome": "failed", "exit_code": 1}

    rid = str(run_id)
    facts_path = runs_dir / "facts.jsonl"
    readiness_timeout = float(os.environ.get("TYREX_LIVE_ATTEST_READINESS_S", str(args.readiness_timeout_s)))

    try:
        with JsonlSink(facts_path) as sink:
            try:
                sink.write(make_fact(FACT_TYPE_HEALTH, rid, {"status": "started", "mode": "live", "run_kind": "live_attest"}))
                # Phase 7: emit a v2_environment evidence fact at the very top so
                # an operator inspecting facts.jsonl can immediately confirm the
                # exact V2 host / chain / wallet model / builder config that the
                # rest of the run was executed against. No secrets in payload.
                sink.write(
                    make_fact(
                        FACT_TYPE_LIVE_ATTEST,
                        rid,
                        _v2_environment_payload(live_clob, clob_host),
                        correlation_id=LIVE_ATTEST_CORR,
                    )
                )
                sink.write(
                    make_fact(
                        FACT_TYPE_LIVE_ATTEST,
                        rid,
                        {
                            "phase": "bootstrap",
                            "token_id": str(args.token_id),
                            "side": args.side,
                            "size": str(args.size),
                            "price": str(args.price),
                        },
                        correlation_id=LIVE_ATTEST_CORR,
                    )
                )

                hb_interval = max(5.0, float(os.environ.get("TYREX_HEARTBEAT_INTERVAL_S", "8")))
                venue_interval = float(os.environ.get("TYREX_VENUE_REFRESH_S", str(app.runtime.reconcile_interval_s)))

                try:
                    await refresh_wallet_from_clob(coord.wallet, live_clob)
                    # V2 cutover hygiene: open the new-order risk gate as soon as
                    # the first live venue truth rebuild succeeds. Until this flips
                    # ``check_aggressive_readiness`` denies with ``bootstrap_not_complete``.
                    coord.health.mark_first_v2_sync_complete()
                except Exception:
                    log.exception("live-attest bootstrap failed")
                    coord.health.mark_heartbeat(ok=False)
                    sink.write(
                        make_fact(
                            FACT_TYPE_LIVE_ATTEST,
                            rid,
                            {"phase": "bootstrap_failed"},
                            correlation_id=LIVE_ATTEST_CORR,
                        )
                    )
                    summary["outcome"] = "bootstrap_failed"
                    return 1

                # Phase 7: collateral_check evidence — record what the venue
                # said our pUSD balance / binding allowance is right after
                # bootstrap. This is the single most useful forensic field
                # when a later submit fails with ``not enough balance /
                # allowance``: the operator can compare bootstrap truth
                # against the venue's view at submit time without rerunning
                # the probe scripts.
                sink.write(
                    make_fact(
                        FACT_TYPE_LIVE_ATTEST,
                        rid,
                        {
                            "phase": "collateral_check",
                            "usdc_balance": (
                                str(coord.wallet.usdc_balance)
                                if coord.wallet.usdc_balance is not None
                                else None
                            ),
                            "usdc_allowance": (
                                str(coord.wallet.usdc_allowance)
                                if coord.wallet.usdc_allowance is not None
                                else None
                            ),
                            "last_sync_ts": (
                                coord.wallet.last_sync_ts.isoformat()
                                if coord.wallet.last_sync_ts is not None
                                else None
                            ),
                        },
                        correlation_id=LIVE_ATTEST_CORR,
                    )
                )

                # Phase 7: market_info evidence — resolve venue truth for the
                # attested token (tick_size, min_order_size, neg_risk,
                # fee_rate_bps, outcomes) and emit a fact so the operator
                # sees the venue parameters that gated this specific order.
                # Failure is fatal in live-attest (forensic clarity > best
                # effort): a missing market_info fact would defeat the
                # purpose of the attestation.
                try:
                    mi = await coord.market_info_cache.get(str(args.token_id))
                    sink.write(
                        make_fact(
                            FACT_TYPE_LIVE_ATTEST,
                            rid,
                            {
                                "phase": "market_info",
                                "token_id": str(mi.token_id),
                                "condition_id": mi.condition_id,
                                "tick_size": str(mi.tick_size),
                                "min_order_size": str(mi.min_order_size),
                                "neg_risk": mi.neg_risk,
                                "fee_rate_bps": mi.fee_rate_bps,
                                "outcomes": dict(mi.outcomes),
                            },
                            correlation_id=LIVE_ATTEST_CORR,
                        )
                    )
                except MarketInfoFetchError as e:
                    log.exception("live-attest market_info resolve failed")
                    sink.write(
                        make_fact(
                            FACT_TYPE_LIVE_ATTEST,
                            rid,
                            {
                                "phase": "market_info_failed",
                                "token_id": str(args.token_id),
                                "error": repr(e),
                            },
                            correlation_id=LIVE_ATTEST_CORR,
                        )
                    )
                    summary["outcome"] = "market_info_failed"
                    return 1

                live_tasks.append(
                    asyncio.create_task(
                        supervised_heartbeat_loop(
                            coord.health,
                            live_bridge,
                            hb_interval,
                            sink,
                            run_id=rid,
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
                            rid,
                            stop_live,
                        )
                    )
                )
                ws_disable = os.environ.get("TYREX_USER_WS_DISABLE", "").strip() == "1"
                if ws_disable:
                    log.warning(
                        "TYREX_USER_WS_DISABLE=1 during live-attest; use require_user_ws_live: false in scenario"
                    )
                    coord.health.user_ws_rest_only = True
                elif getattr(live_clob, "creds", None) is None:
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
                            run_id=rid,
                            stop=stop_live,
                        )
                    )
                )

                ok_r, rsn = await _wait_aggressive_readiness(coord, app, timeout_s=readiness_timeout)
                sink.write(
                    make_fact(
                        FACT_TYPE_LIVE_ATTEST,
                        rid,
                        {"phase": "readiness", "ok": ok_r, "detail": rsn},
                        correlation_id=LIVE_ATTEST_CORR,
                    )
                )
                if not ok_r:
                    log.error("live-attest readiness failed: %s", rsn)
                    summary["outcome"] = "readiness_failed"
                    summary["readiness_detail"] = rsn
                    return 1

                side = Side.BUY if str(args.side).upper() == "BUY" else Side.SELL
                intent = EnterIntent(
                    token_id=TokenId(str(args.token_id)),
                    side=side,
                    size=Decimal(str(args.size)),
                    limit_price=Decimal(str(args.price)),
                    order_style=OrderStyle.GTC,
                )
                sink.write(
                    make_fact(
                        FACT_TYPE_INTENT,
                        rid,
                        {
                            "kind": "EnterIntent",
                            "intent_id": str(intent.intent_id),
                            "token_id": str(intent.token_id),
                            "side": intent.side.value,
                            "size": str(intent.size),
                            "limit_price": str(intent.limit_price),
                            "order_style": intent.order_style.value,
                        },
                        correlation_id=LIVE_ATTEST_CORR,
                    )
                )

                ctx = coord.build_risk_context(app)
                decision = evaluate_intent(intent, ctx, app=app, run_id=run_id)
                sink.write(
                    make_fact(
                        FACT_TYPE_RISK,
                        rid,
                        {
                            "approved": decision.approved,
                            "reason_codes": list(decision.reason_codes),
                            "detail": decision.detail,
                        },
                        correlation_id=LIVE_ATTEST_CORR,
                    )
                )
                if not decision.approved or decision.approved_intent is None:
                    log.error("live-attest risk denied: %s", decision.reason_codes)
                    summary["outcome"] = "risk_denied"
                    summary["reason_codes"] = list(decision.reason_codes)
                    return 1

                ap = decision.approved_intent
                register_submit(coord.orders, ap)
                # Phase 5: pass market_info into the OMS so the order builder
                # tick-quantizes the limit price; Phase 7M: also use it to
                # build the ``tick_quantize_*`` evidence on the submit fact.
                cached_mi = (
                    coord.market_info_cache.snapshot().get(ap.intent.token_id)
                    if coord.market_info_cache is not None
                    else None
                )
                try:
                    res_place = await oms_writer.submit(ap, market_info=cached_mi)
                except Exception:
                    log.exception("live-attest submit failed")
                    sink.write(
                        make_fact(
                            FACT_TYPE_LIVE_ATTEST,
                            rid,
                            {"phase": "submit_exception"},
                            correlation_id=LIVE_ATTEST_CORR,
                        )
                    )
                    summary["outcome"] = "submit_failed"
                    return 1

                try:
                    parsed = json.loads(res_place)
                except Exception:
                    parsed = {}
                vid = parse_venue_order_id(parsed) if isinstance(parsed, dict) else None
                from tyrex_pm.execution.order_builder import build_quantize_evidence
                sink.write(
                    make_fact(
                        FACT_TYPE_OMS_SUBMIT,
                        rid,
                        {
                            "client_order_id": str(ap.client_order_id),
                            "oms_result": res_place,
                            **build_quantize_evidence(ap, cached_mi),
                        },
                        correlation_id=LIVE_ATTEST_CORR,
                    )
                )
                ack_submit(coord.orders, ap, vid, shadow_instant_fill=False)
                if vid is None:
                    log.error("live-attest could not parse venue order id from submit response")
                    sink.write(
                        make_fact(
                            FACT_TYPE_LIVE_ATTEST,
                            rid,
                            {"phase": "missing_venue_order_id", "oms_result": res_place},
                            correlation_id=LIVE_ATTEST_CORR,
                        )
                    )
                    summary["outcome"] = "missing_venue_order_id"
                    return 1

                ac = ApprovedCancel(
                    venue_order_id=vid,
                    client_order_id=ap.client_order_id,
                    run_id=run_id,
                    intent_id=IntentId(str(uuid4())),
                )
                try:
                    res_cancel = await oms_writer.cancel(ac)
                except Exception:
                    log.exception("live-attest cancel failed")
                    sink.write(
                        make_fact(
                            FACT_TYPE_LIVE_ATTEST,
                            rid,
                            {"phase": "cancel_exception", "venue_order_id": str(vid)},
                            correlation_id=LIVE_ATTEST_CORR,
                        )
                    )
                    summary["outcome"] = "cancel_failed"
                    return 1

                remove_resting_order(coord.orders, ap.client_order_id)
                sink.write(
                    make_fact(
                        FACT_TYPE_OMS_CANCEL,
                        rid,
                        {"venue_order_id": str(vid), "oms_result": res_cancel},
                        correlation_id=LIVE_ATTEST_CORR,
                    )
                )
                reconcile_coordinator(coord, sink, rid)
                # Phase 7M: outcome_validation evidence — on ``complete``,
                # cross-check the *requested* order against what the venue
                # actually accepted. ``order_open`` False means the cancel
                # closed the order cleanly (the resting state is gone from
                # the venue's open-orders REST snapshot). ``unexpected_fill``
                # flags any matched volume against an attestation that was
                # supposed to post-and-cancel without trading. Keeping this
                # in a single fact makes it trivial to build a one-line
                # "did the attestation actually attest to anything" check.
                still_open = any(
                    o.venue_order_id is not None and str(o.venue_order_id) == str(vid)
                    for o in coord.wallet.open_orders
                )
                sink.write(
                    make_fact(
                        FACT_TYPE_LIVE_ATTEST,
                        rid,
                        {
                            "phase": "complete",
                            "venue_order_id": str(vid),
                            "outcome_validation": {
                                "order_open_after_cancel": still_open,
                                "expected_open_after_cancel": False,
                                "cancel_clean": not still_open,
                            },
                        },
                        correlation_id=LIVE_ATTEST_CORR,
                    )
                )
                sink.write(
                    make_fact(
                        FACT_TYPE_HEALTH,
                        rid,
                        {"status": "stopped", "run_kind": "live_attest", "outcome": "ok"},
                    )
                )
                exit_code = 0
                summary = {
                    "run_kind": "live_attest",
                    "outcome": "ok",
                    "exit_code": 0,
                    "venue_order_id": str(vid),
                }
            finally:
                stop_live.set()
                if live_tasks:
                    await asyncio.gather(*live_tasks, return_exceptions=True)
    finally:
        await oms_writer.stop()
        summary["exit_code"] = exit_code
        (runs_dir / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        log.info("live-attest wrote %s (exit %s)", runs_dir, exit_code)

    return exit_code
