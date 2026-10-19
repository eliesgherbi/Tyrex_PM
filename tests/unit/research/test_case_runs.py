"""Case runs on the production path: overlays, normalized comparison (plan P6)."""

from __future__ import annotations

from tyrex_pm.research.strategy_lab.case_runs import (
    VETO_CLASS,
    compare,
    decision_class,
    variant_overlays,
)
from tyrex_pm.research.strategy_lab.experiments import RESEARCH_BASE_PARAMETERS, merge
from tyrex_pm.strategies.settlement_value.schema import settlement_value_config_from_parameters


def test_variants_are_valid_strategy_configs_with_distinct_meaning() -> None:
    overlays = variant_overlays(9.0)
    configs = {name: settlement_value_config_from_parameters(
        merge(RESEARCH_BASE_PARAMETERS, overlay)) for name, overlay in overlays.items()}
    assert configs["unchanged"].volatility.variance_time_policy == "per_sample_v0"
    assert configs["time_corrected"].volatility.variance_time_policy == "elapsed_variance_v1"
    assert not configs["time_corrected"].disagreement_veto.enabled
    veto = configs["time_corrected_gap_veto"].disagreement_veto
    assert veto.enabled and veto.max_gap_pp == 9.0 and not veto.recheck_at_dispatch


def test_study_and_strategy_vetoes_share_one_class() -> None:
    assert decision_class({"action": "BLOCKED", "reason": "ENTRY_NOT_ALLOWED",
                           "study_veto": "STUDY_DISAGREEMENT_VETO"}) == VETO_CLASS
    assert decision_class({"action": "WAIT", "reason": "DISAGREEMENT_VETO"}) == VETO_CLASS
    assert decision_class({"action": "WAIT", "reason": "DISAGREEMENT_INPUT_INVALID"}) == VETO_CLASS
    assert decision_class({"action": "BLOCKED", "reason": "ENTRY_NOT_ALLOWED"}) == "BLOCKED"


def _norm(classes, reasons, entries=(), fills=(), pnl=None):
    return {"evaluations": list(range(len(classes))), "classes": list(classes),
            "reasons": list(reasons), "entries": list(entries), "fills": list(fills),
            "economics": {"realized_pnl": pnl, "filled": bool(fills)}}


def test_reason_renames_are_reported_but_behaviour_is_equal() -> None:
    old = _norm([VETO_CLASS, "ENTER"], [("BLOCKED", "ENTRY_NOT_ALLOWED"), ("ENTER", "EDGE")],
                entries=[(1, "UP")], fills=[(2, "up", "5", "0.5", "0")], pnl=0.25)
    new = _norm([VETO_CLASS, "ENTER"], [("WAIT", "DISAGREEMENT_VETO"), ("ENTER", "EDGE")],
                entries=[(1, "UP")], fills=[(2, "up", "5", "0.5", "0")], pnl=0.2500000000001)
    result = compare(old, new)
    assert result["behaviour_equal"]
    assert result["reason_changes"] == {
        "BLOCKED/ENTRY_NOT_ALLOWED -> WAIT/DISAGREEMENT_VETO": 1}


def test_a_different_entry_or_fill_is_a_behaviour_difference() -> None:
    old = _norm(["ENTER"], [("ENTER", "EDGE")], entries=[(0, "UP")])
    new = _norm(["WAIT"], [("WAIT", "EDGE_BELOW_COST")])
    result = compare(old, new)
    assert not result["behaviour_equal"]
    assert result["decision_class_mismatches"] == 1 and not result["same_entries"]
