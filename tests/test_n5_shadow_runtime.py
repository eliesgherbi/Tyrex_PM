"""N5A: OBSERVE/SHADOW parity, lifecycle, promote-flat, labels, architecture."""

from __future__ import annotations

import ast
import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import tyrex_pm
from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import MarketId, TokenId
from tyrex_pm.core.ingress import FeedRole, IngressMeta
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.domain.polymarket.boundary_candidates import BoundaryTickView
from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketStatus, make_binary_instruments
from tyrex_pm.domain.polymarket.ptb_attestation import FixturePtbAttestationProvider
from tyrex_pm.execution.shadow_fill_model import ECONOMICS_LABEL, FILL_MODEL_DEPTH_WALK_V1
from tyrex_pm.indicators.causal_pairing import PriceTickView, TradingReferenceIdentity
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleState
from tyrex_pm.runtime.config import observe_config_from_mapping
from tyrex_pm.runtime.n4_observe_runtime import N4ObserveRuntime, SessionSlot
from tyrex_pm.runtime.n5_shadow_runtime import N5ShadowRuntime
from tyrex_pm.strategies.z_gap.config import ZGapConfig, ZGapPtbTimeQualityConfig

ROOT = Path(tyrex_pm.__file__).resolve().parents[2]
SRC = ROOT / "src" / "tyrex_pm"
CFG = ROOT / "config" / "observe_shadow_z_gap_n5a.json"
START = "2026-07-20T21:15:00+00:00"
END = "2026-07-20T21:20:00+00:00"
WID = "btc-n5-w1"
MID = "m-n5"


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _market(window_id: str = WID, market_id: str = MID) -> BinaryMarket:
    mid = MarketId(market_id)
    yes, no = make_binary_instruments(
        market_id=mid, yes_token=TokenId(f"up-{window_id}"), no_token=TokenId(f"down-{window_id}")
    )
    return BinaryMarket(
        market_id=mid,
        condition_id=window_id,
        question=window_id,
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


def _n5_cfg(tmp: Path, **shadow_overrides) -> object:
    raw = json.loads(CFG.read_text(encoding="utf-8"))
    raw["output_path"] = str(tmp / "facts.jsonl")
    raw["shadow"]["persistence_path"] = str(tmp / "state.json")
    raw["shadow"].update(shadow_overrides)
    # Fixture-only strategy knobs for entry when C_hat >> K is not required
    raw["z_gap"]["basis_max_bps"] = "10000"
    raw["z_gap"]["theta_take"] = "0.0"
    raw["z_gap"]["z_min"] = "0.0"
    return observe_config_from_mapping(raw)


def _seal_aligned(rt: N5ShadowRuntime | N4ObserveRuntime, *, k: str = "100", b: str = "99") -> None:
    m = _market()
    sess = rt.open_session(slot=SessionSlot.ACTIVE, market=m, window_id=WID)
    if hasattr(sess, "up_ask"):
        sess.up_ask, sess.up_bid = Decimal("0.36"), Decimal("0.34")
        sess.down_ask, sess.down_bid = Decimal("0.64"), Decimal("0.60")
    rt.ingest_binance(_bn(b, "2026-07-20T21:14:59.500+00:00", "2026-07-20T21:14:59.600+00:00", 1, "b0"))
    rt.ingest_chainlink(
        window_id=WID,
        market_id=m.market_id,
        tick=_cl(k, START, "2026-07-20T21:15:01+00:00", 2, "exact"),
    )
    rt.attest_and_seal(
        market_id=m.market_id,
        window_id=WID,
        sealed_at=_ts(START) + timedelta(seconds=2),
        require_attestation_match=True,
    )


def _warm_binance(rt, *, price: str = "99", n: int = 25) -> None:
    clock = rt.n4.clock if isinstance(rt, N5ShadowRuntime) else rt.clock
    for i in range(n):
        clock.set_utc(_ts(START) + timedelta(seconds=30 + i))
        rt.ingest_binance(
            _bn(
                price,
                (_ts(START) + timedelta(seconds=29 + i)).isoformat(),
                (_ts(START) + timedelta(seconds=30 + i)).isoformat(),
                20 + i,
                f"w{i}",
            )
        )


def _publish_books(rt: N5ShadowRuntime, *, ask: str = "0.36", bid: str = "0.34", depth: str = "200") -> None:
    m = rt.n4.active.market
    now = rt.n4.clock.now_utc()
    for inst, a, b in (
        (m.yes.instrument_id, ask, bid),
        (m.no.instrument_id, "0.64", "0.60"),
    ):
        rt.publish_book(
            BookSnapshot.from_levels(
                instrument_id=inst,
                ts_event=now,
                bids=[(Decimal(b), Decimal(depth))],
                asks=[(Decimal(a), Decimal(depth))],
            ),
            available_at=now,
        )


def test_observe_shadow_parity_follows_c_hat_not_raw_b(tmp_path: Path) -> None:
    """Raw B < K < C_hat → both OBSERVE and SHADOW follow C_hat (positive gap)."""
    clock = FakeClock(_wall=_ts(START))
    # OBSERVE
    n4 = N4ObserveRuntime.create(
        clock=clock,
        attestation_port=FixturePtbAttestationProvider(by_window={WID: ("100", "fixture", {})}),
        basis_ewma_half_life_s=30.0,
        zgap_config=ZGapConfig(
            ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=Decimal("10000"))
        ),
    )
    _seal_aligned(n4)
    # B=99.5 → C_hat ≈ 100.505 > K while B < K
    for i in range(25):
        clock.set_utc(_ts(START) + timedelta(seconds=40 + i))
        n4.ingest_binance(
            _bn(
                "99.5",
                (_ts(START) + timedelta(seconds=39 + i)).isoformat(),
                (_ts(START) + timedelta(seconds=40 + i)).isoformat(),
                100 + i,
                f"o{i}",
            )
        )
    clock.set_utc(_ts(START) + timedelta(seconds=65))
    n4.ingest_binance(
        _bn("99.5", "2026-07-20T21:16:04.900+00:00", "2026-07-20T21:16:05+00:00", 200, "ofinal")
    )
    obs = n4.evaluate_active()
    assert obs.kind == "evaluated"
    assert float(obs.payload["model_spot"]) > 100.0
    assert float(obs.binance_raw) < 100.0
    assert math.log(float(obs.payload["model_spot"]) / 100.0) > 0
    assert math.log(float(obs.binance_raw) / 100.0) < 0

    # SHADOW — fresh runtime, identical evidence path
    clock2 = FakeClock(_wall=_ts(START))
    cfg = _n5_cfg(tmp_path)
    n5 = N5ShadowRuntime.create(
        cfg,
        clock=clock2,
        attestation_port=FixturePtbAttestationProvider(by_window={WID: ("100", "fixture", {})}),
        basis_ewma_half_life_s=30.0,
    )
    _seal_aligned(n5)
    for i in range(25):
        clock2.set_utc(_ts(START) + timedelta(seconds=40 + i))
        n5.ingest_binance(
            _bn(
                "99.5",
                (_ts(START) + timedelta(seconds=39 + i)).isoformat(),
                (_ts(START) + timedelta(seconds=40 + i)).isoformat(),
                100 + i,
                f"s{i}",
            )
        )
    clock2.set_utc(_ts(START) + timedelta(seconds=65))
    n5.ingest_binance(
        _bn("99.5", "2026-07-20T21:16:04.900+00:00", "2026-07-20T21:16:05+00:00", 200, "sfinal")
    )
    _publish_books(n5)
    sh = n5.evaluate_shadow()
    assert sh.kind == "evaluated"
    assert sh.payload["zgap_S_equals_c_hat"] is True
    assert float(sh.payload["model_spot"]) > 100.0
    assert float(sh.payload["binance_raw"]) < 100.0
    assert math.log(float(sh.payload["model_spot"]) / 100.0) > 0
    # Same model spot as OBSERVE within float tolerance
    assert abs(float(sh.payload["model_spot"]) - float(obs.payload["model_spot"])) < 1e-6


