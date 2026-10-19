"""Disagreement veto (strategy config `disagreement_veto`): a WAIT, never an attempt.

The driver used here is the one live, observation and replay run; the pre-send recheck
uses the shared planner and dispatch_check.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_settlement_value import START, context, driver, run, snapshot, vol

from tyrex_pm.core.disagreement import INPUT_INVALID, VETO, check_disagreement
from tyrex_pm.core.intents import EnterIntent
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.strategies.decisions import StrategyAction
from tyrex_pm.strategies.settlement_value import reasons as R
from tyrex_pm.strategies.settlement_value.state import EpisodePhase

AT = START + timedelta(seconds=120)  # tau 180 s, model P(UP) ~ 0.5, UP ask 0.30


def veto(max_gap_pp: float, **extra) -> dict:
    return {"disagreement_veto": {"enabled": True, "max_gap_pp": max_gap_pp, **extra}}


@pytest.mark.parametrize("p,bid,ask,reason", [
    (0.99, 0.10, 0.12, VETO),
    (0.55, 0.50, 0.52, None),
    (0.55, 0.60, 0.50, INPUT_INVALID),  # crossed
    (0.55, None, 0.52, INPUT_INVALID),  # one-sided
    (float("nan"), 0.10, 0.12, INPUT_INVALID),
    (1.2, 0.10, 0.12, INPUT_INVALID),
])
def test_rule_units_and_invalid_inputs(p, bid, ask, reason) -> None:
    check = check_disagreement(p_up=p, up_bid=bid, up_ask=ask, max_gap_pp=9)
    assert check.reason == reason
    if reason is None:
        assert check.gap_pp == pytest.approx(100 * abs(0.55 - 0.51))


def test_disabled_by_default_changes_nothing() -> None:
    first = run(driver(), AT)
    assert first.decision.action is StrategyAction.ENTER
    assert "disagreement_gap_pp" not in first.reporting_context["decision_row"]


def test_veto_waits_without_starting_an_episode_and_releases() -> None:
    d = driver(**veto(15))
    vetoed = run(d, AT)  # P(UP) 0.5 vs UP mid 0.295: 20.5 points
    assert vetoed.decision.action is StrategyAction.WAIT
    assert vetoed.decision.reason_code == R.DISAGREEMENT_VETO and not vetoed.intents
    row = vetoed.reporting_context["decision_row"]
    assert row["disagreement_gap_pp"] == pytest.approx(20.5) and row["disagreement_reason"] == VETO
    st = d.state
    assert st.phase is EpisodePhase.IDLE
    assert st.episode_seq == 0 and st.attempt_seq == 0 and st.window_attempts == 0
    # The market comes towards the model: the next evaluation re-checks everything.
    released = run(d, AT + timedelta(seconds=1), up_ask="0.40")  # 10.5 points
    assert released.decision.action is StrategyAction.ENTER
    assert isinstance(released.intents[0], EnterIntent)
    assert d.state.attempt_seq == 1 and d.state.window_attempts == 1


def test_invalid_comparison_inputs_wait() -> None:
    d = driver(**veto(9))
    snap = snapshot(AT)
    one_sided = BookSnapshot(instrument_id=snap.yes_book.instrument_id, ts_event=AT,
                             bids=(), asks=snap.yes_book.asks)
    from dataclasses import replace

    snap = replace(snap, yes_book=one_sided)
    result = d.evaluate(market_snapshot=snap, causation_id=None,
                        correlation_id=snap.correlation_id, context=context(snap),
                        trigger="test", features=vol())
    assert result.decision.reason_code in (R.DISAGREEMENT_INPUT_INVALID, "BOOK_NOT_READY",
                                           "DEPTH_INSUFFICIENT")
    assert not result.intents and d.state.attempt_seq == 0


def test_rearmable_retry_only_waits_and_keeps_its_clock() -> None:
    from tyrex_pm.strategies.outcomes import EntryOutcome, EntryOutcomeKind, RetryClass

    d = driver(**veto(50), retry={"enabled": True})
    first = run(d, AT)
    (intent,) = first.intents
    d.on_entry_outcome(EntryOutcome(EntryOutcomeKind.NOT_SENT, intent.intent_id.value,
                                    AT, reason="book_stale",
                                    retry_class=RetryClass.RETRY_WHEN_CLEARED))
    assert d.state.phase is EpisodePhase.REARMABLE
    started = d.state.episode_started_at
    d.config = d.config.__class__(**{**d.config.__dict__, "disagreement_veto":
                                     d.config.disagreement_veto.__class__(
                                         enabled=True, max_gap_pp=9.0)})
    waiting = run(d, AT + timedelta(seconds=5))
    assert waiting.decision.reason_code == R.DISAGREEMENT_VETO and not waiting.intents
    assert d.state.attempt_seq == 1 and d.state.episode_started_at == started


def test_an_existing_position_or_hold_is_never_vetoed() -> None:
    d = driver(**veto(9))
    d.on_hold_committed()
    held = run(d, AT, ctx_kwargs={"qty": "10"})
    assert held.decision.action is StrategyAction.HOLD
    assert held.decision.reason_code != R.DISAGREEMENT_VETO


def test_pre_send_recheck_travels_through_the_planner_to_the_shared_gate() -> None:
    from tyrex_pm.execution.final_gate import _strategy_dispatch_checks
    from tyrex_pm.execution.outcome_reasons import classify_reason
    from tyrex_pm.execution.planner import ExecutionRiskPolicy, IntentOrderPlanner
    from tyrex_pm.strategies.outcomes import RetryClass

    d = driver(**veto(40, recheck_at_dispatch=True))
    (intent,) = run(d, AT).intents  # 20.5 points at decision: within 40
    checks = intent.evidence["dispatch_checks"]["disagreement"]
    assert checks["max_gap_pp"] == 40
    spec = IntentOrderPlanner(ExecutionRiskPolicy(maximum_total_debit=Decimal("5"))).entry(
        intent, token_id="up")
    assert spec.metadata["dispatch_checks"]["disagreement"] == checks

    def view(bid, ask):
        return SimpleNamespace(up=SimpleNamespace(quote=SimpleNamespace(
            best_bid=Decimal(bid), best_ask=Decimal(ask))))

    assert _strategy_dispatch_checks(spec, view("0.40", "0.42")) is None
    refused = _strategy_dispatch_checks(spec, view("0.01", "0.02"))  # 48.5 points
    assert refused == "disagreement_veto_at_dispatch"
    assert classify_reason(f"FINAL_GATE_REJECTED:{refused}") is RetryClass.RETRY_WHEN_CLEARED
    # Without the option nothing is added to the order.
    plain = run(driver(**veto(50)), AT).intents[0]
    assert "dispatch_checks" not in plain.evidence


def test_recheck_requires_the_veto() -> None:
    with pytest.raises(Exception):
        driver(disagreement_veto={"recheck_at_dispatch": True})
