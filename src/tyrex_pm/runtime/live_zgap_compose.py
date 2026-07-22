"""Shared live public-input composition for N3B seal / N4B OBSERVE / N5B SHADOW.

Read-only feeds only. No LiveOMS, auth, or venue mutation.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Literal

from tyrex_pm.adapters.binance.ws_adapter import BinanceTradeWsAdapter
from tyrex_pm.adapters.clock_sync import ClockSyncLoop, OsMonitorClockSyncProvider
from tyrex_pm.adapters.polymarket.discovery import (
    GammaMarketDiscovery,
    current_btc_updown_slug,
)
from tyrex_pm.adapters.polymarket.rtds_adapter import RtdsChainlinkAdapter
from tyrex_pm.adapters.polymarket.ssr_ptb_attestation import (
    SsrDisplayedPtbAttestationProvider,
)
from tyrex_pm.adapters.polymarket.ws_adapter import PolymarketMarketWsAdapter
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
from tyrex_pm.runtime.n4_observe_runtime import N4ObserveRuntime, SessionSlot
from tyrex_pm.strategies.z_gap.config import ZGapConfig, ZGapPtbTimeQualityConfig

# SSR openPrice often lags the EXACT Chainlink tick by a few seconds.
ATTESTATION_SEAL_GRACE_S = 45.0
# Cap OBSERVE/SHADOW eval frequency (Binance trades are far denser than decisions).
EVAL_MIN_INTERVAL_S = 1.0

ComposeMode = Literal["n3_seal", "n4_observe", "n5_shadow"]


@dataclass
class SealRecord:
    window_id: str
    market_id: str
    sealed_k: str
    boundary_rule: str
    chainlink_source_ts: str
    receive_wall_raw_utc: str
    sealed_at: str
    attestation_result: str
    attestation_source: str
    attested_value: str | None
    attestation_exact_diff: str | None
    attestation_bps_diff: str | None
    blockers: list[str]
    immutable_check_ok: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "market_id": self.market_id,
            "sealed_k": self.sealed_k,
            "boundary_rule": self.boundary_rule,
            "chainlink_source_ts": self.chainlink_source_ts,
            "receive_wall_raw_utc": self.receive_wall_raw_utc,
            "sealed_at": self.sealed_at,
            "attestation_result": self.attestation_result,
            "attestation_source": self.attestation_source,
            "attested_value": self.attested_value,
            "attestation_exact_diff": self.attestation_exact_diff,
            "attestation_bps_diff": self.attestation_bps_diff,
            "blockers": list(self.blockers),
            "immutable_check_ok": self.immutable_check_ok,
        }


@dataclass
class LiveComposeSummary:
    mode: ComposeMode
    run_id: str
    out_dir: str
    started_at: str
    ended_at: str | None = None
    tls_verify: bool = True
    auth_touched: bool = False
    orders_touched: bool = False
    oms_touched: bool = False
    venue_mutation: bool = False
    orders_live: int = 0
    not_live_evidence: bool = False
    feeds: dict[str, Any] = field(default_factory=dict)
    discovery: dict[str, Any] = field(default_factory=dict)
    seals: list[dict[str, Any]] = field(default_factory=list)
    missed_windows: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    shadow_records: list[dict[str, Any]] = field(default_factory=list)
    clock: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    gate_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "run_id": self.run_id,
            "out_dir": self.out_dir,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "tls_verify": self.tls_verify,
            "auth_touched": self.auth_touched,
            "orders_touched": self.orders_touched,
            "oms_touched": self.oms_touched,
            "venue_mutation": self.venue_mutation,
            "orders_live": self.orders_live,
            "not_live_evidence": self.not_live_evidence,
            "feeds": self.feeds,
            "discovery": self.discovery,
            "seals": self.seals,
            "missed_windows": self.missed_windows,
            "observation_count": len(self.observations),
            "observations": self.observations,
            "shadow_record_count": len(self.shadow_records),
            "shadow_records": self.shadow_records,
            "clock": self.clock,
            "errors": self.errors,
            "gate_notes": self.gate_notes,
        }


def _window_epoch(dt: datetime) -> int:
    return int(dt.timestamp()) // 300 * 300


async def run_live_zgap_compose(
    *,
    mode: ComposeMode,
    out_dir: Path,
    run_id: str,
    min_seals: int = 3,
    max_duration_s: float = 1200.0,
    prep_lead_s: float = 45.0,
    basis_ewma_half_life_s: float | None = 30.0,
    on_book: Callable[[BookSnapshot], None] | None = None,
    on_after_seal_eval: Callable[[N4ObserveRuntime], list[dict[str, Any]]] | None = None,
    n5_evaluate: Callable[[], list[dict[str, Any]]] | None = None,
    on_active_session: Callable[[Any], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    stop_when_seals_met: bool = True,
    runtime: N4ObserveRuntime | None = None,
) -> LiveComposeSummary:
    """Bounded live composition: discover → ingest → EXACT seal → optional eval."""
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = LiveComposeSummary(
        mode=mode,
        run_id=run_id,
        out_dir=str(out_dir),
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    summary.gate_notes.append(
        "basis_ewma_half_life_s is fixture/live-validation only; production half-life OPEN"
        if basis_ewma_half_life_s is not None
        else "basis_ewma_half_life_s=None (OPEN)"
    )
    _ = prep_lead_s

    clock = runtime.clock if runtime is not None else SystemClock()
    auth = SnapshotTimeAuthority(clock=clock, max_uncertainty_ms=10_000)
    provider = OsMonitorClockSyncProvider(enable_binance_cross_check=True)
    clock_loop = ClockSyncLoop(provider=provider, apply=auth.apply_snapshot, interval_s=30.0)
    try:
        snap = await provider.measure()
        auth.apply_snapshot(snap)
        view = auth.view()
        summary.clock = {
            "sync_status": view.sync_status.value,
            "uncertainty_ms": view.uncertainty_ms,
            "ready": view.ready,
            "estimated_offset_ms": view.estimated_offset_ms,
        }
    except Exception as exc:
        summary.errors.append(f"clock:{type(exc).__name__}:{exc}")

    # Single-attempt + 2s neg-cache; compose retries across ticks during grace.
    attestation = SsrDisplayedPtbAttestationProvider(retries=1, retry_delay_s=2.0)
    if runtime is None:
        runtime = N4ObserveRuntime.create(
            clock=clock,
            attestation_port=attestation,
            basis_ewma_half_life_s=basis_ewma_half_life_s,
            time_authority=auth,
            zgap_config=ZGapConfig(
                ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
            ),
        )
    else:
        runtime.ptb_engine.attestation_port = attestation
        # Rebind strategy time authority to the shared corrected clock view.
        runtime.zgap.time_authority = auth
        for child in runtime._strategy_by_window.values():
            child.time_authority = auth

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
        summary.ended_at = datetime.now(timezone.utc).isoformat()
        _write_summary(out_dir, summary)
        return summary

    active_sess = runtime.open_session(
        slot=SessionSlot.ACTIVE,
        market=active_binding.market,
        window_id=active_binding.window_slug,
        binding=active_binding,
    )
    if on_active_session is not None:
        on_active_session(active_sess)
    runtime.open_session(
        slot=SessionSlot.PREPARED_NEXT,
        market=prepared.market,
        window_id=prepared.window_slug,
        binding=prepared,
        publish_as_active=False,
    )

    disp = EventDispatcher()
    counts = {"chainlink": 0, "binance": 0, "clob": 0}
    sealed_windows: set[str] = set()
    seal_fail_logged: set[str] = set()
    last_eval_mono: float = 0.0

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
        # Refresh comparison attestation; delay seal while SSR openPrice is unpublished.
        try:
            runtime.ptb_engine.attest(
                market_id=sess.market_id, window_id=window_id
            )
        except Exception as exc:
            if window_id not in seal_fail_logged:
                summary.errors.append(
                    f"attest:{window_id}:{type(exc).__name__}:{exc}"
                )
                seal_fail_logged.add(window_id)
            return
        state = runtime.ptb_engine.get_window(sess.market_id, window_id)
        assert state is not None
        att = state.attestation
        now_c = auth.now_corrected_utc()
        age_s = (now_c - state.event_start).total_seconds()
        if (
            att is None
            or att.result is AttestationResult.INCOMPLETE
        ) and age_s < ATTESTATION_SEAL_GRACE_S:
            return
        try:
            sealed = runtime.attest_and_seal(
                market_id=sess.market_id,
                window_id=window_id,
                sealed_at=now_c,
                require_attestation_match=False,
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
        rec = SealRecord(
            window_id=window_id,
            market_id=sess.market_id.value,
            sealed_k=str(sealed.ptb_k),
            boundary_rule=sealed.boundary_rule_id.value,
            chainlink_source_ts=sealed.chainlink_boundary_source_ts.isoformat(),
            receive_wall_raw_utc=recv_wall,
            sealed_at=sealed.sealed_at.isoformat(),
            attestation_result=sealed.ptb_attestation_result.value,
            attestation_source=(
                "none" if att_rec is None else att_rec.attestation_source
            ),
            attested_value=None
            if att_rec is None or att_rec.attested_value is None
            else str(att_rec.attested_value),
            attestation_exact_diff=None
            if att_rec is None or att_rec.exact_diff is None
            else str(att_rec.exact_diff),
            attestation_bps_diff=None
            if att_rec is None or att_rec.bps_diff is None
            else str(att_rec.bps_diff),
            blockers=list(sealed.blocker_reasons),
            immutable_check_ok=immutable_ok,
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
            receive_wall_corrected_utc=(
                None if meta is None else meta.receive_wall_corrected_utc
            ),
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
        if mono - last_eval_mono < EVAL_MIN_INTERVAL_S:
            return
        if runtime.active is None or runtime.active.sealed is None:
            return
        last_eval_mono = mono
        if mode == "n4_observe" and on_after_seal_eval is not None:
            summary.observations.extend(on_after_seal_eval(runtime))
        if mode == "n5_shadow" and n5_evaluate is not None:
            summary.shadow_records.extend(n5_evaluate())

    def on_book_snap(e: BookSnapshotReceived) -> None:
        counts["clob"] += 1
        _apply_book(e.book)

    def on_book_delta(e: BookDeltaReceived) -> None:
        # Deltas confirm CLOB liveness; quote tops come from snapshots.
        _ = e
        counts["clob"] += 1

    def _apply_book(book: BookSnapshot) -> None:
        active = runtime.active
        if active is None:
            return
        ask = book.asks[0].price if book.asks else None
        bid = book.bids[0].price if book.bids else None
        if book.instrument_id == active.market.yes.instrument_id:
            active.up_ask, active.up_bid = ask, bid
        elif book.instrument_id == active.market.no.instrument_id:
            active.down_ask, active.down_bid = ask, bid
        if on_book is not None:
            on_book(book)

    disp.subscribe(SettlementReferenceUpdated, on_cl)
    disp.subscribe(ReferencePriceUpdated, on_bn)
    disp.subscribe(BookSnapshotReceived, on_book_snap)
    disp.subscribe(BookDeltaReceived, on_book_delta)

    cl = RtdsChainlinkAdapter(time_authority=auth, heartbeat_timeout_s=25)
    bn = BinanceTradeWsAdapter(symbol="BTCUSDT", time_authority=auth)
    clob = PolymarketMarketWsAdapter.from_binding(active_binding)
    tasks = [
        asyncio.create_task(clock_loop.run(), name="clock"),
        asyncio.create_task(cl.run(disp), name="cl"),
        asyncio.create_task(bn.run(disp), name="bn"),
        asyncio.create_task(clob.run(disp), name="clob"),
    ]

    started = datetime.now(timezone.utc)
    try:
        while True:
            await asyncio.sleep(1.0)
            now = auth.now_corrected_utc()
            elapsed = (now - started).total_seconds()
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
                    promoted = runtime.promote_prepared_next(at=now)
                    if on_active_session is not None:
                        on_active_session(promoted)
                    # Refresh CLOB subscription for new active — recreate adapter
                    await clob.stop()
                    if runtime.active and runtime.active.binding is not None:
                        clob = PolymarketMarketWsAdapter.from_binding(
                            runtime.active.binding
                        )
                        tasks.append(asyncio.create_task(clob.run(disp), name="clob2"))
                    # Open next prepared
                    try:
                        nxt = await discovery.prepare_next_btc_5m()
                        if (
                            runtime.prepared_next is None
                            or runtime.prepared_next.window_id != nxt.window_slug
                        ):
                            runtime.open_session(
                                slot=SessionSlot.PREPARED_NEXT,
                                market=nxt.market,
                                window_id=nxt.window_slug,
                                binding=nxt,
                                publish_as_active=False,
                            )
                    except Exception as exc:
                        summary.errors.append(
                            f"prepare_next:{type(exc).__name__}:{exc}"
                        )
                except Exception as exc:
                    summary.errors.append(f"promote:{type(exc).__name__}:{exc}")

            if stop_when_seals_met and len(summary.seals) >= min_seals:
                summary.gate_notes.append(
                    f"stop: reached min_seals={min_seals}"
                )
                break
            if should_stop is not None and should_stop():
                summary.gate_notes.append("stop: caller_requested")
                break
            if elapsed >= max_duration_s:
                summary.gate_notes.append(
                    f"stop: max_duration_s={max_duration_s} elapsed"
                )
                break
    finally:
        await clock_loop.stop()
        await cl.stop()
        await bn.stop()
        await clob.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        view = auth.view()
        summary.clock = {
            "sync_status": view.sync_status.value,
            "uncertainty_ms": view.uncertainty_ms,
            "ready": view.ready,
            "estimated_offset_ms": view.estimated_offset_ms,
            "snapshot_age_ms": view.snapshot_age_ms,
        }

    summary.feeds = {
        "chainlink_ticks": counts["chainlink"],
        "binance_ticks": counts["binance"],
        "clob_book_events": counts["clob"],
        "clob_ready": clob.ready,
        "shutdown": "graceful",
    }
    summary.oms_touched = runtime.oms_touched
    summary.orders_touched = runtime.orders_submitted > 0
    summary.orders_live = 0
    summary.venue_mutation = False
    summary.auth_touched = False
    summary.ended_at = datetime.now(timezone.utc).isoformat()
    _write_summary(out_dir, summary)
    return summary


def _write_summary(out_dir: Path, summary: LiveComposeSummary) -> None:
    path = out_dir / "summary.json"
    path.write_text(json.dumps(summary.to_dict(), indent=2) + "\n", encoding="utf-8")


def seconds_until_next_boundary(now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    epoch = int(now.timestamp())
    nxt = (epoch // 300 + 1) * 300
    return float(nxt - epoch)
