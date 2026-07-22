"""N7 CLI safety ceremony tests (no venue I/O)."""

from __future__ import annotations

from tyrex_pm.application.cli import build_parser, main


def test_cli_help_describes_n7_ceremony():
    parser = build_parser()
    help_text = parser.format_help()
    n7 = parser._subparsers._group_actions[0].choices["n7-oneshot"]
    oneshot_help = n7.format_help()
    assert "authorization-phrase" in oneshot_help
    assert "authorization" in oneshot_help.lower()
    assert "dry-run" in oneshot_help
    auth_help = parser._subparsers._group_actions[0].choices["n7-auth-request"].format_help()
    assert "request" in auth_help.lower()
    assert "n7-auth-request" in help_text or "n7-oneshot" in help_text


def test_n7_oneshot_refuses_without_phrase():
    code = main(["n7-oneshot", "--dry-run"])
    assert code == 2


def test_n7_status_mutations_off():
    code = main(["n7-status"])
    assert code == 0
