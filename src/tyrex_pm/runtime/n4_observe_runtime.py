"""N4A offline composition: sealed PTB + dynamic alignment → Z-Gap OBSERVE.

No OMS, no orders, no Portfolio mutation. Strategy modules receive immutable
value objects only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from tyrex_pm.core.clock import Clock, FakeClock, require_utc
from tyrex_pm.core.ids import MarketId, new_correlation_id, new_event_id, new_run_id
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.core.snapshots import ReferencePriceSnapshot
from tyrex_pm.core.time_authority import (
    ClockTimeAuthority,
    FakeTimeAuthority,
    TimeAuthority,
    TimeSyncStatus,
)
from tyrex_pm.domain.polymarket.boundary_candidates import BoundaryTickView
from tyrex_pm.domain.polymarket.discovery_binding import DiscoveredMarketBinding
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.domain.polymarket.ptb import PtbQuality, PtbSnapshot, PtbSourceClass
from tyrex_pm.domain.polymarket.ptb_attestation import (
    AttestationResult,
    PtbAttestationPort,
)
from tyrex_pm.domain.polymarket.ptb_capture import PtbCaptureEngine
from tyrex_pm.domain.polymarket.sealed_reference import (
    DynamicAlignedReference,
    SealedWindowPtb,
)
from tyrex_pm.indicators.causal_pairing import (
    PAIRING_POLICY_ID,
    PriceTickView,
    TradingReferenceIdentity,
    select_latest_binance_at_or_before,
)
from tyrex_pm.indicators.reference_alignment import (
    AcceptedBasisEstimate,
    AlignmentMode,
    BasisEwmaState,
    evaluate_dynamic_alignment,
)
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import ExecutableQuote
from tyrex_pm.market_data.freshness import (
    FreshnessAssessment,
    FreshnessReason,
    TimestampBasis,
)
from tyrex_pm.runtime.strategy_binding import ZGapBinding, build_strategy_binding
from tyrex_pm.strategies.context import DecisionContext
from tyrex_pm.strategies.z_gap.config import ZGapConfig


class SessionSlot(str, Enum):
    ACTIVE = "active"
    PREPARED_NEXT = "prepared_next"


@dataclass
class MarketSession:
    """One market/window session (active or prepared-next)."""

    slot: SessionSlot
    market_id: MarketId
    window_id: str
    event_start: datetime
    event_end: datetime
    market: BinaryMarket
    binding: DiscoveredMarketBinding | None = None
    publish_as_active: bool = False
    sealed: SealedWindowPtb | None = None
    strategy_state_token: str = ""
    up_ask: Decimal | None = None
    up_bid: Decimal | None = None
    down_ask: Decimal | None = None
    down_bid: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.strategy_state_token:
            self.strategy_state_token = f"{self.slot.value}:{self.window_id}"


@dataclass(frozen=True, kw_only=True)
class AlignedEvalReady:
    """Immutable N4 evaluation input shared by OBSERVE and SHADOW (parity)."""

    ok: bool
    skip_reasons: tuple[str, ...]
    session: MarketSession | None
    sealed: SealedWindowPtb | None
    dyn: DynamicAlignedReference | None
    snapshot: DecisionSnapshot | None
    binding: ZGapBinding | None
    binance_raw: Decimal | None
    binance_source_ts: datetime | None
    model_spot: Decimal | None
    model_anchor: Decimal | None


@dataclass(frozen=True, kw_only=True)
class ObserveRecord:
    """One OBSERVE evaluation or skip — replayable fact payload."""

    kind: str  # evaluated | skipped
    window_id: str
    market_id: str
    evaluated_at: datetime
    k: str | None
    boundary_rule: str | None
    attestation_result: str | None
    attestation_classification: str | None
    chainlink_raw: str | None
    chainlink_source_ts: str | None
    binance_raw: str | None
    binance_source_ts: str | None
    pairing_policy_id: str | None
    pairing_skew_ms: int | None
    instantaneous_basis_ln: str | None
    basis_estimate_used_ln: str | None
    basis_estimate_as_of_ts: str | None
    basis_includes_current_chainlink: bool | None
    alignment_mode: str | None
    c_hat: str | None
    tau_s: float | None
    p_up: str | None
    p_down: str | None
    basis_bps_gate: str | None
    edge_summary: str | None
    decision: str | None
    skip_reasons: tuple[str, ...]
    config_fingerprint: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "window_id": self.window_id,
            "market_id": self.market_id,
            "evaluated_at": self.evaluated_at.isoformat(),
            "k": self.k,
            "boundary_rule": self.boundary_rule,
            "attestation_result": self.attestation_result,
            "attestation_classification": self.attestation_classification,
            "chainlink_raw": self.chainlink_raw,
            "chainlink_source_ts": self.chainlink_source_ts,
            "binance_raw": self.binance_raw,
            "binance_source_ts": self.binance_source_ts,
            "pairing_policy_id": self.pairing_policy_id,
            "pairing_skew_ms": self.pairing_skew_ms,
            "instantaneous_basis_ln": self.instantaneous_basis_ln,
            "basis_estimate_used_ln": self.basis_estimate_used_ln,
            "basis_estimate_as_of_ts": self.basis_estimate_as_of_ts,
            "basis_includes_current_chainlink": self.basis_includes_current_chainlink,
            "alignment_mode": self.alignment_mode,
            "c_hat": self.c_hat,
            "tau_s": self.tau_s,
            "p_up": self.p_up,
            "p_down": self.p_down,
            "basis_bps_gate": self.basis_bps_gate,
            "edge_summary": self.edge_summary,
            "decision": self.decision,
            "skip_reasons": list(self.skip_reasons),
            "config_fingerprint": self.config_fingerprint,
            "payload": dict(self.payload),
        }


def _fresh(ok: bool = True) -> FreshnessAssessment:
    now = datetime.now(timezone.utc)
    return FreshnessAssessment(
        is_fresh=ok,
        age_ms=0 if ok else 10_000,
        threshold_ms=5_000,
        timestamp_basis=TimestampBasis.EVENT_TIME,
        reason_code=FreshnessReason.FRESH if ok else FreshnessReason.STALE,
        observed_at=now,
    )


def _quote(ask: Decimal | None, bid: Decimal | None) -> ExecutableQuote:
    mid = None
    spread = None
    if ask is not None and bid is not None:
        mid = (ask + bid) / Decimal("2")
        spread = ask - bid
    return ExecutableQuote(
        best_ask=ask,
        best_bid=bid,
        mid=mid,
        spread=spread,
        ask_size_at_touch=Decimal("100") if ask is not None else Decimal("0"),
        bid_size_at_touch=Decimal("100") if bid is not None else Decimal("0"),
    )


def config_fingerprint(config: ZGapConfig) -> str:
    raw = json.dumps(
        {
            "half_life_s": config.volatility.half_life_s,
            "basis_max_bps": str(config.ptb_time_quality.basis_max_bps),
            "theta_take": str(config.entry.theta_take),
            "z_min": str(config.entry.z_min),
            "z_max": str(config.entry.z_max),
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class N4ObserveRuntime:
    """OBSERVE composition over N2/N3 contracts (fixture FakeClock or live SystemClock)."""

    clock: Clock
    ptb_engine: PtbCaptureEngine
    zgap: ZGapBinding
    accepted_basis: AcceptedBasisEstimate = field(default_factory=AcceptedBasisEstimate)
    active: MarketSession | None = None
    prepared_next: MarketSession | None = None
    observations: list[ObserveRecord] = field(default_factory=list)
    oms_touched: bool = False
    portfolio_touched: bool = False
    orders_submitted: int = 0
    _latest_binance: PriceTickView | None = None
    _latest_chainlink: PriceTickView | None = None
    _strategy_by_window: dict[str, ZGapBinding] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        clock: Clock | None = None,
        attestation_port: PtbAttestationPort | None = None,
        basis_ewma_half_life_s: float | None = None,
        zgap_config: ZGapConfig | None = None,
        time_authority: TimeAuthority | None = None,
    ) -> "N4ObserveRuntime":
        clock = clock or FakeClock(
            _wall=datetime(2026, 7, 20, 21, 15, 0, tzinfo=timezone.utc)
        )
        if time_authority is not None:
            auth = time_authority
        elif isinstance(clock, FakeClock):
            auth = FakeTimeAuthority(
                clock=clock, sync_status=TimeSyncStatus.READY, uncertainty_ms=50
            )
        else:
            # Live wall clock: READY with loose uncertainty for validation runs.
            # Production uncertainty limits remain OPEN.
            auth = ClockTimeAuthority(
                clock=clock,
                sync_status=TimeSyncStatus.READY,
                uncertainty_ms=50,
                max_uncertainty_ms=10_000,
            )
        engine = PtbCaptureEngine(
            attestation_port=attestation_port,
            ewma=BasisEwmaState(half_life_s=basis_ewma_half_life_s),
        )
        binding = build_strategy_binding(
            strategy_kind="z_gap",
            zgap_config=zgap_config or ZGapConfig(),
            time_authority=auth,
            clock=clock,
        )
        assert isinstance(binding, ZGapBinding)
        return cls(clock=clock, ptb_engine=engine, zgap=binding)

    def open_session(
        self,
        *,
        slot: SessionSlot,
        market: BinaryMarket,
        window_id: str,
        binding: DiscoveredMarketBinding | None = None,
        publish_as_active: bool | None = None,
    ) -> MarketSession:
        if market.event_start is None or market.event_end is None:
            raise ValueError("market requires event_start/event_end")
        mid = market.market_id
        pub = (
            publish_as_active
            if publish_as_active is not None
            else slot is SessionSlot.ACTIVE
        )
        sess = MarketSession(
            slot=slot,
            market_id=mid,
            window_id=window_id,
            event_start=market.event_start,
            event_end=market.event_end,
            market=market,
            binding=binding,
            publish_as_active=pub,
        )
        self.ptb_engine.open_window(
            market_id=mid,
            window_id=window_id,
            event_start=market.event_start,
            event_end=market.event_end,
        )
        auth = self.zgap.time_authority
        child = build_strategy_binding(
            strategy_kind="z_gap",
            zgap_config=self.zgap.config,
            time_authority=auth,
            clock=self.clock,
            window_id=window_id,
            target_notional=self.zgap.target_notional,
            fee_curve=self.zgap.fee_curve,
            fee_resolved=self.zgap.fee_resolved,
        )
        assert isinstance(child, ZGapBinding)
        self._strategy_by_window[window_id] = child
        if slot is SessionSlot.ACTIVE:
            self.active = sess
        else:
            self.prepared_next = sess
            sess.publish_as_active = False
        return sess

    def ingest_binance(self, tick: PriceTickView) -> DynamicAlignedReference:
        self.ptb_engine.ingest_binance(tick)
        self._latest_binance = tick
        return evaluate_dynamic_alignment(
            current_binance=tick.value,
            binance_source_ts=tick.source_ts,
            trading_identity=tick.identity.value,
            pairing_policy_id=PAIRING_POLICY_ID,
            accepted=self.accepted_basis,
            ewma=self.ptb_engine.ewma,
            evaluated_at=self.clock.now_utc(),
            update_accepted_from_current_pair=False,
        )

    def ingest_chainlink(
        self,
        *,
        window_id: str,
        market_id: MarketId,
        tick: BoundaryTickView,
        update_basis_estimate: bool = True,
    ) -> None:
        self.ptb_engine.ingest_chainlink(
            market_id=market_id, window_id=window_id, tick=tick
        )
        cl = PriceTickView(
            value=tick.value,
            source_ts=tick.source_ts,
            receive_wall_raw_utc=tick.receive_wall_raw_utc,
            receive_wall_corrected_utc=tick.receive_wall_corrected_utc,
            receive_monotonic_ns=tick.receive_monotonic_ns,
            identity=TradingReferenceIdentity.UNKNOWN,
            raw_fingerprint=tick.raw_fingerprint,
        )
        self._latest_chainlink = cl
        pair = select_latest_binance_at_or_before(
            chainlink=cl,
            binance_ticks=self.ptb_engine._binance_history,
            primary_identity=TradingReferenceIdentity.BINANCE_SPOT,
        )
        if update_basis_estimate and pair.paired and pair.binance is not None:
            evaluate_dynamic_alignment(
                current_binance=pair.binance.value,
                binance_source_ts=pair.binance.source_ts,
                trading_identity=pair.binance.identity.value,
                pairing_policy_id=PAIRING_POLICY_ID,
                accepted=self.accepted_basis,
                ewma=self.ptb_engine.ewma,
                current_chainlink=cl.value,
                chainlink_source_ts=cl.source_ts,
                pairing_source_skew_ms=pair.source_skew_ms,
                evaluated_at=self.clock.now_utc(),
                update_accepted_from_current_pair=True,
            )

    def attest_and_seal(
        self,
        *,
        market_id: MarketId,
        window_id: str,
        sealed_at: datetime | None = None,
        require_attestation_match: bool = False,
    ) -> SealedWindowPtb:
        self.ptb_engine.attest(market_id=market_id, window_id=window_id)
        sealed = self.ptb_engine.seal(
            market_id=market_id,
            window_id=window_id,
            sealed_at=sealed_at,
            require_attestation_match=require_attestation_match,
        )
        for sess in (self.active, self.prepared_next):
            if sess is not None and sess.window_id == window_id:
                sess.sealed = sealed
        zg = self._strategy_by_window.get(window_id)
        if zg is not None:
            ptb = PtbSnapshot(
                market_id=sealed.market_id,
                window_id=sealed.window_id,
                event_start=sealed.event_start,
                event_end=sealed.event_end,
                k=sealed.ptb_k,
                source_class=PtbSourceClass.SETTLEMENT_BOUNDARY,
                source_ts=sealed.chainlink_boundary_source_ts,
                receive_ts=sealed.sealed_at,
                boundary_lag_ms=0,
                quality=(
                    PtbQuality.CONFIRMED_CANONICAL
                    if sealed.ptb_attestation_result is AttestationResult.MATCH
                    else PtbQuality.PROVISIONAL
                ),
                locked=True,
                provenance_ref=f"n4:{sealed.boundary_rule_id.value}",
                readiness_reasons=sealed.blocker_reasons,
                attestation={
                    "result": sealed.ptb_attestation_result.value,
                    "classification": sealed.ptb_attestation_classification.value,
                },
            )
            zg.configure_fixture_ptb(ptb)
            zg.ptb_store.lock(ptb)
        return sealed

    def promote_prepared_next(self, *, at: datetime | None = None) -> MarketSession:
        if self.prepared_next is None:
            raise ValueError("no prepared_next session")
        _ = require_utc(at or self.clock.now_utc(), field_name="at")
        prev = self.active
        nxt = self.prepared_next
        nxt.slot = SessionSlot.ACTIVE
        nxt.publish_as_active = True
        self.active = nxt
        self.prepared_next = None
        if prev is not None:
            self._strategy_by_window.pop(prev.window_id, None)
        return nxt

    def evaluate_active(
        self,
        *,
        trigger: str = "feed",
        include_current_chainlink: bool = False,
    ) -> ObserveRecord:
        sess = self.active
        if sess is None or not sess.publish_as_active:
            return self._skip(
                window_id="none",
                market_id="none",
                reasons=("no_active_session",),
            )
        return self._evaluate_session(
            sess,
            trigger=trigger,
            include_current_chainlink=include_current_chainlink,
        )

    def prepare_aligned_eval(
        self,
        *,
        include_current_chainlink: bool = False,
        yes_book=None,
        no_book=None,
    ) -> AlignedEvalReady:
        """Build the immutable N4 evaluation input (S=C_hat, K=sealed) without deciding.

        Shared by OBSERVE and SHADOW so both consume the same sealed path.
        """
        sess = self.active
        if sess is None or not sess.publish_as_active:
            return AlignedEvalReady(
                ok=False,
                skip_reasons=("no_active_session",),
                session=None,
                sealed=None,
                dyn=None,
                snapshot=None,
                binding=None,
                binance_raw=None,
                binance_source_ts=None,
                model_spot=None,
                model_anchor=None,
            )
        now = self.zgap.time_authority.now_corrected_utc()
        reasons: list[str] = []
        if sess.sealed is None:
            reasons.append("exact_candidate_absent")
            st = self.ptb_engine.get_window(sess.market_id, sess.window_id)
            if st is not None:
                reasons.extend(st.blockers)
            return AlignedEvalReady(
                ok=False,
                skip_reasons=tuple(dict.fromkeys(reasons)),
                session=sess,
                sealed=None,
                dyn=None,
                snapshot=None,
                binding=None,
                binance_raw=None,
                binance_source_ts=None,
                model_spot=None,
                model_anchor=None,
            )

        sealed = sess.sealed
        if sealed.ptb_attestation_result is AttestationResult.MISMATCH:
            reasons.append("attestation_mismatch")
        if sealed.ptb_attestation_result is AttestationResult.INCOMPLETE:
            reasons.append("attestation_unavailable")
        if sealed.clock_status == "UNSYNCHRONIZED":
            reasons.append("unsynchronized_clock")
        if sealed.clock_status == "DEGRADED":
            reasons.append("degraded_clock")

        if self._latest_binance is None:
            reasons.append("stale_reference")
            return AlignedEvalReady(
                ok=False,
                skip_reasons=tuple(dict.fromkeys(reasons)),
                session=sess,
                sealed=sealed,
                dyn=None,
                snapshot=None,
                binding=None,
                binance_raw=None,
                binance_source_ts=None,
                model_spot=None,
                model_anchor=None,
            )

        bn = self._latest_binance
        if bn.source_ts > now:
            reasons.append("future_binance_rejected")

        if include_current_chainlink and self._latest_chainlink is not None:
            if self._latest_chainlink.source_ts <= now:
                dyn = evaluate_dynamic_alignment(
                    current_binance=bn.value,
                    binance_source_ts=bn.source_ts,
                    trading_identity=bn.identity.value,
                    pairing_policy_id=PAIRING_POLICY_ID,
                    accepted=self.accepted_basis,
                    ewma=self.ptb_engine.ewma,
                    current_chainlink=self._latest_chainlink.value,
                    chainlink_source_ts=self._latest_chainlink.source_ts,
                    evaluated_at=now,
                    update_accepted_from_current_pair=False,
                )
            else:
                reasons.append("future_chainlink_rejected")
                dyn = evaluate_dynamic_alignment(
                    current_binance=bn.value,
                    binance_source_ts=bn.source_ts,
                    trading_identity=bn.identity.value,
                    pairing_policy_id=PAIRING_POLICY_ID,
                    accepted=self.accepted_basis,
                    ewma=self.ptb_engine.ewma,
                    evaluated_at=now,
                )
        else:
            dyn = evaluate_dynamic_alignment(
                current_binance=bn.value,
                binance_source_ts=bn.source_ts,
                trading_identity=bn.identity.value,
                pairing_policy_id=PAIRING_POLICY_ID,
                accepted=self.accepted_basis,
                ewma=self.ptb_engine.ewma,
                evaluated_at=now,
            )

        reasons.extend(list(dyn.blocker_reasons))
        if dyn.alignment_mode is AlignmentMode.UNAVAILABLE or dyn.c_hat is None:
            reasons.append("basis_estimate_unavailable")
        reasons = list(dict.fromkeys(reasons))

        hard_skip = set(reasons) & {
            "attestation_mismatch",
            "attestation_unavailable",
            "exact_candidate_absent",
            "unsynchronized_clock",
            "no_causal_binance_pair",
            "future_binance_rejected",
            "stale_reference",
            "basis_estimate_unavailable",
        }
        zg = self._strategy_by_window.get(sess.window_id)
        if hard_skip or dyn.c_hat is None or zg is None:
            return AlignedEvalReady(
                ok=False,
                skip_reasons=tuple(reasons),
                session=sess,
                sealed=sealed,
                dyn=dyn,
                snapshot=None,
                binding=zg,
                binance_raw=bn.value,
                binance_source_ts=bn.source_ts,
                model_spot=None,
                model_anchor=sealed.ptb_k,
            )

        model_spot = dyn.c_hat
        ref = ReferencePriceSnapshot(
            symbol="BTCUSD_ALIGNED",
            price=model_spot,
            ts_event=bn.source_ts,
            venue="aligned_estimate",
        )
        snap = DecisionSnapshot(
            market=sess.market,
            yes_book=yes_book,
            no_book=no_book,
            yes_quote=_quote(sess.up_ask, sess.up_bid),
            no_quote=_quote(sess.down_ask, sess.down_bid),
            reference=ref,
            yes_freshness=_fresh(True),
            no_freshness=_fresh(True),
            reference_freshness=_fresh(True),
            observed_at=now,
            correlation_id=new_correlation_id(),
            causation_id=new_event_id(),
        )
        return AlignedEvalReady(
            ok=True,
            skip_reasons=tuple(reasons),
            session=sess,
            sealed=sealed,
            dyn=dyn,
            snapshot=snap,
            binding=zg,
            binance_raw=bn.value,
            binance_source_ts=bn.source_ts,
            model_spot=model_spot,
            model_anchor=sealed.ptb_k,
        )

    def _evaluate_session(
        self,
        sess: MarketSession,
        *,
        trigger: str,
        include_current_chainlink: bool,
    ) -> ObserveRecord:
        ready = self.prepare_aligned_eval(
            include_current_chainlink=include_current_chainlink
        )
        if not ready.ok:
            return self._skip(
                window_id=sess.window_id,
                market_id=sess.market_id.value,
                reasons=ready.skip_reasons,
                sealed=ready.sealed,
                dyn=ready.dyn,
            )

        assert ready.snapshot is not None
        assert ready.binding is not None
        assert ready.dyn is not None
        assert ready.sealed is not None
        assert ready.binance_raw is not None
        assert ready.binance_source_ts is not None
        assert ready.model_spot is not None

        zg = ready.binding
        snap = ready.snapshot
        dyn = ready.dyn
        sealed = ready.sealed
        bn_value = ready.binance_raw
        bn_ts = ready.binance_source_ts
        model_spot = ready.model_spot
        now = snap.observed_at
        reasons = list(ready.skip_reasons)

        ctx = DecisionContext(
            run_id=new_run_id(),
            mode=RuntimeMode.OBSERVE,
            snapshot=snap,
            target_notional=zg.target_notional,
            now=now,
        )
        # Residual alignment gate: C_hat vs latest accepted Chainlink observation.
        settlement = dyn.chainlink_raw
        result = zg.evaluate(
            market_snapshot=snap,
            causation_id=snap.causation_id,
            correlation_id=snap.correlation_id,
            trigger=trigger,
            decision_context=ctx,
            momentum_value=None,
            momentum_ready=False,
            momentum_reason="n4_unused",
            momentum_threshold=Decimal("0"),
            max_book_spread=Decimal("1"),
            settlement_ref=settlement,
            settlement_ref_fresh=settlement is not None,
            volatility_price=bn_value,
            volatility_ts=bn_ts,
        )

        p_up = p_down = basis_bps = None
        tau = None
        decision = "no_intent"
        for fact_type, payload in result.extra_facts:
            if fact_type == "zgap_model_snapshot":
                p_up = None if payload.get("p_up") is None else str(payload["p_up"])
                p_down = None if payload.get("p_down") is None else str(payload["p_down"])
                basis_bps = (
                    None
                    if payload.get("basis_bps") is None
                    else str(payload["basis_bps"])
                )
                tau = payload.get("tau_s")
                rejects = payload.get("reject_reasons") or []
                if rejects and not result.intents:
                    decision = f"skip:{','.join(map(str, rejects))}"
        if result.intents:
            decision = ",".join(type(i).__name__ for i in result.intents)
        # OBSERVE composition never mutates OMS/Portfolio
        assert not self.oms_touched
        assert self.orders_submitted == 0

        rec = ObserveRecord(
            kind="evaluated",
            window_id=sess.window_id,
            market_id=sess.market_id.value,
            evaluated_at=now,
            k=str(sealed.ptb_k),
            boundary_rule=sealed.boundary_rule_id.value,
            attestation_result=sealed.ptb_attestation_result.value,
            attestation_classification=sealed.ptb_attestation_classification.value,
            chainlink_raw=None if dyn.chainlink_raw is None else str(dyn.chainlink_raw),
            chainlink_source_ts=(
                None
                if dyn.chainlink_source_ts is None
                else dyn.chainlink_source_ts.isoformat()
            ),
            binance_raw=str(dyn.binance_raw),
            binance_source_ts=dyn.binance_source_ts.isoformat(),
            pairing_policy_id=dyn.pairing_policy_id,
            pairing_skew_ms=dyn.pairing_source_skew_ms,
            instantaneous_basis_ln=(
                None
                if dyn.instantaneous_basis_ln is None
                else str(dyn.instantaneous_basis_ln)
            ),
            basis_estimate_used_ln=(
                None
                if dyn.basis_estimate_used_ln is None
                else str(dyn.basis_estimate_used_ln)
            ),
            basis_estimate_as_of_ts=(
                None
                if dyn.basis_estimate_as_of_ts is None
                else dyn.basis_estimate_as_of_ts.isoformat()
            ),
            basis_includes_current_chainlink=dyn.basis_estimate_includes_current_chainlink,
            alignment_mode=dyn.alignment_mode.value,
            c_hat=None if dyn.c_hat is None else str(dyn.c_hat),
            tau_s=tau,
            p_up=p_up,
            p_down=p_down,
            basis_bps_gate=basis_bps,
            edge_summary=None,
            decision=decision,
            skip_reasons=tuple(reasons),
            config_fingerprint=config_fingerprint(zg.config),
            payload={
                "trigger": trigger,
                "soft_blockers": reasons,
                "oms_touched": self.oms_touched,
                "portfolio_touched": self.portfolio_touched,
                "orders_submitted": self.orders_submitted,
                "model_spot": str(model_spot),
                "model_spot_source": "aligned_c_hat",
                "model_anchor": str(sealed.ptb_k),
                "model_anchor_source": "sealed_chainlink_ptb",
                "sigma_source": "binance_raw_returns",
                "binance_raw_price": str(bn_value),
                "aligned_model_price": str(model_spot),
                "sealed_ptb_k": str(sealed.ptb_k),
                "zgap_S_equals_c_hat": True,
            },
        )
        self.observations.append(rec)
        return rec

    def _skip(
        self,
        *,
        window_id: str,
        market_id: str,
        reasons: tuple[str, ...],
        sealed: SealedWindowPtb | None = None,
        dyn: DynamicAlignedReference | None = None,
    ) -> ObserveRecord:
        now = self.clock.now_utc()
        rec = ObserveRecord(
            kind="skipped",
            window_id=window_id,
            market_id=market_id,
            evaluated_at=now,
            k=None if sealed is None else str(sealed.ptb_k),
            boundary_rule=None if sealed is None else sealed.boundary_rule_id.value,
            attestation_result=(
                None if sealed is None else sealed.ptb_attestation_result.value
            ),
            attestation_classification=(
                None if sealed is None else sealed.ptb_attestation_classification.value
            ),
            chainlink_raw=(
                None if dyn is None or dyn.chainlink_raw is None else str(dyn.chainlink_raw)
            ),
            chainlink_source_ts=(
                None
                if dyn is None or dyn.chainlink_source_ts is None
                else dyn.chainlink_source_ts.isoformat()
            ),
            binance_raw=None if dyn is None else str(dyn.binance_raw),
            binance_source_ts=(
                None if dyn is None else dyn.binance_source_ts.isoformat()
            ),
            pairing_policy_id=None if dyn is None else dyn.pairing_policy_id,
            pairing_skew_ms=None if dyn is None else dyn.pairing_source_skew_ms,
            instantaneous_basis_ln=(
                None
                if dyn is None or dyn.instantaneous_basis_ln is None
                else str(dyn.instantaneous_basis_ln)
            ),
            basis_estimate_used_ln=(
                None
                if dyn is None or dyn.basis_estimate_used_ln is None
                else str(dyn.basis_estimate_used_ln)
            ),
            basis_estimate_as_of_ts=(
                None
                if dyn is None or dyn.basis_estimate_as_of_ts is None
                else dyn.basis_estimate_as_of_ts.isoformat()
            ),
            basis_includes_current_chainlink=(
                None if dyn is None else dyn.basis_estimate_includes_current_chainlink
            ),
            alignment_mode=None if dyn is None else dyn.alignment_mode.value,
            c_hat=None if dyn is None or dyn.c_hat is None else str(dyn.c_hat),
            tau_s=None,
            p_up=None,
            p_down=None,
            basis_bps_gate=None,
            edge_summary=None,
            decision="skipped",
            skip_reasons=reasons,
            config_fingerprint=config_fingerprint(self.zgap.config),
            payload={"oms_touched": False, "orders_submitted": 0},
        )
        self.observations.append(rec)
        return rec
