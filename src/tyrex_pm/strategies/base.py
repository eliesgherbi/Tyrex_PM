"""Shared base for strategy drivers: the optional half of the driver contract.

Every host (live, paper, observation, replay) calls these methods directly. A strategy
overrides the ones it needs; the defaults here mean "this strategy does not use that
feedback". Hosts never probe a driver with ``getattr``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tyrex_pm.domain.polymarket.ptb import PtbSnapshot
from tyrex_pm.strategies.context import StrategyContext
from tyrex_pm.strategies.outcomes import EntryOutcome, ExitOutcome


@dataclass(frozen=True)
class EpisodeView:
    """What the host may report about a strategy's current entry episode in its window."""

    window_id: str
    phase: str
    episode_started_at: str | None = None
    next_attempt_not_before: str | None = None
    entry_side: str | None = None
    consecutive_service_failures: int = 0
    closed_reason: str | None = None
    window_attempts: int | None = None


class BaseDriver:
    """No-op defaults. Drivers are dataclasses that inherit this and add their state."""

    def on_start(self, context: StrategyContext) -> None:
        del context

    def on_stop(self, reason: str) -> None:
        del reason

    def configure_ptb(self, ptb: PtbSnapshot) -> None:
        """The window's price to beat was sealed; strategies that use K keep it."""
        del ptb

    def on_entry_outcome(self, outcome: EntryOutcome) -> None:
        del outcome

    def on_exit_outcome(self, outcome: ExitOutcome) -> None:
        del outcome

    def on_hold_committed(self) -> None:
        """The host committed the position to resolution (no further exit)."""

    def restore_window_usage(self, **usage: Any) -> None:
        """Restart in a window already traded: restore attempt counters from the ledger."""
        del usage

    def value_exit_refusal(
        self, snapshot: Any, context: Any, features: Any, token_id: str, minimum_price: Any
    ) -> str | None:
        """Final-gate check of a value exit; a reason refuses it. None accepts it."""
        del snapshot, context, features, token_id, minimum_price
        return None

    def episode_view(self) -> EpisodeView | None:
        """The current entry episode, for host reports and retry clocks. None: no episodes."""
        return None

    def persistence_slice(self) -> dict[str, Any]:
        return {}

    def restore_persistence_slice(self, data: Mapping[str, Any]) -> None:
        del data
