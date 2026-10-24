"""What woke the strategy: one vocabulary for every host (live, observation, replay)."""

from __future__ import annotations

from enum import StrEnum


class EvaluationTrigger(StrEnum):
    # A market-data fact the strategy's contract listens to arrived (live and paper).
    FEED = "feed"
    # The strategy's timer fired (contract ``OnTimer``).
    TIMER = "timer"
    # Offline replay of recorded data (research).
    REPLAY = "replay"
    # Signal-only observation of live feeds; never trades.
    OBSERVE = "observe"


def evaluation_trigger(value: str) -> EvaluationTrigger:
    """Parse a trigger; an unknown value is a host bug, not a strategy choice."""
    try:
        return EvaluationTrigger(value)
    except ValueError as exc:
        known = ", ".join(item.value for item in EvaluationTrigger)
        raise ValueError(f"unknown evaluation trigger {value!r}; expected one of {known}") from exc
