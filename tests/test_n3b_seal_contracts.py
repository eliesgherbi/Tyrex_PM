"""N3B seal contracts: EXACT boundary, immutability, isolation, attestation compare."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tyrex_pm.adapters.polymarket.ssr_ptb_attestation import extract_open_close
from tyrex_pm.core.ids import MarketId
from tyrex_pm.core.ingress import FeedRole, IngressMeta
from tyrex_pm.domain.polymarket.boundary_candidates import (
    BoundaryRuleId,
    BoundaryTickView,
    evaluate_boundary_candidates,
)
from tyrex_pm.domain.polymarket.ptb_attestation import (
    AttestationResult,
    FixturePtbAttestationProvider,
    compare_attestation,
)
from tyrex_pm.domain.polymarket.ptb_capture import PtbCaptureEngine, PtbLifecyclePhase


TS0 = datetime(2026, 7, 21, 20, 0, 0, tzinfo=timezone.utc)
MID = MarketId("m-n3b")
WID = "btc-updown-5m-1784664000"


def _tick(value: str, *, source_ts: datetime, recv: datetime | None = None) -> BoundaryTickView:
    recv = recv or source_ts + timedelta(milliseconds=50)
    meta = IngressMeta(
        receive_monotonic_ns=1,
        ingress_sequence=1,
        connection_generation=1,
        receive_wall_raw_utc=recv,
        receive_wall_corrected_utc=recv,
        clock_offset_ms=0.0,
        clock_uncertainty_ms=50,
        clock_status="READY",
        clock_snapshot_id="t",
        role=FeedRole.SETTLEMENT_REFERENCE,
    )
    return BoundaryTickView(
        value=Decimal(value),
        source_ts=source_ts,
        receive_wall_raw_utc=recv,
        receive_wall_corrected_utc=recv,
        receive_monotonic_ns=1,
        ingress=meta,
        raw_fingerprint=f"{value}-{source_ts.isoformat()}",
    )


def test_exact_selection_clears_stale_absence_blockers() -> None:
    eng = PtbCaptureEngine(
        attestation_port=FixturePtbAttestationProvider(
            by_window={WID: ("100.5", "fixture", {"matched_start": True})}
        )
    )
    eng.open_window(
        market_id=MID, window_id=WID, event_start=TS0, event_end=TS0 + timedelta(minutes=5)
    )
    eng.ingest_chainlink(
        market_id=MID,
        window_id=WID,
        tick=_tick("99", source_ts=TS0 - timedelta(seconds=2)),
    )
    assert "exact_candidate_absent" in eng.get_window(MID, WID).blockers
    eng.ingest_chainlink(
        market_id=MID, window_id=WID, tick=_tick("100.5", source_ts=TS0)
    )
    blockers = eng.get_window(MID, WID).blockers
    assert "exact_candidate_absent" not in blockers
    assert "incomplete_fallback_candidates" not in blockers


def test_exact_boundary_association_and_seal() -> None:
    eng = PtbCaptureEngine(
        attestation_port=FixturePtbAttestationProvider(
            by_window={WID: ("100.5", "fixture", {"matched_start": True})}
        )
    )
    eng.open_window(
        market_id=MID, window_id=WID, event_start=TS0, event_end=TS0 + timedelta(minutes=5)
    )
    # Off-boundary tick must not seal
    eng.ingest_chainlink(
        market_id=MID,
        window_id=WID,
        tick=_tick("99", source_ts=TS0 - timedelta(seconds=1)),
    )
    assert eng.get_window(MID, WID).selected_candidate is None
    # EXACT
    eng.ingest_chainlink(
        market_id=MID, window_id=WID, tick=_tick("100.5", source_ts=TS0)
    )
    assert eng.get_window(MID, WID).selected_candidate is not None
    assert (
        eng.get_window(MID, WID).selected_candidate.rule_id
        is BoundaryRuleId.EXACT_AT_START
    )
    eng.attest(market_id=MID, window_id=WID)
    sealed = eng.seal(market_id=MID, window_id=WID, sealed_at=TS0 + timedelta(seconds=2))
    assert sealed.ptb_k == Decimal("100.5")
    assert sealed.boundary_rule_id is BoundaryRuleId.EXACT_AT_START
    assert sealed.ptb_attestation_result is AttestationResult.MATCH


def test_late_candidate_after_seal_does_not_change_k() -> None:
    eng = PtbCaptureEngine(
        attestation_port=FixturePtbAttestationProvider(
            by_window={WID: ("100", "fixture", {})}
        )
    )
    eng.open_window(
        market_id=MID, window_id=WID, event_start=TS0, event_end=TS0 + timedelta(minutes=5)
    )
    eng.ingest_chainlink(market_id=MID, window_id=WID, tick=_tick("100", source_ts=TS0))
    eng.attest(market_id=MID, window_id=WID)
    sealed = eng.seal(market_id=MID, window_id=WID, sealed_at=TS0 + timedelta(seconds=1))
    eng.ingest_chainlink(
        market_id=MID,
        window_id=WID,
        tick=_tick("999", source_ts=TS0 + timedelta(seconds=10)),
    )
    again = eng.seal(market_id=MID, window_id=WID)
    assert again.ptb_k == sealed.ptb_k == Decimal("100")
    assert eng.get_window(MID, WID).phase is PtbLifecyclePhase.SEALED


def test_wrong_window_isolation() -> None:
    eng = PtbCaptureEngine()
    w1, w2 = "win-a", "win-b"
    t1, t2 = TS0, TS0 + timedelta(minutes=5)
    eng.open_window(market_id=MID, window_id=w1, event_start=t1, event_end=t2)
    eng.open_window(
        market_id=MID, window_id=w2, event_start=t2, event_end=t2 + timedelta(minutes=5)
    )
    eng.ingest_chainlink(market_id=MID, window_id=w1, tick=_tick("10", source_ts=t1))
    eng.ingest_chainlink(market_id=MID, window_id=w2, tick=_tick("20", source_ts=t2))
    s1 = eng.seal(market_id=MID, window_id=w1, sealed_at=t1 + timedelta(seconds=1))
    s2 = eng.seal(market_id=MID, window_id=w2, sealed_at=t2 + timedelta(seconds=1))
    assert s1.ptb_k == Decimal("10")
    assert s2.ptb_k == Decimal("20")
    assert s1.window_id != s2.window_id


def test_duplicate_exact_delivery_idempotent() -> None:
    eng = PtbCaptureEngine()
    eng.open_window(
        market_id=MID, window_id=WID, event_start=TS0, event_end=TS0 + timedelta(minutes=5)
    )
    tick = _tick("55.5", source_ts=TS0)
    eng.ingest_chainlink(market_id=MID, window_id=WID, tick=tick)
    eng.ingest_chainlink(market_id=MID, window_id=WID, tick=tick)  # same fingerprint
    state = eng.get_window(MID, WID)
    assert state.selected_candidate is not None
    assert state.selected_candidate.value == Decimal("55.5")
    assert state.phase is not PtbLifecyclePhase.FAILED


def test_boundary_edge_exact_vs_first() -> None:
    ticks = [
        _tick("1", source_ts=TS0 - timedelta(milliseconds=1)),
        _tick("2", source_ts=TS0),
        _tick("3", source_ts=TS0 + timedelta(milliseconds=1)),
    ]
    cs = evaluate_boundary_candidates(
        market_id=MID,
        window_id=WID,
        event_start=TS0,
        event_end=TS0 + timedelta(minutes=5),
        ticks=ticks,
    )
    assert cs.selected_for_provisional_lock is not None
    assert cs.selected_for_provisional_lock.value == Decimal("2")
    assert cs.selected_for_provisional_lock.rule_id is BoundaryRuleId.EXACT_AT_START


def test_attestation_mismatch_still_allows_seal_without_require_match() -> None:
    eng = PtbCaptureEngine(
        attestation_port=FixturePtbAttestationProvider(
            by_window={WID: ("200", "fixture", {})}
        )
    )
    eng.open_window(
        market_id=MID, window_id=WID, event_start=TS0, event_end=TS0 + timedelta(minutes=5)
    )
    eng.ingest_chainlink(market_id=MID, window_id=WID, tick=_tick("100", source_ts=TS0))
    att = eng.attest(market_id=MID, window_id=WID)
    assert att.result is AttestationResult.MISMATCH
    sealed = eng.seal(market_id=MID, window_id=WID, require_attestation_match=False)
    assert sealed.ptb_k == Decimal("100")
    assert sealed.ptb_attestation_result is AttestationResult.MISMATCH


def test_require_attestation_match_blocks_seal() -> None:
    eng = PtbCaptureEngine(
        attestation_port=FixturePtbAttestationProvider(
            by_window={WID: ("200", "fixture", {})}
        )
    )
    eng.open_window(
        market_id=MID, window_id=WID, event_start=TS0, event_end=TS0 + timedelta(minutes=5)
    )
    eng.ingest_chainlink(market_id=MID, window_id=WID, tick=_tick("100", source_ts=TS0))
    eng.attest(market_id=MID, window_id=WID)
    try:
        eng.seal(market_id=MID, window_id=WID, require_attestation_match=True)
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_ssr_extract_matched_start_precision() -> None:
    start = "2026-07-21T20:00:00Z"
    html = (
        r'{"data":{"openPrice":66415.18883423829,"closePrice":null},'
        r'"queryKey":["crypto-prices","price","BTC","' + start + r'"]}'
    )
    # Escaped form as in real SSR
    html_esc = html.replace('"', r"\"")
    got = extract_open_close(html_esc, start)
    assert got is not None
    assert got["matched_start"] is True
    assert abs(got["openPrice"] - 66415.18883423829) < 1e-9


def test_compare_attestation_exact_zero_only() -> None:
    rec = compare_attestation(
        market_id=MID,
        window_id=WID,
        candidate_value="10.0",
        attested_value="10.0",
        candidate_rule=BoundaryRuleId.EXACT_AT_START,
        attestation_source="t",
    )
    assert rec.result is AttestationResult.MATCH
    bad = compare_attestation(
        market_id=MID,
        window_id=WID,
        candidate_value="10.0",
        attested_value="10.0000001",
        candidate_rule=BoundaryRuleId.EXACT_AT_START,
        attestation_source="t",
    )
    assert bad.result is AttestationResult.MISMATCH
