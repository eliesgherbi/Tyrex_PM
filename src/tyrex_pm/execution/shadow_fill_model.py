"""SHADOW fill-model identifiers and assumption records (no venue mutation)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# Provisional N5 fill model — fixture/replay parameters are not production defaults.
FILL_MODEL_DEPTH_WALK_V1 = "shadow_depth_walk_v1"
FILL_MODEL_LEGACY_IMMEDIATE = "shadow_immediate_visible_depth_v0"

ECONOMICS_LABEL = "simulated_shadow"
FEES_LABEL = "estimated"
PNL_LABEL = "simulated_shadow_pnl"


@dataclass(frozen=True, kw_only=True)
class DepthWalkAssumptions:
    """Assumptions recorded on every depth-walk fill / no-fill fact."""

    fill_model_id: str = FILL_MODEL_DEPTH_WALK_V1
    latency_ms: float = 0.0
    extra_slip_ticks: Decimal = Decimal("0")
    tick_size: Decimal = Decimal("0.01")
    queue_priority_claimed: bool = False
    look_ahead_books: bool = False
    economics_label: str = ECONOMICS_LABEL
    fees_label: str = FEES_LABEL

    def to_fact_dict(self) -> dict[str, object]:
        return {
            "fill_model_id": self.fill_model_id,
            "latency_ms": self.latency_ms,
            "extra_slip_ticks": str(self.extra_slip_ticks),
            "tick_size": str(self.tick_size),
            "queue_priority_claimed": self.queue_priority_claimed,
            "look_ahead_books": self.look_ahead_books,
            "economics_label": self.economics_label,
            "fees_label": self.fees_label,
            "pnl_label": PNL_LABEL,
        }
