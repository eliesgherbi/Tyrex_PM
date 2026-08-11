"""N3A offline deterministic tests: PTB capture, pairing, alignment, seal/replay."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

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
from tyrex_pm.indicators.causal_pairing import (
    PAIRING_POLICY_ID,
    PriceTickView,
    TradingReferenceIdentity,
    select_latest_binance_at_or_before,
)
from tyrex_pm.indicators.reference_alignment import (
    AlignmentInitState,
    BasisEwmaState,
    aligned_chainlink_estimate,
    build_aligned_reference,
    compute_log_basis,
)
from tyrex_pm.indicators.reference_basis import compute_basis_bps

REPO = Path(__file__).resolve().parents[3]
FIXTURE = REPO / "tests" / "fixtures" / "n3" / "n1_three_windows.json"


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _cl_tick(row: dict, *, clock_status: str | None = "READY", gen: int = 1) -> BoundaryTickView:
    meta = IngressMeta(
        receive_monotonic_ns=int(row["receive_monotonic_ns"]),
        ingress_sequence=max(1, int(row["receive_monotonic_ns"])),
        connection_generation=gen,
        receive_wall_raw_utc=_ts(row["receive_wall_raw_utc"]),
        receive_wall_corrected_utc=_ts(row["receive_wall_raw_utc"]),
        clock_offset_ms=0.0,
        clock_uncertainty_ms=50,
        clock_status=clock_status,
        clock_snapshot_id="test-snap",
        raw_fingerprint=row.get("fingerprint", ""),
        role=FeedRole.SETTLEMENT_REFERENCE,
    )
    return BoundaryTickView(
        value=Decimal(str(row["value"])),
        source_ts=_ts(row["source_ts"]),
        receive_wall_raw_utc=_ts(row["receive_wall_raw_utc"]),
        receive_wall_corrected_utc=_ts(row["receive_wall_raw_utc"]),
        receive_monotonic_ns=int(row["receive_monotonic_ns"]),
        ingress=meta,
        raw_fingerprint=row.get("fingerprint"),
        event_id=row.get("fingerprint"),
    )


def _bn_tick(row: dict) -> PriceTickView:
    ident = TradingReferenceIdentity(row.get("identity", "binance_spot"))
    return PriceTickView(
        value=Decimal(str(row["value"])),
        source_ts=_ts(row["source_ts"]),
        receive_wall_raw_utc=_ts(row["receive_wall_raw_utc"]),
        receive_wall_corrected_utc=_ts(row["receive_wall_raw_utc"]),
        receive_monotonic_ns=int(row["receive_monotonic_ns"]),
        identity=ident,
        raw_fingerprint=row.get("fingerprint"),
        event_id=row.get("fingerprint"),
    )


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_exact_boundary_candidate() -> None:
    data = _load_fixture()
    w = data["windows"][0]
    ticks = [_cl_tick(t) for t in w["chainlink_ticks"]]
    cs = evaluate_boundary_candidates(
        market_id=MarketId(w["market_id"]),
        window_id=w["window_id"],
        event_start=_ts(w["event_start"]),
        event_end=_ts(w["event_end"]),
        ticks=ticks,
    )
    assert cs.exact is not None
    assert cs.exact.value == Decimal(w["attested_open_price"])
    assert cs.preferred_rule_id is BoundaryRuleId.EXACT_AT_START
    assert "exact_candidate_absent" not in cs.blocker_reasons


def test_first_after_last_before_divergence_no_silent_fallback() -> None:
    data = _load_fixture()
    d = data["divergence_case"]
    ticks = [_cl_tick(t) for t in d["chainlink_ticks"]]
    cs = evaluate_boundary_candidates(
        market_id=MarketId(d["market_id"]),
        window_id=d["window_id"],
        event_start=_ts(d["event_start"]),
        event_end=_ts(d["event_end"]),
        ticks=ticks,
    )
    assert cs.exact is None
    assert cs.first_at_or_after is not None
    assert cs.last_at_or_before is not None
    assert cs.first_last_diverge
    assert cs.selected_for_provisional_lock is None
    assert "ambiguous_fallback_candidates" in cs.blocker_reasons
    assert "exact_candidate_absent" in cs.blocker_reasons


def test_three_window_n1_fixture_replay_and_attestation_match() -> None:
    data = _load_fixture()
    attest_map = {
        w["window_id"]: (
            w["attested_open_price"],
            w["attestation_source"],
            {"fixture": True, "not_live": True},
        )
        for w in data["windows"]
    }
    engine = PtbCaptureEngine(
        attestation_port=FixturePtbAttestationProvider(by_window=attest_map),
        ewma=BasisEwmaState(half_life_s=30.0),
    )
    sealed_ks = []
    for w in data["windows"]:
        mid = MarketId(w["market_id"])
        engine.open_window(
            market_id=mid,
            window_id=w["window_id"],
            event_start=_ts(w["event_start"]),
            event_end=_ts(w["event_end"]),
        )
        for b in w["binance_ticks"]:
            engine.ingest_binance(_bn_tick(b))
        for t in w["chainlink_ticks"]:
            engine.ingest_chainlink(market_id=mid, window_id=w["window_id"], tick=_cl_tick(t))
        att = engine.attest(market_id=mid, window_id=w["window_id"])
        assert att.result is AttestationResult.MATCH
        assert att.bps_diff == 0
        sealed = engine.seal(
            market_id=mid,
            window_id=w["window_id"],
            sealed_at=_ts(w["event_start"]) + timedelta(seconds=10),
            require_attestation_match=True,
        )
        assert sealed.ptb_k == Decimal(w["attested_open_price"])
        assert sealed.boundary_rule_id is BoundaryRuleId.EXACT_AT_START
        sealed_ks.append(sealed.ptb_k)
    assert len(sealed_ks) == 3
    # EWMA continuous across windows
    assert engine.ewma.samples >= 3


def test_attestation_mismatch_and_missing() -> None:
    mid = MarketId("m1")
    wid = "w-mismatch"
    start = _ts("2026-07-20T21:15:00+00:00")
    end = start + timedelta(seconds=300)
    engine = PtbCaptureEngine(
        attestation_port=FixturePtbAttestationProvider(
            by_window={
                wid: ("99999.0", "fixture_ssr_openPrice", {}),
            }
        )
    )
    engine.open_window(market_id=mid, window_id=wid, event_start=start, event_end=end)
    engine.ingest_chainlink(
        market_id=mid,
        window_id=wid,
        tick=_cl_tick(
            {
                "value": "65276.78644629988",
                "source_ts": "2026-07-20T21:15:00+00:00",
                "receive_wall_raw_utc": "2026-07-20T21:15:01+00:00",
                "receive_monotonic_ns": 1,
                "fingerprint": "ex1",
            }
        ),
    )
    att = engine.attest(market_id=mid, window_id=wid)
    assert att.result is AttestationResult.MISMATCH
    assert engine.get_window(mid, wid).phase is PtbLifecyclePhase.DEGRADED
    assert "attestation_mismatch" in engine.get_window(mid, wid).blockers

    missing = compare_attestation(
        market_id=mid,
        window_id="w-miss",
        candidate_value="1",
        attested_value=None,
        candidate_rule=BoundaryRuleId.EXACT_AT_START,
        attestation_source="fixture_missing",
    )
    assert missing.result is AttestationResult.INCOMPLETE


def test_sealed_immutability_and_duplicate_idempotency() -> None:
    mid = MarketId("m1")
    wid = "w-seal"
    start = _ts("2026-07-20T21:15:00+00:00")
    end = start + timedelta(seconds=300)
    engine = PtbCaptureEngine()
    engine.open_window(market_id=mid, window_id=wid, event_start=start, event_end=end)
    tick = _cl_tick(
        {
            "value": "100.0",
            "source_ts": "2026-07-20T21:15:00+00:00",
            "receive_wall_raw_utc": "2026-07-20T21:15:01+00:00",
            "receive_monotonic_ns": 1,
            "fingerprint": "same-fp",
        }
    )
    engine.ingest_binance(
        _bn_tick(
            {
                "value": "99.9",
                "source_ts": "2026-07-20T21:14:59+00:00",
                "receive_wall_raw_utc": "2026-07-20T21:14:59.1+00:00",
                "receive_monotonic_ns": 1,
                "identity": "binance_spot",
                "fingerprint": "bn",
            }
        )
    )
    engine.ingest_chainlink(market_id=mid, window_id=wid, tick=tick)
    sealed = engine.seal(market_id=mid, window_id=wid, sealed_at=start + timedelta(seconds=5))
    # duplicate identical
    engine.ingest_chainlink(market_id=mid, window_id=wid, tick=tick)
    assert engine.get_window(mid, wid).sealed is sealed
    assert sealed.ptb_k == Decimal("100.0")
    locked = engine.lock_store.get_locked(mid, wid)
    assert locked is not None and locked.locked and locked.k == Decimal("100.0")


def test_conflicting_duplicate_before_seal_fails() -> None:
    mid = MarketId("m1")
    wid = "w-conflict"
    start = _ts("2026-07-20T21:15:00+00:00")
    end = start + timedelta(seconds=300)
    engine = PtbCaptureEngine()
    engine.open_window(market_id=mid, window_id=wid, event_start=start, event_end=end)
    engine.ingest_chainlink(
        market_id=mid,
        window_id=wid,
        tick=_cl_tick(
            {
                "value": "100.0",
                "source_ts": "2026-07-20T21:15:00+00:00",
                "receive_wall_raw_utc": "2026-07-20T21:15:01+00:00",
                "receive_monotonic_ns": 1,
                "fingerprint": "a",
            }
        ),
    )
    engine.ingest_chainlink(
        market_id=mid,
        window_id=wid,
        tick=_cl_tick(
            {
                "value": "101.0",
                "source_ts": "2026-07-20T21:15:00+00:00",
                "receive_wall_raw_utc": "2026-07-20T21:15:02+00:00",
                "receive_monotonic_ns": 2,
                "fingerprint": "b",
            }
        ),
    )
    st = engine.get_window(mid, wid)
    assert st is not None
    assert st.phase is PtbLifecyclePhase.FAILED
    assert "conflicting_duplicate" in st.blockers
    assert st.sealed is None


def test_late_exact_before_and_after_seal() -> None:
    mid = MarketId("m1")
    wid = "w-late"
    start = _ts("2026-07-20T21:15:00+00:00")
    end = start + timedelta(seconds=300)
    engine = PtbCaptureEngine()
    engine.open_window(market_id=mid, window_id=wid, event_start=start, event_end=end)
    # Pre-boundary only first
    engine.ingest_chainlink(
        market_id=mid,
        window_id=wid,
        tick=_cl_tick(
            {
                "value": "99.0",
                "source_ts": "2026-07-20T21:14:59+00:00",
                "receive_wall_raw_utc": "2026-07-20T21:15:00.5+00:00",
                "receive_monotonic_ns": 1,
                "fingerprint": "pre",
            }
        ),
    )
    assert engine.get_window(mid, wid).selected_candidate is None
    # Late exact before seal
    engine.ingest_chainlink(
        market_id=mid,
        window_id=wid,
        tick=_cl_tick(
            {
                "value": "100.0",
                "source_ts": "2026-07-20T21:15:00+00:00",
                "receive_wall_raw_utc": "2026-07-20T21:15:08+00:00",
                "receive_monotonic_ns": 2,
                "fingerprint": "exact-late",
            }
        ),
    )
    engine.ingest_binance(
        _bn_tick(
            {
                "value": "99.5",
                "source_ts": "2026-07-20T21:14:59.5+00:00",
                "receive_wall_raw_utc": "2026-07-20T21:14:59.6+00:00",
                "receive_monotonic_ns": 1,
                "identity": "binance_spot",
                "fingerprint": "bn",
            }
        )
    )
    sealed = engine.seal(market_id=mid, window_id=wid, sealed_at=start + timedelta(seconds=9))
    assert sealed.ptb_k == Decimal("100.0")
    # Late conflicting after seal
    engine.ingest_chainlink(
        market_id=mid,
        window_id=wid,
        tick=_cl_tick(
            {
                "value": "105.0",
                "source_ts": "2026-07-20T21:15:00+00:00",
                "receive_wall_raw_utc": "2026-07-20T21:15:20+00:00",
                "receive_monotonic_ns": 3,
                "fingerprint": "after-seal-conflict",
            }
        ),
    )
    st = engine.get_window(mid, wid)
    assert st is not None
    assert st.sealed is not None and st.sealed.ptb_k == Decimal("100.0")
    assert any(e.noted is None for e in st.late_after_seal) or len(st.late_after_seal) >= 1
    assert "late_event_after_seal" in st.blockers
    assert st.phase is PtbLifecyclePhase.DEGRADED


def test_no_future_binance_look_ahead() -> None:
    data = _load_fixture()
    w = data["windows"][0]
    cl = _cl_tick(w["chainlink_ticks"][1])  # exact
    bns = [_bn_tick(b) for b in w["binance_ticks"]]
    # Numerically, future tick 65290 is farther from 65276 than 65275 — still reject future.
    cl_view = PriceTickView(
        value=cl.value,
        source_ts=cl.source_ts,
        receive_wall_raw_utc=cl.receive_wall_raw_utc,
        receive_monotonic_ns=cl.receive_monotonic_ns,
    )
    # Make future tick numerically closer
    future_closer = PriceTickView(
        value=Decimal("65276.78644629988"),
        source_ts=cl.source_ts + timedelta(milliseconds=50),
        receive_wall_raw_utc=cl.receive_wall_raw_utc,
        receive_monotonic_ns=9999,
        identity=TradingReferenceIdentity.BINANCE_SPOT,
        raw_fingerprint="closest-future",
    )
    past = bns[0]
    pair = select_latest_binance_at_or_before(
        chainlink=cl_view,
        binance_ticks=[past, future_closer],
        primary_identity=TradingReferenceIdentity.BINANCE_SPOT,
    )
    assert pair.paired
    assert pair.binance is not None
    assert pair.binance.raw_fingerprint == "w1-bn-ok"
    assert "rejected_future_binance_source_ts" in pair.notes
    assert pair.policy_id == PAIRING_POLICY_ID


def test_missing_causal_binance_pair() -> None:
    cl = PriceTickView(
        value=Decimal("100"),
        source_ts=_ts("2026-07-20T21:15:00+00:00"),
        receive_wall_raw_utc=_ts("2026-07-20T21:15:01+00:00"),
    )
    only_future = PriceTickView(
        value=Decimal("100"),
        source_ts=_ts("2026-07-20T21:15:01+00:00"),
        receive_wall_raw_utc=_ts("2026-07-20T21:15:01+00:00"),
        identity=TradingReferenceIdentity.BINANCE_SPOT,
    )
    pair = select_latest_binance_at_or_before(chainlink=cl, binance_ticks=[only_future])
    assert not pair.paired
    assert "no_causal_binance_pair" in pair.blocker_reasons


def test_basis_sign_and_formula_regression() -> None:
    # C > B → positive ln basis
    log = compute_log_basis(chainlink="110", binance="100")
    assert log.ready
    assert log.basis_ln is not None and log.basis_ln > 0
    expected = Decimal(str(math.log(1.1)))
    assert abs(log.basis_ln - expected) < Decimal("1e-12")
    c_hat = aligned_chainlink_estimate(binance="100", basis_ln_latest=log.basis_ln)
    assert abs(c_hat - Decimal("110")) < Decimal("1e-6")
    # Legacy linear bps is different formula — document coexistence
    legacy = compute_basis_bps(trading_ref="100", settlement_ref="110")
    assert legacy.basis_bps is not None
    # linear (B-C)/C*1e4 vs ln(C/B)*1e4 — not equal for 10% move
    assert legacy.basis_bps != log.basis_ln_x_1e4


def test_ewma_continuity_across_windows() -> None:
    ewma = BasisEwmaState(half_life_s=60.0)
    engine = PtbCaptureEngine(ewma=ewma)
    t0 = _ts("2026-07-20T21:15:00+00:00")
    engine.ingest_binance(
        PriceTickView(
            value=Decimal("100"),
            source_ts=t0 - timedelta(milliseconds=100),
            receive_wall_raw_utc=t0,
            identity=TradingReferenceIdentity.BINANCE_SPOT,
        )
    )
    samples_before = ewma.samples
    for i, offset in enumerate((0, 300, 600)):
        ts = t0 + timedelta(seconds=offset)
        cl = PriceTickView(
            value=Decimal("101"),
            source_ts=ts,
            receive_wall_raw_utc=ts + timedelta(milliseconds=10),
        )
        engine.ingest_binance(
            PriceTickView(
                value=Decimal("100"),
                source_ts=ts - timedelta(milliseconds=50),
                receive_wall_raw_utc=ts,
                identity=TradingReferenceIdentity.BINANCE_SPOT,
            )
        )
        snap = engine.update_alignment_on_pair(chainlink=cl)
        assert snap is not None
        assert snap.basis_ln_instant is not None
        _ = i
    assert ewma.samples == samples_before + 3
    assert ewma.value is not None


def test_clock_status_propagation() -> None:
    mid = MarketId("m1")
    wid = "w-clock"
    start = _ts("2026-07-20T21:15:00+00:00")
    engine = PtbCaptureEngine()
    engine.open_window(
        market_id=mid,
        window_id=wid,
        event_start=start,
        event_end=start + timedelta(seconds=300),
    )
    engine.ingest_chainlink(
        market_id=mid,
        window_id=wid,
        tick=_cl_tick(
            {
                "value": "100",
                "source_ts": "2026-07-20T21:15:00+00:00",
                "receive_wall_raw_utc": "2026-07-20T21:15:01+00:00",
                "receive_monotonic_ns": 1,
                "fingerprint": "c",
            },
            clock_status="UNSYNCHRONIZED",
        ),
    )
    st = engine.get_window(mid, wid)
    assert st is not None
    assert "unsynchronized_clock" in st.blockers
    assert st.phase is PtbLifecyclePhase.DEGRADED


def test_connection_generation_gap() -> None:
    mid = MarketId("m1")
    wid = "w-gen"
    start = _ts("2026-07-20T21:15:00+00:00")
    engine = PtbCaptureEngine()
    engine.open_window(
        market_id=mid,
        window_id=wid,
        event_start=start,
        event_end=start + timedelta(seconds=300),
    )
    engine.ingest_chainlink(
        market_id=mid,
        window_id=wid,
        tick=_cl_tick(
            {
                "value": "100",
                "source_ts": "2026-07-20T21:15:00+00:00",
                "receive_wall_raw_utc": "2026-07-20T21:15:01+00:00",
                "receive_monotonic_ns": 1,
                "fingerprint": "g1",
            },
            gen=1,
        ),
    )
    engine.ingest_chainlink(
        market_id=mid,
        window_id=wid,
        tick=_cl_tick(
            {
                "value": "100.1",
                "source_ts": "2026-07-20T21:15:01+00:00",
                "receive_wall_raw_utc": "2026-07-20T21:15:02+00:00",
                "receive_monotonic_ns": 2,
                "fingerprint": "g2",
            },
            gen=2,
        ),
    )
    st = engine.get_window(mid, wid)
    assert st is not None
    assert "source_reconnect_gap" in st.blockers


def test_deterministic_restart_replay() -> None:
    data = _load_fixture()
    w = data["windows"][0]
    mid = MarketId(w["market_id"])
    attest = FixturePtbAttestationProvider(
        by_window={
            w["window_id"]: (
                w["attested_open_price"],
                w["attestation_source"],
                {},
            )
        }
    )
    ticks = [_cl_tick(t) for t in w["chainlink_ticks"]]
    bns = [_bn_tick(b) for b in w["binance_ticks"]]
    sealed_at = _ts(w["event_start"]) + timedelta(seconds=10)

    e1 = PtbCaptureEngine(attestation_port=attest)
    s1 = e1.replay_from_evidence(
        market_id=mid,
        window_id=w["window_id"],
        event_start=_ts(w["event_start"]),
        event_end=_ts(w["event_end"]),
        chainlink_ticks=ticks,
        binance_ticks=bns,
        seal=True,
        sealed_at=sealed_at,
        require_attestation_match=True,
    )
    e2 = PtbCaptureEngine(attestation_port=attest)
    s2 = e2.replay_from_evidence(
        market_id=mid,
        window_id=w["window_id"],
        event_start=_ts(w["event_start"]),
        event_end=_ts(w["event_end"]),
        chainlink_ticks=ticks,
        binance_ticks=bns,
        seal=True,
        sealed_at=sealed_at,
        require_attestation_match=True,
    )
    assert s1.sealed is not None and s2.sealed is not None
    assert s1.sealed.ptb_k == s2.sealed.ptb_k
    assert s1.sealed.boundary_rule_id == s2.sealed.boundary_rule_id
    assert s1.last_boundary_pair_skew_ms == s2.last_boundary_pair_skew_ms


def test_threshold_not_configured_visible() -> None:
    snap = build_aligned_reference(
        chainlink="101",
        binance="100",
        pairing_policy_id=PAIRING_POLICY_ID,
        source_skew_ms=10,
        trading_identity="binance_spot",
        clock_status="READY",
        ewma=None,
    )
    assert "threshold_not_configured" in snap.blocker_reasons
    assert snap.init_state is AlignmentInitState.EWMA_NOT_CONFIGURED
    assert snap.c_hat is not None  # instantaneous still usable


def test_market_alignment_modules_do_not_import_retired_package() -> None:
    root = REPO / "src" / "tyrex_pm"
    module_files = [
        root / "domain" / "polymarket" / "boundary_candidates.py",
        root / "domain" / "polymarket" / "ptb_attestation.py",
        root / "domain" / "polymarket" / "ptb_capture.py",
        root / "domain" / "polymarket" / "sealed_reference.py",
        root / "indicators" / "causal_pairing.py",
        root / "indicators" / "reference_alignment.py",
    ]
    for path in module_files:
        text = path.read_text(encoding="utf-8")
        assert "old." not in text and "from old" not in text
