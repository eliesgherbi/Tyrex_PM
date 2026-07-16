"""Timing / latency instrumentation for paired binary (Phase 4.6 + M7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tyrex_pm.core.time import monotonic_s
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook


@dataclass(frozen=True)
class LatencyChain:
    decision_id: str
    trigger_to_submit_ms: int | None
    submit_to_ack_ms: int | None
    trigger_to_fill_ms: int | None
    ack_to_user_fill_ms: int | None
    fill_to_sellable_ms: int | None
    market_book_age_ms: int | None
    user_ws_age_ms: int | None
    wallet_position_age_ms: int | None
    source: str | None
    missing_fields_reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "decision_id": self.decision_id,
            "trigger_to_submit_ms": self.trigger_to_submit_ms,
            "submit_to_ack_ms": self.submit_to_ack_ms,
            "trigger_to_fill_ms": self.trigger_to_fill_ms,
            "ack_to_user_fill_ms": self.ack_to_user_fill_ms,
            "fill_to_sellable_ms": self.fill_to_sellable_ms,
            "market_book_age_ms": self.market_book_age_ms,
            "user_ws_age_ms": self.user_ws_age_ms,
            "wallet_position_age_ms": self.wallet_position_age_ms,
            "source": self.source,
        }
        if self.missing_fields_reason is not None:
            payload["missing_fields_reason"] = self.missing_fields_reason
        return payload


@dataclass
class LatencyTracker:
    """Collect timestamps for one paired-binary lifecycle segment."""

    decision_id: str | None = None
    book_update_ts: str | None = None
    book_age_ms: int | None = None
    source: str | None = None
    user_ws_age_ms: int | None = None
    wallet_position_age_ms: int | None = None
    decision_ts: float | None = None
    intent_created_ts: float | None = None
    risk_done_ts: float | None = None
    plan_done_ts: float | None = None
    oms_submit_ts: float | None = None
    oms_ack_ts: float | None = None
    fill_seen_ts: float | None = None
    user_fill_seen_ts: float | None = None
    sellable_seen_ts: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def mark_decision(self) -> None:
        self.decision_ts = monotonic_s()

    def mark_intent_created(self) -> None:
        self.intent_created_ts = monotonic_s()

    def mark_risk_done(self) -> None:
        self.risk_done_ts = monotonic_s()

    def mark_plan_done(self) -> None:
        self.plan_done_ts = monotonic_s()

    def mark_oms_submit(self) -> None:
        self.oms_submit_ts = monotonic_s()

    def mark_oms_ack(self) -> None:
        self.oms_ack_ts = monotonic_s()

    def mark_fill_seen(self) -> None:
        self.fill_seen_ts = monotonic_s()

    def mark_user_fill_seen(self) -> None:
        self.user_fill_seen_ts = monotonic_s()

    def mark_sellable_seen(self) -> None:
        self.sellable_seen_ts = monotonic_s()

    def set_book_capture(self, yes: LegBook, no: LegBook) -> None:
        ages = [b for b in (yes.book_age_ms, no.book_age_ms) if b is not None]
        self.book_age_ms = max(ages) if ages else None

    def _ms(self, start: float | None, end: float | None) -> int | None:
        if start is None or end is None:
            return None
        return int((end - start) * 1000)

    def build_chain(self, *, decision_id: str | None = None) -> LatencyChain:
        did = decision_id or self.decision_id or "unknown"
        missing: list[str] = []
        if self.oms_ack_ts is None:
            missing.append("oms_ack")
        if self.fill_seen_ts is None:
            missing.append("fill")
        return LatencyChain(
            decision_id=did,
            trigger_to_submit_ms=self._ms(self.decision_ts, self.oms_submit_ts),
            submit_to_ack_ms=self._ms(self.oms_submit_ts, self.oms_ack_ts),
            trigger_to_fill_ms=self._ms(self.decision_ts, self.fill_seen_ts),
            ack_to_user_fill_ms=self._ms(self.oms_ack_ts, self.user_fill_seen_ts),
            fill_to_sellable_ms=self._ms(self.fill_seen_ts, self.sellable_seen_ts),
            market_book_age_ms=self.book_age_ms,
            user_ws_age_ms=self.user_ws_age_ms,
            wallet_position_age_ms=self.wallet_position_age_ms,
            source=self.source,
            missing_fields_reason=";".join(missing) if missing else None,
        )

    def payload(self, *, event: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "event": event,
            "book_update_ts": self.book_update_ts,
            "book_age_ms": self.book_age_ms,
            "source": self.source,
        }
        if self.decision_id is not None:
            out["decision_id"] = self.decision_id
        if self.decision_ts is not None:
            out["decision_ts"] = self.decision_ts
        if self.intent_created_ts is not None:
            out["intent_created_ts"] = self.intent_created_ts
        if self.risk_done_ts is not None:
            out["risk_done_ts"] = self.risk_done_ts
        if self.plan_done_ts is not None:
            out["plan_done_ts"] = self.plan_done_ts
        if self.oms_submit_ts is not None:
            out["oms_submit_ts"] = self.oms_submit_ts
        if self.oms_ack_ts is not None:
            out["oms_ack_ts"] = self.oms_ack_ts
        if self.fill_seen_ts is not None:
            out["fill_seen_ts"] = self.fill_seen_ts
        if self.sellable_seen_ts is not None:
            out["sellable_seen_ts"] = self.sellable_seen_ts
        out["decision_to_submit_ms"] = self._ms(self.decision_ts, self.oms_submit_ts)
        out["submit_to_ack_ms"] = self._ms(self.oms_submit_ts, self.oms_ack_ts)
        out["trigger_to_submit_ms"] = self._ms(self.decision_ts, self.oms_submit_ts)
        out["trigger_to_fill_ms"] = self._ms(self.decision_ts, self.fill_seen_ts)
        out["fill_to_sellable_ms"] = self._ms(self.fill_seen_ts, self.sellable_seen_ts)
        out.update(self.extra)
        return out
