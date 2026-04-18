from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace

import httpx
from py_clob_client.exceptions import PolyApiException

from tyrex_pm.core.enums import ExecutionMode
from tyrex_pm.core.ids import RunId
from tyrex_pm.core.models import (
    CancelIntent,
    EnterIntent,
    ExitIntent,
    GuruTradeSignal,
    Intent,
    ReduceIntent,
)
from tyrex_pm.core import reason_codes as rc
from tyrex_pm.execution.adapters import OMSBackend
from tyrex_pm.execution.order_lifecycle import (
    ack_submit,
    register_submit,
    release_after_ack,
    remove_resting_order,
    submit_fingerprint_for_intent,
    sync_local_open_orders_from_venue_wallet,
)
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_GURU_SIGNAL,
    FACT_TYPE_INTENT,
    FACT_TYPE_OMS_CANCEL,
    FACT_TYPE_OMS_REJECT,
    FACT_TYPE_OMS_SUBMIT,
    FACT_TYPE_RECONCILE,
    FACT_TYPE_RISK,
    FACT_TYPE_STRATEGY_SKIP,
    FACT_TYPE_WALLET_SYNC,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.risk.engine import evaluate_intent
from tyrex_pm.risk.evidence_format import s_usd
from tyrex_pm.runtime.config import AppConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.signals.guru_copy_signal import to_copy_signal
from tyrex_pm.state.reconcile import reconcile_open_orders
from tyrex_pm.state.shadow_wallet import apply_shadow_fill
from tyrex_pm.strategies.guru_follow.strategy import GuruFollowStrategy
from tyrex_pm.venue.polymarket.clob_bridge import parse_venue_order_id
from tyrex_pm.venue.polymarket.clob_wallet_sync import refresh_wallet_from_clob
from tyrex_pm.venue.polymarket.gamma_client import GammaClient


def _guru_payload(sig: GuruTradeSignal) -> dict:
    return {
        "dedup_key": sig.dedup_key,
        "guru_wallet": sig.guru_wallet,
        "token_id": str(sig.token_id),
        "side": sig.side.value,
        "size": str(sig.size),
        "price": str(sig.price) if sig.price is not None else None,
        "notional_usd": str(sig.notional_usd) if sig.notional_usd is not None else None,
        "conviction_score": str(sig.conviction_score) if sig.conviction_score is not None else None,
    }


def _intent_payload(intent: Intent) -> dict:
    if isinstance(intent, (EnterIntent, ExitIntent, ReduceIntent)):
        return {
            "kind": intent.__class__.__name__,
            "intent_id": str(intent.intent_id),
            "token_id": str(intent.token_id),
            "side": intent.side.value,
            "size": str(intent.size),
            "limit_price": str(intent.limit_price) if intent.limit_price is not None else None,
            "order_style": intent.order_style.value,
        }
    if isinstance(intent, CancelIntent):
        return {
            "kind": "CancelIntent",
            "intent_id": str(intent.intent_id),
            "venue_order_id": str(intent.venue_order_id) if intent.venue_order_id else None,
            "client_order_id": str(intent.client_order_id) if intent.client_order_id else None,
        }
    return {"kind": "other", "repr": repr(intent)}


def _reconcile_kw(coord: RuntimeCoordinator) -> dict:
    submit_grace = float(coord.submit_grace_s)
    env_g = os.environ.get("TYREX_SUBMIT_GRACE_S") or os.environ.get("TYREX_VENUE_CONFIRM_GRACE_S")
    if env_g is not None:
        submit_grace = float(env_g)
    terminal_timeout = float(coord.provisional_unknown_terminal_timeout_s)
    env_t = (
        os.environ.get("TYREX_PROVISIONAL_UNKNOWN_TERMINAL_TIMEOUT_S")
        or os.environ.get("TYREX_VENUE_CONFIRM_PROVISIONAL_TIMEOUT_S")
    )
    if env_t is not None:
        terminal_timeout = float(env_t)
    adoption_grace = float(coord.adoption_grace_s)
    env_a = os.environ.get("TYREX_ADOPTION_GRACE_S")
    if env_a is not None:
        adoption_grace = float(env_a)
    return {
        "venue_user_ws_stale": coord.health.venue_truth_stale,
        "venue_restart_suspected": coord.health.venue_restart_suspected,
        "submit_grace_s": submit_grace,
        "unknown_terminal_timeout_s": terminal_timeout,
        "adoption_grace_s": adoption_grace,
    }


