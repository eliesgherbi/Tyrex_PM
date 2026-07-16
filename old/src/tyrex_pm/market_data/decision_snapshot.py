"""Decision-level observability bundles (Phase 2 M7)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import uuid4

from tyrex_pm.market_data.executable_book import PlannerEvidence
from tyrex_pm.market_data.features import FeatureBuilder
from tyrex_pm.market_data.models import BasicFeatureSnapshot, MarketStateSnapshot, PairMarketSnapshot
from tyrex_pm.market_data.quality import DataQualityReport


@dataclass(frozen=True)
class DecisionSnapshot:
    decision_id: str
    decision_type: str
    snapshot_ids: dict[str, str]
    pair_snapshot_id: str | None
    quality_report: DataQualityReport | None
    planner_evidence: PlannerEvidence | None
    features: BasicFeatureSnapshot | None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "decision_id": self.decision_id,
            "decision_type": self.decision_type,
            "snapshot_ids": dict(self.snapshot_ids),
            "pair_snapshot_id": self.pair_snapshot_id,
        }
        if self.quality_report is not None:
            payload["quality_report"] = self.quality_report.to_payload()
        if self.planner_evidence is not None:
            payload["planner_evidence"] = self.planner_evidence.to_payload()
        if self.features is not None:
            payload["features"] = _feature_payload(self.features)
        return payload


def build_decision_snapshot(
    *,
    decision_type: str,
    snap: MarketStateSnapshot | None = None,
    pair: PairMarketSnapshot | None = None,
    quality_report: DataQualityReport | None = None,
    planner_evidence: PlannerEvidence | None = None,
    size: Decimal,
    decision_id: str | None = None,
) -> DecisionSnapshot:
    did = decision_id or (planner_evidence.decision_id if planner_evidence else str(uuid4()))
    snapshot_ids: dict[str, str] = {}
    pair_snapshot_id: str | None = None
    features: BasicFeatureSnapshot | None = None
    qv = quality_report.verdict.value if quality_report else None

    if pair is not None:
        snapshot_ids[str(pair.yes.token_id)] = pair.yes.snapshot_id
        snapshot_ids[str(pair.no.token_id)] = pair.no.snapshot_id
        pair_snapshot_id = pair.pair_snapshot_id
        features = FeatureBuilder.build_pair(pair, size=size, quality_verdict=qv)
    elif snap is not None:
        snapshot_ids[str(snap.token_id)] = snap.snapshot_id
        features = FeatureBuilder.build(snap, size=size, quality_verdict=qv)

    return DecisionSnapshot(
        decision_id=did,
        decision_type=decision_type,
        snapshot_ids=snapshot_ids,
        pair_snapshot_id=pair_snapshot_id,
        quality_report=quality_report,
        planner_evidence=planner_evidence,
        features=features,
    )


def _feature_payload(features: BasicFeatureSnapshot) -> dict[str, Any]:
    return {
        "snapshot_id": features.snapshot_id,
        "pair_snapshot_id": features.pair_snapshot_id,
        "source": features.source,
        "book_age_ms": features.book_age_ms,
        "spread": str(features.spread) if features.spread is not None else None,
        "mid": str(features.mid) if features.mid is not None else None,
        "best_bid": str(features.best_bid) if features.best_bid is not None else None,
        "best_ask": str(features.best_ask) if features.best_ask is not None else None,
        "best_bid_size": str(features.best_bid_size) if features.best_bid_size is not None else None,
        "best_ask_size": str(features.best_ask_size) if features.best_ask_size is not None else None,
        "depth_at_size": str(features.depth_at_size) if features.depth_at_size is not None else None,
        "effective_bid_at_size": str(features.effective_bid_at_size)
        if features.effective_bid_at_size is not None
        else None,
        "effective_ask_at_size": str(features.effective_ask_at_size)
        if features.effective_ask_at_size is not None
        else None,
        "sweep_vwap_buy": str(features.sweep_vwap_buy) if features.sweep_vwap_buy is not None else None,
        "sweep_vwap_sell": str(features.sweep_vwap_sell) if features.sweep_vwap_sell is not None else None,
        "quality_verdict": features.quality_verdict,
        "missing_fields": list(features.missing_fields),
    }
