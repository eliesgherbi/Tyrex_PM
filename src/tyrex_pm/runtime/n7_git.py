"""Git HEAD / worktree helpers for N7 authorization gates."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, kw_only=True)
class GitIdentity:
    head: str
    worktree_clean: bool
    status_porcelain: str


def inspect_git(repo: Path) -> GitIdentity:
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        text=True,
    ).strip()
    porcelain = subprocess.check_output(
        ["git", "status", "--porcelain"],
        cwd=str(repo),
        text=True,
    )
    return GitIdentity(
        head=head,
        worktree_clean=porcelain.strip() == "",
        status_porcelain=porcelain,
    )
