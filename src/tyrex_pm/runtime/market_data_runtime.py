"""Live public-data runtime for the Z-Gap strategy.

This module owns public-feed evidence only. Authentication, orders, account
state and venue mutations belong to the unified execution runtime.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from tyrex_pm.adapters.binance.ws_adapter import BinanceTradeWsAdapter
from tyrex_pm.adapters.clock_sync import ClockSyncLoop, OsMonitorClockSyncProvider
from tyrex_pm.adapters.polymarket.discovery import (
    GammaMarketDiscovery,
    current_btc_updown_slug,
)
from tyrex_pm.adapters.polymarket.rtds_adapter import RtdsChainlinkAdapter
from tyrex_pm.adapters.polymarket.ssr_ptb_attestation import (
    DisabledSsrAttestationProvider,
    SsrDisplayedPtbAttestationProvider,
)
from tyrex_pm.core.book_events import BookDeltaReceived, BookSnapshotReceived
from tyrex_pm.core.clock import SystemClock
from tyrex_pm.core.events import ReferencePriceUpdated, SettlementReferenceUpdated
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.core.time_authority import SnapshotTimeAuthority
from tyrex_pm.domain.polymarket.boundary_candidates import BoundaryTickView
from tyrex_pm.domain.polymarket.discovery_binding import DiscoverySessionRole
from tyrex_pm.domain.polymarket.ptb_attestation import AttestationResult
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.indicators.causal_pairing import PriceTickView, TradingReferenceIdentity
from tyrex_pm.market_data.binding_record import (
    BindingLifecycleRole,
    binding_record_from_discovery,
)
from tyrex_pm.market_data.book_feed import BookFeedSupervisor
from tyrex_pm.market_data.book_store import MarketStateStore
from tyrex_pm.runtime.market_runtime import (
    SessionSlot,
    ZGapMarketRuntime,
    project_session_quotes_from_view,
)
from tyrex_pm.strategies.z_gap.config import ZGapConfig, ZGapPtbTimeQualityConfig

# SSR openPrice often lags the EXACT Chainlink tick by a few seconds.
ATTESTATION_SEAL_GRACE_S = 45.0
# Cap strategy evaluation frequency (Binance trades are far denser than decisions).
EVAL_MIN_INTERVAL_S = 1.0

DurationAnchor = Literal["compose_start", "prepared_window_start"]


@dataclass
class SealRecord:
    window_id: str
    market_id: str
    sealed_k: str
    boundary_rule: str
    chainlink_source_ts: str
    receive_wall_raw_utc: str
    sealed_at: str
    boundary_lag_ms: int | None
    attestation_result: str
    attestation_source: str
    attested_value: str | None
    attestation_exact_diff: str | None
    attestation_bps_diff: str | None
    blockers: list[str]
    provenance_warnings: list[str]
    immutable_check_ok: bool
    ptb_authority: str = "chainlink_sealed_k_and_ssr_match"
    ssr_match_required: bool = True
    ssr_check_status: str = "REQUIRED"
    ptb_ready: bool = False
    source_timestamp_exact: bool = False
    arrival_within_policy: bool = False
    maximum_arrival_lag_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "market_id": self.market_id,
            "sealed_k": self.sealed_k,
            "boundary_rule": self.boundary_rule,
            "chainlink_source_ts": self.chainlink_source_ts,
            "receive_wall_raw_utc": self.receive_wall_raw_utc,
            "sealed_at": self.sealed_at,
            "boundary_lag_ms": self.boundary_lag_ms,
            "attestation_result": self.attestation_result,
            "attestation_source": self.attestation_source,
            "attested_value": self.attested_value,
            "attestation_exact_diff": self.attestation_exact_diff,
            "attestation_bps_diff": self.attestation_bps_diff,
            "blockers": list(self.blockers),
            "provenance_warnings": list(self.provenance_warnings),
            "immutable_check_ok": self.immutable_check_ok,
            "ptb_authority": self.ptb_authority,
            "ssr_match_required": self.ssr_match_required,
            "ssr_check_status": self.ssr_check_status,
            "ptb_ready": self.ptb_ready,
            "source_timestamp_exact": self.source_timestamp_exact,
            "arrival_within_policy": self.arrival_within_policy,
            "maximum_arrival_lag_ms": self.maximum_arrival_lag_ms,
        }


@dataclass
class MarketDataSummary:
    run_id: str
    out_dir: str
    started_at: str
    ended_at: str | None = None
    tls_verify: bool = True
    feeds: dict[str, Any] = field(default_factory=dict)
    discovery: dict[str, Any] = field(default_factory=dict)
    seals: list[dict[str, Any]] = field(default_factory=list)
    missed_windows: list[dict[str, Any]] = field(default_factory=list)
    clock: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    gate_notes: list[str] = field(default_factory=list)
    duration: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": "market_data_runtime",
            "run_id": self.run_id,
            "out_dir": self.out_dir,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "tls_verify": self.tls_verify,
            "feeds": self.feeds,
            "discovery": self.discovery,
            "seals": self.seals,
            "missed_windows": self.missed_windows,
            "clock": self.clock,
            "errors": self.errors,
            "gate_notes": self.gate_notes,
            "duration": self.duration,
        }


def _window_epoch(dt: datetime) -> int:
    return int(dt.timestamp()) // 300 * 300


def _trading_elapsed_s(
    *,
    now: datetime,
    compose_started: datetime,
    target_window_start: datetime | None,
    anchor: DurationAnchor,
) -> float:
    start = (
        target_window_start
        if anchor == "prepared_window_start" and target_window_start is not None
        else compose_started
    )
    return max(0.0, (now - start).total_seconds())


def _clock_payload(auth: SnapshotTimeAuthority) -> dict[str, Any]:
    view = auth.view()
    snapshot = auth.last_snapshot
    return {
        "sync_status": view.sync_status.value,
        "uncertainty_ms": view.uncertainty_ms,
        "maximum_uncertainty_ms": auth.max_uncertainty_ms,
        "ready": view.ready,
        "reason_code": view.reason_code,
        "estimated_offset_ms": view.estimated_offset_ms,
        "snapshot_age_ms": view.snapshot_age_ms,
        "clock_snapshot_id": view.clock_snapshot_id,
        "sources": []
        if snapshot is None
        else [
            {
                "source": source.source,
                "ok": source.ok,
                "offset_ms": source.offset_ms,
                "round_trip_ms": source.round_trip_ms,
                "uncertainty_ms": source.uncertainty_ms,
                "detail": source.detail,
            }
            for source in snapshot.sources
        ],
    }


async def run_market_data_runtime(
    *,
    out_dir: Path,
    run_id: str,
    min_seals: int = 3,
    max_duration_s: float = 1200.0,
    duration_anchor: DurationAnchor = "compose_start",
    preparation_timeout_s: float | None = None,
    prep_lead_s: float = 45.0,
    binance_symbol: str = "BTCUSDT",
    evaluation_interval_s: float = 1.0,
    basis_ewma_half_life_s: float | None = 30.0,
    on_book: Callable[[BookSnapshot], None] | None = None,
    on_evaluation: Callable[[ZGapMarketRuntime], list[dict[str, Any]]] | None = None,
    on_active_session: Callable[[Any], None] | None = None,
    on_prepared_session: Callable[[Any], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    stop_when_seals_met: bool = True,
    runtime: ZGapMarketRuntime | None = None,
    require_ssr_price_match: bool = True,
    zgap_config: ZGapConfig | None = None,
    target_notional: Decimal | None = None,
    on_rollover: Callable[[dict[str, Any]], None] | None = None,
    evaluation_ready: Callable[[], bool] | None = None,
    readiness_diagnostics: Callable[[], dict[str, Any]] | None = None,
    before_promote: Callable[[], bool] | None = None,
    on_feed_supervisor: Callable[[BookFeedSupervisor], None] | None = None,
    on_async_tick: Callable[[ZGapMarketRuntime], Awaitable[None]] | None = None,
    on_runtime_error: Callable[[str, BaseException], None] | None = None,
    max_clock_uncertainty_ms: int = 250,
) -> MarketDataSummary:
    """Bounded live composition: discover → ingest → EXACT seal → optional eval.

    Feeds remain alive until ``should_stop`` or the configured duration ends.
    Execution is delegated through callbacks and never projected by this module.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    run_started_raw = datetime.now(timezone.utc)
    run_started_mono_ns = time.monotonic_ns()
    summary = MarketDataSummary(
        run_id=run_id,
        out_dir=str(out_dir),
        started_at=run_started_raw.isoformat(),
    )

    def notify_runtime_error(stage: str, exc: BaseException) -> None:
        if on_runtime_error is None:
            return
        try:
            on_runtime_error(stage, exc)
        except Exception as callback_exc:  # evidence reporting cannot break public feeds
            summary.errors.append(
                f"runtime_error_callback:{type(callback_exc).__name__}:{callback_exc}"
            )

    summary.gate_notes.append(
        "basis_ewma_half_life_s is fixture/live-validation only; production half-life OPEN"
        if basis_ewma_half_life_s is not None
        else "basis_ewma_half_life_s=None (OPEN)"
    )
    _ = prep_lead_s

    clock = runtime.clock if runtime is not None else SystemClock()
    auth = SnapshotTimeAuthority(
        clock=clock,
        max_uncertainty_ms=max_clock_uncertainty_ms,
    )
    provider = OsMonitorClockSyncProvider(enable_binance_cross_check=True)
    clock_loop = ClockSyncLoop(provider=provider, apply=auth.apply_snapshot, interval_s=5.0)
    try:
        snap = await provider.measure()
        auth.apply_snapshot(snap)
        summary.clock = _clock_payload(auth)
    except Exception as exc:
        summary.errors.append(f"clock:{type(exc).__name__}:{exc}")
        notify_runtime_error("clock", exc)

    if not isinstance(require_ssr_price_match, bool):
        raise ValueError("require_ssr_price_match must be a boolean")
    if duration_anchor not in {"compose_start", "prepared_window_start"}:
        raise ValueError(f"unsupported duration_anchor={duration_anchor!r}")
    if preparation_timeout_s is not None and preparation_timeout_s <= 0:
        raise ValueError("preparation_timeout_s must be positive when provided")
    # When SSR match is not required, do not scrape Polymarket HTML at all.
    attestation = (
        SsrDisplayedPtbAttestationProvider(retries=1, retry_delay_s=2.0)
        if require_ssr_price_match
        else DisabledSsrAttestationProvider()
    )
    if runtime is None:
        cfg = zgap_config
        if cfg is None:
            cfg = ZGapConfig(
                ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
            )
        runtime = ZGapMarketRuntime.create(
            clock=clock,
            attestation_port=None if not require_ssr_price_match else attestation,
            basis_ewma_half_life_s=basis_ewma_half_life_s,
            time_authority=auth,
            zgap_config=cfg,
            require_ssr_price_match=require_ssr_price_match,
            target_notional=target_notional,
        )
    else:
        runtime.require_ssr_price_match = require_ssr_price_match
        runtime.ptb_engine.attestation_port = None if not require_ssr_price_match else attestation
        # Rebind strategy time authority to the shared corrected clock view.
        runtime.zgap.time_authority = auth
        if target_notional is not None:
            runtime.zgap.target_notional = target_notional
        for child in runtime._strategy_by_window.values():
            child.time_authority = auth
            if target_notional is not None:
                child.target_notional = target_notional
    summary.gate_notes.append(
        "ssr_match_required=true"
        if require_ssr_price_match
        else "ssr_match_required=false; ptb_authority=chainlink_sealed_k; ssr_check_status=DISABLED"
    )

    discovery = GammaMarketDiscovery()
    try:
        active_binding = await discovery.resolve_btc_5m_window(
            slug=current_btc_updown_slug(),
            session_role=DiscoverySessionRole.ACTIVE,
        )
        prepared = await discovery.prepare_next_btc_5m()
        summary.discovery = {
            "active_slug": active_binding.window_slug,
            "prepared_slug": prepared.window_slug,
            "active_market_id": active_binding.market.market_id.value,
            "prepared_market_id": prepared.market.market_id.value,
            "outcome_semantics": active_binding.outcome_semantics,
        }
    except Exception as exc:
        summary.errors.append(f"discovery:{type(exc).__name__}:{exc}")
        notify_runtime_error("discovery", exc)
        summary.ended_at = datetime.now(timezone.utc).isoformat()
        _write_summary(out_dir, summary)
        return summary

    active_sess = runtime.open_session(
        slot=SessionSlot.ACTIVE,
        market=active_binding.market,
        window_id=active_binding.window_slug,
        binding=active_binding,
        entry_enabled=duration_anchor != "prepared_window_start",
    )
    if on_active_session is not None:
        on_active_session(active_sess)
    prepared_sess = runtime.open_session(
        slot=SessionSlot.PREPARED_NEXT,
        market=prepared.market,
        window_id=prepared.window_slug,
        binding=prepared,
        publish_as_active=False,
        entry_enabled=True,
    )
    if on_prepared_session is not None:
        on_prepared_session(prepared_sess)

    disp = EventDispatcher()
    book_store = MarketStateStore()
    book_store.attach(disp)
    runtime.book_store = book_store
    feed_supervisor = BookFeedSupervisor(store=book_store, dispatcher=disp, out_dir=out_dir)
    if on_feed_supervisor is not None:
        on_feed_supervisor(feed_supervisor)
    counts = {"chainlink": 0, "binance": 0, "clob": 0}
    feed_notes: list[str] = []
    sealed_windows: set[str] = set()
    seal_fail_logged: set[str] = set()
    last_eval_mono: float = 0.0

    def _project_active_quotes() -> None:
        view = feed_supervisor.active_view()
        if view is None or runtime.active is None:
            return
        project_session_quotes_from_view(runtime.active, view)
        runtime.active_binding_record = (
            None if feed_supervisor.active is None else feed_supervisor.active.binding
        )

    def _try_seal(window_id: str) -> None:
        if window_id in sealed_windows:
            return
        sess = None
        for s in (runtime.active, runtime.prepared_next):
            if s is not None and s.window_id == window_id:
                sess = s
                break
        if sess is None:
            return
        state = runtime.ptb_engine.get_window(sess.market_id, window_id)
        if state is None or state.selected_candidate is None:
            return
        if state.sealed is not None:
            sealed_windows.add(window_id)
            return
        now_c = auth.now_corrected_utc()
        if require_ssr_price_match:
            # Refresh comparison attestation; delay seal while SSR openPrice is unpublished.
            try:
                runtime.ptb_engine.attest(market_id=sess.market_id, window_id=window_id)
            except Exception as exc:
                if window_id not in seal_fail_logged:
                    summary.errors.append(f"attest:{window_id}:{type(exc).__name__}:{exc}")
                    notify_runtime_error("ptb_attestation", exc)
                    seal_fail_logged.add(window_id)
                return
            state = runtime.ptb_engine.get_window(sess.market_id, window_id)
            assert state is not None
            att = state.attestation
            age_s = (now_c - state.event_start).total_seconds()
            if (
                att is None or att.result is AttestationResult.INCOMPLETE
            ) and age_s < ATTESTATION_SEAL_GRACE_S:
                return
        try:
            sealed = runtime.attest_and_seal(
                market_id=sess.market_id,
                window_id=window_id,
                sealed_at=now_c,
                require_attestation_match=False,
                skip_attestation=not require_ssr_price_match,
            )
        except Exception as exc:
            summary.missed_windows.append(
                {
                    "window_id": window_id,
                    "reason": f"seal_failed:{type(exc).__name__}:{exc}",
                }
            )
            return
        # Immutability: second seal must return identical K
        sealed2 = runtime.ptb_engine.seal(
            market_id=sess.market_id, window_id=window_id, sealed_at=now_c
        )
        immutable_ok = sealed2.ptb_k == sealed.ptb_k
        state_after = runtime.ptb_engine.get_window(sess.market_id, window_id)
        att_rec = None if state_after is None else state_after.attestation
        recv_wall = ""
        if state.selected_candidate is not None:
            recv_wall = state.selected_candidate.receive_wall_raw_utc.isoformat()
        ssr_status = "REQUIRED" if require_ssr_price_match else "DISABLED"
        provenance_warnings = list(sealed.blocker_reasons)
        entry_blockers: list[str] = []
        maximum_ptb_lag_ms = runtime.zgap.config.ptb_time_quality.max_ptb_lag_ms
        lag_ok = (
            sealed.boundary_lag_ms is not None
            and sealed.boundary_lag_ms <= maximum_ptb_lag_ms
        )
        if not lag_ok:
            entry_blockers.append(
                f"ptb_lag_exceeded:{sealed.boundary_lag_ms}>{maximum_ptb_lag_ms}"
            )
        exact_source_timestamp = sealed.boundary_rule_id.value == "EXACT_AT_START"
        if not exact_source_timestamp:
            entry_blockers.append(f"ptb_boundary_rule:{sealed.boundary_rule_id.value}")
        if sealed.clock_status in {"UNSYNCHRONIZED", "DEGRADED"}:
            entry_blockers.append(f"ptb_clock_status:{sealed.clock_status}")
        if (
            require_ssr_price_match
            and sealed.ptb_attestation_result is not AttestationResult.MATCH
        ):
            entry_blockers.append(
                f"ptb_attestation:{sealed.ptb_attestation_result.value}"
            )
        chainlink_ok = (
            sealed.ptb_k > 0
            and exact_source_timestamp
            and immutable_ok
            and lag_ok
            and sealed.clock_status not in {"UNSYNCHRONIZED", "DEGRADED"}
        )
        ptb_ready = bool(
            chainlink_ok
            and (
                not require_ssr_price_match
                or sealed.ptb_attestation_result is AttestationResult.MATCH
            )
        )
        rec = SealRecord(
            window_id=window_id,
            market_id=sess.market_id.value,
            sealed_k=str(sealed.ptb_k),
            boundary_rule=sealed.boundary_rule_id.value,
            chainlink_source_ts=sealed.chainlink_boundary_source_ts.isoformat(),
            receive_wall_raw_utc=recv_wall,
            sealed_at=sealed.sealed_at.isoformat(),
            boundary_lag_ms=sealed.boundary_lag_ms,
            attestation_result=(
                "DISABLED" if not require_ssr_price_match else sealed.ptb_attestation_result.value
            ),
            attestation_source=(
                "ssr_check_disabled"
                if not require_ssr_price_match
                else ("none" if att_rec is None else att_rec.attestation_source)
            ),
            attested_value=None
            if (not require_ssr_price_match) or att_rec is None or att_rec.attested_value is None
            else str(att_rec.attested_value),
            attestation_exact_diff=None
            if (not require_ssr_price_match) or att_rec is None or att_rec.exact_diff is None
            else str(att_rec.exact_diff),
            attestation_bps_diff=None
            if (not require_ssr_price_match) or att_rec is None or att_rec.bps_diff is None
            else str(att_rec.bps_diff),
            blockers=list(dict.fromkeys(entry_blockers)),
            provenance_warnings=list(dict.fromkeys(provenance_warnings)),
            immutable_check_ok=immutable_ok,
            ptb_authority=(
                "chainlink_sealed_k"
                if not require_ssr_price_match
                else "chainlink_sealed_k_and_ssr_match"
            ),
            ssr_match_required=require_ssr_price_match,
            ssr_check_status=ssr_status,
            ptb_ready=ptb_ready,
            source_timestamp_exact=exact_source_timestamp,
            arrival_within_policy=lag_ok,
            maximum_arrival_lag_ms=maximum_ptb_lag_ms,
        )
        summary.seals.append(rec.to_dict())
        sealed_windows.add(window_id)
        (out_dir / f"seal_{window_id}.json").write_text(
            json.dumps(rec.to_dict(), indent=2) + "\n", encoding="utf-8"
        )

    def on_cl(e: SettlementReferenceUpdated) -> None:
        counts["chainlink"] += 1
        meta = e.ingress
        tick = BoundaryTickView(
            value=e.settlement.price,
            source_ts=e.ts_event,
            receive_wall_raw_utc=e.ts_received,
            receive_wall_corrected_utc=(None if meta is None else meta.receive_wall_corrected_utc),
            receive_monotonic_ns=0 if meta is None else meta.receive_monotonic_ns,
            ingress=meta,
            raw_fingerprint=None if meta is None else meta.raw_fingerprint,
        )
        for sess in (runtime.active, runtime.prepared_next):
            if sess is None:
                continue
            runtime.ingest_chainlink(
                window_id=sess.window_id,
                market_id=sess.market_id,
                tick=tick,
            )
            _try_seal(sess.window_id)

    def on_bn(e: ReferencePriceUpdated) -> None:
        nonlocal last_eval_mono
        if e.source.value != "binance":
            return
        counts["binance"] += 1
        meta = e.ingress
        runtime.ingest_binance(
            PriceTickView(
                value=e.reference.price,
                source_ts=e.ts_event,
                receive_wall_raw_utc=e.ts_received,
                receive_wall_corrected_utc=(
                    None if meta is None else meta.receive_wall_corrected_utc
                ),
                receive_monotonic_ns=0 if meta is None else meta.receive_monotonic_ns,
                identity=TradingReferenceIdentity.BINANCE_SPOT,
                ingress=meta,
            )
        )
        mono = clock.monotonic_ns() / 1e9
        if mono - last_eval_mono < evaluation_interval_s:
            return
        if runtime.active is None or runtime.active.sealed is None:
            return
        # FRH-01: stop entry evaluation only — keep feeds/book updates alive.
        if evaluation_ready is not None and not evaluation_ready():
            af = feed_supervisor.active
            diagnostics = {} if readiness_diagnostics is None else readiness_diagnostics()
            blocker_codes = [
                str(b.get("code"))
                for b in diagnostics.get("blockers", [])
                if isinstance(b, dict) and b.get("code")
            ]
            note = (
                "entry_eval_skipped:not_ready"
                f" blockers={','.join(blocker_codes) or 'UNSPECIFIED'}"
                f" feed_phase={None if af is None else af.phase.value}"
            )
            if not feed_notes or feed_notes[-1] != note:
                feed_notes.append(note)
            return
        last_eval_mono = mono
        if on_evaluation is not None:
            try:
                on_evaluation(runtime)
            except Exception as exc:  # noqa: BLE001 - keep the Binance socket alive
                summary.errors.append(f"entry_eval:{type(exc).__name__}:callback_failed")
                notify_runtime_error("entry_evaluation", exc)
                note = f"entry_eval_failed:{type(exc).__name__}"
                if not feed_notes or feed_notes[-1] != note:
                    feed_notes.append(note)

    def on_book_snap(e: BookSnapshotReceived) -> None:
        # Store owns mutation via MarketStateStore.attach; this is accounting only.
        counts["clob"] += 1
        _project_active_quotes()
        if on_book is not None:
            on_book(e.book)

    def on_book_delta(e: BookDeltaReceived) -> None:
        counts["clob"] += 1
        _project_active_quotes()

    disp.subscribe(SettlementReferenceUpdated, on_cl)
    disp.subscribe(ReferencePriceUpdated, on_bn)
    disp.subscribe(BookSnapshotReceived, on_book_snap)
    disp.subscribe(BookDeltaReceived, on_book_delta)

    cl = RtdsChainlinkAdapter(time_authority=auth, heartbeat_timeout_s=25)

    def _on_binance_health(status: str, detail: dict[str, Any]) -> None:
        if status == "handler_error":
            error_class = str(detail.get("error") or "UNKNOWN")
            summary.errors.append(f"binance_handler:{error_class}:event_dispatch_handler_failed")
        elif status == "reconnecting":
            note = f"binance_reconnecting:{detail.get('error') or 'connection_closed'}"
            if not feed_notes or feed_notes[-1] != note:
                feed_notes.append(note)

    bn = BinanceTradeWsAdapter(
        symbol=binance_symbol, time_authority=auth, on_health=_on_binance_health
    )
    # Binding-scoped feeds own WS + REST bootstrap (not session quote mutation).
    active_rec = binding_record_from_discovery(
        active_binding, role=BindingLifecycleRole.ACTIVE, role_epoch=0
    )
    prepared_rec = binding_record_from_discovery(
        prepared, role=BindingLifecycleRole.PREPARED_NEXT, role_epoch=0
    )
    await feed_supervisor.set_active(active_rec)
    await feed_supervisor.set_prepared(prepared_rec)
    runtime.active_binding_record = active_rec
    summary.gate_notes.append(
        "book_path=MarketStateStore→BookView; binding_feeds=ACTIVE+PREPARED_NEXT"
    )
    tasks = [
        asyncio.create_task(clock_loop.run(), name="clock"),
        asyncio.create_task(cl.run(disp), name="cl"),
        asyncio.create_task(bn.run(disp), name="bn"),
    ]

    compose_started_raw = datetime.now(timezone.utc)
    compose_started_mono_ns = time.monotonic_ns()
    target_window_start = (
        runtime.prepared_next.event_start
        if duration_anchor == "prepared_window_start" and runtime.prepared_next is not None
        else None
    )
    trading_deadline = (
        None
        if target_window_start is None
        else target_window_start + timedelta(seconds=max_duration_s)
    )
    summary.duration = {
        "anchor": duration_anchor,
        "compose_started_at_raw": compose_started_raw.isoformat(),
        "compose_started_monotonic_ns": compose_started_mono_ns,
        "target_window_started_at": None
        if target_window_start is None
        else target_window_start.isoformat(),
        "max_trading_duration_s": max_duration_s,
        "preparation_timeout_s": preparation_timeout_s,
        "trading_deadline_at": (
            None if trading_deadline is None else trading_deadline.isoformat()
        ),
    }
    try:
        while True:
            before_tick = auth.now_corrected_utc()
            if trading_deadline is not None and before_tick >= trading_deadline:
                summary.gate_notes.append("stop: authoritative_trading_deadline")
                break
            if on_async_tick is not None:
                try:
                    # Never cancel an in-flight execution operation to enforce
                    # the outer observation deadline. SDK/lifecycle operations
                    # own their bounded timeouts; the loop stops at the next
                    # safe boundary after the tick completes.
                    await on_async_tick(runtime)
                except Exception as exc:  # noqa: BLE001
                    summary.errors.append(f"async_tick:{type(exc).__name__}:{exc}")
                    notify_runtime_error("async_tick", exc)
            sleep_s = 1.0
            if trading_deadline is not None:
                sleep_s = min(
                    sleep_s,
                    max(
                        0.0,
                        (trading_deadline - auth.now_corrected_utc()).total_seconds(),
                    ),
                )
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)
            now = auth.now_corrected_utc()
            preparation_elapsed = max(
                0.0,
                (time.monotonic_ns() - compose_started_mono_ns) / 1e9,
            )
            elapsed = _trading_elapsed_s(
                now=now,
                compose_started=compose_started_raw,
                target_window_start=target_window_start,
                anchor=duration_anchor,
            )
            if (
                duration_anchor == "prepared_window_start"
                and target_window_start is not None
                and now < target_window_start
                and preparation_timeout_s is not None
                and preparation_elapsed >= preparation_timeout_s
            ):
                summary.gate_notes.append(
                    f"stop: preparation_timeout_s={preparation_timeout_s} elapsed"
                )
                break
            # For target-anchored LIVE, stop before promoting into a market that
            # is outside this one-shot trading budget.
            if elapsed >= max_duration_s:
                summary.gate_notes.append(f"stop: max_duration_s={max_duration_s} trading_elapsed")
                break
            # Promote when active window ends and we have sealed prepared-next
            if (
                runtime.active is not None
                and runtime.active.market.event_end is not None
                and now >= runtime.active.market.event_end
                and runtime.prepared_next is not None
            ):
                old_id = runtime.active.window_id
                if old_id not in sealed_windows:
                    summary.missed_windows.append(
                        {
                            "window_id": old_id,
                            "reason": "window_ended_without_exact_seal",
                            "note": "started mid-window or EXACT tick absent",
                        }
                    )
                try:
                    if before_promote is not None and not before_promote():
                        summary.gate_notes.append(f"promote_blocked:{old_id}")
                        # Retry on a later loop iteration once flat/ready.
                    else:
                        promoted = runtime.promote_prepared_next(at=now)
                        # Promote warm prepared feed without invalidating its books.
                        await feed_supervisor.promote_prepared()
                        runtime.active_binding_record = (
                            None
                            if feed_supervisor.active is None
                            else feed_supervisor.active.binding
                        )
                        _project_active_quotes()
                        if on_active_session is not None:
                            on_active_session(promoted)
                        if on_rollover is not None:
                            on_rollover(
                                {
                                    "from_window_id": old_id,
                                    "to_window_id": promoted.window_id,
                                    "to_market_id": promoted.market_id.value,
                                    "promoted_at": now.isoformat(),
                                }
                            )
                        # Open / warm next prepared binding (new feed).
                        try:
                            nxt = await discovery.prepare_next_btc_5m()
                            if (
                                runtime.prepared_next is None
                                or runtime.prepared_next.window_id != nxt.window_slug
                            ):
                                next_prepared_session = runtime.open_session(
                                    slot=SessionSlot.PREPARED_NEXT,
                                    market=nxt.market,
                                    window_id=nxt.window_slug,
                                    binding=nxt,
                                    publish_as_active=False,
                                )
                                if on_prepared_session is not None:
                                    on_prepared_session(next_prepared_session)
                                nxt_rec = binding_record_from_discovery(
                                    nxt,
                                    role=BindingLifecycleRole.PREPARED_NEXT,
                                    role_epoch=feed_supervisor.role_epoch_counter,
                                )
                                await feed_supervisor.set_prepared(nxt_rec)
                        except Exception as exc:
                            summary.errors.append(f"prepare_next:{type(exc).__name__}:{exc}")
                            notify_runtime_error("prepare_next", exc)
                except Exception as exc:
                    summary.errors.append(f"promote:{type(exc).__name__}:{exc}")
                    notify_runtime_error("market_promotion", exc)

            # Submission and exit callbacks continue while public feeds remain live.
            if stop_when_seals_met and len(summary.seals) >= min_seals:
                summary.gate_notes.append(f"stop: reached min_seals={min_seals}")
                break
            if should_stop is not None and should_stop():
                summary.gate_notes.append("stop: caller_requested")
                break
    finally:
        await clock_loop.stop()
        await cl.stop()
        await bn.stop()
        await feed_supervisor.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        summary.clock = _clock_payload(auth)

    active_feed = feed_supervisor.active
    prepared_feed = feed_supervisor.prepared
    book_metrics = book_store.metrics.to_dict() if hasattr(book_store.metrics, "to_dict") else {}
    summary.feeds = {
        "chainlink_ticks": counts["chainlink"],
        "binance_ticks": counts["binance"],
        "clob_book_events": counts["clob"],
        "clob_ready": bool(active_feed is not None and active_feed.phase.value == "READY"),
        "book_path": "MarketStateStore",
        "active_binding_id": None if active_feed is None else active_feed.binding.binding_id,
        "prepared_binding_id": None if prepared_feed is None else prepared_feed.binding.binding_id,
        "active_feed_phase": None if active_feed is None else active_feed.phase.value,
        "prepared_feed_phase": None if prepared_feed is None else prepared_feed.phase.value,
        "book_metrics": book_metrics,
        "shutdown": "graceful",
        "quote_source": "MarketStateStore→BookView",
        "feed_notes": list(feed_notes),
    }
    if feed_notes:
        summary.gate_notes.extend(feed_notes[-10:])
    ended_raw = datetime.now(timezone.utc)
    ended_authoritative = auth.now_corrected_utc()
    ended_mono_ns = time.monotonic_ns()
    summary.ended_at = ended_raw.isoformat()
    summary.duration["ended_at_raw"] = ended_raw.isoformat()
    summary.duration["ended_at_authoritative"] = ended_authoritative.isoformat()
    summary.duration["ended_monotonic_ns"] = ended_mono_ns
    summary.duration["trading_elapsed_s"] = _trading_elapsed_s(
        now=ended_authoritative,
        compose_started=compose_started_raw,
        target_window_start=target_window_start,
        anchor=duration_anchor,
    )
    summary.duration["preparation_elapsed_s"] = max(
        0.0,
        (ended_mono_ns - compose_started_mono_ns) / 1e9,
    )
    summary.duration["total_runtime_monotonic_s"] = max(
        0.0,
        (ended_mono_ns - run_started_mono_ns) / 1e9,
    )
    _write_summary(out_dir, summary)
    return summary


def _write_summary(out_dir: Path, summary: MarketDataSummary) -> None:
    path = out_dir / "summary.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(summary.to_dict(), indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def seconds_until_next_boundary(now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    epoch = int(now.timestamp())
    nxt = (epoch // 300 + 1) * 300
    return float(nxt - epoch)
