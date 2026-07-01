"""Decision freshness helpers for paired-binary activation and M8 analysis."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.execution.planner import build_quality_gate_from_config
from tyrex_pm.market_data.quality import (
    DataQualityReport,
    DecisionContext,
    EnforcementMode,
    QualityVerdict,
)
from tyrex_pm.runtime.config import AppConfig, PairedBinaryStrategyConfig


STRICT_DECISION_CATEGORIES = frozenset({"ENTRY", "TAKE_PROFIT", "ACTIVATION"})
RISK_REDUCTION_CATEGORIES = frozenset({"STOP", "URGENT_EXIT", "TIMEOUT_EXIT"})


@dataclass(frozen=True)
class PairFreshnessResult:
    fresh: bool
    report: DataQualityReport | None
    block_reason: str | None = None


def evaluate_pair_freshness(
    *,
    app: AppConfig,
    coord,
    cfg: PairedBinaryStrategyConfig,
    context: DecisionContext,
    size: Decimal,
) -> PairFreshnessResult:
    gate = build_quality_gate_from_config(app)
    store = coord.market_state
    if store is None or not hasattr(store, "capture_pair"):
        return PairFreshnessResult(False, None, "missing_market_state")
    pair = store.capture_pair(TokenId(cfg.yes_token_id), TokenId(cfg.no_token_id), "freshness_gate")
    report = gate.evaluate_pair(pair, context=context, size=size)
    enforce = app.runtime.market_data.quality.enforcement_mode == EnforcementMode.ENFORCE.value
    primary = bool(app.runtime.market_data.websocket.primary_enabled)
    if context in (DecisionContext.ENTRY, DecisionContext.ACTIVATION, DecisionContext.TAKE_PROFIT):
        if primary and report.verdict != QualityVerdict.PASS:
            return PairFreshnessResult(False, report, "ws_primary_requires_pass")
        if enforce and not gate.allows_decision(report, context):
            return PairFreshnessResult(False, report, "quality_gate_reject")
        return PairFreshnessResult(report.verdict == QualityVerdict.PASS, report)
    if enforce and not gate.allows_decision(report, context):
        return PairFreshnessResult(False, report, "quality_gate_reject")
    return PairFreshnessResult(True, report)


def activation_may_proceed(
    *,
    app: AppConfig,
    coord,
    cfg: PairedBinaryStrategyConfig,
    size: Decimal,
) -> PairFreshnessResult:
    return evaluate_pair_freshness(
        app=app,
        coord=coord,
        cfg=cfg,
        context=DecisionContext.ACTIVATION,
        size=size,
    )


def snapshot_analyzer_category(decision_type: str, *, trigger_type: str | None = None) -> str:
    dt = decision_type.lower()
    if dt in ("entry_eval", "entry"):
        return "ENTRY"
    if dt == "activation":
        return "ACTIVATION"
    if dt in ("take_profit_trigger", "take_profit"):
        return "TAKE_PROFIT"
    if dt in ("stop_trigger", "stop_loss", "stop"):
        return "STOP"
    if dt in ("timeout_exit", "timeout"):
        return "TIMEOUT_EXIT"
    if trigger_type == "timeout":
        return "TIMEOUT_EXIT"
    if dt in ("urgent_exit", "exit_submit", "fak_retry"):
        if trigger_type in ("timeout",):
            return "TIMEOUT_EXIT"
        if trigger_type in ("stop_loss", "stop"):
            return "STOP"
        if trigger_type in ("take_profit",):
            return "TAKE_PROFIT"
        return "URGENT_EXIT"
    return dt.upper()


def risk_reduction_verdict_ok(report: dict, *, emergency_max_age_ms: int = 3000) -> bool:
    verdict = str(report.get("verdict", ""))
    age = report.get("book_age_ms")
    src = str(report.get("source") or "")
    sq = str(report.get("source_quality") or "")
    if age is not None and int(age) > emergency_max_age_ms:
        return False
    if verdict == "pass":
        return True
    if verdict == "degraded":
        return sq == "ws_primary" or src == "websocket"
    if verdict == "emergency_only":
        if not report.get("emergency_reason"):
            return False
        return sq == "ws_primary" or src in ("websocket", "rest_recovery")
    return False
