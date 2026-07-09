"""Market-aware strategy lifecycle policy (Phase 1 Wave A / M0).

Decouples process runtime from strategy exit when ``runtime.strategy_lifecycle`` is configured.
When the config block is absent, callers use legacy tick-budget behavior (Phase 2 back-compat).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

PHASE_NEAR_CLOSE = "near_close"
PHASE_CLOSED = "closed"
PHASE_UNKNOWN = "unknown"

LIFECYCLE_MODE_MARKET_AWARE = "market_aware"
LIFECYCLE_MODE_FIXED_DURATION = "fixed_duration"
LIFECYCLE_MODE_RUN_ONCE = "run_once"

EXIT_CLOCK_EVENT_END_TS = "event_end_ts"

ENTRY_BLOCK_CLOCK_UNKNOWN = "clock_unknown_no_fallback"
ENTRY_BLOCK_PHASE = "market_phase_blocked"
ENTRY_BLOCK_MIN_SURVIVAL_WINDOW = "min_survival_window"


class MarketTimingView(Protocol):
    phase: str
    event_start_ts: float | None
    event_end_ts: float | None
    seconds_to_close: float | None


@dataclass(frozen=True)
class StrategyRuntimePolicy:
    """Parsed ``runtime.strategy_lifecycle``; ``enabled=False`` preserves Phase 2 tick budget."""

    enabled: bool
    mode: str
    exit_clock_source: str
    max_runtime_s: float | None
    fallback_max_runtime_s: float
    flatten_before_event_end_s: float
    block_new_entry_phases: frozenset[str]
    emit_unknown_market_end_warning: bool
    min_survival_window_s: float


@dataclass(frozen=True)
class LoopDecision:
    continue_loop: bool
    reason: str
    clock_known: bool
    seconds_to_close: float | None
    used_fallback_runtime: bool = False


@dataclass(frozen=True)
class EntryBlockDecision:
    blocked: bool
    reason: str | None
    phase: str | None
    evidence: dict[str, Any]


@dataclass(frozen=True)
class PreCloseFlattenDecision:
    required: bool
    reason: str
    seconds_to_close: float | None


def parse_strategy_lifecycle_config(raw: dict[str, Any] | None) -> StrategyRuntimePolicy:
    """Return disabled policy when YAML block is absent (Phase 2 back-compat)."""
    if not raw:
        return StrategyRuntimePolicy(
            enabled=False,
            mode=LIFECYCLE_MODE_MARKET_AWARE,
            exit_clock_source=EXIT_CLOCK_EVENT_END_TS,
            max_runtime_s=None,
            fallback_max_runtime_s=900.0,
            flatten_before_event_end_s=20.0,
            block_new_entry_phases=frozenset({PHASE_NEAR_CLOSE, PHASE_CLOSED}),
            emit_unknown_market_end_warning=True,
            min_survival_window_s=45.0,
        )

    phases_raw = raw.get("block_new_entry_phases")
    if isinstance(phases_raw, (list, tuple)):
        phases = frozenset(str(p) for p in phases_raw)
    else:
        phases = frozenset({PHASE_NEAR_CLOSE, PHASE_CLOSED})

    max_rt = raw.get("max_runtime_s")
    max_runtime_s: float | None
    if max_rt in (None, "", "null"):
        max_runtime_s = None
    else:
        max_runtime_s = float(max_rt)

    fallback_raw = raw.get("fallback_max_runtime_s", 900)
    fallback = float(fallback_raw) if fallback_raw not in (None, "", "null") else 900.0

    return StrategyRuntimePolicy(
        enabled=True,
        mode=str(raw.get("mode", LIFECYCLE_MODE_MARKET_AWARE)),
        exit_clock_source=str(raw.get("exit_clock_source", EXIT_CLOCK_EVENT_END_TS)),
        max_runtime_s=max_runtime_s,
        fallback_max_runtime_s=fallback,
        flatten_before_event_end_s=float(raw.get("flatten_before_event_end_s", 20)),
        block_new_entry_phases=phases,
        emit_unknown_market_end_warning=bool(raw.get("emit_unknown_market_end_warning", True)),
        min_survival_window_s=float(raw.get("min_survival_window_s", 45)),
    )


def clock_is_known(snapshot: MarketTimingView) -> bool:
    return (
        snapshot.event_end_ts is not None
        and snapshot.event_start_ts is not None
        and snapshot.phase != PHASE_UNKNOWN
    )


class MarketLifecycleGuard:
    def __init__(
        self,
        policy: StrategyRuntimePolicy,
        *,
        loop_started_mono: float,
    ) -> None:
        self._policy = policy
        self._loop_started_mono = loop_started_mono
        self._fallback_warning_eligible = True

    @property
    def policy(self) -> StrategyRuntimePolicy:
        return self._policy

    def consume_fallback_warning_eligibility(self) -> bool:
        if not self._policy.enabled or not self._policy.emit_unknown_market_end_warning:
            return False
        if not self._fallback_warning_eligible:
            return False
        self._fallback_warning_eligible = False
        return True

    def suppress_strategy_max_runtime_tick_cap(self, snapshot: MarketTimingView) -> bool:
        """When True, strategy ``max_runtime_s`` must not end the loop normally."""
        if not self._policy.enabled:
            return False
        if self._policy.mode != LIFECYCLE_MODE_MARKET_AWARE:
            return False
        return clock_is_known(snapshot)

    def uses_fallback_runtime(self, snapshot: MarketTimingView) -> bool:
        if not self._policy.enabled:
            return False
        if self._policy.mode == LIFECYCLE_MODE_FIXED_DURATION:
            return False
        if self._policy.mode != LIFECYCLE_MODE_MARKET_AWARE:
            return False
        return not clock_is_known(snapshot)

    def fixed_duration_runtime_s(self) -> float | None:
        if not self._policy.enabled:
            return None
        if self._policy.mode == LIFECYCLE_MODE_FIXED_DURATION:
            return self._policy.max_runtime_s
        return None

    def is_fallback_runtime_exhausted(
        self, snapshot: MarketTimingView, *, now_mono: float
    ) -> bool:
        if not self.uses_fallback_runtime(snapshot):
            return False
        elapsed = now_mono - self._loop_started_mono
        return elapsed >= self._policy.fallback_max_runtime_s

    def should_block_new_entry(self, snapshot: MarketTimingView) -> EntryBlockDecision:
        if not self._policy.enabled:
            return EntryBlockDecision(blocked=False, reason=None, phase=snapshot.phase, evidence={})

        evidence: dict[str, Any] = {
            "phase": snapshot.phase,
            "seconds_to_close": snapshot.seconds_to_close,
            "min_survival_window_s": self._policy.min_survival_window_s,
            "event_end_ts": snapshot.event_end_ts,
        }

        if not clock_is_known(snapshot):
            if self._policy.fallback_max_runtime_s <= 0:
                return EntryBlockDecision(
                    blocked=True,
                    reason=ENTRY_BLOCK_CLOCK_UNKNOWN,
                    phase=snapshot.phase,
                    evidence=evidence,
                )
            return EntryBlockDecision(blocked=False, reason=None, phase=snapshot.phase, evidence=evidence)

        if snapshot.phase in self._policy.block_new_entry_phases:
            return EntryBlockDecision(
                blocked=True,
                reason=ENTRY_BLOCK_PHASE,
                phase=snapshot.phase,
                evidence=evidence,
            )

        stc = snapshot.seconds_to_close
        if stc is not None and stc < self._policy.min_survival_window_s:
            return EntryBlockDecision(
                blocked=True,
                reason=ENTRY_BLOCK_MIN_SURVIVAL_WINDOW,
                phase=snapshot.phase,
                evidence=evidence,
            )

        return EntryBlockDecision(blocked=False, reason=None, phase=snapshot.phase, evidence=evidence)

    def should_pre_close_flatten(
        self,
        snapshot: MarketTimingView,
        *,
        has_open_exposure: bool,
    ) -> PreCloseFlattenDecision:
        if not self._policy.enabled or not has_open_exposure:
            return PreCloseFlattenDecision(required=False, reason="not_applicable", seconds_to_close=None)

        stc = snapshot.seconds_to_close
        if snapshot.phase == PHASE_CLOSED:
            return PreCloseFlattenDecision(
                required=True,
                reason="market_closed",
                seconds_to_close=stc,
            )

        if stc is not None and 0 < stc <= self._policy.flatten_before_event_end_s:
            return PreCloseFlattenDecision(
                required=True,
                reason="pre_close_window",
                seconds_to_close=stc,
            )

        return PreCloseFlattenDecision(required=False, reason="not_in_window", seconds_to_close=stc)

    def should_continue_loop(
        self,
        *,
        snapshot: MarketTimingView,
        has_open_exposure: bool,
        strategy_terminal: bool,
        operator_stop: bool,
    ) -> LoopDecision:
        known = clock_is_known(snapshot)
        stc = snapshot.seconds_to_close

        if operator_stop:
            return LoopDecision(
                continue_loop=False,
                reason="operator_stop",
                clock_known=known,
                seconds_to_close=stc,
            )

        if strategy_terminal:
            return LoopDecision(
                continue_loop=False,
                reason="strategy_terminal",
                clock_known=known,
                seconds_to_close=stc,
            )

        if not self._policy.enabled:
            return LoopDecision(
                continue_loop=True,
                reason="legacy_runtime",
                clock_known=known,
                seconds_to_close=stc,
            )

        if known and snapshot.phase == PHASE_CLOSED and not has_open_exposure:
            return LoopDecision(
                continue_loop=False,
                reason="market_closed_no_exposure",
                clock_known=True,
                seconds_to_close=stc,
            )

        if self.uses_fallback_runtime(snapshot):
            return LoopDecision(
                continue_loop=True,
                reason="fallback_runtime_active",
                clock_known=False,
                seconds_to_close=stc,
                used_fallback_runtime=True,
            )

        return LoopDecision(
            continue_loop=True,
            reason="market_clock_active",
            clock_known=known,
            seconds_to_close=stc,
        )