def _reconcile_signature(res, suppressed: tuple[str, ...]) -> tuple:
    """Operator-meaningful state of a reconcile result.

    Two reconciles producing the same signature carry the same headline truth — same drift
    flags, same blocking severity, same suppressed-by-tombstone REST ids, same number of
    repair / adoption decisions. Tight REST-poll bursts that re-confirm the same state are
    collapsed by comparing this tuple against ``coord.last_reconcile_signature``.

    Counts (not the per-row decision payloads themselves) are included so that a *new*
    repair/adoption decision row still produces a fresh fact even when the headline flags
    are unchanged.
    """
    return (
        tuple(sorted(res.drift_flags)),
        tuple(sorted(res.blocking_drift_flags)),
        bool(res.blocking_drift_flags),
        res.reconcile_severity,
        tuple(suppressed),
        len(res.provisional_repair_decisions or ()),
        len(res.venue_adoption_decisions or ()),
        len(res.provisional_timeout_resolutions or ()),
        len(res.pruned_terminal_venue_order_ids or ()),
    )


def _wallet_sync_signature(coord: RuntimeCoordinator) -> tuple:
    """Operator-meaningful state of the wallet refresh.

    Two consecutive REST ticks producing the same signature mean *nothing the operator can act
    on changed*: same balance, same allowance, same positions count, same open orders count,
    same mark coverage. Suppress the duplicate fact so a 30 s refresh loop does not flood the
    file with effectively-identical rows.

    Note: ``last_sync_ts`` and ``last_positions_sync_ts`` are intentionally **excluded** from
    the signature. They advance on every successful REST tick regardless of whether anything
    actionable changed, so including them would defeat the dedup entirely (observed in
    ``var/reporting/runs/live_tes_700`` where 70/97 wallet_sync facts were operator-identical
    duplicates only differing in refresh timestamp). The emitted fact still carries both
    timestamps in its payload for forensic timing — they just do not participate in dedup.
    """
    w = coord.wallet
    bal = str(w.usdc_balance) if w.usdc_balance is not None else None
    allow = str(w.usdc_allowance) if w.usdc_allowance is not None else None
    mark_count = sum(1 for p in w.positions.values() if p.avg_price_usd is not None)
    return (
        bal,
        allow,
        len(w.positions),
        len(w.open_orders),
        mark_count,
    )


def emit_wallet_sync(coord: RuntimeCoordinator, sink: JsonlSink, run_id: str) -> None:
    """Write a ``wallet_sync`` fact when the wallet snapshot has materially changed.

    Pairs with the REST refresh in :func:`tyrex_pm.runtime.live_supervisor.venue_refresh_loop`
    so operators can see *positively* (not just by absence of failures) that the positions /
    balance safety net is firing. Dedup uses :func:`_wallet_sync_signature` to suppress
    no-op refresh ticks.
    """
    sig = _wallet_sync_signature(coord)
    if coord.last_wallet_sync_signature is not None and sig == coord.last_wallet_sync_signature:
        return
    coord.last_wallet_sync_signature = sig
    w = coord.wallet
    payload: dict = {
        "wallet_usdc_balance": s_usd(w.usdc_balance),
        "wallet_usdc_allowance": s_usd(w.usdc_allowance),
        "last_sync_ts": w.last_sync_ts.isoformat() if w.last_sync_ts is not None else None,
        "last_positions_sync_ts": (
            w.last_positions_sync_ts.isoformat()
            if w.last_positions_sync_ts is not None
            else None
        ),
        "position_count": len(w.positions),
        "open_order_count": len(w.open_orders),
        "marks_present_count": sum(
            1 for p in w.positions.values() if p.avg_price_usd is not None
        ),
        "marks_missing_count": sum(
            1 for p in w.positions.values() if p.avg_price_usd is None and p.qty != 0
        ),
    }
    sink.write(make_fact(FACT_TYPE_WALLET_SYNC, run_id, payload))


