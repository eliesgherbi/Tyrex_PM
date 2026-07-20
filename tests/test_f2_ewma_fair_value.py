"""F2: EWMA volatility, binary fair value goldens, basis."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import tyrex_pm
from tyrex_pm.indicators.binary_fair_value import FairValueInput, compute_fair_value, normal_cdf
from tyrex_pm.indicators.ewma_volatility import EwmaVolatilityEstimator, SigmaConfig
from tyrex_pm.indicators.reference_basis import BasisValidity, compute_basis_bps

ROOT = Path(tyrex_pm.__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "fixtures" / "z_gap" / "fair_value_golden.json"
TS0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def test_ewma_warmup_units_and_readiness() -> None:
    est = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=5.0, sample_interval_s=1.0))
    # Seed steadily increasing prices over 10s
    for i in range(12):
        px = Decimal("100") + Decimal(i) * Decimal("0.01")
        snap = est.update(px, TS0 + timedelta(seconds=i))
    assert snap.sigma_units == "per_sqrt_second"
    assert snap.ready
    assert snap.sigma is not None and snap.sigma > 0
    assert snap.effective_samples_s >= 5.0


def test_ewma_zero_return_and_deterministic_replay() -> None:
    cfg = SigmaConfig(min_samples_s=3.0, sample_interval_s=1.0)
    a = EwmaVolatilityEstimator(cfg)
    b = EwmaVolatilityEstimator(cfg)
    obs = [(Decimal("100"), TS0 + timedelta(seconds=i)) for i in range(8)]
    # Add a move then flat
    obs[3] = (Decimal("100.5"), TS0 + timedelta(seconds=3))
    for px, ts in obs:
        a.update(px, ts)
        b.update(px, ts)
    assert a.snapshot().sigma == b.snapshot().sigma
    assert a.snapshot().effective_samples_s == b.snapshot().effective_samples_s


def test_ewma_jump_guard_and_seed() -> None:
    est = EwmaVolatilityEstimator(
        SigmaConfig(min_samples_s=2.0, sample_interval_s=1.0, jump_threshold_sigma=4.0)
    )
    # Warm with tiny moves
    for i in range(6):
        est.update(Decimal("100") + Decimal("0.001") * i, TS0 + timedelta(seconds=i))
    assert est.snapshot().ready
    # Huge jump
    jumped = est.update(Decimal("200"), TS0 + timedelta(seconds=10))
    assert jumped.jump_guard_tripped
    assert not jumped.ready
    assert jumped.reject_reason == "jump_guard_tripped"

    est2 = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=2.0))
    seed = est2.seed_observations(
        [
            (Decimal("100"), TS0),
            (Decimal("100"), TS0),  # duplicate
            (Decimal("100.1"), TS0 + timedelta(seconds=1)),
            (Decimal("100.2"), TS0 + timedelta(seconds=2)),
            (Decimal("100.3"), TS0 + timedelta(hours=1)),  # future vs now
        ],
        now_ts=TS0 + timedelta(seconds=5),
    )
    assert seed.rejected_duplicate >= 1
    assert seed.rejected_future >= 1
    assert seed.accepted >= 2


def test_ewma_lambda_decay() -> None:
    cfg = SigmaConfig(half_life_s=30.0)
    lam = cfg.ewma_lambda()
    assert abs(lam - math.exp(-math.log(2.0) / 30.0)) < 1e-12


def test_fair_value_golden_parity() -> None:
    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert "tests/fixtures/z_gap" in GOLDEN.as_posix()
    assert "old/" not in GOLDEN.as_posix()

    for case in cases:
        inp = FairValueInput(
            S=None if case["S"] is None else Decimal(str(case["S"])),
            K=None if case["K"] is None else Decimal(str(case["K"])),
            sigma=case.get("sigma"),
            tau_s=case.get("tau_s"),
            tau_floor_s=float(case.get("tau_floor_s", 1)),
            snapshot_ts=TS0,
        )
        out = compute_fair_value(inp)
        assert out.model_status == case["model_status"], case["name"]
        if case["model_status"] == "not_ready":
            assert out.reject_reason == case["reject_reason"], case["name"]
            continue
        assert out.z is not None and out.p_up is not None and out.p_down is not None
        assert abs(out.z - case["expected_z"]) < 1e-12, case["name"]
        assert abs(out.p_up - case["expected_p_up"]) < 1e-12, case["name"]
        assert abs(out.p_down - case["expected_p_down"]) < 1e-12, case["name"]
        assert abs(out.p_up + out.p_down - 1.0) < 1e-12


def test_fair_value_tau_floor_and_extreme_z() -> None:
    # tau=0.1 with floor=1 should use tau_eff=1
    out = compute_fair_value(
        FairValueInput(
            S=Decimal("110"),
            K=Decimal("100"),
            sigma=0.01,
            tau_s=0.1,
            tau_floor_s=1.0,
            snapshot_ts=TS0,
        )
    )
    assert out.model_status == "ready"
    # Extreme separation
    extreme = compute_fair_value(
        FairValueInput(
            S=Decimal("1000"),
            K=Decimal("1"),
            sigma=0.01,
            tau_s=100,
            snapshot_ts=TS0,
        )
    )
    assert extreme.model_status == "ready"
    assert extreme.p_up is not None and 0.0 <= extreme.p_up <= 1.0
    assert abs(extreme.p_up + extreme.p_down - 1.0) < 1e-12


def test_normal_cdf_midpoint() -> None:
    assert abs(normal_cdf(0.0) - 0.5) < 1e-12


def test_basis_sign_units_freshness() -> None:
    pos = compute_basis_bps(trading_ref="101", settlement_ref="100")
    assert pos.ready and pos.basis_bps == Decimal("100")
    neg = compute_basis_bps(trading_ref="99", settlement_ref="100")
    assert neg.basis_bps == Decimal("-100")
    stale = compute_basis_bps(
        trading_ref="101", settlement_ref="100", settlement_ref_fresh=False
    )
    assert stale.validity is BasisValidity.STALE
    assert not stale.ready
    # Threshold is policy-owned — indicator does not reject on magnitude
    wide = compute_basis_bps(trading_ref="110", settlement_ref="100")
    assert wide.ready and wide.basis_bps == Decimal("1000")
