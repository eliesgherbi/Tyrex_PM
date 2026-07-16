"""CLI smoke tests for the R1 skeleton."""

from __future__ import annotations

from tyrex_pm import __version__
from tyrex_pm.application.cli import main


def test_version_matches_package() -> None:
    assert __version__ == "0.3.0"


def test_cli_version_command(capsys) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_cli_help_command(capsys) -> None:
    assert main(["help"]) == 0
    out = capsys.readouterr().out
    assert "tyrex-pm" in out
    assert "version" in out
