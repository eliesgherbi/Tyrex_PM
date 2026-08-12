"""EvaluationScheduler — wake strategy.evaluate from declared triggers only."""

from __future__ import annotations

from collections.abc import Callable

from tyrex_pm.facts.contract import InputContract, OnFact, OnTimer


class EvaluationScheduler:
    """Decides whether a fact update should call evaluate().

    Ingest is independent: callers always update stores, then ask the scheduler.
    A fact not listed in ``evaluate_on`` never wakes evaluation.
    """

    def __init__(
        self,
        contract: InputContract,
        *,
        derived_ready: Callable[[str], bool] | None = None,
        default_coalesce_s: float | None = None,
    ) -> None:
        self.contract = contract
        self._derived_ready = derived_ready or (lambda _fact: True)
        self._default_coalesce_s = default_coalesce_s
        self._last_eval_mono: float = 0.0
        self._last_timer_mono: float = 0.0

    def should_evaluate(self, fact: str, *, mono_s: float) -> bool:
        triggers = [
            trigger
            for trigger in self.contract.evaluate_on
            if isinstance(trigger, OnFact) and trigger.fact == fact
        ]
        if not triggers:
            return False
        for trigger in triggers:
            if trigger.after_ready is not None and not self._derived_ready(trigger.after_ready):
                return False
            coalesce = (
                self._default_coalesce_s
                if self._default_coalesce_s is not None
                else trigger.coalesce_s
            )
            if coalesce > 0 and mono_s - self._last_eval_mono < coalesce:
                return False
            return True
        return False

    def should_evaluate_timer(self, *, mono_s: float) -> bool:
        timers = [t for t in self.contract.evaluate_on if isinstance(t, OnTimer)]
        if not timers:
            return False
        interval = min(t.interval_s for t in timers)
        if self._default_coalesce_s is not None:
            interval = min(interval, self._default_coalesce_s)
        if mono_s - self._last_eval_mono < interval:
            return False
        if mono_s - self._last_timer_mono < interval:
            return False
        return True

    def mark_evaluated(self, *, mono_s: float) -> None:
        self._last_eval_mono = mono_s
        self._last_timer_mono = mono_s
