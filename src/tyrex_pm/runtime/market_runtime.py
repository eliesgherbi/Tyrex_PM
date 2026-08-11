"""Z-Gap market state, PTB sealing and immutable decision snapshots.

No OMS, no orders, no Portfolio mutation. Strategy modules receive immutable
value objects only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from tyrex_pm.core.clock import Clock, FakeClock, require_utc
from tyrex_pm.core.ids import MarketId, RunId, new_correlation_id, new_event_id
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
from tyrex_pm.market_data.binding_record import MarketBindingRecord, binding_record_from_discovery
from tyrex_pm.market_data.book_health import SyncHealth
from tyrex_pm.market_data.book_store import MarketStateStore
from tyrex_pm.market_data.book_view import BookView
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import ExecutableQuote, book_quote
from tyrex_pm.market_data.freshness import (
    FreshnessAssessment,
    FreshnessReason,
    TimestampBasis,
)
from tyrex_pm.strategies.context import StrategyContext
from tyrex_pm.strategies.z_gap.config import ZGapConfig
from tyrex_pm.strategies.z_gap.driver import ZGapDriver, create_z_gap_driver


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
    entry_enabled: bool = True
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
    """Immutable aligned strategy-evaluation input."""

    ok: bool
    skip_reasons: tuple[str, ...]
    session: MarketSession | None
    sealed: SealedWindowPtb | None
    dyn: DynamicAlignedReference | None
    snapshot: DecisionSnapshot | None
    binding: ZGapDriver | None
    binance_raw: Decimal | None
    binance_source_ts: datetime | None
    model_spot: Decimal | None
    model_anchor: Decimal | None


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


def _fresh_from_sync(sync: SyncHealth, *, now: datetime) -> FreshnessAssessment:
    ok = sync is SyncHealth.READY
    return FreshnessAssessment(
        is_fresh=ok,
        age_ms=0 if ok else 10_000,
        threshold_ms=5_000,
        timestamp_basis=TimestampBasis.EVENT_TIME,
        reason_code=FreshnessReason.FRESH if ok else FreshnessReason.STALE,
        observed_at=now,
    )


def project_session_quotes_from_view(sess: MarketSession, view: BookView) -> None:
    """Read-only projection of tops onto session fields (never the source of truth)."""
    sess.up_ask = view.up.quote.best_ask
    sess.up_bid = view.up.quote.best_bid
    sess.down_ask = view.down.quote.best_ask
    sess.down_bid = view.down.quote.best_bid


@dataclass
class ZGapMarketRuntime:
    """Strategy-data runtime over normalized public market events."""

    clock: Clock
    ptb_engine: PtbCaptureEngine
    zgap: ZGapDriver
    accepted_basis: AcceptedBasisEstimate = field(default_factory=AcceptedBasisEstimate)
    active: MarketSession | None = None
    prepared_next: MarketSession | None = None
    require_ssr_price_match: bool = True
    # Authoritative quote store for the active market binding.
    book_store: MarketStateStore | None = None
    active_binding_record: MarketBindingRecord | None = None
    _latest_binance: PriceTickView | None = None
    _latest_chainlink: PriceTickView | None = None
    _strategy_by_window: dict[str, ZGapDriver] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        clock: Clock | None = None,
        attestation_port: PtbAttestationPort | None = None,
        basis_ewma_half_life_s: float | None = None,
        zgap_config: ZGapConfig | None = None,
        time_authority: TimeAuthority | None = None,
        require_ssr_price_match: bool = True,
        target_notional: Decimal | None = None,
    ) -> "ZGapMarketRuntime":
        clock = clock or FakeClock(_wall=datetime(2026, 7, 20, 21, 15, 0, tzinfo=timezone.utc))
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
        binding_kwargs: dict[str, Any] = {
            "strategy_kind": "z_gap",
            "zgap_config": zgap_config or ZGapConfig(),
            "time_authority": auth,
            "clock": clock,
        }
        if target_notional is not None:
            binding_kwargs["target_notional"] = target_notional
        binding_kwargs.pop("strategy_kind")
        binding = create_z_gap_driver(config=binding_kwargs.pop("zgap_config"), **binding_kwargs)
        if not isinstance(require_ssr_price_match, bool):
            raise ValueError("require_ssr_price_match must be a boolean")
        return cls(
            clock=clock,
            ptb_engine=engine,
            zgap=binding,
            require_ssr_price_match=require_ssr_price_match,
        )

    def open_session(
        self,
        *,
        slot: SessionSlot,
        market: BinaryMarket,
        window_id: str,
        binding: DiscoveredMarketBinding | None = None,
        publish_as_active: bool | None = None,
        entry_enabled: bool = True,
    ) -> MarketSession:
        if market.event_start is None or market.event_end is None:
            raise ValueError("market requires event_start/event_end")
        mid = market.market_id
        pub = publish_as_active if publish_as_active is not None else slot is SessionSlot.ACTIVE
        sess = MarketSession(
            slot=slot,
            market_id=mid,
            window_id=window_id,
            event_start=market.event_start,
            event_end=market.event_end,
            market=market,
            binding=binding,
            publish_as_active=pub,
            entry_enabled=entry_enabled,
        )
        self.ptb_engine.open_window(
            market_id=mid,
            window_id=window_id,
            event_start=market.event_start,
            event_end=market.event_end,
        )
        auth = self.zgap.time_authority
        child = create_z_gap_driver(
            config=self.zgap.config,
            time_authority=auth,
            clock=self.clock,
            window_id=window_id,
            target_notional=self.zgap.target_notional,
            fee_curve=self.zgap.fee_curve,
        )
        child.on_start(
            StrategyContext(
                run_id=RunId(window_id),
                strategy_id=child.strategy_id,
                market=market,
            )
        )
        history = self.ptb_engine.binance_history
        if history:
            child.seed_volatility(
                [(tick.value, tick.source_ts) for tick in history],
                now_ts=self.zgap.time_authority.now_corrected_utc(),
            )
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
        # Volatility is a continuous reference-data concern, not a side effect of
        # the once-per-second strategy evaluation. Warm ACTIVE and PREPARED_NEXT
        # bindings so promotion does not reset sigma to MODEL_NOT_READY.
        for binding in tuple(self._strategy_by_window.values()):
            binding.ingest_volatility(tick.value, tick.source_ts)
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
        self.ptb_engine.ingest_chainlink(market_id=market_id, window_id=window_id, tick=tick)
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
            binance_ticks=self.ptb_engine.binance_history,
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
        skip_attestation: bool = False,
    ) -> SealedWindowPtb:
        if not skip_attestation:
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
            state = self.ptb_engine.get_window(market_id, window_id)
            if state is None or state.ptb_snapshot is None:
                raise RuntimeError("sealed PTB has no authoritative snapshot")
            zg.configure_ptb(state.ptb_snapshot)
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

    def prepare_aligned_eval(
        self,
        *,
        include_current_chainlink: bool = False,
        yes_book=None,
        no_book=None,
    ) -> AlignedEvalReady:
        """Build one immutable aligned input (S=C_hat, K=sealed) without deciding."""
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
        if self.require_ssr_price_match:
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

        hard_skip_codes = {
            "exact_candidate_absent",
            "unsynchronized_clock",
            "no_causal_binance_pair",
            "future_binance_rejected",
            "stale_reference",
            "basis_estimate_unavailable",
        }
        if self.require_ssr_price_match:
            hard_skip_codes |= {"attestation_mismatch", "attestation_unavailable"}
        hard_skip = set(reasons) & hard_skip_codes
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

        book_view: BookView | None = None
        yes_q = _quote(sess.up_ask, sess.up_bid)
        no_q = _quote(sess.down_ask, sess.down_bid)
        yes_fresh = _fresh(True)
        no_fresh = _fresh(True)
        yes_b = yes_book
        no_b = no_book

        # Authoritative path: MarketStateStore â†’ immutable BookView.
        if self.book_store is not None:
            binding_rec = self.active_binding_record
            if binding_rec is None and sess.binding is not None:
                binding_rec = binding_record_from_discovery(sess.binding)
            if binding_rec is not None:
                book_view = self.book_store.capture_pair(binding_rec)
                project_session_quotes_from_view(sess, book_view)
                yes_q = book_view.up.quote if book_view.up.book is not None else book_quote(None)
                no_q = book_view.down.quote if book_view.down.book is not None else book_quote(None)
                # Prefer store books over caller overrides unless explicitly provided.
                yes_b = yes_book if yes_book is not None else book_view.up.book
                no_b = no_book if no_book is not None else book_view.down.book
                yes_fresh = _fresh_from_sync(book_view.up.sync_health, now=now)
                no_fresh = _fresh_from_sync(book_view.down.sync_health, now=now)

        snap = DecisionSnapshot(
            market=sess.market,
            yes_book=yes_b,
            no_book=no_b,
            yes_quote=yes_q,
            no_quote=no_q,
            reference=ref,
            yes_freshness=yes_fresh,
            no_freshness=no_fresh,
            reference_freshness=_fresh(True),
            observed_at=now,
            correlation_id=new_correlation_id(),
            causation_id=new_event_id(),
            book_view=book_view,
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
