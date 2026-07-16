"""DataQualityGate — central freshness/source/depth policy (Phase 2 M3).

In ``observe_only`` enforcement mode the gate evaluates and reports verdicts but
does not block live decisions (REST remains authoritative until M8). Tests and
``enforce`` mode apply the exact M8 rules documented in milestone_3.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from tyrex_pm.market_data.models import (
    BookSource,
    MarketStateSnapshot,
    PairMarketSnapshot,
    SourceQuality,
)
from tyrex_pm.market_data.features import _depth_at_size


class DecisionContext(str, Enum):
    ENTRY = "entry"
    ACTIVATION = "activation"
    TAKE_PROFIT = "take_profit"
    STOP = "stop"
    URGENT_EXIT = "urgent_exit"


class QualityVerdict(str, Enum):
    PASS = "pass"
    DEGRADED = "degraded"
    EMERGENCY_ONLY = "emergency_only"
    REJECT_DECISION = "reject_decision"


class EnforcementMode(str, Enum):
    OBSERVE_ONLY = "observe_only"
    ENFORCE = "enforce"


@dataclass(frozen=True)
class MarketProfileThresholds:
    pass_max_age_ms: int = 750
    reject_max_age_ms: int = 1500
    emergency_max_age_ms: int = 3000
    max_spread: Decimal = Decimal("0.15")
    min_depth_at_size: Decimal = Decimal("5")
    require_external_price: bool = False


CRYPTO_5M_PROFILE = MarketProfileThresholds()


@dataclass(frozen=True)
class DataQualityGateConfig:
    enforcement_mode: str = EnforcementMode.OBSERVE_ONLY.value
    market_profile: str = "crypto_5m"
    require_ws_primary_for_entry: bool = True
    allow_rest_recovery_for_exit: bool = True
    allow_rest_recovery_for_entry: bool = False
    profiles: dict[str, MarketProfileThresholds] | None = None

    def profile(self) -> MarketProfileThresholds:
        profiles = self.profiles or {"crypto_5m": CRYPTO_5M_PROFILE}
        return profiles.get(self.market_profile, CRYPTO_5M_PROFILE)


@dataclass(frozen=True)
class DataQualityReport:
    verdict: QualityVerdict
    reasons: tuple[str, ...]
    book_age_ms: int | None
    source: str | None
    source_quality: str | None
    reconnect_gap: bool
    spread: Decimal | None
    depth_at_size: Decimal | None
    profile_id: str
    emergency_reason: str | None = None

    def to_payload(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "reasons": list(self.reasons),
            "book_age_ms": self.book_age_ms,
            "source": self.source,
            "source_quality": self.source_quality,
            "reconnect_gap": self.reconnect_gap,
            "spread": str(self.spread) if self.spread is not None else None,
            "depth_at_size": str(self.depth_at_size) if self.depth_at_size is not None else None,
            "profile_id": self.profile_id,
            "emergency_reason": self.emergency_reason,
        }


class DataQualityGate:
    def __init__(self, cfg: DataQualityGateConfig | None = None) -> None:
        self._cfg = cfg or DataQualityGateConfig()

    @property
    def enforcement_mode(self) -> str:
        return self._cfg.enforcement_mode

    def allows_decision(self, report: DataQualityReport, context: DecisionContext) -> bool:
        if self._cfg.enforcement_mode == EnforcementMode.OBSERVE_ONLY.value:
            return True
        if report.verdict == QualityVerdict.PASS:
            return True
        if context in (DecisionContext.STOP, DecisionContext.URGENT_EXIT):
            return report.verdict in (QualityVerdict.DEGRADED, QualityVerdict.EMERGENCY_ONLY)
        return False

    def evaluate_snapshot(
        self,
        snap: MarketStateSnapshot | None,
        *,
        context: DecisionContext,
        size: Decimal,
    ) -> DataQualityReport:
        profile = self._cfg.profile()
        if snap is None:
            return DataQualityReport(
                verdict=QualityVerdict.REJECT_DECISION,
                reasons=("missing_snapshot",),
                book_age_ms=None,
                source=None,
                source_quality=None,
                reconnect_gap=False,
                spread=None,
                depth_at_size=None,
                profile_id=self._cfg.market_profile,
            )
        reasons: list[str] = []
        depth = _depth_at_size(snap, size)
        spread = snap.spread

        age_verdict = _age_verdict(snap.book_age_ms, context, profile)
        if age_verdict == QualityVerdict.REJECT_DECISION:
            reasons.append("book_age_reject")
        elif age_verdict == QualityVerdict.EMERGENCY_ONLY:
            reasons.append("book_age_emergency")
        elif age_verdict == QualityVerdict.DEGRADED:
            reasons.append("book_age_degraded")

        source_verdict, source_reasons = _source_verdict(snap, context, self._cfg)
        reasons.extend(source_reasons)

        if snap.reconnect_gap and context in (
            DecisionContext.ENTRY,
            DecisionContext.ACTIVATION,
            DecisionContext.TAKE_PROFIT,
        ):
            reasons.append("reconnect_gap")
            source_verdict = QualityVerdict.REJECT_DECISION

        if spread is None:
            reasons.append("missing_spread")
        elif spread > profile.max_spread:
            reasons.append("spread_exceeds_max")
            if context in (DecisionContext.ENTRY, DecisionContext.ACTIVATION, DecisionContext.TAKE_PROFIT):
                source_verdict = QualityVerdict.REJECT_DECISION

        if depth is None or depth < profile.min_depth_at_size:
            reasons.append("insufficient_depth")
            if context in (DecisionContext.ENTRY, DecisionContext.ACTIVATION, DecisionContext.TAKE_PROFIT):
                source_verdict = QualityVerdict.REJECT_DECISION

        if snap.best_bid is None or snap.best_ask is None:
            reasons.append("missing_bid_or_ask")
            if context in (DecisionContext.ENTRY, DecisionContext.ACTIVATION, DecisionContext.TAKE_PROFIT):
                source_verdict = QualityVerdict.REJECT_DECISION

        verdict = _combine_verdicts(age_verdict, source_verdict, context)
        emergency_reason = _emergency_reason(verdict, reasons)
        return DataQualityReport(
            verdict=verdict,
            reasons=tuple(dict.fromkeys(reasons)),
            book_age_ms=snap.book_age_ms,
            source=snap.source,
            source_quality=snap.source_quality,
            reconnect_gap=snap.reconnect_gap,
            spread=spread,
            depth_at_size=depth,
            profile_id=self._cfg.market_profile,
            emergency_reason=emergency_reason,
        )

    def evaluate_pair(
        self,
        pair: PairMarketSnapshot | None,
        *,
        context: DecisionContext,
        size: Decimal,
    ) -> DataQualityReport:
        if pair is None:
            return DataQualityReport(
                verdict=QualityVerdict.REJECT_DECISION,
                reasons=("missing_pair_snapshot",),
                book_age_ms=None,
                source=None,
                source_quality=None,
                reconnect_gap=False,
                spread=None,
                depth_at_size=None,
                profile_id=self._cfg.market_profile,
            )
        yes = self.evaluate_snapshot(pair.yes, context=context, size=size)
        no = self.evaluate_snapshot(pair.no, context=context, size=size)
        return _merge_pair_reports(yes, no, profile_id=self._cfg.market_profile)


def _age_verdict(age_ms: int, context: DecisionContext, profile: MarketProfileThresholds) -> QualityVerdict:
    if age_ms > profile.emergency_max_age_ms:
        return QualityVerdict.REJECT_DECISION
    if age_ms > profile.reject_max_age_ms:
        if context in (DecisionContext.STOP, DecisionContext.URGENT_EXIT):
            return QualityVerdict.EMERGENCY_ONLY
        return QualityVerdict.REJECT_DECISION
    if age_ms > profile.pass_max_age_ms:
        if context in (DecisionContext.STOP, DecisionContext.URGENT_EXIT):
            return QualityVerdict.DEGRADED
        return QualityVerdict.REJECT_DECISION
    return QualityVerdict.PASS


def _source_verdict(
    snap: MarketStateSnapshot,
    context: DecisionContext,
    cfg: DataQualityGateConfig,
) -> tuple[QualityVerdict, list[str]]:
    reasons: list[str] = []
    sq = snap.source_quality
    src = snap.source

    if context in (DecisionContext.ENTRY, DecisionContext.ACTIVATION, DecisionContext.TAKE_PROFIT):
        if cfg.require_ws_primary_for_entry and sq != SourceQuality.WS_PRIMARY:
            reasons.append("source_not_ws_primary")
            return QualityVerdict.REJECT_DECISION, reasons
        if src in (BookSource.REST_BOOTSTRAP, BookSource.REST_RECOVERY, BookSource.REST_POLL):
            reasons.append(f"source_{src}_not_allowed_for_entry")
            return QualityVerdict.REJECT_DECISION, reasons

    if context == DecisionContext.ENTRY:
        if src == BookSource.REST_RECOVERY or not cfg.allow_rest_recovery_for_entry:
            if src == BookSource.REST_RECOVERY:
                reasons.append("rest_recovery_not_allowed_for_entry")
                return QualityVerdict.REJECT_DECISION, reasons

    if context in (DecisionContext.STOP, DecisionContext.URGENT_EXIT):
        if sq == SourceQuality.WS_PRIMARY:
            return QualityVerdict.PASS, reasons
        if src == BookSource.REST_RECOVERY:
            if cfg.allow_rest_recovery_for_exit:
                reasons.append("rest_recovery_exit_allowed")
                return QualityVerdict.PASS, reasons
            reasons.append("rest_recovery_exit_not_configured")
            return QualityVerdict.REJECT_DECISION, reasons
        if src in (BookSource.REST_BOOTSTRAP, BookSource.REST_POLL):
            reasons.append(f"source_{src}_not_allowed_for_exit")
            return QualityVerdict.REJECT_DECISION, reasons

    return QualityVerdict.PASS, reasons


def _combine_verdicts(
    age_verdict: QualityVerdict,
    source_verdict: QualityVerdict,
    context: DecisionContext,
) -> QualityVerdict:
    if source_verdict == QualityVerdict.REJECT_DECISION:
        return QualityVerdict.REJECT_DECISION
    if age_verdict == QualityVerdict.REJECT_DECISION:
        return QualityVerdict.REJECT_DECISION
    order = {
        QualityVerdict.PASS: 0,
        QualityVerdict.DEGRADED: 1,
        QualityVerdict.EMERGENCY_ONLY: 2,
        QualityVerdict.REJECT_DECISION: 3,
    }
    worst = max(age_verdict, source_verdict, key=lambda v: order[v])
    if context in (DecisionContext.ENTRY, DecisionContext.ACTIVATION, DecisionContext.TAKE_PROFIT):
        if worst != QualityVerdict.PASS:
            return QualityVerdict.REJECT_DECISION
    return worst


def _merge_pair_reports(yes: DataQualityReport, no: DataQualityReport, *, profile_id: str) -> DataQualityReport:
    order = {
        QualityVerdict.PASS: 0,
        QualityVerdict.DEGRADED: 1,
        QualityVerdict.EMERGENCY_ONLY: 2,
        QualityVerdict.REJECT_DECISION: 3,
    }
    verdict = max(yes.verdict, no.verdict, key=lambda v: order[v])
    reasons = tuple(dict.fromkeys((*yes.reasons, *no.reasons)))
    ages = [a for a in (yes.book_age_ms, no.book_age_ms) if a is not None]
    depths = [d for d in (yes.depth_at_size, no.depth_at_size) if d is not None]
    spreads = [s for s in (yes.spread, no.spread) if s is not None]
    emergency = yes.emergency_reason or no.emergency_reason
    if verdict == QualityVerdict.EMERGENCY_ONLY and emergency is None:
        emergency = ";".join(reasons) or "pair_quality_emergency"
    return DataQualityReport(
        verdict=verdict,
        reasons=reasons,
        book_age_ms=max(ages) if ages else None,
        source=yes.source,
        source_quality=yes.source_quality,
        reconnect_gap=yes.reconnect_gap or no.reconnect_gap,
        spread=max(spreads) if spreads else None,
        depth_at_size=min(depths) if depths else None,
        profile_id=profile_id,
        emergency_reason=emergency,
    )


def _emergency_reason(verdict: QualityVerdict, reasons: list[str]) -> str | None:
    if verdict != QualityVerdict.EMERGENCY_ONLY:
        return None
    return ";".join(reasons) if reasons else "emergency_only"
