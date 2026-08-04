"""CLI tests for tyrex-pm run (YAML OBSERVE/SHADOW)."""

from __future__ import annotations

from pathlib import Path

from tyrex_pm.application.cli import main

ROOT = Path(__file__).resolve().parents[1]


def _base_args(*extra: str) -> list[str]:
    return [
        "run",
        "--strategy",
        str(ROOT / "config/strategies/z_gap.yaml"),
        "--risk",
        str(ROOT / "config/risk/example_risk.yaml"),
        "--execution",
        str(ROOT / "config/execution/example.yaml"),
        "--runtime",
        str(ROOT / "config/runtime/observe_btc_5m.yaml"),
        "--mode",
        "observe",
        *extra,
    ]


def test_validate_config_ok():
    assert main(_base_args("--validate-config", "--scenario", "aggressive")) == 0


def test_validate_config_fails_unknown(tmp_path: Path, monkeypatch):
    bad = tmp_path / "bad_risk.yaml"
    bad.write_text("target_notional: \"5\"\nnot_a_field: 1\n", encoding="utf-8")
    code = main(
        [
            "run",
            "--strategy",
            str(ROOT / "config/strategies/z_gap.yaml"),
            "--risk",
            str(bad),
            "--execution",
            str(ROOT / "config/execution/example.yaml"),
            "--runtime",
            str(ROOT / "config/runtime/observe_btc_5m.yaml"),
            "--mode",
            "observe",
            "--validate-config",
        ]
    )
    assert code == 2


def test_show_config_no_host(capsys):
    assert main(_base_args("--show-config", "--run-name", "show_test")) == 0
    out = capsys.readouterr().out
    assert '"theta_fill_floor"' in out
    assert '"fee_curve"' in out
    assert "show_test" in out


def test_mutually_exclusive_inspect_flags():
    import pytest

    with pytest.raises(SystemExit) as exc:
        main(_base_args("--validate-config", "--show-config"))
    assert exc.value.code == 2


def test_accepts_live_mode_choice():
    assert (
        main(
            [
                "run",
                "--mode",
                "live",
                "--runtime",
                str(ROOT / "config/runtime/live_btc_5m.yaml"),
                "--validate-config",
            ]
        )
        == 0
    )


def test_run_observe_cli(tmp_path: Path):
    assert (
        main(
            _base_args(
                "--scenario",
                "aggressive",
                "--run-name",
                "cli_yaml_observe",
                "--out-dir",
                str(tmp_path / "observe_run"),
            )
        )
        == 0
    )
    assert (tmp_path / "observe_run" / "manifest.json").is_file()
    assert (tmp_path / "observe_run" / "run_summary.json").is_file()


def test_run_shadow_cli(tmp_path: Path):
    assert (
        main(
            [
                "run",
                "--strategy",
                str(ROOT / "config/strategies/z_gap.yaml"),
                "--risk",
                str(ROOT / "config/risk/example_risk.yaml"),
                "--execution",
                str(ROOT / "config/execution/shadow_example.yaml"),
                "--runtime",
                str(ROOT / "config/runtime/observe_btc_5m.yaml"),
                "--scenario",
                "aggressive",
                "--mode",
                "shadow",
                "--run-name",
                "cli_yaml_shadow",
                "--out-dir",
                str(tmp_path / "shadow_run"),
            ]
        )
        == 0
    )
    assert (tmp_path / "shadow_run" / "manifest.json").is_file()


def test_live_flag_on_run_parser_for_live_mode():
    from tyrex_pm.application.cli import build_parser

    parser = build_parser()
    run = None
    for action in parser._subparsers._group_actions:  # noqa: SLF001
        if action.dest == "command":
            run = action.choices.get("run")
    assert run is not None
    option_strings = {opt for a in run._actions for opt in a.option_strings}  # noqa: SLF001
    assert "--live" in option_strings
    assert "--fake-rehearsal" in option_strings
    assert "--reporting" in option_strings
    mode_action = next(a for a in run._actions if "--mode" in a.option_strings)
    assert set(mode_action.choices) == {"observe", "shadow", "live"}
