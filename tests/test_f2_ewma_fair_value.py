"""F2: EWMA volatility, binary fair value goldens, basis.

F3 precondition: golden expectations are anchored by an independent oracle
(``tests/oracles/binary_fair_value_oracle.py``), not by writing F2 outputs
back into the fixture.
"""

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

# Independent dual oracle (not imported from tyrex_pm.indicators).
# Canonical copy also lives at tests/oracles/binary_fair_value_oracle.py.


def _oracle_normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def oracle_fair_value(
    *,
    S: Decimal | str | float,
    K: Decimal | str | float,
    sigma: float,
    tau_s: float,
    tau_floor_s: float = 1.0,
):
    s = float(Decimal(str(S)))
    k = float(Decimal(str(K)))
    tau_eff = max(float(tau_s), float(tau_floor_s))
    z = math.log(s / k) / (sigma * math.sqrt(tau_eff))
    p_up = max(0.0, min(1.0, _oracle_normal_cdf(z)))
    return type("OracleFV", (), {"z": z, "p_up": p_up, "p_down": 1.0 - p_up})()


ROOT = Path(tyrex_pm.__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "fixtures" / "z_gap" / "fair_value_golden.json"
TS0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)

# Legacy fixture used a slightly different rounded CDF; z stays tight.
_LEGACY_Z_TOL = 1e-6
_LEGACY_P_TOL = 1e-3


def test_ewma_warmup_units_and_readiness() -> None:
    est = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=5.0, sample_interval_s=1.0))
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
    for i in range(6):
        est.update(Decimal("100") + Decimal("0.001") * i, TS0 + timedelta(seconds=i))
    assert est.snapshot().ready
    jumped = est.update(Decimal("200"), TS0 + timedelta(seconds=10))
    assert jumped.jump_guard_tripped
    assert not jumped.ready
    assert jumped.reject_reason == "jump_guard_tripped"

    est2 = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=2.0))
    seed = est2.seed_observations(
        [
            (Decimal("100"), TS0),
            (Decimal("100"), TS0),
            (Decimal("100.1"), TS0 + timedelta(seconds=1)),
            (Decimal("100.2"), TS0 + timedelta(seconds=2)),
            (Decimal("100.3"), TS0 + timedelta(hours=1)),
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


def test_fair_value_independent_oracle_parity() -> None:
    """Production compute_fair_value must match the independent dual oracle."""
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

        oracle = oracle_fair_value(
            S=case["S"],
            K=case["K"],
            sigma=float(case["sigma"]),
            tau_s=float(case["tau_s"]),
            tau_floor_s=float(case.get("tau_floor_s", 1)),
        )
        assert out.z is not None and out.p_up is not None and out.p_down is not None
        assert abs(out.z - oracle.z) < 1e-12, case["name"]
        assert abs(out.p_up - oracle.p_up) < 1e-12, case["name"]
        assert abs(out.p_down - oracle.p_down) < 1e-12, case["name"]
        assert abs(out.p_up + out.p_down - 1.0) < 1e-12

        # Soft lock vs legacy rounded vectors (not the production oracle).
        assert abs(out.z - case["legacy_expected_z"]) < _LEGACY_Z_TOL, case["name"]
        assert abs(out.p_up - case["legacy_expected_p_up"]) < _LEGACY_P_TOL, case["name"]
        assert abs(out.p_down - case["legacy_expected_p_down"]) < _LEGACY_P_TOL, case["name"]


def test_fair_value_analytical_s_equals_k() -> None:
    out = compute_fair_value(
        FairValueInput(
            S=Decimal("100"),
            K=Decimal("100"),
            sigma=0.01,
            tau_s=100,
            snapshot_ts=TS0,
        )
    )
    assert out.model_status == "ready"
    assert out.z == 0.0
    assert out.p_up == 0.5
    assert out.p_down == 0.5


def test_fair_value_complement_and_monotonicity() -> None:
    low = compute_fair_value(
        FairValueInput(
            S=Decimal("95"), K=Decimal("100"), sigma=0.01, tau_s=100, snapshot_ts=TS0
        )
    )
    high = compute_fair_value(
        FairValueInput(
            S=Decimal("110"), K=Decimal("100"), sigma=0.01, tau_s=100, snapshot_ts=TS0
        )
    )
    assert low.p_up is not None and high.p_up is not None
    assert high.p_up > low.p_up
    assert abs(low.p_up + low.p_down - 1.0) < 1e-12
    assert abs(high.p_up + high.p_down - 1.0) < 1e-12


def test_fair_value_tau_floor_and_extreme_z() -> None:
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
    oracle = oracle_fair_value(
        S="110", K="100", sigma=0.01, tau_s=0.1, tau_floor_s=1.0
    )
    assert abs(out.z - oracle.z) < 1e-12
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


def test_normal_cdf_midpoint() -> None:
    assert abs(normal_cdf(0.0) - 0.5) < 1e-12


def test_oracle_does_not_import_indicators() -> None:
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parents[0] / "oracles" / "binary_fair_value_oracle.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("tyrex_pm.indicators")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("tyrex_pm.indicators")


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
    wide = compute_basis_bps(trading_ref="110", settlement_ref="100")
    assert wide.ready and wide.basis_bps == Decimal("1000")
