"""Decisive N4A regressions: Z-Gap must consume C_hat, never raw Binance vs K."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import MarketId, TokenId
from tyrex_pm.core.ingress import FeedRole, IngressMeta
from tyrex_pm.domain.polymarket.boundary_candidates import BoundaryTickView
from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketStatus, make_binary_instruments
from tyrex_pm.domain.polymarket.ptb_attestation import FixturePtbAttestationProvider
from tyrex_pm.indicators.causal_pairing import PriceTickView, TradingReferenceIdentity
from tyrex_pm.indicators.reference_alignment import AlignmentMode
from tyrex_pm.runtime.n4_observe_runtime import N4ObserveRuntime, SessionSlot
from tyrex_pm.strategies.z_gap.config import ZGapConfig, ZGapPtbTimeQualityConfig


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


START = "2026-07-20T21:15:00+00:00"
END = "2026-07-20T21:20:00+00:00"
WID = "btc-aligned-n4"
MID = "m-aligned"


def _market() -> BinaryMarket:
    mid = MarketId(MID)
    yes, no = make_binary_instruments(
        market_id=mid, yes_token=TokenId("up"), no_token=TokenId("down")
    )
    return BinaryMarket(
        market_id=mid,
        condition_id=WID,
        question=WID,
        yes=yes,
        no=no,
        event_start=_ts(START),
        event_end=_ts(END),
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
        status=MarketStatus.ACTIVE,
    )


def _cl(value: str, source: str, recv: str, mono: int, fp: str) -> BoundaryTickView:
    return BoundaryTickView(
        value=Decimal(value),
        source_ts=_ts(source),
        receive_wall_raw_utc=_ts(recv),
        receive_wall_corrected_utc=_ts(recv),
        receive_monotonic_ns=mono,
        raw_fingerprint=fp,
        event_id=fp,
        ingress=IngressMeta(
            receive_monotonic_ns=mono,
            ingress_sequence=max(1, mono),
            connection_generation=1,
            receive_wall_raw_utc=_ts(recv),
            receive_wall_corrected_utc=_ts(recv),
            clock_status="READY",
            clock_offset_ms=0.0,
            clock_uncertainty_ms=50,
            raw_fingerprint=fp,
            role=FeedRole.SETTLEMENT_REFERENCE,
        ),
    )


def _bn(value: str, source: str, recv: str, mono: int, fp: str) -> PriceTickView:
    return PriceTickView(
        value=Decimal(value),
        source_ts=_ts(source),
        receive_wall_raw_utc=_ts(recv),
        receive_monotonic_ns=mono,
        identity=TradingReferenceIdentity.BINANCE_SPOT,
        raw_fingerprint=fp,
    )


def _seal_k100(rt: N4ObserveRuntime) -> None:
    m = _market()
    sess = rt.open_session(slot=SessionSlot.ACTIVE, market=m, window_id=WID)
    sess.up_ask, sess.up_bid = Decimal("0.55"), Decimal("0.45")
    sess.down_ask, sess.down_bid = Decimal("0.55"), Decimal("0.45")
    # Causal pair establishing basis ln(100/99)
    rt.ingest_binance(_bn("99", "2026-07-20T21:14:59.500+00:00", "2026-07-20T21:14:59.600+00:00", 1, "b0"))
    rt.ingest_chainlink(
        window_id=WID,
        market_id=m.market_id,
        tick=_cl("100", START, "2026-07-20T21:15:01+00:00", 2, "exact"),
    )
    rt.attest_and_seal(
        market_id=m.market_id,
        window_id=WID,
        sealed_at=_ts(START) + timedelta(seconds=2),
        require_attestation_match=True,
    )


def test_nonzero_basis_model_gap_near_zero_not_raw_binance() -> None:
    """K=100, B=99, estimate=ln(100/99) → C_hat≈100 → ln(S/K)≈0, not ln(99/100)."""
    clock = FakeClock(_wall=_ts(START))
    rt = N4ObserveRuntime.create(
        clock=clock,
        attestation_port=FixturePtbAttestationProvider(
            by_window={WID: ("100", "fixture", {})}
        ),
        basis_ewma_half_life_s=30.0,  # fixture-only estimator config
        zgap_config=ZGapConfig(
            ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
        ),
    )
    _seal_k100(rt)
    # Hold estimate; B stays 99 → C_hat = 99 * exp(ln(100/99)) = 100
    clock.set_utc(_ts(START) + timedelta(seconds=30))
    dyn = rt.ingest_binance(
        _bn("99", "2026-07-20T21:15:29.900+00:00", "2026-07-20T21:15:30+00:00", 10, "b1")
    )
    assert dyn.alignment_mode is AlignmentMode.BETWEEN_TICKS
    assert dyn.c_hat is not None
    assert abs(float(dyn.c_hat) - 100.0) < 1e-6

    # Warm EWMA with binance prices so fair value can compute z
    for i in range(25):
        clock.set_utc(_ts(START) + timedelta(seconds=31 + i))
        rt.ingest_binance(
            _bn(
                "99",
                (_ts(START) + timedelta(seconds=30 + i)).isoformat(),
                (_ts(START) + timedelta(seconds=31 + i)).isoformat(),
                20 + i,
                f"w{i}",
            )
        )
    rec = rt.evaluate_active()
    assert rec.kind == "evaluated"
    assert rec.payload["model_spot_source"] == "aligned_c_hat"
    assert rec.payload["zgap_S_equals_c_hat"] is True
    assert Decimal(rec.c_hat) == Decimal(rec.payload["model_spot"])
    assert Decimal(rec.binance_raw) == Decimal("99")
    assert Decimal(rec.k) == Decimal("100")
    # Model S from facts
    model_s = None
    # Re-read from last evaluate via strategy fact path — use payload model_spot
    model_s = Decimal(rec.payload["model_spot"])
    assert abs(float(model_s) - 100.0) < 1e-6
    # Raw Binance gap would be ln(99/100) < 0; aligned gap ≈ 0
    raw_gap = math.log(99 / 100)
    aligned_gap = math.log(float(model_s) / 100.0)
    assert aligned_gap == 0.0 or abs(aligned_gap) < 1e-9
    assert raw_gap < -0.009
    assert abs(aligned_gap) < abs(raw_gap) / 10


def test_direction_follows_c_hat_not_raw_b() -> None:
    """Construct B vs K vs C_hat where directions disagree; OBSERVE follows C_hat."""
    clock = FakeClock(_wall=_ts(START))
    rt = N4ObserveRuntime.create(
        clock=clock,
        attestation_port=FixturePtbAttestationProvider(
            by_window={WID: ("100", "fixture", {})}
        ),
        basis_ewma_half_life_s=30.0,
        zgap_config=ZGapConfig(
            ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
        ),
    )
    _seal_k100(rt)
    # Estimate still ln(100/99). Move B to 101 → C_hat = 101*(100/99) ≈ 102.02 > K
    # Raw B=101 > K=100 → also positive, need case where B < K but C_hat > K
    # B=99.5, C_hat = 99.5 * 100/99 ≈ 100.505 > K while B < K
    for i in range(25):
        clock.set_utc(_ts(START) + timedelta(seconds=40 + i))
        rt.ingest_binance(
            _bn(
                "99.5",
                (_ts(START) + timedelta(seconds=39 + i)).isoformat(),
                (_ts(START) + timedelta(seconds=40 + i)).isoformat(),
                100 + i,
                f"d{i}",
            )
        )
    clock.set_utc(_ts(START) + timedelta(seconds=65))
    dyn = rt.ingest_binance(
        _bn("99.5", "2026-07-20T21:16:04.900+00:00", "2026-07-20T21:16:05+00:00", 200, "final")
    )
    assert dyn.c_hat is not None
    c_hat = float(dyn.c_hat)
    assert 99.5 < 100.0 < c_hat  # raw below K, aligned above K
    rec = rt.evaluate_active()
    assert rec.kind == "evaluated"
    model_s = float(rec.payload["model_spot"])
    assert model_s > 100.0
    assert float(rec.binance_raw) < 100.0
    # z sign follows ln(S/K) with S=C_hat > 0
    assert math.log(model_s / 100.0) > 0
    assert math.log(float(rec.binance_raw) / 100.0) < 0


def test_between_ticks_c_hat_moves_with_b_k_fixed() -> None:
    clock = FakeClock(_wall=_ts(START))
    rt = N4ObserveRuntime.create(
        clock=clock,
        attestation_port=FixturePtbAttestationProvider(
            by_window={WID: ("100", "fixture", {})}
        ),
        basis_ewma_half_life_s=30.0,
        zgap_config=ZGapConfig(
            ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
        ),
    )
    _seal_k100(rt)
    k0 = rt.active.sealed.ptb_k
    spots = []
    for i, bpx in enumerate(("99", "99.2", "99.4")):
        clock.set_utc(_ts(START) + timedelta(seconds=50 + i * 2))
        for j in range(5):
            rt.ingest_binance(
                _bn(
                    bpx,
                    (_ts(START) + timedelta(seconds=49 + i * 2 + j * 0.1)).isoformat(),
                    (_ts(START) + timedelta(seconds=50 + i * 2 + j * 0.1)).isoformat(),
                    300 + i * 10 + j,
                    f"m{i}-{j}",
                )
            )
        rec = rt.evaluate_active()
        assert rt.active.sealed.ptb_k == k0
        spots.append(Decimal(rec.payload["model_spot"]))
    assert spots[0] < spots[1] < spots[2]
    assert all(s != Decimal("99") for s in spots)  # not raw B


def test_newer_basis_affects_later_not_earlier() -> None:
    clock = FakeClock(_wall=_ts(START))
    rt = N4ObserveRuntime.create(
        clock=clock,
        attestation_port=FixturePtbAttestationProvider(
            by_window={WID: ("100", "fixture", {})}
        ),
        basis_ewma_half_life_s=30.0,
        zgap_config=ZGapConfig(
            ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
        ),
    )
    _seal_k100(rt)
    clock.set_utc(_ts(START) + timedelta(seconds=60))
    for i in range(20):
        rt.ingest_binance(
            _bn(
                "99",
                (_ts(START) + timedelta(seconds=59 + i)).isoformat(),
                (_ts(START) + timedelta(seconds=60 + i)).isoformat(),
                400 + i,
                f"e{i}",
            )
        )
    early = rt.evaluate_active()
    early_asof = early.basis_estimate_as_of_ts
    early_est = early.basis_estimate_used_ln
    # New causal pair with different basis
    rt.ingest_binance(
        _bn("100", "2026-07-20T21:16:09.500+00:00", "2026-07-20T21:16:09.600+00:00", 500, "nb")
    )
    rt.ingest_chainlink(
        window_id=WID,
        market_id=MarketId(MID),
        tick=_cl("102", "2026-07-20T21:16:10+00:00", "2026-07-20T21:16:11+00:00", 501, "nc"),
    )
    clock.set_utc(_ts("2026-07-20T21:16:15+00:00"))
    rt.ingest_binance(
        _bn("100", "2026-07-20T21:16:14.900+00:00", "2026-07-20T21:16:15+00:00", 502, "nb2")
    )
    later = rt.evaluate_active()
    assert early_asof == _ts(START).isoformat()
    assert later.basis_estimate_as_of_ts == _ts("2026-07-20T21:16:10+00:00").isoformat()
    assert later.basis_estimate_used_ln != early_est
    # Earlier observation unchanged in history
    assert rt.observations[0].basis_estimate_as_of_ts == early_asof


def test_unavailable_alignment_no_raw_binance_decision() -> None:
    clock = FakeClock(_wall=_ts(START))
    rt = N4ObserveRuntime.create(
        clock=clock,
        attestation_port=FixturePtbAttestationProvider(
            by_window={WID: ("100", "fixture", {})}
        ),
        basis_ewma_half_life_s=None,  # OPEN — fixture may still accept instantaneous
        zgap_config=ZGapConfig(
            ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
        ),
    )
    m = _market()
    sess = rt.open_session(slot=SessionSlot.ACTIVE, market=m, window_id=WID)
    sess.up_ask = Decimal("0.5")
    # Seal with exact CL but never ingest a causal Binance before seal/eval estimate
    rt.ingest_chainlink(
        window_id=WID,
        market_id=m.market_id,
        tick=_cl("100", START, "2026-07-20T21:15:01+00:00", 1, "exact"),
    )
    # Seal may succeed without pair; readiness may be degraded
    sealed = rt.attest_and_seal(market_id=m.market_id, window_id=WID)
    assert sealed.ptb_k == Decimal("100")
    # Binance only in the future relative to any accepted estimate — clear accepted
    rt.accepted_basis.basis_ln = None
    rt.accepted_basis.as_of_chainlink_source_ts = None
    clock.set_utc(_ts(START) + timedelta(seconds=30))
    rt.ingest_binance(
        _bn("99", "2026-07-20T21:15:29+00:00", "2026-07-20T21:15:30+00:00", 9, "only-b")
    )
    rec = rt.evaluate_active()
    assert rec.kind == "skipped"
    assert "basis_estimate_unavailable" in rec.skip_reasons
    assert rec.payload.get("zgap_S_equals_c_hat") is None


def test_facts_identify_model_vs_raw() -> None:
    clock = FakeClock(_wall=_ts(START))
    rt = N4ObserveRuntime.create(
        clock=clock,
        attestation_port=FixturePtbAttestationProvider(
            by_window={WID: ("100", "fixture", {})}
        ),
        basis_ewma_half_life_s=30.0,
        zgap_config=ZGapConfig(
            ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
        ),
    )
    _seal_k100(rt)
    for i in range(20):
        clock.set_utc(_ts(START) + timedelta(seconds=70 + i))
        rt.ingest_binance(
            _bn(
                "99",
                (_ts(START) + timedelta(seconds=69 + i)).isoformat(),
                (_ts(START) + timedelta(seconds=70 + i)).isoformat(),
                600 + i,
                f"f{i}",
            )
        )
    rec = rt.evaluate_active()
    assert rec.kind == "evaluated"
    assert rec.binance_raw is not None and rec.c_hat is not None
    assert rec.payload["binance_raw_price"] == rec.binance_raw
    assert rec.payload["aligned_model_price"] == rec.c_hat
    assert rec.payload["model_spot_source"] == "aligned_c_hat"
    assert rec.payload["sigma_source"] == "binance_raw_returns"
    assert rec.binance_raw != rec.c_hat


def test_replay_identical_model_spots() -> None:
    def run_once() -> list[str]:
        clock = FakeClock(_wall=_ts(START))
        rt = N4ObserveRuntime.create(
            clock=clock,
            attestation_port=FixturePtbAttestationProvider(
                by_window={WID: ("100", "fixture", {})}
            ),
            basis_ewma_half_life_s=30.0,
            zgap_config=ZGapConfig(
                ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
            ),
        )
        _seal_k100(rt)
        out = []
        for i in range(2):
            for j in range(15):
                clock.set_utc(_ts(START) + timedelta(seconds=80 + i * 20 + j))
                rt.ingest_binance(
                    _bn(
                        str(99 + i),
                        (_ts(START) + timedelta(seconds=79 + i * 20 + j)).isoformat(),
                        (_ts(START) + timedelta(seconds=80 + i * 20 + j)).isoformat(),
                        700 + i * 20 + j,
                        f"r{i}-{j}",
                    )
                )
            rec = rt.evaluate_active()
            assert rec.kind == "evaluated"
            out.append(rec.payload["model_spot"])
        return out

    assert run_once() == run_once()
