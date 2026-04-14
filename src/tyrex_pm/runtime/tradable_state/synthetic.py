"""WP4 — explicit, truthful health snapshots for reporting joinability (no policy side effects)."""

from __future__ import annotations

from datetime import UTC, datetime

from tyrex_pm.runtime.tradable_state.types import TradableStateHealth, TradableStateHealthSnapshot


def synthetic_snapshot_health_source_missing(
    *,
    observed_at_utc: datetime | None = None,
) -> TradableStateHealthSnapshot:
    """
    Minimal snapshot when the tradable health gate is on but no producer is wired.

    ``UNKNOWN_BOOTSTRAP`` matches the risk deny path (``RISK_HEALTH_UNKNOWN_BOOTSTRAP``);
    ``reason_code`` is explicit for operators (not framework-derived health).
    """
    return TradableStateHealthSnapshot(
        level=TradableStateHealth.UNKNOWN_BOOTSTRAP,
        reason_code="health_source_missing",
        observed_at_utc=observed_at_utc or datetime.now(tz=UTC),
        framework_detail=None,
    )