def reconcile_coordinator(coord: RuntimeCoordinator, sink: JsonlSink, run_id: str) -> None:
    kw = _reconcile_kw(coord)
    res = reconcile_open_orders(coord.wallet, coord.orders, **kw)
    coord.health.apply_reconcile(res)
    suppressed = coord.wallet.get_tombstoned_rest_vids()
    sig = _reconcile_signature(res, suppressed)
    if coord.last_reconcile_signature is not None and sig == coord.last_reconcile_signature:
        # Unchanged operator-relevant state — skip writing a duplicate fact. We still
        # ran the reconcile (and applied health side-effects) so behavior is unchanged.
        return
    coord.last_reconcile_signature = sig
    payload: dict = {
        "drift_flags": list(res.drift_flags),
        "blocking_drift_flags": list(res.blocking_drift_flags),
        "reconcile_blocks_live": len(res.blocking_drift_flags) > 0,
        "reconcile_severity": res.reconcile_severity,
        "quantity_semantics": "remaining_vs_remaining; original_vs_original_when_both_set",
        "pruned_terminal_venue_order_ids": list(res.pruned_terminal_venue_order_ids),
        "reconcile_policy_summary": res.policy_summary,
        "venue_user_ws_stale": kw["venue_user_ws_stale"],
        "venue_restart_suspected": kw["venue_restart_suspected"],
        "submit_grace_s": kw["submit_grace_s"],
        "unknown_terminal_timeout_s": kw["unknown_terminal_timeout_s"],
        "adoption_grace_s": kw["adoption_grace_s"],
    }
    if res.drift_flags:
        payload["drift_flag_counts"] = dict(Counter(res.drift_flags))
    if res.blocking_drift_flags:
        payload["blocking_drift_flag_counts"] = dict(Counter(res.blocking_drift_flags))
    if res.order_comparisons:
        payload["order_comparisons"] = [dict(x) for x in res.order_comparisons]
    if res.provisional_repair_decisions:
        payload["provisional_repair_decisions"] = [dict(x) for x in res.provisional_repair_decisions]
    if res.venue_adoption_decisions:
        payload["venue_adoption_decisions"] = [dict(x) for x in res.venue_adoption_decisions]
    if res.provisional_timeout_resolutions:
        # Back-compat field for older operator dashboards/log-scrapers.
        payload["provisional_timeout_resolutions"] = [
            dict(x) for x in res.provisional_timeout_resolutions
        ]
    # Observability for the inverse race: REST briefly shows ids WS has already declared
    # terminal. With the WS-terminal tombstone in place those ids are correctly suppressed
    # from `wallet.open_orders` (and therefore not re-flagged as `venue_open_not_tracked_locally`).
    # Surfacing the suppressed ids here lets operators tell apart "real venue-only order"
    # from "stale REST resurrection caught by tombstone" without having to read WS logs.
    if suppressed:
        payload["tombstoned_rest_vids"] = list(suppressed)
    sink.write(
        make_fact(
            FACT_TYPE_RECONCILE,
            run_id,
            payload,
        )
    )


