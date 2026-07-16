"""CLI wrapper tests for go_z_gap_tiny_live.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_go_script_non_interactive_requires_next_window_or_url() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "go_z_gap_tiny_live.py"), "--run-name", "test"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2


def test_go_script_rejects_non_interactive_with_execute() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "go_z_gap_tiny_live.py"),
            "--run-name",
            "test",
            "--next-window",
            "--non-interactive",
            "--execute",
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "non-interactive" in proc.stderr.lower() or "non-interactive" in proc.stdout.lower()
