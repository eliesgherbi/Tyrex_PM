"""Decide whether an inspection snapshot may change readiness or clearance.

Network reads happen outside this check. The caller captures an epoch before
the read and compares it with the coordinator after the read returns.
"""

from __future__ import annotations

from dataclasses import dataclass

from tyrex_pm.execution.session_state import ExecutionSessionState


@dataclass(frozen=True, kw_only=True)
class ExecutionEpoch:
    journal_sequence: int
    journal_event_id: str | None
    unresolved_attempt_ids: frozenset[str]


def capture_execution_epoch(coordinator: object) -> ExecutionEpoch:
    journal = getattr(coordinator, "journal")
    tail = getattr(journal, "tail", None)
    if not callable(tail):
        raise RuntimeError("execution journal cannot report its tail")
    sequence, event_id = tail()
    states = getattr(coordinator, "states", {})
    return ExecutionEpoch(
        journal_sequence=int(sequence),
        journal_event_id=None if event_id is None else str(event_id),
        unresolved_attempt_ids=_unresolved_attempt_ids(states.values()),
    )


def authored_publication_holds(
    *,
    before_unresolved: frozenset[str],
    after_unresolved: frozenset[str],
    authored_event_ids: frozenset[str],
    observed_event_ids: frozenset[str],
) -> bool:
    """True when only this publication wrote, and no new submission appeared."""
    if not after_unresolved <= before_unresolved:
        return False
    return observed_event_ids <= authored_event_ids


def publication_decision(start: ExecutionEpoch, end: ExecutionEpoch) -> str:
    """Return accepted, discarded, or withheld.

    ``discarded`` means execution evidence changed while the snapshot was in
    flight. ``withheld`` means the snapshot is stable but a submission response
    is still absent, so classification may be shown and clearance must not.
    """
    if (start.journal_sequence, start.journal_event_id) != (
        end.journal_sequence,
        end.journal_event_id,
    ):
        return "discarded"
    if start.unresolved_attempt_ids != end.unresolved_attempt_ids:
        return "discarded"
    if end.unresolved_attempt_ids:
        return "withheld"
    return "accepted"


def _unresolved_attempt_ids(states: object) -> frozenset[str]:
    found: set[str] = set()
    for state in states:
        if not isinstance(state, ExecutionSessionState):
            continue
        for order in state.orders.values():
            if order.accepted is not None and not order.ambiguous:
                continue
            for attempt_id in order.attempt_ids:
                if attempt_id not in state.resolved_attempt_ids:
                    found.add(str(attempt_id))
        if state.entry_match_awaiting_confirmation and state.session_id:
            found.add(f"awaiting-match:{state.session_id}")
    return frozenset(found)
