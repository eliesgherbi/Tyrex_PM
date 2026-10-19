"""Feature readiness required before an entry is sent (one rule for live and replay)."""

from __future__ import annotations

from typing import Any, Iterable

EWMA_NOT_READY = "EWMA_NOT_READY"


def required_feature_specs(specs: Iterable[Any]) -> tuple[Any, ...]:
    """Indicator specs whose readiness gates an entry (today: ``ewma_vol``)."""
    return tuple(spec for spec in specs if spec.name == "ewma_vol")


def features_ready_reason(graph: Any, required: Iterable[Any]) -> bool | str:
    """True when every required feature is ready, else the refusal reason.

    Uses the graph snapshot (freshness included), exactly as the live final gate does.
    """
    required = tuple(required)
    if not required:
        return True
    if graph is None:
        return EWMA_NOT_READY
    bundle = graph.snapshot()
    for spec in required:
        snap = bundle.get(spec.instance_id)
        if snap is None or not bool(getattr(snap, "ready", False)):
            return EWMA_NOT_READY
    return True
