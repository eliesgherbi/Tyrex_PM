"""Model/market disagreement: one pure rule shared by the strategy and the pre-send gate.

``gap_pp = 100 * |p_up - mid_up|`` in percentage points of probability, where
``mid_up`` is the UP leg's two-sided midpoint. Invalid inputs (missing, non-finite,
outside [0, 1], crossed or one-sided book) are reported as such, never treated as
agreement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

VETO = "DISAGREEMENT_VETO"
INPUT_INVALID = "DISAGREEMENT_INPUT_INVALID"


@dataclass(frozen=True)
class DisagreementCheck:
    reason: str | None  # None = within the limit
    gap_pp: float | None
    mid_up: float | None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def check_disagreement(*, p_up: Any, up_bid: Any, up_ask: Any,
                       max_gap_pp: float) -> DisagreementCheck:
    p, bid, ask = _number(p_up), _number(up_bid), _number(up_ask)
    if p is None or bid is None or ask is None:
        return DisagreementCheck(INPUT_INVALID, None, None)
    if not (0 <= p <= 1 and 0 <= bid <= ask <= 1):
        return DisagreementCheck(INPUT_INVALID, None, None)
    mid = (bid + ask) / 2
    gap = 100 * abs(p - mid)
    return DisagreementCheck(VETO if gap > max_gap_pp else None, gap, mid)
