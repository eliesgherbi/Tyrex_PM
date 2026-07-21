"""N4A offline OBSERVE composition tests."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import MarketId, TokenId
from tyrex_pm.core.ingress import FeedRole, IngressMeta
from tyrex_pm.domain.polymarket.boundary_candidates import BoundaryTickView
from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketStatus, make_binary_instruments
from tyrex_pm.domain.polymarket.ptb_attestation import (
    AttestationResult,
    FixturePtbAttestationProvider,
)
from tyrex_pm.domain.polymarket.sealed_reference import SealedWindowPtb
from tyrex_pm.indicators.causal_pairing import PriceTickView, TradingReferenceIdentity
from tyrex_pm.indicators.reference_alignment import AlignmentMode
from tyrex_pm.runtime.n4_observe_runtime import N4ObserveRuntime, SessionSlot
from tyrex_pm.strategies.z_gap.config import ZGapConfig, ZGapPtbTimeQualityConfig

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "n3" / "n1_three_windows.json"


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _market(window_id: str, market_id: str, start: datetime, end: datetime) -> BinaryMarket:
    mid = MarketId(market_id)
    yes, no = make_binary_instruments(
        market_id=mid,
        yes_token=TokenId(f"up-{window_id}"),
        no_token=TokenId(f"down-{window_id}"),
    )
    return BinaryMarket(
        market_id=mid,
        condition_id=window_id,
        question=window_id,
        yes=yes,
        no=no,
        event_start=start,
        event_end=end,
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
        status=MarketStatus.ACTIVE,
    )


def _cl(
    value: str,
    source_ts: str,
    recv: str,
    mono: int,
    fp: str,
    *,
    clock: str = "READY",
) -> BoundaryTickView:
    return BoundaryTickView(
        value=Decimal(value),
        source_ts=_ts(source_ts),
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
            clock_status=clock,
            clock_offset_ms=0.0,
            clock_uncertainty_ms=50,
            raw_fingerprint=fp,
            role=FeedRole.SETTLEMENT_REFERENCE,
        ),
    )


def _bn(value: str, source_ts: str, recv: str, mono: int, fp: str) -> PriceTickView:
    return PriceTickView(
        value=Decimal(value),
        source_ts=_ts(source_ts),
        receive_wall_raw_utc=_ts(recv),
        receive_monotonic_ns=mono,
        identity=TradingReferenceIdentity.BINANCE_SPOT,
        raw_fingerprint=fp,
    )


def _runtime_for_window(w: dict, *, half_life: float | None = 30.0) -> N4ObserveRuntime:
    clock = FakeClock(_wall=_ts(w["event_start"]))
    return N4ObserveRuntime.create(
        clock=clock,
        attestation_port=FixturePtbAttestationProvider(
            by_window={
                w["window_id"]: (
                    w["attested_open_price"],
                    w["attestation_source"],
                    {"fixture": True},
                )
            }
        ),
        basis_ewma_half_life_s=half_life,
        zgap_config=ZGapConfig(
            ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
        ),
    )


def test_sealed_k_immutable_while_dynamic_c_hat_moves() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    w = data["windows"][0]
    rt = _runtime_for_window(w)
    m = _market(w["window_id"], w["market_id"], _ts(w["event_start"]), _ts(w["event_end"]))
    sess = rt.open_session(slot=SessionSlot.ACTIVE, market=m, window_id=w["window_id"])
    sess.up_ask, sess.up_bid = Decimal("0.5"), Decimal("0.48")
    sess.down_ask, sess.down_bid = Decimal("0.52"), Decimal("0.5")
    for b in w["binance_ticks"]:
        rt.ingest_binance(_bn(str(b["value"]), b["source_ts"], b["receive_wall_raw_utc"], int(b["receive_monotonic_ns"]), b["fingerprint"]))
    for t in w["chainlink_ticks"]:
        rt.ingest_chainlink(
            window_id=w["window_id"],
            market_id=m.market_id,
            tick=_cl(str(t["value"]), t["source_ts"], t["receive_wall_raw_utc"], int(t["receive_monotonic_ns"]), t["fingerprint"]),
        )
    sealed = rt.attest_and_seal(
        market_id=m.market_id,
        window_id=w["window_id"],
        sealed_at=_ts(w["event_start"]) + timedelta(seconds=5),
        require_attestation_match=True,
    )
    assert isinstance(sealed, SealedWindowPtb)
    k0 = sealed.ptb_k
    hats = []
    for i in range(3):
        rt.clock.set_utc(_ts(w["event_start"]) + timedelta(seconds=40 + i))
        dyn = rt.ingest_binance(
            _bn(str(65275 + i), (_ts(w["event_start"]) + timedelta(seconds=39 + i)).isoformat(), (_ts(w["event_start"]) + timedelta(seconds=40 + i)).isoformat(), 5000 + i, f"d{i}")
        )
        hats.append(dyn.c_hat)
        rec = rt.evaluate_active()
        assert rt.active.sealed.ptb_k == k0
        assert rec.k == str(k0)
    assert hats[0] != hats[-1]
    assert rt.orders_submitted == 0 and not rt.oms_touched


def test_new_chainlink_updates_basis_estimate_as_of() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    w = data["windows"][0]
    rt = _runtime_for_window(w)
    m = _market(w["window_id"], w["market_id"], _ts(w["event_start"]), _ts(w["event_end"]))
    rt.open_session(slot=SessionSlot.ACTIVE, market=m, window_id=w["window_id"])
    rt.ingest_binance(_bn("65275", "2026-07-20T21:14:59.500+00:00", "2026-07-20T21:14:59.700+00:00", 1, "b0"))
    rt.ingest_chainlink(
        window_id=w["window_id"],
        market_id=m.market_id,
        tick=_cl("65276.78644629988", "2026-07-20T21:15:00+00:00", "2026-07-20T21:15:05+00:00", 2, "c0"),
    )
    as_of1 = rt.accepted_basis.as_of_chainlink_source_ts
    assert as_of1 == _ts("2026-07-20T21:15:00+00:00")
    rt.ingest_binance(_bn("65280", "2026-07-20T21:15:00.900+00:00", "2026-07-20T21:15:01+00:00", 3, "b1"))
    rt.ingest_chainlink(
        window_id=w["window_id"],
        market_id=m.market_id,
        tick=_cl("65290", "2026-07-20T21:15:01+00:00", "2026-07-20T21:15:02+00:00", 4, "c1"),
    )
    assert rt.accepted_basis.as_of_chainlink_source_ts == _ts("2026-07-20T21:15:01+00:00")
    assert rt.accepted_basis.as_of_chainlink_source_ts > as_of1


def test_no_future_binance_leakage_and_reconstruction_label() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    w = data["windows"][0]
    rt = _runtime_for_window(w, half_life=None)
    m = _market(w["window_id"], w["market_id"], _ts(w["event_start"]), _ts(w["event_end"]))
    rt.open_session(slot=SessionSlot.ACTIVE, market=m, window_id=w["window_id"])
    # Past BN then future-closer BN
    rt.ingest_binance(_bn("65275", "2026-07-20T21:14:59.500+00:00", "2026-07-20T21:14:59.700+00:00", 1, "past"))
    rt.ingest_binance(_bn("65276.78644629988", "2026-07-20T21:15:00.050+00:00", "2026-07-20T21:15:00.100+00:00", 2, "future"))
    rt.ingest_chainlink(
        window_id=w["window_id"],
        market_id=m.market_id,
        tick=_cl("65276.78644629988", "2026-07-20T21:15:00+00:00", "2026-07-20T21:15:05+00:00", 3, "exact"),
    )
    # Accepted pair must use past BN
    assert rt.accepted_basis.binance_value == Decimal("65275")
    # Reconstruction path when evaluating with current CL pair
    rt.attest_and_seal(
        market_id=m.market_id,
        window_id=w["window_id"],
        sealed_at=_ts(w["event_start"]) + timedelta(seconds=5),
    )
    rt.clock.set_utc(_ts(w["event_start"]) + timedelta(seconds=10))
    # Between ticks: use current BN with prior estimate
    dyn = rt.ingest_binance(_bn("65300", "2026-07-20T21:15:08+00:00", "2026-07-20T21:15:08.1+00:00", 9, "btw"))
    assert dyn.alignment_mode is AlignmentMode.BETWEEN_TICKS
    assert dyn.basis_estimate_includes_current_chainlink is False
    rec = rt.evaluate_active(include_current_chainlink=True)
    assert rec.alignment_mode in {AlignmentMode.RECONSTRUCTION.value, AlignmentMode.BETWEEN_TICKS.value}


def test_missing_exact_blocks_without_silent_fallback() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    d = data["divergence_case"]
    clock = FakeClock(_wall=_ts(d["event_start"]))
    rt = N4ObserveRuntime.create(clock=clock)
    m = _market(d["window_id"], d["market_id"], _ts(d["event_start"]), _ts(d["event_end"]))
    rt.open_session(slot=SessionSlot.ACTIVE, market=m, window_id=d["window_id"])
    for t in d["chainlink_ticks"]:
        rt.ingest_chainlink(
            window_id=d["window_id"],
            market_id=m.market_id,
            tick=_cl(str(t["value"]), t["source_ts"], t["receive_wall_raw_utc"], int(t["receive_monotonic_ns"]), t["fingerprint"]),
        )
    with pytest.raises(ValueError, match="cannot seal without selected EXACT"):
        rt.attest_and_seal(market_id=m.market_id, window_id=d["window_id"])
    rec = rt.evaluate_active()
    assert rec.kind == "skipped"
    assert "exact_candidate_absent" in rec.skip_reasons
    assert "ambiguous_fallback_candidates" in rec.skip_reasons


def test_attestation_mismatch_precise_blocker_no_tolerance() -> None:
    w = {
        "window_id": "w-mm",
        "market_id": "m-mm",
        "event_start": "2026-07-20T21:15:00+00:00",
        "event_end": "2026-07-20T21:20:00+00:00",
        "attested_open_price": "99999",
        "attestation_source": "fixture",
    }
    rt = _runtime_for_window(w)
    m = _market(w["window_id"], w["market_id"], _ts(w["event_start"]), _ts(w["event_end"]))
    rt.open_session(slot=SessionSlot.ACTIVE, market=m, window_id=w["window_id"])
    rt.ingest_binance(_bn("100", "2026-07-20T21:14:59+00:00", "2026-07-20T21:14:59.1+00:00", 1, "b"))
    rt.ingest_chainlink(
        window_id=w["window_id"],
        market_id=m.market_id,
        tick=_cl("100", "2026-07-20T21:15:00+00:00", "2026-07-20T21:15:01+00:00", 2, "c"),
    )
    sealed = rt.attest_and_seal(market_id=m.market_id, window_id=w["window_id"])
    assert sealed.ptb_attestation_result is AttestationResult.MISMATCH
    # Numerical evidence preserved; no invented tolerance applied
    st = rt.ptb_engine.get_window(m.market_id, w["window_id"])
    assert st is not None and st.attestation is not None
    assert st.attestation.exact_diff is not None
    rec = rt.evaluate_active()
    assert "attestation_mismatch" in rec.skip_reasons


def test_late_after_seal_does_not_rewrite_k() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    w = data["windows"][0]
    rt = _runtime_for_window(w)
    m = _market(w["window_id"], w["market_id"], _ts(w["event_start"]), _ts(w["event_end"]))
    rt.open_session(slot=SessionSlot.ACTIVE, market=m, window_id=w["window_id"])
    for b in w["binance_ticks"][:1]:
        rt.ingest_binance(_bn(str(b["value"]), b["source_ts"], b["receive_wall_raw_utc"], int(b["receive_monotonic_ns"]), b["fingerprint"]))
    for t in w["chainlink_ticks"]:
        if t["fingerprint"] == "w1-cl-exact":
            rt.ingest_chainlink(
                window_id=w["window_id"],
                market_id=m.market_id,
                tick=_cl(str(t["value"]), t["source_ts"], t["receive_wall_raw_utc"], int(t["receive_monotonic_ns"]), t["fingerprint"]),
            )
    sealed = rt.attest_and_seal(market_id=m.market_id, window_id=w["window_id"])
    k0 = sealed.ptb_k
    rt.ingest_chainlink(
        window_id=w["window_id"],
        market_id=m.market_id,
        tick=_cl("1", "2026-07-20T21:15:00+00:00", "2026-07-20T21:15:30+00:00", 99, "late-conflict"),
    )
    assert rt.active.sealed.ptb_k == k0


def test_prepared_next_isolated_then_atomic_promote() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    w1, w2 = data["windows"][0], data["windows"][1]
    rt = N4ObserveRuntime.create(
        clock=FakeClock(_wall=_ts(w1["event_start"])),
        attestation_port=FixturePtbAttestationProvider(
            by_window={
                w1["window_id"]: (w1["attested_open_price"], "fx", {}),
                w2["window_id"]: (w2["attested_open_price"], "fx", {}),
            }
        ),
        basis_ewma_half_life_s=30.0,
        zgap_config=ZGapConfig(
            ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
        ),
    )
    m1 = _market(w1["window_id"], w1["market_id"], _ts(w1["event_start"]), _ts(w1["event_end"]))
    m2 = _market(w2["window_id"], w2["market_id"], _ts(w2["event_start"]), _ts(w2["event_end"]))
    a = rt.open_session(slot=SessionSlot.ACTIVE, market=m1, window_id=w1["window_id"])
    p = rt.open_session(slot=SessionSlot.PREPARED_NEXT, market=m2, window_id=w2["window_id"])
    assert p.publish_as_active is False
    # Seal both
    for w, m in ((w1, m1), (w2, m2)):
        for b in w["binance_ticks"]:
            rt.ingest_binance(_bn(str(b["value"]), b["source_ts"], b["receive_wall_raw_utc"], int(b["receive_monotonic_ns"]), b["fingerprint"]))
        for t in w["chainlink_ticks"]:
            rt.ingest_chainlink(
                window_id=w["window_id"],
                market_id=m.market_id,
                tick=_cl(str(t["value"]), t["source_ts"], t["receive_wall_raw_utc"], int(t["receive_monotonic_ns"]), t["fingerprint"]),
            )
        rt.attest_and_seal(market_id=m.market_id, window_id=w["window_id"])
    assert a.sealed.ptb_k != p.sealed.ptb_k
    # Evaluating active must not use prepared K
    rt.clock.set_utc(_ts(w1["event_start"]) + timedelta(seconds=60))
    rt.ingest_binance(_bn("65275", (_ts(w1["event_start"]) + timedelta(seconds=59)).isoformat(), (_ts(w1["event_start"]) + timedelta(seconds=60)).isoformat(), 7, "x"))
    rec = rt.evaluate_active()
    assert rec.k == str(a.sealed.ptb_k)
    promoted = rt.promote_prepared_next()
    assert promoted.window_id == w2["window_id"]
    assert rt.prepared_next is None
    assert w1["window_id"] not in rt._strategy_by_window


def test_ewma_continuity_across_windows() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rt = N4ObserveRuntime.create(
        clock=FakeClock(_wall=_ts(data["windows"][0]["event_start"])),
        basis_ewma_half_life_s=30.0,
    )
    samples = []
    for w in data["windows"][:2]:
        m = _market(w["window_id"], w["market_id"], _ts(w["event_start"]), _ts(w["event_end"]))
        slot = SessionSlot.ACTIVE if rt.active is None else SessionSlot.PREPARED_NEXT
        rt.open_session(slot=slot, market=m, window_id=w["window_id"])
        for b in w["binance_ticks"]:
            rt.ingest_binance(_bn(str(b["value"]), b["source_ts"], b["receive_wall_raw_utc"], int(b["receive_monotonic_ns"]), b["fingerprint"]))
        for t in w["chainlink_ticks"]:
            rt.ingest_chainlink(
                window_id=w["window_id"],
                market_id=m.market_id,
                tick=_cl(str(t["value"]), t["source_ts"], t["receive_wall_raw_utc"], int(t["receive_monotonic_ns"]), t["fingerprint"]),
            )
        samples.append(rt.ptb_engine.ewma.samples)
        if slot is SessionSlot.PREPARED_NEXT:
            rt.promote_prepared_next()
    assert samples[-1] > samples[0]


def test_deterministic_replay_identical_observations() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    w = data["windows"][0]

    def once() -> list[dict]:
        rt = _runtime_for_window(w)
        m = _market(w["window_id"], w["market_id"], _ts(w["event_start"]), _ts(w["event_end"]))
        sess = rt.open_session(slot=SessionSlot.ACTIVE, market=m, window_id=w["window_id"])
        sess.up_ask = Decimal("0.5")
        sess.up_bid = Decimal("0.48")
        sess.down_ask = Decimal("0.52")
        sess.down_bid = Decimal("0.5")
        for b in w["binance_ticks"]:
            rt.ingest_binance(_bn(str(b["value"]), b["source_ts"], b["receive_wall_raw_utc"], int(b["receive_monotonic_ns"]), b["fingerprint"]))
        for t in w["chainlink_ticks"]:
            rt.ingest_chainlink(
                window_id=w["window_id"],
                market_id=m.market_id,
                tick=_cl(str(t["value"]), t["source_ts"], t["receive_wall_raw_utc"], int(t["receive_monotonic_ns"]), t["fingerprint"]),
            )
        rt.attest_and_seal(
            market_id=m.market_id,
            window_id=w["window_id"],
            sealed_at=_ts(w["event_start"]) + timedelta(seconds=5),
            require_attestation_match=True,
        )
        out = []
        for i in range(2):
            rt.clock.set_utc(_ts(w["event_start"]) + timedelta(seconds=30 + i))
            rt.ingest_binance(
                _bn(str(65275 + i), (_ts(w["event_start"]) + timedelta(seconds=29 + i)).isoformat(), (_ts(w["event_start"]) + timedelta(seconds=30 + i)).isoformat(), 800 + i, f"r{i}")
            )
            out.append(rt.evaluate_active().to_dict())
        return out

    a, b = once(), once()
    # Compare stable fields (ignore run-specific correlation noise in payload)
    for x, y in zip(a, b):
        for key in ("k", "c_hat", "basis_estimate_used_ln", "alignment_mode", "decision", "skip_reasons"):
            assert x[key] == y[key]


def test_fixture_cli_offline() -> None:
    out = REPO / "var" / "reporting" / "n4" / "_pytest_fixture_summary.json"
    cmd = [
        sys.executable,
        str(REPO / "tools" / "n4_observe" / "run_n4_observe.py"),
        "--mode",
        "fixture",
        "--fixture",
        str(FIXTURE),
        "--windows",
        "2",
        "--out",
        str(out),
    ]
    proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    summary = json.loads(out.read_text(encoding="utf-8"))
    assert summary["not_live_evidence"] is True
    assert summary["oms_touched"] is False
    assert summary["orders_submitted"] == 0


def test_architecture_no_old_imports() -> None:
    paths = [
        REPO / "src" / "tyrex_pm" / "runtime" / "n4_observe_runtime.py",
        REPO / "tools" / "n4_observe" / "run_n4_observe.py",
        REPO / "src" / "tyrex_pm" / "domain" / "polymarket" / "sealed_reference.py",
    ]
    for p in paths:
        text = p.read_text(encoding="utf-8")
        assert "from old" not in text and "old." not in text
