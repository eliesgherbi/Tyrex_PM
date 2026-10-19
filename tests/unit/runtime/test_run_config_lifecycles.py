"""Run-config rules for managed exits, continuous operation, run limits and paper venues."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tyrex_pm.runtime.run_config import RunConfigError, load_trading_run_config

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "config" / "runs"
TRIAL = RUNS / "settlement_value_trial.yaml"
PAPER = RUNS / "settlement_value_continuous_paper.yaml"
MANAGED = RUNS / "settlement_value_managed_paper.yaml"
LIVE_TEMPLATE = RUNS / "settlement_value_continuous_live.template.yaml"


def write(tmp_path: Path, source: Path, mutate) -> Path:
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_shipped_profiles() -> None:
    paper = load_trading_run_config(PAPER)
    assert (paper.venue, paper.market.run_mode, paper.lifecycle_mode) == (
        "paper", "continuous", "hold_to_resolution")
    assert paper.settlement.run_limits is not None and paper.strategy.retry.enabled
    assert paper.strategy.retry.max_attempts_per_episode == 3
    managed = load_trading_run_config(MANAGED)
    assert managed.lifecycle_mode == "managed_exit" and managed.protection is not None
    assert managed.strategy.reentry.max_trades_per_window == 2
    trial = load_trading_run_config(TRIAL)
    assert trial.market.run_mode == "single_window" and trial.venue == "live"
    with pytest.raises(RunConfigError, match="run_limits"):
        load_trading_run_config(LIVE_TEMPLATE)


def test_protection_cannot_be_combined_with_hold_to_resolution(tmp_path: Path) -> None:
    def add(raw):
        raw["protection"] = {"mark": "bid", "size_mode": "full",
                             "stop_loss": {"style": "stop_market_fak", "absolute": "0.2"}}

    with pytest.raises(RunConfigError, match="never sold"):
        load_trading_run_config(write(tmp_path, TRIAL, add))


def test_lifecycle_mode_must_match_the_strategy_exit_policy(tmp_path: Path) -> None:
    with pytest.raises(RunConfigError, match="supports lifecycle.mode"):
        load_trading_run_config(
            write(tmp_path, TRIAL, lambda r: r["lifecycle"].update(mode="managed_exit"))
        )


def test_continuous_requires_explicit_run_limits(tmp_path: Path) -> None:
    def drop(raw):
        raw["settlement"].pop("run_limits")

    with pytest.raises(RunConfigError, match="explicit settlement.run_limits"):
        load_trading_run_config(write(tmp_path, PAPER, drop))

    def partial(raw):
        raw["settlement"]["run_limits"].pop("daily_loss_limit")

    with pytest.raises(RunConfigError, match="no defaults"):
        load_trading_run_config(write(tmp_path, PAPER, partial))


def test_continuous_duration_is_unbounded_unless_explicit(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from tyrex_pm.runtime.trading_runtime import TradingRuntime

    paper = load_trading_run_config(PAPER)
    assert TradingRuntime._max_duration_s(SimpleNamespace(config=paper)) is None
    bounded = load_trading_run_config(
        write(tmp_path, PAPER, lambda r: r["market"].update(maximum_duration_s=3600))
    )
    assert TradingRuntime._max_duration_s(SimpleNamespace(config=bounded)) == 3600
    trial = load_trading_run_config(TRIAL)
    assert TradingRuntime._max_duration_s(SimpleNamespace(config=trial)) == 300


def test_paper_state_must_be_separate(tmp_path: Path) -> None:
    with pytest.raises(RunConfigError, match="contains 'paper'"):
        load_trading_run_config(
            write(tmp_path, PAPER, lambda r: r.update(state_directory="var/runtime_state/x"))
        )
    with pytest.raises(RunConfigError, match="requires venue: paper"):
        load_trading_run_config(
            write(tmp_path, TRIAL, lambda r: r.update(paper={"initial_collateral": "1"}))
        )


def test_managed_exit_cutoff_precedes_the_host_flatten(tmp_path: Path) -> None:
    def early(raw):
        raw["strategy"]["parameters"]["exit"]["exit_cutoff_tau_s"] = 10

    with pytest.raises(RunConfigError, match="exit_cutoff_tau_s"):
        load_trading_run_config(write(tmp_path, MANAGED, early))


def test_live_opposite_side_reentry_is_refused(tmp_path: Path) -> None:
    def opposite(raw):
        raw["strategy"]["parameters"]["reentry"]["opposite_side_allowed"] = True

    with pytest.raises(RunConfigError, match="research-only"):
        load_trading_run_config(write(tmp_path, MANAGED, opposite))


def test_cli_paper_flags(capsys) -> None:
    from tyrex_pm.application.cli import main

    assert main(["run", "--config", str(PAPER), "--live"]) == 2
    assert "refusing --live" in capsys.readouterr().err
    assert main(["run", "--config", str(PAPER)]) == 2
    assert "--paper" in capsys.readouterr().err
    assert main(["run", "--config", str(TRIAL), "--paper"]) == 2
    assert "requires a config with venue: paper" in capsys.readouterr().err
    assert main(["run", "--config", str(PAPER), "--validate-config"]) == 0
    assert "venue=paper run_mode=continuous" in capsys.readouterr().out


def test_paper_composition_never_reads_credentials(tmp_path: Path, monkeypatch) -> None:
    from tyrex_pm.execution.paper import PaperVenue
    from tyrex_pm.runtime import trading_runtime as tr

    def boom(*_a, **_k):
        raise AssertionError("authenticated gateway must not be constructed in paper mode")

    monkeypatch.setattr(tr.PolymarketAsyncGateway, "from_env", boom)
    config = load_trading_run_config(
        write(tmp_path, PAPER, lambda r: r.update(
            state_directory=str(tmp_path / "paper" / "state")))
    )
    venue = tr.build_paper_venue(config)
    assert isinstance(venue, PaperVenue)
    assert (tmp_path / "paper" / "state" / "paper_account.json").exists()


def test_settlement_cli_acknowledges_a_loss_pause(tmp_path: Path, capsys) -> None:
    import json

    from tyrex_pm.application.cli import main

    cfg = write(tmp_path, PAPER, lambda r: r["settlement"].update(
        ledger_path=str(tmp_path / "paper" / "l.jsonl")))
    assert main(["settlement", "--config", str(cfg), "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["consecutive_losses"] == 0 and status["run_limits"]["accounting_timezone"]
    assert status["accounting_day_utc"] and status["reported_at_paris"].endswith(("+01:00", "+02:00"))
    assert main(["settlement", "--config", str(cfg), "ack-loss-pause", "--reason", "review"]) == 0
    assert json.loads(capsys.readouterr().out)["acknowledged_losses"] == 0


def test_recording_block_is_parsed_and_validated(tmp_path: Path) -> None:
    from tyrex_pm.recording import RecordingLevel

    assert load_trading_run_config(PAPER).recording.level is RecordingLevel.DECISIONS

    def explain(raw):
        raw["recording"] = {"level": "explain", "explain_trigger": "all", "queue_max_mib": 8}

    cfg = load_trading_run_config(write(tmp_path, PAPER, explain)).recording
    assert cfg.level is RecordingLevel.EXPLAIN and cfg.queue_max_bytes == 8 * 1024 * 1024

    def bad(raw):
        raw["recording"] = {"level": "verbose"}

    with pytest.raises(RunConfigError):
        load_trading_run_config(write(tmp_path, PAPER, bad))
