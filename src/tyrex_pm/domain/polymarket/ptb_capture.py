"""Deterministic PTB capture lifecycle (N3A).

Lifecycle phases (derived view; orthogonal quality/lock/readiness preserved):

  WAITING_BOUNDARY → CANDIDATE_CAPTURED → ATTESTED → SEALED
                                         ↘ DEGRADED / FAILED

Invariants:
- Candidate selection is rule-labelled (prefer EXACT_AT_START provisionally).
- Sealed PTB is immutable; late ticks after seal are evidence only.
- Duplicate identical delivery is idempotent.
- Conflicting duplicates degrade/fail safely without rewriting sealed K.
- Restart/replay from append-only evidence is deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Sequence

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import MarketId
from tyrex_pm.domain.polymarket.boundary_candidates import (
    BoundaryCandidate,
    BoundaryCandidateSet,
    BoundaryRuleId,
    BoundaryTickView,
    PROVISIONAL_PREFERRED_CLASSIFICATION,
    PROVISIONAL_PREFERRED_RULE,
    evaluate_boundary_candidates,
)
from tyrex_pm.domain.polymarket.ptb import (
    PtbLockStore,
    PtbQuality,
    PtbSnapshot,
    PtbSourceClass,
)
from tyrex_pm.domain.polymarket.ptb_attestation import (
    AttestationClassification,
    AttestationResult,
    PtbAttestationPort,
    PtbAttestationRecord,
    compare_attestation,
)
from tyrex_pm.domain.polymarket.reference_blockers import ReferenceBlockerReason
from tyrex_pm.domain.polymarket.sealed_reference import SealedReferenceInput
from tyrex_pm.indicators.causal_pairing import (
    PAIRING_POLICY_ID,
    PriceTickView,
    TradingReferenceIdentity,
    select_latest_binance_at_or_before,
)
from tyrex_pm.indicators.reference_alignment import (
    AlignedReferenceSnapshot,
    BasisEwmaState,
    build_aligned_reference,
)


class PtbLifecyclePhase(str, Enum):
    WAITING_BOUNDARY = "WAITING_BOUNDARY"
    CANDIDATE_CAPTURED = "CANDIDATE_CAPTURED"
    ATTESTED = "ATTESTED"
    SEALED = "SEALED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


@dataclass(frozen=True, kw_only=True)
class IngressEvidenceRow:
    """Append-only settlement evidence for replay."""

    sequence: int
    tick: BoundaryTickView
    connection_generation: int | None
    noted: str | None = None


@dataclass
class WindowPtbState:
    market_id: MarketId
    window_id: str
    event_start: datetime
    event_end: datetime
    phase: PtbLifecyclePhase = PtbLifecyclePhase.WAITING_BOUNDARY
    evidence: list[IngressEvidenceRow] = field(default_factory=list)
    candidate_set: BoundaryCandidateSet | None = None
    selected_candidate: BoundaryCandidate | None = None
    attestation: PtbAttestationRecord | None = None
    sealed: SealedReferenceInput | None = None
    ptb_snapshot: PtbSnapshot | None = None
    blockers: list[str] = field(default_factory=list)
    conflict_evidence: list[dict[str, Any]] = field(default_factory=list)
    late_after_seal: list[IngressEvidenceRow] = field(default_factory=list)
    last_connection_generation: int | None = None
    _seen_fingerprints: set[str] = field(default_factory=set)


@dataclass
class PtbCaptureEngine:
    """Per-window PTB capture with continuous cross-window alignment state."""

    lock_store: PtbLockStore = field(default_factory=PtbLockStore)
    attestation_port: PtbAttestationPort | None = None
    ewma: BasisEwmaState = field(default_factory=lambda: BasisEwmaState(half_life_s=None))
    primary_trading_identity: TradingReferenceIdentity = (
        TradingReferenceIdentity.BINANCE_SPOT
    )
    max_skew_ms: int | None = None  # OPEN when None
    _windows: dict[tuple[str, str], WindowPtbState] = field(default_factory=dict)
    _evidence_seq: int = 0
    _binance_history: list[PriceTickView] = field(default_factory=list)
    latest_alignment: AlignedReferenceSnapshot | None = None

    def _key(self, market_id: MarketId, window_id: str) -> tuple[str, str]:
        return (market_id.value, window_id)

    def get_window(
        self, market_id: MarketId, window_id: str
    ) -> WindowPtbState | None:
        return self._windows.get(self._key(market_id, window_id))

    def open_window(
        self,
        *,
        market_id: MarketId,
        window_id: str,
        event_start: datetime,
        event_end: datetime,
    ) -> WindowPtbState:
        key = self._key(market_id, window_id)
        existing = self._windows.get(key)
        if existing is not None:
            return existing
        state = WindowPtbState(
            market_id=market_id,
            window_id=window_id,
            event_start=require_utc(event_start, field_name="event_start"),
            event_end=require_utc(event_end, field_name="event_end"),
        )
        self._windows[key] = state
        return state

    def ingest_binance(self, tick: PriceTickView) -> None:
        """Append trading-reference history (does not reset on window change)."""
        if tick.identity is not self.primary_trading_identity:
            # Distinct identity retained but not used as primary without explicit switch.
            return
        self._binance_history.append(tick)
        # Bound memory for tests/process — keep chronological tail.
        if len(self._binance_history) > 50_000:
            self._binance_history = self._binance_history[-25_000:]

    def ingest_chainlink(
        self,
        *,
        market_id: MarketId,
        window_id: str,
        tick: BoundaryTickView,
    ) -> WindowPtbState:
        state = self._windows.get(self._key(market_id, window_id))
        if state is None:
            raise KeyError(f"window not open: {window_id}")

        self._evidence_seq += 1
        gen = None if tick.ingress is None else tick.ingress.connection_generation
        row = IngressEvidenceRow(
            sequence=self._evidence_seq,
            tick=tick,
            connection_generation=gen,
        )

        if (
            state.last_connection_generation is not None
            and gen is not None
            and gen != state.last_connection_generation
        ):
            state.blockers.append(ReferenceBlockerReason.SOURCE_RECONNECT_GAP.value)
            if state.phase not in {
                PtbLifecyclePhase.SEALED,
                PtbLifecyclePhase.FAILED,
            }:
                state.phase = PtbLifecyclePhase.DEGRADED
        if gen is not None:
            state.last_connection_generation = gen

        fp = tick.raw_fingerprint or (
            None if tick.ingress is None else tick.ingress.raw_fingerprint
        )
        if fp and fp in state._seen_fingerprints:
            # Idempotent duplicate — re-append with note but do not change sealed K.
            row = IngressEvidenceRow(
                sequence=self._evidence_seq,
                tick=tick,
                connection_generation=gen,
                noted="duplicate_idempotent",
            )
            state.evidence.append(row)
            return state
        if fp:
            # Conflict check: same sequence/role different value already sealed
            state._seen_fingerprints.add(fp)

        if state.phase is PtbLifecyclePhase.SEALED and state.sealed is not None:
            state.late_after_seal.append(row)
            state.evidence.append(row)
            state.blockers.append(ReferenceBlockerReason.LATE_EVENT_AFTER_SEAL.value)
            # Conflict if exact boundary tick disagrees with sealed K
            if (
                tick.source_ts == state.event_start
                and tick.value != state.sealed.ptb_k
            ):
                state.conflict_evidence.append(
                    {
                        "kind": "late_conflict_after_seal",
                        "sealed_k": str(state.sealed.ptb_k),
                        "tick_value": str(tick.value),
                        "source_ts": tick.source_ts.isoformat(),
                    }
                )
                state.blockers.append(
                    ReferenceBlockerReason.CONFLICTING_DUPLICATE.value
                )
                state.phase = PtbLifecyclePhase.DEGRADED
            return state

        state.evidence.append(row)
        self._reevaluate(state)
        return state

    def _ticks(self, state: WindowPtbState) -> list[BoundaryTickView]:
        return [e.tick for e in state.evidence]

    def _reevaluate(self, state: WindowPtbState) -> None:
        if state.phase is PtbLifecyclePhase.SEALED:
            return
        cand_set = evaluate_boundary_candidates(
            market_id=state.market_id,
            window_id=state.window_id,
            event_start=state.event_start,
            event_end=state.event_end,
            ticks=self._ticks(state),
        )
        state.candidate_set = cand_set
        # Merge blockers
        for b in cand_set.blocker_reasons:
            if b not in state.blockers:
                state.blockers.append(b)

        if "conflicting_duplicate" in cand_set.blocker_reasons:
            state.conflict_evidence.append(
                {
                    "kind": "conflicting_duplicate",
                    "candidates": [
                        {
                            "rule": c.rule_id.value,
                            "value": str(c.value),
                            "source_ts": c.chainlink_source_ts.isoformat(),
                        }
                        for c in cand_set.candidates
                    ],
                }
            )
            if ReferenceBlockerReason.CONFLICTING_DUPLICATE.value not in state.blockers:
                state.blockers.append(ReferenceBlockerReason.CONFLICTING_DUPLICATE.value)
            state.phase = PtbLifecyclePhase.FAILED
            return

        selected = cand_set.selected_for_provisional_lock
        if selected is None:
            if state.phase is PtbLifecyclePhase.WAITING_BOUNDARY:
                pass
            return

        # Conflict with prior selected candidate before seal
        if (
            state.selected_candidate is not None
            and state.selected_candidate.value != selected.value
            and state.selected_candidate.rule_id == selected.rule_id
        ):
            state.conflict_evidence.append(
                {
                    "kind": "conflicting_duplicate",
                    "prior": str(state.selected_candidate.value),
                    "new": str(selected.value),
                    "rule": selected.rule_id.value,
                }
            )
            state.blockers.append(ReferenceBlockerReason.CONFLICTING_DUPLICATE.value)
            state.phase = PtbLifecyclePhase.FAILED
            return

        state.selected_candidate = selected
        if state.phase in {
            PtbLifecyclePhase.WAITING_BOUNDARY,
            PtbLifecyclePhase.DEGRADED,
        }:
            state.phase = PtbLifecyclePhase.CANDIDATE_CAPTURED

        clock = selected.clock_status
        if clock == "UNSYNCHRONIZED":
            state.blockers.append(ReferenceBlockerReason.CLOCK_UNSYNCHRONIZED.value)
            state.phase = PtbLifecyclePhase.DEGRADED
        elif clock == "DEGRADED":
            state.blockers.append(ReferenceBlockerReason.CLOCK_DEGRADED.value)
            state.phase = PtbLifecyclePhase.DEGRADED

    def attest(self, *, market_id: MarketId, window_id: str) -> PtbAttestationRecord:
        state = self._require(market_id, window_id)
        if state.selected_candidate is None:
            rec = compare_attestation(
                market_id=market_id,
                window_id=window_id,
                candidate_value=None,
                attested_value=None,
                candidate_rule=None,
                attestation_source="none",
                attestation_provenance={},
            )
            state.attestation = rec
            state.blockers.append(ReferenceBlockerReason.ATTESTATION_UNAVAILABLE.value)
            return rec

        attested_value: Decimal | None = None
        source = "unavailable"
        prov: dict[str, Any] = {}
        available_at: datetime | None = None
        if self.attestation_port is not None:
            attested_value, source, prov, available_at = self.attestation_port.fetch_attestation(
                market_id=market_id,
                window_id=window_id,
                event_start=state.event_start,
            )
        rec = compare_attestation(
            market_id=market_id,
            window_id=window_id,
            candidate_value=state.selected_candidate.value,
            attested_value=attested_value,
            candidate_rule=state.selected_candidate.rule_id,
            attestation_source=source,
            attestation_provenance=prov,
            available_at=available_at,
        )
        state.attestation = rec
        if rec.result is AttestationResult.INCOMPLETE:
            state.blockers.append(ReferenceBlockerReason.ATTESTATION_UNAVAILABLE.value)
            if state.phase is PtbLifecyclePhase.CANDIDATE_CAPTURED:
                state.phase = PtbLifecyclePhase.DEGRADED
        elif rec.result is AttestationResult.MISMATCH:
            state.blockers.append(ReferenceBlockerReason.ATTESTATION_MISMATCH.value)
            state.phase = PtbLifecyclePhase.FAILED
        else:
            if state.phase is PtbLifecyclePhase.CANDIDATE_CAPTURED:
                state.phase = PtbLifecyclePhase.ATTESTED
            elif state.phase is PtbLifecyclePhase.DEGRADED:
                # Keep degraded if other blockers remain
                pass
        return rec

    def seal(
        self,
        *,
        market_id: MarketId,
        window_id: str,
        sealed_at: datetime | None = None,
        require_attestation_match: bool = False,
    ) -> SealedReferenceInput:
        """Explicit seal operation — immutable thereafter."""
        state = self._require(market_id, window_id)
        if state.sealed is not None:
            return state.sealed
        if state.selected_candidate is None:
            raise ValueError("cannot seal without selected EXACT candidate")
        if state.phase is PtbLifecyclePhase.FAILED:
            raise ValueError("cannot seal FAILED window")

        if require_attestation_match:
            if state.attestation is None:
                self.attest(market_id=market_id, window_id=window_id)
            if (
                state.attestation is None
                or state.attestation.result is not AttestationResult.MATCH
            ):
                raise ValueError("cannot seal without attestation MATCH")

        cand = state.selected_candidate
        sealed_at = sealed_at or datetime.now(timezone.utc)
        sealed_at = require_utc(sealed_at, field_name="sealed_at")

        # Causal pair for boundary tick as trading ref sample
        cl_view = PriceTickView(
            value=cand.value,
            source_ts=cand.chainlink_source_ts,
            receive_wall_raw_utc=cand.receive_wall_raw_utc,
            receive_wall_corrected_utc=cand.receive_wall_corrected_utc,
            receive_monotonic_ns=cand.receive_monotonic_ns,
            identity=TradingReferenceIdentity.UNKNOWN,
            event_id=cand.event_id,
            raw_fingerprint=cand.raw_fingerprint,
            late_or_out_of_order=cand.late_or_out_of_order,
        )
        pair = select_latest_binance_at_or_before(
            chainlink=cl_view,
            binance_ticks=self._binance_history,
            primary_identity=self.primary_trading_identity,
            max_skew_ms=self.max_skew_ms,
        )
        for b in pair.blocker_reasons:
            if b not in state.blockers:
                state.blockers.append(b)

        alignment: AlignedReferenceSnapshot | None = None
        if pair.paired and pair.binance is not None:
            alignment = build_aligned_reference(
                chainlink=cand.value,
                binance=pair.binance.value,
                pairing_policy_id=PAIRING_POLICY_ID,
                source_skew_ms=pair.source_skew_ms,
                trading_identity=pair.binance.identity.value,
                clock_status=cand.clock_status,
                ewma=self.ewma,
                source_ts=cand.chainlink_source_ts,
                extra_blockers=tuple(pair.blocker_reasons),
            )
            self.latest_alignment = alignment
            for b in alignment.blocker_reasons:
                if b not in state.blockers:
                    state.blockers.append(b)

        att = state.attestation
        att_result = (
            AttestationResult.INCOMPLETE if att is None else att.result
        )
        att_class = (
            AttestationClassification.OPEN if att is None else att.classification
        )

        readiness_ready = (
            cand.rule_id is BoundaryRuleId.EXACT_AT_START
            and state.phase
            not in {PtbLifecyclePhase.FAILED, PtbLifecyclePhase.DEGRADED}
            and ReferenceBlockerReason.ATTESTATION_MISMATCH.value not in state.blockers
        )
        # Degraded clocks / missing attestation still allow seal of K for audit,
        # but readiness_ready stays false.
        if cand.clock_status in {"UNSYNCHRONIZED", "DEGRADED"}:
            readiness_ready = False
        if att_result is not AttestationResult.MATCH:
            readiness_ready = False
        if pair.paired is False:
            readiness_ready = False

        evidence_ids = [
            e.tick.event_id or e.tick.raw_fingerprint or f"seq:{e.sequence}"
            for e in state.evidence
        ]

        sealed = SealedReferenceInput(
            market_id=market_id,
            window_id=window_id,
            event_start=state.event_start,
            event_end=state.event_end,
            ptb_k=cand.value,
            boundary_rule_id=cand.rule_id,
            boundary_classification=cand.rule_classification,
            ptb_attestation_result=att_result,
            ptb_attestation_classification=att_class,
            trading_reference=None if pair.binance is None else pair.binance.value,
            trading_reference_identity=(
                None if pair.binance is None else pair.binance.identity.value
            ),
            trading_reference_source_ts=(
                None if pair.binance is None else pair.binance.source_ts
            ),
            aligned_chainlink_raw=None if alignment is None else alignment.chainlink_raw,
            aligned_binance_raw=None if alignment is None else alignment.binance_raw,
            basis_ln_instant=None if alignment is None else alignment.basis_ln_instant,
            basis_ln_smoothed=None if alignment is None else alignment.basis_ln_smoothed,
            c_hat=None if alignment is None else alignment.c_hat,
            alignment_init_state=None if alignment is None else alignment.init_state,
            pairing_policy_id=PAIRING_POLICY_ID,
            pairing_source_skew_ms=pair.source_skew_ms,
            chainlink_boundary_source_ts=cand.chainlink_source_ts,
            clock_status=cand.clock_status,
            clock_uncertainty_ms=cand.clock_uncertainty_ms,
            clock_snapshot_id=cand.clock_snapshot_id,
            freshness_ready=pair.paired,
            readiness_ready=readiness_ready,
            blocker_reasons=tuple(dict.fromkeys(state.blockers)),
            sealed_at=sealed_at,
            evidence_ids=tuple(x for x in evidence_ids if x),
            provenance={
                "preferred_rule": PROVISIONAL_PREFERRED_RULE.value,
                "preferred_classification": PROVISIONAL_PREFERRED_CLASSIFICATION.value,
                "lifecycle_phase_at_seal": state.phase.value,
            },
        )

        snap = PtbSnapshot(
            market_id=market_id,
            window_id=window_id,
            event_start=state.event_start,
            event_end=state.event_end,
            k=cand.value,
            source_class=PtbSourceClass.SETTLEMENT_BOUNDARY,
            source_ts=cand.chainlink_source_ts,
            receive_ts=cand.receive_wall_raw_utc,
            boundary_lag_ms=cand.receive_delay_ms,
            quality=(
                PtbQuality.CONFIRMED_CANONICAL
                if att_result is AttestationResult.MATCH
                else PtbQuality.PROVISIONAL
            ),
            locked=False,
            provenance_ref=f"n3:{cand.rule_id.value}",
            readiness_reasons=tuple(sealed.blocker_reasons),
            attestation={
                "result": att_result.value,
                "classification": att_class.value,
            },
        )
        locked = self.lock_store.lock(snap)
        state.ptb_snapshot = locked
        state.sealed = sealed
        state.phase = PtbLifecyclePhase.SEALED
        return sealed

    def update_alignment_on_pair(
        self, *, chainlink: PriceTickView, max_skew_ms: int | None = None
    ) -> AlignedReferenceSnapshot | None:
        """Continuous alignment update (survives window rollover)."""
        pair = select_latest_binance_at_or_before(
            chainlink=chainlink,
            binance_ticks=self._binance_history,
            primary_identity=self.primary_trading_identity,
            max_skew_ms=self.max_skew_ms if max_skew_ms is None else max_skew_ms,
        )
        if not pair.paired or pair.binance is None:
            return None
        snap = build_aligned_reference(
            chainlink=chainlink.value,
            binance=pair.binance.value,
            pairing_policy_id=PAIRING_POLICY_ID,
            source_skew_ms=pair.source_skew_ms,
            trading_identity=pair.binance.identity.value,
            clock_status=None
            if chainlink.ingress is None
            else chainlink.ingress.clock_status,
            ewma=self.ewma,
            source_ts=chainlink.source_ts,
            extra_blockers=tuple(pair.blocker_reasons),
        )
        self.latest_alignment = snap
        return snap

    def replay_from_evidence(
        self,
        *,
        market_id: MarketId,
        window_id: str,
        event_start: datetime,
        event_end: datetime,
        chainlink_ticks: Sequence[BoundaryTickView],
        binance_ticks: Sequence[PriceTickView] = (),
        seal: bool = True,
        sealed_at: datetime | None = None,
        require_attestation_match: bool = False,
    ) -> WindowPtbState:
        """Deterministic rebuild from ordered evidence."""
        # Fresh engine window — caller should use a fresh engine for full restart proof.
        self.open_window(
            market_id=market_id,
            window_id=window_id,
            event_start=event_start,
            event_end=event_end,
        )
        for b in binance_ticks:
            self.ingest_binance(b)
        for t in chainlink_ticks:
            self.ingest_chainlink(market_id=market_id, window_id=window_id, tick=t)
        if self.attestation_port is not None:
            self.attest(market_id=market_id, window_id=window_id)
        if seal and self.get_window(market_id, window_id).selected_candidate is not None:
            try:
                self.seal(
                    market_id=market_id,
                    window_id=window_id,
                    sealed_at=sealed_at,
                    require_attestation_match=require_attestation_match,
                )
            except ValueError:
                pass
        return self._require(market_id, window_id)

    def _require(self, market_id: MarketId, window_id: str) -> WindowPtbState:
        state = self.get_window(market_id, window_id)
        if state is None:
            raise KeyError(f"window not open: {window_id}")
        return state
