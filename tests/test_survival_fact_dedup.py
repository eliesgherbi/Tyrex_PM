"""Survival advisory periodic fact dedup tests."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.strategies.paired_binary.facts import should_emit_survival_periodic
from tyrex_pm.strategies.paired_binary.state import PairedBinaryRuntimeState


def test_repeated_identical_ticks_deduped() -> None:
    state = PairedBinaryRuntimeState()
    assert should_emit_survival_periodic(
        state, "surv_exec", fingerprint="snap:0.55", min_emit_interval_s=5.0
    )
    assert not should_emit_survival_periodic(
        state, "surv_exec", fingerprint="snap:0.55", min_emit_interval_s=5.0, now_mono=1.0
    )


def test_verdict_change_emits_immediately() -> None:
    state = PairedBinaryRuntimeState()
    assert should_emit_survival_periodic(
        state, "surv_reach", fingerprint="weak:0.40", min_emit_interval_s=5.0, now_mono=0.0
    )
    assert should_emit_survival_periodic(
        state,
        "surv_reach",
        fingerprint="reachable:0.80",
        min_emit_interval_s=5.0,
        emit_on_change=True,
        now_mono=0.1,
    )


def test_material_stall_not_using_periodic_dedup() -> None:
    from tyrex_pm.strategies.paired_binary.facts import should_emit

    state = PairedBinaryRuntimeState()
    assert should_emit(state, "surv_stall_detected")
    assert not should_emit(state, "surv_stall_detected")