def test_no_raw_binance_fallback_when_alignment_unavailable(tmp_path: Path) -> None:
    cfg = _n5_cfg(tmp_path)
    clock = FakeClock(_wall=_ts(START))
    n5 = N5ShadowRuntime.create(
        cfg,
        clock=clock,
        attestation_port=FixturePtbAttestationProvider(by_window={WID: ("100", "fixture", {})}),
        basis_ewma_half_life_s=30.0,
    )
    m = _market()
    n5.open_session(slot=SessionSlot.ACTIVE, market=m, window_id=WID)
    n5.ingest_chainlink(
        window_id=WID,
        market_id=m.market_id,
        tick=_cl("100", START, "2026-07-20T21:15:01+00:00", 1, "exact"),
    )
    n5.attest_and_seal(market_id=m.market_id, window_id=WID, sealed_at=_ts(START) + timedelta(seconds=2))
    n5.n4.accepted_basis.basis_ln = None
    n5.n4.accepted_basis.as_of_chainlink_source_ts = None
    clock.set_utc(_ts(START) + timedelta(seconds=30))
    n5.ingest_binance(_bn("99", "2026-07-20T21:15:29+00:00", "2026-07-20T21:15:30+00:00", 9, "only-b"))
    rec = n5.evaluate_shadow()
    assert rec.kind == "skipped"
    assert "basis_estimate_unavailable" in rec.skip_reasons
    assert n5.host.portfolio.is_flat()
    assert n5.orders_submitted == 0


