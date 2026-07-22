"""N7 CLI: no ceremony; --live is the authorization."""

from __future__ import annotations

from tyrex_pm.application.cli import build_parser, main


def test_cli_help_has_live_not_phrase():
    parser = build_parser()
    n7 = parser._subparsers._group_actions[0].choices["n7-live"]
    help_text = n7.format_help().lower()
    assert "--live" in help_text
    assert "authorization-phrase" not in help_text
    assert "envelope" not in help_text or "no envelope" in help_text


def test_n7_status_no_ceremony():
    assert main(["n7-status"]) == 0


def test_n7_auth_request_removed():
    # Subcommand should not exist
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert "n7-auth-request" not in choices
    assert "n7-oneshot" not in choices or "n7-live" in choices
