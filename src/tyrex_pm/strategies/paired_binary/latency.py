"""Timing / latency instrumentation for paired binary (Phase 4.6 robustness)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tyrex_pm.core.time import monotonic_s
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook


@dataclass
class LatencyTracker:
    """Collect timestamps for one paired-binary lifecycle segment."""

    book_update_ts: str | None = None
    book_age_ms: int | None = None
    decision_ts: float | None = None
    intent_created_ts: float | None = None
    risk_done_ts: float | None = None
    plan_done_ts: float | None = None
    oms_submit_ts: float | None = None
    oms_ack_ts: float | None = None
    fill_seen_ts: float | None = None
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

    def mark_sellable_seen(self) -> None:
        self.sellable_seen_ts = monotonic_s()

    def set_book_capture(self, yes: LegBook, no: LegBook) -> None:
        ages = [b for b in (yes.book_age_ms, no.book_age_ms) if b is not None]
        self.book_age_ms = max(ages) if ages else None

    def _ms(self, start: float | None, end: float | None) -> int | None:
        if start is None or end is None:
            return None
        return int((end - start) * 1000)

    def payload(self, *, event: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "event": event,
            "book_update_ts": self.book_update_ts,
            "book_age_ms": self.book_age_ms,
        }
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
