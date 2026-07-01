"""Paired-binary decision gates for WS-primary + quality enforcement (M8)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.execution.planner import build_quality_gate_from_config
from tyrex_pm.market_data.quality import DecisionContext, EnforcementMode
from tyrex_pm.market_data.readiness import MarketReadinessTracker
from tyrex_pm.runtime.config import AppConfig, PairedBinaryStrategyConfig


@dataclass(frozen=True)
class DecisionGateResult:
    allowed: bool
    block_reason: str | None = None
    readiness_state: str | None = None
    quality_verdict: str | None = None
    quality_reasons: tuple[str, ...] = ()


def ws_primary_enabled(app: AppConfig) -> bool:
    return bool(app.runtime.market_data.websocket.primary_enabled)


def quality_enforcement_active(app: AppConfig) -> bool:
    return app.runtime.market_data.quality.enforcement_mode == EnforcementMode.ENFORCE.value


def should_block_paired_binary_decision(
    *,
    app: AppConfig,
    coord,
    cfg: PairedBinaryStrategyConfig,
    context: DecisionContext,
    size: Decimal,
) -> DecisionGateResult:
    """Return block result when WS-primary / quality / readiness policy forbids action."""
    gate = build_quality_gate_from_config(app)
    enforce = quality_enforcement_active(app)
    primary = ws_primary_enabled(app)
    tracker: MarketReadinessTracker | None = getattr(coord, "market_readiness_tracker", None)

    if primary and context in (DecisionContext.ENTRY, DecisionContext.ACTIVATION, DecisionContext.TAKE_PROFIT):
        if tracker is not None and not tracker.allows_new_entries():
            return DecisionGateResult(
                allowed=False,
                block_reason="readiness_not_trading_enabled",
                readiness_state=tracker.state.value,
            )
        if tracker is not None and tracker.reconnect_gap:
            return DecisionGateResult(
                allowed=False,
                block_reason="reconnect_gap",
                readiness_state=tracker.state.value,
            )

    store = coord.market_state
    if store is None or not hasattr(store, "capture_pair"):
        if enforce or (primary and context in (DecisionContext.ENTRY, DecisionContext.TAKE_PROFIT)):
            return DecisionGateResult(allowed=False, block_reason="missing_market_state")
        return DecisionGateResult(allowed=True)

    pair_id = "paired_binary_gate"
    pair = store.capture_pair(TokenId(cfg.yes_token_id), TokenId(cfg.no_token_id), pair_id)
    report = gate.evaluate_pair(pair, context=context, size=size)

    if enforce and not gate.allows_decision(report, context):
        return DecisionGateResult(
            allowed=False,
            block_reason="quality_gate_reject",
            quality_verdict=report.verdict.value,
            quality_reasons=report.reasons,
            readiness_state=tracker.state.value if tracker is not None else None,
        )

    if primary and context in (DecisionContext.ENTRY, DecisionContext.ACTIVATION, DecisionContext.TAKE_PROFIT):
        if report.verdict.value != "pass":
            return DecisionGateResult(
                allowed=False,
                block_reason="ws_primary_requires_pass",
                quality_verdict=report.verdict.value,
                quality_reasons=report.reasons,
                readiness_state=tracker.state.value if tracker is not None else None,
            )

    return DecisionGateResult(
        allowed=True,
        quality_verdict=report.verdict.value,
        readiness_state=tracker.state.value if tracker is not None else None,
    )


def rest_poll_should_run(app: AppConfig, *, ws_connected: bool = False) -> bool:
    """REST poll runs only when explicitly enabled; disabled in healthy WS-primary mode."""
    rest = app.runtime.market_data.rest
    if not rest.poll_enabled:
        return False
    if ws_primary_enabled(app) and ws_connected:
        return False
    return True
