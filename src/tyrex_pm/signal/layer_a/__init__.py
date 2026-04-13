"""Layer A — composable guru signal filters (gating + exit interpretation)."""

from __future__ import annotations

from tyrex_pm.config.loaders import LayerAFiltersSettings
from tyrex_pm.signal.layer_a.orchestrator import LayerAOrchestrator
from tyrex_pm.signal.layer_a.filters.exit_interpretation import ExitInterpretationFilter
from tyrex_pm.signal.layer_a.filters.significance_conviction import SignificanceConvictionFilter
from tyrex_pm.signal.layer_a.filters.static_amount import StaticAmountGatingFilter
from tyrex_pm.signal.layer_a.filters.token_allowlist import TokenAllowlistGatingFilter
from tyrex_pm.signal.layer_a.types import (
    Branch,
    LayerAContext,
    LayerAOutcome,
    LayerAStepRecord,
    json_safe_metadata,
)
from tyrex_pm.signal.token_filter_spec import TokenFilterSpec


def build_layer_a_orchestrator(
    layer_a: LayerAFiltersSettings,
    token_spec: TokenFilterSpec,
) -> LayerAOrchestrator:
    significance = SignificanceConvictionFilter(
        layer_a.significance_filter.significance_conviction,
    )
    return LayerAOrchestrator(
        token=TokenAllowlistGatingFilter(token_spec),
        static=StaticAmountGatingFilter(layer_a.significance_filter.static_amount),
        significance=significance,
        exit_interpretation=ExitInterpretationFilter(layer_a.exit_filter),
    )


__all__ = [
    "Branch",
    "LayerAContext",
    "LayerAOutcome",
    "LayerAStepRecord",
    "LayerAOrchestrator",
    "build_layer_a_orchestrator",
    "json_safe_metadata",
]
