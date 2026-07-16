"""R6D user-stream idle auth classification (no network required)."""

from __future__ import annotations


def test_idle_account_auth_rules() -> None:
    """Document acceptance: connect+auth send+pong+no error ⇒ authenticated."""
    report = {
        "connected": True,
        "auth_message_sent": True,
        "auth_error": False,
        "pong_seen": True,
        "events_received": 0,
    }
    authenticated = (
        report["connected"]
        and report["auth_message_sent"]
        and not report["auth_error"]
        and report["pong_seen"]
    )
    assert authenticated is True
    assert report["events_received"] == 0  # idle OK
