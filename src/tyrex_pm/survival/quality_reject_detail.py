"""Quality-gate reject diagnostics for survival exit planning."""

from __future__ import annotations

from tyrex_pm.market_data.quality import DataQualityReport

_FRESHNESS_REASONS = frozenset(
    {
        "book_age_reject",
        "book_age_emergency",
        "book_age_degraded",
        "missing_snapshot",
        "missing_bid_or_ask",
    }
)
_SPREAD_REASONS = frozenset({"spread_exceeds_max", "missing_spread"})
_DEPTH_REASONS = frozenset({"insufficient_depth"})
_SEQUENCE_GAP_REASONS = frozenset({"reconnect_gap"})


def build_quality_reject_detail(report: DataQualityReport) -> dict[str, bool]:
    """Map gate report reasons to explicit failure categories for tuning."""
    reasons = set(report.reasons)
    return {
        "failed_freshness": bool(reasons & _FRESHNESS_REASONS),
        "failed_spread": bool(reasons & _SPREAD_REASONS),
        "failed_depth": bool(reasons & _DEPTH_REASONS),
        "failed_sequence_gap": bool(reasons & _SEQUENCE_GAP_REASONS),
    }