def test_economic_labels_and_fill_model(tmp_path: Path) -> None:
    cfg = _n5_cfg(tmp_path)
    n5 = N5ShadowRuntime.create(
        cfg,
        clock=FakeClock(_wall=_ts(START)),
        attestation_port=FixturePtbAttestationProvider(by_window={WID: ("100", "fixture", {})}),
        basis_ewma_half_life_s=30.0,
    )
    _seal_aligned(n5)
    _warm_binance(n5)
    _publish_books(n5)
    rec = n5.evaluate_shadow()
    assert rec.payload["runtime_mode"] == RuntimeMode.SHADOW.value
    assert rec.payload["fill_model_id"] == FILL_MODEL_DEPTH_WALK_V1
    assert rec.payload["economics_label"] == ECONOMICS_LABEL
    assert rec.payload["fees_label"] == "estimated"
    assert rec.payload["pnl_label"] == "simulated_shadow_pnl"
    assert rec.payload["sigma_source"] == "binance_raw_returns"


def test_promote_requires_flat(tmp_path: Path) -> None:
    """Non-flat portfolio blocks prepared-next promotion with an operator reason."""
    cfg = _n5_cfg(tmp_path)
    clock = FakeClock(_wall=_ts(START))
    n5 = N5ShadowRuntime.create(
        cfg,
        clock=clock,
        attestation_port=FixturePtbAttestationProvider(
            by_window={WID: ("100", "fixture", {}), "btc-n5-w2": ("101", "fixture", {})}
        ),
        basis_ewma_half_life_s=30.0,
    )
    _seal_aligned(n5)
    m2 = _market(window_id="btc-n5-w2", market_id="m-n5-2")
    n5.open_session(slot=SessionSlot.PREPARED_NEXT, market=m2, window_id="btc-n5-w2")
    n5.host.portfolio.is_flat = lambda: False  # type: ignore[method-assign]
    blocked = n5.promote_prepared_next()
    assert getattr(blocked, "kind", None) == "promotion_blocked"
    assert "portfolio_not_flat" in blocked.skip_reasons

    # Flat + lifecycle FLAT → promote succeeds
    n5.host.portfolio.is_flat = lambda: True  # type: ignore[method-assign]
    assert n5.host.lifecycle.state is LifecycleState.FLAT
    promoted = n5.promote_prepared_next()
    assert getattr(promoted, "window_id", None) == "btc-n5-w2"
    assert n5.n4.active is not None
    assert n5.n4.active.window_id == "btc-n5-w2"


