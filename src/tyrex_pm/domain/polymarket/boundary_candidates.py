"""N3 boundary candidate capture for Chainlink settlement ticks.

Rule IDs are explicit and provisional — N3A does not promote any rule to PROVEN.
When EXACT_AT_START is absent, FIRST_AT_OR_AFTER and LAST_AT_OR_BEFORE are
retained and reported; callers must not silently pick one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import MarketId
from tyrex_pm.core.ingress import IngressMeta
from tyrex_pm.core.numerics import as_decimal


class BoundaryRuleId(str, Enum):
    EXACT_AT_START = "EXACT_AT_START"
    FIRST_AT_OR_AFTER = "FIRST_AT_OR_AFTER"
    LAST_AT_OR_BEFORE = "LAST_AT_OR_BEFORE"


class BoundaryRuleClassification(str, Enum):
    """Evidence classification — never auto-promoted to PROVEN in N3A."""

    PROVISIONAL = "PROVISIONAL"
    OPEN = "OPEN"
    PROVEN = "PROVEN"
    REFUTED = "REFUTED"


# N1: exact-on-boundary matched displayed openPrice in all three windows.
PROVISIONAL_PREFERRED_RULE = BoundaryRuleId.EXACT_AT_START
PROVISIONAL_PREFERRED_CLASSIFICATION = BoundaryRuleClassification.PROVISIONAL


@dataclass(frozen=True, kw_only=True)
class BoundaryTickView:
    """Minimal settlement tick view for boundary evaluation."""

    value: Decimal
    source_ts: datetime
    receive_wall_raw_utc: datetime
    receive_wall_corrected_utc: datetime | None
    receive_monotonic_ns: int
    ingress: IngressMeta | None = None
    raw_fingerprint: str | None = None
    late_or_out_of_order: str | None = None
    event_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_ts", require_utc(self.source_ts, field_name="source_ts"))
        object.__setattr__(
            self,
            "receive_wall_raw_utc",
            require_utc(self.receive_wall_raw_utc, field_name="receive_wall_raw_utc"),
        )
        if self.receive_wall_corrected_utc is not None:
            object.__setattr__(
                self,
                "receive_wall_corrected_utc",
                require_utc(
                    self.receive_wall_corrected_utc,
                    field_name="receive_wall_corrected_utc",
                ),
            )
        object.__setattr__(self, "value", as_decimal(self.value, field_name="value"))


@dataclass(frozen=True, kw_only=True)
class BoundaryCandidate:
    """One labelled boundary candidate for a market window."""

    market_id: MarketId
    window_id: str
    event_start: datetime
    event_end: datetime
    rule_id: BoundaryRuleId
    rule_classification: BoundaryRuleClassification
    value: Decimal
    chainlink_source_ts: datetime
    receive_wall_raw_utc: datetime
    receive_wall_corrected_utc: datetime | None
    receive_monotonic_ns: int
    clock_status: str | None
    clock_offset_ms: float | None
    clock_uncertainty_ms: int | None
    clock_snapshot_id: str | None
    ingress_sequence: int | None
    connection_generation: int | None
    raw_fingerprint: str | None
    event_id: str | None
    boundary_source_delta_ms: int
    receive_delay_ms: int
    late_or_out_of_order: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "event_start", require_utc(self.event_start, field_name="event_start")
        )
        object.__setattr__(self, "event_end", require_utc(self.event_end, field_name="event_end"))
        object.__setattr__(
            self,
            "chainlink_source_ts",
            require_utc(self.chainlink_source_ts, field_name="chainlink_source_ts"),
        )
        object.__setattr__(
            self,
            "receive_wall_raw_utc",
            require_utc(self.receive_wall_raw_utc, field_name="receive_wall_raw_utc"),
        )
        if self.receive_wall_corrected_utc is not None:
            object.__setattr__(
                self,
                "receive_wall_corrected_utc",
                require_utc(
                    self.receive_wall_corrected_utc,
                    field_name="receive_wall_corrected_utc",
                ),
            )
        object.__setattr__(self, "value", as_decimal(self.value, field_name="value"))
        object.__setattr__(self, "provenance", dict(self.provenance))


@dataclass(frozen=True, kw_only=True)
class BoundaryCandidateSet:
    """All evaluated candidates for one window — no silent fallback selection."""

    market_id: MarketId
    window_id: str
    event_start: datetime
    event_end: datetime
    candidates: tuple[BoundaryCandidate, ...]
    preferred_rule_id: BoundaryRuleId | None
    preferred_classification: BoundaryRuleClassification | None
    blocker_reasons: tuple[str, ...] = ()

    @property
    def by_rule(self) -> dict[BoundaryRuleId, BoundaryCandidate]:
        return {c.rule_id: c for c in self.candidates}

    @property
    def exact(self) -> BoundaryCandidate | None:
        return self.by_rule.get(BoundaryRuleId.EXACT_AT_START)

    @property
    def first_at_or_after(self) -> BoundaryCandidate | None:
        return self.by_rule.get(BoundaryRuleId.FIRST_AT_OR_AFTER)

    @property
    def last_at_or_before(self) -> BoundaryCandidate | None:
        return self.by_rule.get(BoundaryRuleId.LAST_AT_OR_BEFORE)

    @property
    def selected_for_provisional_lock(self) -> BoundaryCandidate | None:
        """Prefer EXACT when present; otherwise None (ambiguous / incomplete)."""
        if self.exact is not None:
            return self.exact
        return None

    @property
    def first_last_diverge(self) -> bool:
        a = self.first_at_or_after
        b = self.last_at_or_before
        if a is None or b is None:
            return False
        return a.chainlink_source_ts != b.chainlink_source_ts or a.value != b.value


def _ms_delta(a: datetime, b: datetime) -> int:
    return int((a - b).total_seconds() * 1000.0)


def _candidate_from_tick(
    *,
    market_id: MarketId,
    window_id: str,
    event_start: datetime,
    event_end: datetime,
    rule_id: BoundaryRuleId,
    tick: BoundaryTickView,
) -> BoundaryCandidate:
    meta = tick.ingress
    return BoundaryCandidate(
        market_id=market_id,
        window_id=window_id,
        event_start=event_start,
        event_end=event_end,
        rule_id=rule_id,
        rule_classification=PROVISIONAL_PREFERRED_CLASSIFICATION
        if rule_id is PROVISIONAL_PREFERRED_RULE
        else BoundaryRuleClassification.OPEN,
        value=tick.value,
        chainlink_source_ts=tick.source_ts,
        receive_wall_raw_utc=tick.receive_wall_raw_utc,
        receive_wall_corrected_utc=tick.receive_wall_corrected_utc,
        receive_monotonic_ns=tick.receive_monotonic_ns,
        clock_status=None if meta is None else meta.clock_status,
        clock_offset_ms=None if meta is None else meta.clock_offset_ms,
        clock_uncertainty_ms=None if meta is None else meta.clock_uncertainty_ms,
        clock_snapshot_id=None if meta is None else meta.clock_snapshot_id,
        ingress_sequence=None if meta is None else meta.ingress_sequence,
        connection_generation=None if meta is None else meta.connection_generation,
        raw_fingerprint=tick.raw_fingerprint
        or (None if meta is None else meta.raw_fingerprint or None),
        event_id=tick.event_id,
        boundary_source_delta_ms=_ms_delta(tick.source_ts, event_start),
        receive_delay_ms=_ms_delta(tick.receive_wall_raw_utc, tick.source_ts),
        late_or_out_of_order=tick.late_or_out_of_order
        or (None if meta is None else meta.late_or_out_of_order),
        provenance={
            "settlement_provider": "chainlink",
            "pairing_irrelevant": True,
        },
    )


def find_exact_at_start(
    ticks: Sequence[BoundaryTickView], event_start: datetime
) -> BoundaryTickView | None:
    """Return the exact-on-boundary tick, or None.

    If multiple exact ticks disagree on value, returns the first and callers
    should consult ``exact_value_conflicts``.
    """
    event_start = require_utc(event_start, field_name="event_start")
    for t in ticks:
        if t.source_ts == event_start:
            return t
    return None


def exact_value_conflicts(ticks: Sequence[BoundaryTickView], event_start: datetime) -> bool:
    event_start = require_utc(event_start, field_name="event_start")
    values = {t.value for t in ticks if t.source_ts == event_start}
    return len(values) > 1


def find_first_at_or_after(
    ticks: Sequence[BoundaryTickView], event_start: datetime
) -> BoundaryTickView | None:
    event_start = require_utc(event_start, field_name="event_start")
    ordered = sorted(ticks, key=lambda t: t.source_ts)
    for t in ordered:
        if t.source_ts >= event_start:
            return t
    return None


def find_last_at_or_before(
    ticks: Sequence[BoundaryTickView], event_start: datetime
) -> BoundaryTickView | None:
    event_start = require_utc(event_start, field_name="event_start")
    ordered = sorted(ticks, key=lambda t: t.source_ts)
    cand: BoundaryTickView | None = None
    for t in ordered:
        if t.source_ts <= event_start:
            cand = t
        else:
            break
    return cand


def evaluate_boundary_candidates(
    *,
    market_id: MarketId,
    window_id: str,
    event_start: datetime,
    event_end: datetime,
    ticks: Sequence[BoundaryTickView],
) -> BoundaryCandidateSet:
    """Evaluate all three rule identities without silently choosing a fallback."""
    event_start = require_utc(event_start, field_name="event_start")
    event_end = require_utc(event_end, field_name="event_end")
    if not window_id.strip():
        raise ValueError("window_id must be non-empty")

    candidates: list[BoundaryCandidate] = []
    blockers: list[str] = []

    exact_t = find_exact_at_start(ticks, event_start)
    first_t = find_first_at_or_after(ticks, event_start)
    last_t = find_last_at_or_before(ticks, event_start)
    if exact_value_conflicts(ticks, event_start):
        blockers.append("conflicting_duplicate")

    if exact_t is not None:
        candidates.append(
            _candidate_from_tick(
                market_id=market_id,
                window_id=window_id,
                event_start=event_start,
                event_end=event_end,
                rule_id=BoundaryRuleId.EXACT_AT_START,
                tick=exact_t,
            )
        )
    else:
        blockers.append("exact_candidate_absent")

    if first_t is not None:
        candidates.append(
            _candidate_from_tick(
                market_id=market_id,
                window_id=window_id,
                event_start=event_start,
                event_end=event_end,
                rule_id=BoundaryRuleId.FIRST_AT_OR_AFTER,
                tick=first_t,
            )
        )
    if last_t is not None:
        candidates.append(
            _candidate_from_tick(
                market_id=market_id,
                window_id=window_id,
                event_start=event_start,
                event_end=event_end,
                rule_id=BoundaryRuleId.LAST_AT_OR_BEFORE,
                tick=last_t,
            )
        )

    if not candidates:
        blockers.append("no_boundary_candidate")

    preferred: BoundaryRuleId | None = None
    preferred_class: BoundaryRuleClassification | None = None
    if exact_t is not None:
        preferred = BoundaryRuleId.EXACT_AT_START
        preferred_class = PROVISIONAL_PREFERRED_CLASSIFICATION
    elif first_t is not None and last_t is not None:
        # Both present without exact — do not select; report ambiguity if they diverge.
        if first_t.source_ts != last_t.source_ts or first_t.value != last_t.value:
            blockers.append("ambiguous_fallback_candidates")
        else:
            # Same tick under both inequalities but not labelled EXACT (should be rare
            # if source_ts == start); still do not auto-promote.
            blockers.append("exact_candidate_absent")
    elif first_t is None and last_t is None:
        pass
    else:
        blockers.append("exact_candidate_absent")
        blockers.append("incomplete_fallback_candidates")

    # Deduplicate blocker order
    seen: set[str] = set()
    uniq_blockers: list[str] = []
    for b in blockers:
        if b not in seen:
            seen.add(b)
            uniq_blockers.append(b)

    return BoundaryCandidateSet(
        market_id=market_id,
        window_id=window_id,
        event_start=event_start,
        event_end=event_end,
        candidates=tuple(candidates),
        preferred_rule_id=preferred,
        preferred_classification=preferred_class,
        blocker_reasons=tuple(uniq_blockers),
    )
