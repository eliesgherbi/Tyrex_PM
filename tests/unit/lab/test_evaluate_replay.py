"""F5 lab: evaluate() on fixtures without the venue gateway."""

from __future__ import annotations

from pathlib import Path

from tyrex_pm.application.cli import main
from tyrex_pm.core.intents import EnterIntent
from tyrex_pm.lab.evaluate_replay import replay_configured_run
from tyrex_pm.strategies.decisions import StrategyAction
from tyrex_pm.strategies.feature_probe.reasons import FeatureProbeReason

ASK70_YAML = Path("config/runs/ask70_protection_tiny_live.yaml")
PROBE_YAML = Path("config/runs/feature_probe_catalog.yaml")
ZGAP_YAML = Path("config/runs/z_gap_tiny_live.yaml")
ASK70_FIX = Path("tests/fixtures/lab/ask70_books.json")
PROBE_FIX = Path("tests/fixtures/lab/feature_probe_l2.json")
ZGAP_FIX = Path("tests/fixtures/lab/z_gap_trades.json")


def test_lab_module_does_not_import_gateway() -> None:
    source = Path("src/tyrex_pm/lab/evaluate_replay.py").read_text(encoding="utf-8")
    assert "polymarket.gateway" not in source
    assert "AsyncSecureClient" not in source


def test_ask70_lab_enters_when_ask_hits() -> None:
    results = replay_configured_run(config_path=ASK70_YAML, fixture_path=ASK70_FIX)
    assert len(results) == 1
    result = results[0]
    assert result.decision.action is StrategyAction.ENTER
    assert result.intents
    assert isinstance(result.intents[0], EnterIntent)


def test_feature_probe_lab_records_imbalance_without_entry() -> None:
    results = replay_configured_run(config_path=PROBE_YAML, fixture_path=PROBE_FIX)
    assert len(results) == 1
    result = results[0]
    assert result.intents == ()
    assert result.decision.reason_code == FeatureProbeReason.FEATURE_OBSERVED.value
    assert result.decision.evidence.get("imbalance") is not None
    assert result.eligibility.strategy_inputs_eligible is True


def test_z_gap_lab_reaches_evaluate_without_gateway() -> None:
    results = replay_configured_run(config_path=ZGAP_YAML, fixture_path=ZGAP_FIX)
    assert len(results) == 1
    result = results[0]
    assert result.eligibility is not None
    assert result.decision.action in {
        StrategyAction.SKIP,
        StrategyAction.HOLD,
        StrategyAction.WAIT,
        StrategyAction.BLOCKED,
    }


def test_eval_cli_refuses_live() -> None:
    code = main(
        [
            "eval",
            "--config",
            str(ASK70_YAML),
            "--fixture",
            str(ASK70_FIX),
            "--live",
        ]
    )
    assert code == 2


def test_eval_cli_prints_decisions(capsys) -> None:
    code = main(["eval", "--config", str(ASK70_YAML), "--fixture", str(ASK70_FIX)])
    assert code == 0
    captured = capsys.readouterr()
    assert "ENTER" in captured.out
    assert "intent_count" in captured.out
