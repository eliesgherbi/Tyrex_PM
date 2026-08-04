"""N7 live one-shot session: one BTC 5m window, at most one entry lineage.

Does not import ``runtime.r7*``. Operator ``--live`` arms mutations.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.adapters.polymarket.sdk_errors import (
    primary_blocker_from_compose_errors,
    vpn_hint_from_error_texts,
)
from tyrex_pm.core.clock import SystemClock
from tyrex_pm.core.ids import new_run_id
from tyrex_pm.core.intents import EnterIntent
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.domain.polymarket.fees import FeeCurveParams
from tyrex_pm.execution.polymarket.mutation_transport import SdkMutationTransport
from tyrex_pm.execution.polymarket.sdk_readonly import build_official_readonly_client
from tyrex_pm.runtime.live_zgap_compose import (
    run_live_zgap_compose,
    seconds_until_next_boundary,
)
from tyrex_pm.runtime.n4_observe_runtime import N4ObserveRuntime
from tyrex_pm.runtime.n7_oneshot_host import N7OneShotHost
from tyrex_pm.runtime.n7_ptb_policy import no_entry_reason, ptb_trust_fields
from tyrex_pm.runtime.n7_sealed import N7SealedConfig
from tyrex_pm.strategies.context import DecisionContext


def _load_dotenv(path: Path | None) -> dict[str, str]:
    env: dict[str, str] = {}
    if path is None or not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _book_from_session(runtime: N4ObserveRuntime) -> BookSnapshot | None:
    """Prefer authoritative store book; session tops are read-only projections."""
    sess = runtime.active
    if sess is None:
        return None
    if runtime.book_store is not None:
        st = runtime.book_store.get(sess.market.yes.instrument_id)
        if st.book is not None:
            return st.book
    if sess.up_ask is None or sess.up_bid is None:
        return None
    if sess.up_bid > sess.up_ask:
        return None
    return BookSnapshot.from_levels(
        instrument_id=sess.market.yes.instrument_id,
        ts_event=runtime.clock.now_utc(),
        bids=[(str(sess.up_bid), "10")],
        asks=[(str(sess.up_ask), "10")],
    )


def _capture_intents(runtime: N4ObserveRuntime, captured: dict[str, Any]) -> list[dict]:
    """Evaluate sealed active session; stash EnterIntent if any.

    Persists full decision diagnostics via optional ``captured['reporter']``
    (RunReporter). Never reduces the decision to a Python class name.
    """
    ready = runtime.prepare_aligned_eval()
    if not ready.ok or ready.session is None or ready.snapshot is None:
        captured["last_skip_reasons"] = list(ready.skip_reasons)
        reporter = captured.get("reporter")
        if reporter is not None:
            reporter.emit_dict(
                event_family="data_health",
                event_type="data_health.skipped_eval",
                payload={"skipped": True, "reasons": list(ready.skip_reasons)},
                producer="n7_live_session",
            )
        return [{"kind": "skip", "reasons": list(ready.skip_reasons)}]
    assert ready.binding is not None and ready.dyn is not None
    assert ready.sealed is not None and ready.binance_raw is not None
    assert ready.binance_source_ts is not None

    snap = ready.snapshot
    ctx = DecisionContext(
        run_id=new_run_id(),
        mode=RuntimeMode.OBSERVE,
        snapshot=snap,
        target_notional=ready.binding.target_notional,
        now=snap.observed_at,
    )
    result = ready.binding.evaluate(
        market_snapshot=snap,
        causation_id=snap.causation_id,
        correlation_id=snap.correlation_id,
        trigger="feed",
        decision_context=ctx,
        momentum_value=None,
        momentum_ready=False,
        momentum_reason="n7_unused",
        momentum_threshold=Decimal("0"),
        max_book_spread=Decimal("1"),
        settlement_ref=ready.dyn.chainlink_raw,
        settlement_ref_fresh=ready.dyn.chainlink_raw is not None,
        volatility_price=ready.binance_raw,
        volatility_ts=ready.binance_source_ts,
    )
    captured["evals"] = int(captured.get("evals") or 0) + 1
    captured["market"] = ready.session.market
    captured["book"] = _book_from_session(runtime)
    decision = result.decision
    captured["last_decision"] = {
        "action": decision.action.value,
        "reason_code": decision.reason_code,
        "decision_id": decision.decision_id,
        "evidence": dict(decision.evidence),
    }
    captured["model_anchor_k"] = None if ready.model_anchor is None else str(ready.model_anchor)
    captured["sealed_k"] = str(ready.sealed.ptb_k)
    enters = [i for i in result.intents if isinstance(i, EnterIntent)]
    if enters and not captured.get("intents"):
        captured["intents"] = enters
        captured["stop_requested"] = True

    reporter = captured.get("reporter")
    if reporter is not None:
        from tyrex_pm.reporting.adapters import emit_decision_from_eval
        from tyrex_pm.strategies.z_gap.reporting import (
            STRATEGY_VERSION,
            ZGapDiagnosticsContract,
        )

        diag_contract = captured.get("diagnostics_contract") or ZGapDiagnosticsContract()
        ctx_rep = result.reporting_context or {
            "decision": decision,
            "actionable": bool(result.intents),
        }
        diagnostics = None
        gates: list = []
        closest = None
        if result.reporting_context is not None:
            diagnostics = diag_contract.build_diagnostics(ctx_rep)
            gates = diag_contract.build_gates(ctx_rep)
            closest = diag_contract.closest_candidate_fields(ctx_rep)
        emit_decision_from_eval(
            reporter,
            action=decision.action.value,
            reason_code=decision.reason_code,
            decision_id=decision.decision_id,
            evaluation_id=(
                None
                if result.reporting_context is None
                else result.reporting_context["decision_input"].epoch.epoch_id
            ),
            gates=gates,
            diagnostics=diagnostics,
            closest_candidate=closest,
            intent_emitted=bool(enters),
            blocked_safety=decision.action.value == "BLOCKED",
            producer="n7_live_session",
            strategy_id=str(result.strategy_id.value),
            strategy_version=STRATEGY_VERSION,
            market_id=str(ready.session.market.market_id.value),
            extra_payload={"evidence": dict(decision.evidence)},
        )

    return [
        {
            "kind": "evaluated",
            "action": decision.action.value,
            "reason_code": decision.reason_code,
            "decision_id": decision.decision_id,
            "intent_types": [type(i).__name__ for i in result.intents],
            "enter_captured": bool(enters),
            "sealed_k": captured.get("sealed_k"),
            "model_anchor_k": captured.get("model_anchor_k"),
        }
    ]


def _trust_from_seal(
    seal: dict[str, Any], *, require_ssr: bool
) -> dict[str, Any]:
    return ptb_trust_fields(
        sealed_k=seal.get("sealed_k"),
        require_ssr_price_match=require_ssr,
        ptb_ready=bool(seal.get("ptb_ready", True)),
        ssr_check_status=str(
            seal.get("ssr_check_status")
            or ("REQUIRED" if require_ssr else "DISABLED")
        ),
    )


async def run_live_oneshot_session(
    *,
    repo: Path,
    out_dir: Path,
    sealed: N7SealedConfig,
    dotenv: Path | None,
    max_duration_s: float,
    preflight: dict[str, Any],
    zgap_config: Any | None = None,
    target_notional: Decimal | None = None,
    reporter: Any | None = None,
) -> dict[str, Any]:
    """Discover → seal → evaluate → at most one live entry → Scope A exit."""
    _ = repo
    wait = seconds_until_next_boundary() - 45.0
    if wait > 1:
        await asyncio.sleep(min(wait, 180.0))

    require_ssr = sealed.require_ssr_price_match
    captured: dict[str, Any] = {
        "intents": [],
        "evals": 0,
        "last_skip_reasons": [],
        "reporter": reporter,
    }

    def on_eval(runtime: N4ObserveRuntime) -> list[dict]:
        return _capture_intents(runtime, captured)

    summary = await run_live_zgap_compose(
        mode="n4_observe",
        out_dir=out_dir / "compose",
        run_id=datetime.now(timezone.utc).strftime("n7_%Y%m%dT%H%M%SZ"),
        min_seals=1,
        max_duration_s=max_duration_s,
        prep_lead_s=45.0,
        stop_when_seals_met=False,
        on_after_seal_eval=on_eval,
        should_stop=lambda: bool(captured.get("stop_requested")),
        require_ssr_price_match=require_ssr,
        zgap_config=zgap_config,
        target_notional=target_notional,
    )
    d = summary.to_dict()
    seals = d.get("seals") or []

    if not seals:
        compose_errors = list(d.get("errors") or [])
        primary, downstream = primary_blocker_from_compose_errors(compose_errors)
        payload = {
            "mode": "n7_operator_oneshot",
            "outcome": "ABORTED_BEFORE_MUTATION",
            "live": True,
            # Preserve discovery/identity primary cause; no_ptb_seal is downstream.
            "reason": primary or "no_ptb_seal",
            "primary_abort_code": primary or "no_ptb_seal",
            "downstream_abort_codes": [downstream or "PTB_NOT_STARTED", "no_ptb_seal"],
            "real_venue_mutations": 0,
            "preflight": preflight,
            "compose": {
                "feeds": d.get("feeds"),
                "discovery": d.get("discovery"),
                "errors": compose_errors,
            },
            "vpn_hint": vpn_hint_from_error_texts(compose_errors),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        payload.update(
            ptb_trust_fields(
                sealed_k=None,
                require_ssr_price_match=require_ssr,
                ptb_ready=False,
            )
        )
        return payload

    seal0 = seals[0]
    trust = _trust_from_seal(seal0, require_ssr=require_ssr)
    enters = list(captured.get("intents") or [])
    evals = int(captured.get("evals") or 0)
    if not enters:
        reason = no_entry_reason(
            evals=evals,
            last_skip_reasons=list(captured.get("last_skip_reasons") or []),
            require_ssr_price_match=require_ssr,
        )
        payload = {
            "mode": "n7_operator_oneshot",
            "outcome": "PASS_N7_SAFE_NO_ENTRY",
            "live": True,
            "reason": reason,
            "real_venue_mutations": 0,
            "preflight": preflight,
            "seal": seal0,
            "discovery": d.get("discovery"),
            "evals": evals,
            "model_anchor_k": captured.get("model_anchor_k"),
            "mutations_disabled": True,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        payload.update(trust)
        return payload

    market = captured.get("market")
    book = captured.get("book")
    if market is None or book is None:
        payload = {
            "mode": "n7_operator_oneshot",
            "outcome": "ABORTED_BEFORE_MUTATION",
            "live": True,
            "reason": "market_or_book_unavailable",
            "real_venue_mutations": 0,
            "preflight": preflight,
            "evals": evals,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        payload.update(trust)
        return payload

    env = _load_dotenv(dotenv)
    client = build_official_readonly_client(env=env)
    transport = SdkMutationTransport(_client=client)
    host = N7OneShotHost(
        sealed=sealed,
        clock=SystemClock(),
        transport=transport,
        market=market,
        persistence_path=out_dir / "runtime_state.json",
        fee_curve=FeeCurveParams(fee_rate=Decimal("0.07"), exponent=Decimal("1")),
    )
    if reporter is not None:
        host.attach_reporter(reporter)
    arm_err = host.arm_operator_live()
    if arm_err is not None:
        payload = {
            "mode": "n7_operator_oneshot",
            "outcome": "ABORTED_BEFORE_MUTATION",
            "live": True,
            "reason": arm_err.value,
            "real_venue_mutations": 0,
            "preflight": preflight,
            "evals": evals,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        payload.update(trust)
        return payload

    entered = host.try_enter(enters[0], book=book)
    mutations = (
        1
        if entered.get("status") in {"ACKNOWLEDGED", "DISPATCHED", "AMBIGUOUS"}
        else 0
    )

    exit_report: dict[str, Any] | None = None
    if entered.get("status") in {"ACKNOWLEDGED", "DISPATCHED"}:
        await asyncio.sleep(3.0)
        if host.inner is not None and host.inner.portfolio is not None:
            if not host.inner.portfolio.is_flat():
                bid = book.best_bid.price if book.best_bid else Decimal("0.01")
                exit_report = host.run_bounded_exit_ladder(book=book, limit_price=bid)
                await asyncio.sleep(3.0)
                if not host.inner.portfolio.is_flat():
                    exit_report = host.run_bounded_exit_ladder(
                        book=book, limit_price=bid, strategy_stale=True
                    )

    econ = host.economics_report()
    host.terminate(reason="operator_oneshot_complete")

    flat = bool(econ.get("flat"))
    residual = econ.get("remaining_inventory") or {}
    if mutations == 0:
        outcome = "PASS_N7_SAFE_NO_ENTRY"
        reason = "entry_not_dispatched"
    elif flat:
        outcome = "PASS_N7_ONE_SHOT_FLAT"
        reason = "one_shot_flat"
    elif residual:
        outcome = "PARTIAL_N7_RESIDUAL_OPERATOR_ACTION_REQUIRED"
        reason = "residual_inventory"
    else:
        outcome = "PASS_N7_SAFE_NO_ENTRY"
        reason = "safe_no_entry"

    payload = {
        "mode": "n7_operator_oneshot",
        "outcome": outcome,
        "live": True,
        "reason": reason,
        "real_venue_mutations": mutations,
        "preflight": preflight,
        "seal": seal0,
        "discovery": d.get("discovery"),
        "evals": evals,
        "model_anchor_k": captured.get("model_anchor_k"),
        "entry": entered,
        "exit": exit_report,
        "economics": econ,
        "status": host.status(),
        "mutations_disabled": host.mutations_force_off,
        "config_fingerprint": sealed.fingerprint(),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(trust)
    return payload