async def refresh_wallet_coordinated_after_live_submit(
    coord: RuntimeCoordinator,
    live_clob_client: object,
    *,
    transient_retry_s: float = 0.45,
) -> None:
    """
    Pull venue open orders after a live ack. If the new hedge is not visible yet (REST lag),
    refresh once more before leaving the coordinator to per-signal reconcile.
    """
    await refresh_wallet_from_clob(coord.wallet, live_clob_client)
    sync_local_open_orders_from_venue_wallet(coord.orders, coord.wallet)
    res = reconcile_open_orders(coord.wallet, coord.orders, **_reconcile_kw(coord))
    if res.blocking_drift_flags and all(f == "local_open_not_on_venue" for f in res.blocking_drift_flags):
        await asyncio.sleep(transient_retry_s)
        await refresh_wallet_from_clob(coord.wallet, live_clob_client)
        sync_local_open_orders_from_venue_wallet(coord.orders, coord.wallet)


async def process_new_guru_signals(
    new_signals: Sequence[GuruTradeSignal],
    *,
    app: AppConfig,
    run_id: RunId,
    strategy: GuruFollowStrategy,
    coord: RuntimeCoordinator,
    sink: JsonlSink,
    oms: OMSBackend,
    apply_local_shadow_fill: bool = True,
    http_client: httpx.AsyncClient | None = None,
    gamma_client: GammaClient | None = None,
    live_clob_client: object | None = None,
) -> None:
    """Guru → strategy → risk → OMS; optional synthetic shadow fill for offline parity."""
    rid = str(run_id)
    gamma = gamma_client or GammaClient()
    for sig in new_signals:
        corr = sig.dedup_key
        sink.write(
            make_fact(
                FACT_TYPE_GURU_SIGNAL,
                rid,
                _guru_payload(sig),
                correlation_id=corr,
            )
        )
        if app.strategy.filters.exclude_untradeable_markets:
            if http_client is None:
                sink.write(
                    make_fact(
                        FACT_TYPE_STRATEGY_SKIP,
                        rid,
                        {"reason": rc.MARKET_METADATA_UNAVAILABLE, "dedup_key": sig.dedup_key},
                        correlation_id=corr,
                    )
                )
                reconcile_coordinator(coord, sink, rid)
                continue
            ok_t, rsn = await gamma.is_token_tradeable(http_client, str(sig.token_id))
            if not ok_t:
                sink.write(
                    make_fact(
                        FACT_TYPE_STRATEGY_SKIP,
                        rid,
                        {"reason": rsn or rc.MARKET_UNTRADEABLE, "dedup_key": sig.dedup_key},
                        correlation_id=corr,
                    )
                )
                reconcile_coordinator(coord, sink, rid)
                continue

        copy_sig = to_copy_signal(sig)
        risk_ctx = coord.build_risk_context(app)
        intents, skip_reason, sizing_meta = strategy.on_guru_signal(copy_sig, coord.holdings())
        if skip_reason:
            sink.write(
                make_fact(
                    FACT_TYPE_STRATEGY_SKIP,
                    rid,
                    {"reason": skip_reason, "dedup_key": sig.dedup_key},
                    correlation_id=corr,
                )
            )
            reconcile_coordinator(coord, sink, rid)
            continue
        for intent in intents:
            intent_payload = _intent_payload(intent)
            if sizing_meta:
                intent_payload = {**intent_payload, **sizing_meta}
            sink.write(
                make_fact(
                    FACT_TYPE_INTENT,
                    rid,
                    intent_payload,
                    correlation_id=corr,
                )
            )
            risk_ctx = coord.build_risk_context(app)
            decision = evaluate_intent(intent, risk_ctx, app=app, run_id=run_id)
            risk_payload: dict = {
                "approved": decision.approved,
                "reason_codes": list(decision.reason_codes),
                "detail": decision.detail,
            }
            if decision.extensions:
                risk_payload.update(decision.extensions)
            sink.write(
                make_fact(
                    FACT_TYPE_RISK,
                    rid,
                    risk_payload,
                    correlation_id=corr,
                )
            )
            if decision.approved and decision.approved_cancel is not None:
                ac0 = decision.approved_cancel
                vid = ac0.venue_order_id
                cid = ac0.client_order_id
                if vid is None and cid is not None:
                    lo = coord.orders.orders.get(cid)
                    vid = lo.venue_order_id if lo else None
                if vid is None:
                    sink.write(
                        make_fact(
                            FACT_TYPE_OMS_CANCEL,
                            rid,
                            {"error": "missing_venue_order_id", "client_order_id": str(cid) if cid else None},
                            correlation_id=corr,
                        )
                    )
                    reconcile_coordinator(coord, sink, rid)
                    continue
                ac = replace(ac0, venue_order_id=vid)
                res = await oms.cancel(ac)
                if cid is not None:
                    remove_resting_order(coord.orders, cid)
                else:
                    for c, lo in list(coord.orders.orders.items()):
                        if lo.venue_order_id == vid:
                            remove_resting_order(coord.orders, c)
                            break
                sink.write(
                    make_fact(
                        FACT_TYPE_OMS_CANCEL,
                        rid,
                        {"venue_order_id": str(vid), "oms_result": res},
                        correlation_id=corr,
                    )
                )
            elif decision.approved and decision.approved_intent:
                ap = decision.approved_intent
                fp = submit_fingerprint_for_intent(ap)
                if coord.orders.has_pending_submit_fingerprint(fp):
                    sink.write(
                        make_fact(
                            FACT_TYPE_OMS_REJECT,
                            rid,
                            {
                                "client_order_id": str(ap.client_order_id),
                                "status_code": None,
                                "error_msg": rc.DUPLICATE_SUBMIT_BLOCKED,
                                "error": (
                                    "duplicate_submit_blocked: equivalent provisional order is "
                                    "still in repair (matching submit_fingerprint)"
                                ),
                                "submit_fingerprint": fp,
                            },
                            correlation_id=corr,
                        )
                    )
                    reconcile_coordinator(coord, sink, rid)
                    continue
                register_submit(coord.orders, ap)
                try:
                    res = await oms.submit(ap)
                except PolyApiException as e:
                    release_after_ack(coord.orders, ap.client_order_id)
                    err_body = e.error_msg
                    if e.status_code == 425:
                        coord.health.mark_venue_restart_suspected()
                    sink.write(
                        make_fact(
                            FACT_TYPE_OMS_REJECT,
                            rid,
                            {
                                "client_order_id": str(ap.client_order_id),
                                "status_code": e.status_code,
                                "error_msg": err_body,
                                "error": str(e),
                                "venue_restart_suspected": e.status_code == 425,
                            },
                            correlation_id=corr,
                        )
                    )
                    reconcile_coordinator(coord, sink, rid)
                    continue
                try:
                    parsed = json.loads(res)
                except Exception:
                    parsed = {}
                v_oid = parse_venue_order_id(parsed) if isinstance(parsed, dict) else None
                ack_status = None
                if isinstance(parsed, dict):
                    ack_status = parsed.get("status") or parsed.get("orderStatus")
                ack_submit(
                    coord.orders,
                    ap,
                    v_oid,
                    shadow_instant_fill=apply_local_shadow_fill,
                    ack_status=str(ack_status) if ack_status is not None else None,
                )
                if (
                    v_oid is not None
                    and not apply_local_shadow_fill
                    and live_clob_client is not None
                    and app.runtime.execution_mode == ExecutionMode.LIVE
                ):
                    await refresh_wallet_coordinated_after_live_submit(coord, live_clob_client)
                if apply_local_shadow_fill:
                    apply_shadow_fill(coord.wallet, ap)
                sink.write(
                    make_fact(
                        FACT_TYPE_OMS_SUBMIT,
                        rid,
                        {
                            "client_order_id": str(ap.client_order_id),
                            "oms_result": res,
                        },
                        correlation_id=corr,
                    )
                )
            reconcile_coordinator(coord, sink, rid)