def test_unknown_inventory_blocks_sell(tmp_path: Path) -> None:
    cfg = _n5_cfg(tmp_path)
    n5 = N5ShadowRuntime.create(
        cfg,
        clock=FakeClock(_wall=_ts(START)),
        attestation_port=FixturePtbAttestationProvider(by_window={WID: ("100", "fixture", {})}),
        basis_ewma_half_life_s=30.0,
    )
    _seal_aligned(n5)
    _warm_binance(n5)
    _publish_books(n5)
    n5.mark_unknown_inventory(True)
    before = n5.orders_submitted
    n5.evaluate_shadow()
    # No guessed sells while UNKNOWN
    assert n5.host.lifecycle.state is LifecycleState.FLAT or n5.orders_submitted == before


def test_flat_vs_active_degradation(tmp_path: Path) -> None:
    """FLAT: missing alignment blocks entry. ACTIVE path uses host lifecycle gates."""
    cfg = _n5_cfg(tmp_path)
    clock = FakeClock(_wall=_ts(START))
    n5 = N5ShadowRuntime.create(
        cfg,
        clock=clock,
        attestation_port=FixturePtbAttestationProvider(by_window={WID: ("100", "fixture", {})}),
        basis_ewma_half_life_s=30.0,
    )
    m = _market()
    n5.open_session(slot=SessionSlot.ACTIVE, market=m, window_id=WID)
    # No seal → skip while FLAT
    rec = n5.evaluate_shadow()
    assert rec.kind == "skipped"
    assert n5.host.portfolio.is_flat()


def test_persistence_roundtrip(tmp_path: Path) -> None:
    cfg = _n5_cfg(tmp_path)
    clock = FakeClock(_wall=_ts(START))
    n5 = N5ShadowRuntime.create(
        cfg,
        clock=clock,
        attestation_port=FixturePtbAttestationProvider(by_window={WID: ("100", "fixture", {})}),
        basis_ewma_half_life_s=30.0,
    )
    _seal_aligned(n5)
    _warm_binance(n5)
    _publish_books(n5)
    n5.evaluate_shadow()
    n5.persist()
    assert (tmp_path / "state.json").exists()
    payload = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert payload["runtime_mode"] == "SHADOW"
    assert payload.get("n5_sessions", {}).get("fill_model_id") == FILL_MODEL_DEPTH_WALK_V1
    assert payload.get("n5_sessions", {}).get("active_window_id") == WID


def test_no_liveoms_on_n5_decision_path() -> None:
    text = (SRC / "runtime" / "n5_shadow_runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    assert not any("live_oms" in m for m in imports)
    assert not any(m.startswith("old") for m in imports)
    assert "tyrex_pm.strategies.z_gap.fair_value" not in imports
    assert "tyrex_pm.strategies.z_gap.assemble" not in imports


def test_fixture_cli_smoke(tmp_path: Path) -> None:
    import subprocess
    import sys

    out = tmp_path / "summary.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "n5_shadow" / "run_n5_shadow.py"),
            "--mode",
            "fixture",
            "--config",
            str(CFG),
            "--out",
            str(out),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(out.read_text(encoding="utf-8"))
    assert summary["mode"] == "fixture"
    assert summary.get("not_live_evidence") is True
    assert summary.get("fill_model_id") == FILL_MODEL_DEPTH_WALK_V1
    assert summary.get("orders_live") in (0, None) or summary.get("live_oms") is False
