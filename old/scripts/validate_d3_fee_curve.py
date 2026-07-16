#!/usr/bin/env python3
"""D3.a fee-curve validation artifact generator."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from tyrex_pm.quant.fees import parse_fee_model_from_raw, phi_taker_fee

LIVE_FD_RAW = {"c": "0x507f97b1", "fd": {"r": 0.07, "e": 1, "to": True}}
PRODUCTION_BTC_FD = {"c": "0xabc", "fd": {"r": 0.07, "e": 1, "to": True}}
GRID = [Decimal(str(p)) for p in [
    "0.0", "0.05", "0.10", "0.15", "0.20", "0.25", "0.30", "0.35", "0.40", "0.45",
    "0.50", "0.55", "0.60", "0.65", "0.70", "0.75", "0.80", "0.85", "0.90", "0.95", "1.0",
]]


def main() -> int:
    model = parse_fee_model_from_raw(LIVE_FD_RAW, condition_id="0x507f97b1", market_id="btc_5m_test")
    prod = parse_fee_model_from_raw(PRODUCTION_BTC_FD, condition_id="0xabc", market_id="btc_5m")
    curve = {str(p): str(phi_taker_fee(p, model)) for p in GRID}

    increasing_0_50 = all(
        phi_taker_fee(GRID[i], model) <= phi_taker_fee(GRID[i + 1], model)
        for i in range(len(GRID) - 1)
        if GRID[i] < Decimal("0.5") <= GRID[i + 1] or (GRID[i] < Decimal("0.5") and GRID[i + 1] <= Decimal("0.5"))
    )
    # explicit checks on grid
    inc_left = all(phi_taker_fee(GRID[i], model) < phi_taker_fee(GRID[i + 1], model) for i in range(10))
    dec_right = all(phi_taker_fee(GRID[i], model) > phi_taker_fee(GRID[i + 1], model) for i in range(10, 20))
    sym = all(phi_taker_fee(p, model) == phi_taker_fee(Decimal("1") - p, model) for p in GRID if p <= Decimal("0.5"))
    peak = phi_taker_fee(Decimal("0.5"), model)
    peak_ok = peak >= phi_taker_fee(Decimal("0.1"), model) and peak >= phi_taker_fee(Decimal("0.9"), model)
    boundary_zero = phi_taker_fee(Decimal("0"), model) == Decimal("0") and phi_taker_fee(Decimal("1"), model) == Decimal("0")

    report = {
        "model_id": model.fee_model_id,
        "fd_r": str(model.fd_r),
        "fd_e": str(model.fd_e),
        "fd_to": model.fd_to,
        "grid_points": len(GRID),
        "phi_grid": curve,
        "monotonic_increasing_0_to_0_5": inc_left,
        "monotonic_decreasing_0_5_to_1": dec_right,
        "symmetry_phi_p_equals_phi_1_minus_p": sym,
        "maximum_at_p_0_5": peak_ok,
        "phi_0_and_1_are_zero": boundary_zero,
        "production_btc_phi_at_0_5": str(phi_taker_fee(Decimal("0.5"), prod)),
        "all_checks_pass": inc_left and dec_right and sym and peak_ok and boundary_zero,
    }
    out = Path("var/reporting/z_gap/d3_fee_curve_validation.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["all_checks_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
