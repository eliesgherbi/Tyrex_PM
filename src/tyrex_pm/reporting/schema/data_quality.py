"""Data quality / completeness flags (SCH-05)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunDataQuality:
    """Attached to run_manifest + summary.json."""

    run_ended_cleanly: bool = True
    facts_incomplete: bool = False
    #: Framework path saw no order lifecycle / fill facts although live submit occurred.
    order_events_sparse: bool = False
    #: Legacy HTTP submit path — venue events not wired through Nautilus strategy.
    legacy_execution_truth_partial: bool = False
    #: Fraction 0..1 of orders missing guru_cid tag (detected on cache snapshot when available).
    tags_missing_rate: float | None = None
    unrealized_pnl_unavailable_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "run_ended_cleanly": self.run_ended_cleanly,
            "facts_incomplete": self.facts_incomplete,
            "order_events_sparse": self.order_events_sparse,
            "legacy_execution_truth_partial": self.legacy_execution_truth_partial,
            "tags_missing_rate": self.tags_missing_rate,
            "unrealized_pnl_unavailable_reason": self.unrealized_pnl_unavailable_reason,
        }
        d.update(self.extra)
        return d
