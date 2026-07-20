"""Independent fair-value oracle for F3 golden precondition.

This module intentionally does **not** import ``tyrex_pm.indicators``.
It re-implements the frozen mathematical specification so production
``compute_fair_value`` is checked against a dual implementation, not against
its own previously written outputs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class OracleFairValue:
    z: float
    p_up: float
    p_down: float


def oracle_normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def oracle_fair_value(
    *,
    S: Decimal | str | float,
    K: Decimal | str | float,
    sigma: float,
    tau_s: float,
    tau_floor_s: float = 1.0,
) -> OracleFairValue:
    """Independent Φ(z) digital fair value (specification dual)."""
    s = float(Decimal(str(S)))
    k = float(Decimal(str(K)))
    if s <= 0 or k <= 0 or sigma <= 0 or tau_s <= 0 or tau_floor_s <= 0:
        raise ValueError("oracle inputs must be positive")
    tau_eff = max(float(tau_s), float(tau_floor_s))
    z = math.log(s / k) / (sigma * math.sqrt(tau_eff))
    p_up = oracle_normal_cdf(z)
    p_up = max(0.0, min(1.0, p_up))
    return OracleFairValue(z=z, p_up=p_up, p_down=1.0 - p_up)
