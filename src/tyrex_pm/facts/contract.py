"""Strategy-owned input contract: facts, indicators, and evaluation triggers."""

from __future__ import annotations

from dataclasses import dataclass

from tyrex_pm.facts.ids import KNOWN_FACTS, SOURCE_FACTS
from tyrex_pm.facts.producers import expand_facts
from tyrex_pm.indicators.spec import IndicatorSpec


@dataclass(frozen=True, slots=True)
class OnFact:
    """Wake evaluation when this fact updates, after optional coalesce."""

    fact: str
    coalesce_s: float = 1.0
    after_ready: str | None = None

    def __post_init__(self) -> None:
        if not self.fact.strip():
            raise ValueError("OnFact.fact must be non-empty")
        if self.coalesce_s < 0:
            raise ValueError("OnFact.coalesce_s must be >= 0")
        if self.after_ready is not None and not self.after_ready.strip():
            raise ValueError("OnFact.after_ready must be non-empty when set")


@dataclass(frozen=True, slots=True)
class OnTimer:
    """Wake evaluation on a wall/monotonic interval."""

    interval_s: float

    def __post_init__(self) -> None:
        if self.interval_s <= 0:
            raise ValueError("OnTimer.interval_s must be > 0")


EvalTrigger = OnFact | OnTimer


@dataclass(frozen=True, kw_only=True)
class EligibilityFacts:
    """Neutral readiness the host reads instead of strategy-kind introspection."""

    strategy_inputs_eligible: bool
    model_ready: bool
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class InputContract:
    """What a strategy reads, which indicators to run, and what wakes evaluate()."""

    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    indicators: tuple[IndicatorSpec, ...] = ()
    evaluate_on: tuple[EvalTrigger, ...] = ()

    def __post_init__(self) -> None:
        required = tuple(dict.fromkeys(self.required))
        optional = tuple(dict.fromkeys(self.optional))
        overlap = set(required) & set(optional)
        if overlap:
            raise ValueError(f"facts cannot be both required and optional: {sorted(overlap)}")
        unknown = [fact for fact in (*required, *optional) if fact not in KNOWN_FACTS]
        if unknown:
            raise ValueError(f"unknown fact id(s): {unknown}")
        object.__setattr__(self, "required", required)
        object.__setattr__(self, "optional", optional)
        trigger_facts = [
            trigger.fact for trigger in self.evaluate_on if isinstance(trigger, OnFact)
        ]
        contracted = set(required) | set(optional)
        for fact in trigger_facts:
            if fact not in contracted and fact not in expand_facts(frozenset(contracted)):
                raise ValueError(f"evaluate_on fact {fact!r} is not in the contract")
        after = [
            trigger.after_ready
            for trigger in self.evaluate_on
            if isinstance(trigger, OnFact) and trigger.after_ready is not None
        ]
        for fact in after:
            if fact not in contracted and fact not in expand_facts(frozenset(contracted)):
                raise ValueError(f"evaluate_on after_ready {fact!r} is not in the contract")
        for spec in self.indicators:
            for source in spec.all_sources:
                if source not in contracted and source not in expand_facts(frozenset(contracted)):
                    raise ValueError(
                        f"indicator {spec.name!r} source {source!r} is not in the contract"
                    )

    @property
    def contracted(self) -> frozenset[str]:
        return frozenset(self.required) | frozenset(self.optional)

    def requires(self, fact: str) -> bool:
        return fact in self.required

    def includes(self, fact: str) -> bool:
        return fact in self.contracted

    def source_facts(self) -> frozenset[str]:
        """Connectors the runtime must start (transitive closure, sources only)."""
        closed = expand_facts(self.contracted)
        for spec in self.indicators:
            closed |= expand_facts(frozenset(spec.all_sources))
        return frozenset(fact for fact in closed if fact in SOURCE_FACTS)

    def derived_facts(self) -> frozenset[str]:
        closed = expand_facts(self.contracted)
        return frozenset(fact for fact in closed if fact not in SOURCE_FACTS)
