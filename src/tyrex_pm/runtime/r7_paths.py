"""R7 directory contract: durable runtime state vs disposable reports.

``var/runs/`` — common reporter outputs (strategy runs and ``_ops`` tools).
``var/runtime_state/`` — required local runtime/safety artifacts (must survive
report cleanup). Replaces the former ``var/state/`` active root.
``var/recordings/`` — raw feed captures (N1/N3).
``config/r7/`` — sealed acknowledgment policy (committed source of truth).

Deleting historical reports under ``var/runs/`` or legacy ``var/reporting/``
must never delete acknowledgment, policy, or residual-registry state under
``var/runtime_state/`` / ``config/r7/``.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Disposable (legacy root retained only for historical evidence classification)
REPORTING_ROOT = Path("var/reporting")
R7B_REPORT_DIR = Path("var/runs/_ops/r7b")
R7C_REPORT_DIR = Path("var/runs/_ops/r7c")

# Durable required state (active)
STATE_ROOT = Path("var/runtime_state")
LEGACY_STATE_ROOT = Path("var/state")
R7_STATE_DIR = STATE_ROOT / "r7"
DEFAULT_ACKNOWLEDGMENT_PATH = R7_STATE_DIR / "position_acknowledgment.json"
DEFAULT_LIFECYCLE_DUST_PATH = R7_STATE_DIR / "lifecycle_dust.json"  # legacy scalar
DEFAULT_LIFECYCLE_RESIDUALS_PATH = R7_STATE_DIR / "lifecycle_residuals.json"
DEFAULT_ACK_POLICY_STATE_PATH = R7_STATE_DIR / "acknowledgment_policy.json"

# Committed sealed policy (not disposable reports)
CONFIG_ACK_POLICY_PATH = Path("config/r7/acknowledgment_policy.json")

DISPOSABLE_GLOBS = (
    "var/reporting/**",
    "var/runs/**",
)
DURABLE_STATE_GLOBS = (
    "var/runtime_state/r7/position_acknowledgment.json",
    "var/runtime_state/r7/lifecycle_residuals.json",
    "var/runtime_state/r7/lifecycle_dust.json",
    "var/runtime_state/r7/acknowledgment_policy.json",
    "config/r7/acknowledgment_policy.json",
)

_AUTHORITATIVE_RELATIVE = (
    "r7/position_acknowledgment.json",
    "r7/lifecycle_residuals.json",
    "r7/lifecycle_dust.json",
    "r7/acknowledgment_policy.json",
)


@dataclass(frozen=True)
class StateMigrationResult:
    ok: bool
    migrated: list[str]
    skipped_identical: list[str]
    conflicts: list[str]
    refused_live: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "migrated": list(self.migrated),
            "skipped_identical": list(self.skipped_identical),
            "conflicts": list(self.conflicts),
            "refused_live": self.refused_live,
            "message": self.message,
        }


def ensure_r7_state_dir(repo_root: Path | None = None) -> Path:
    root = (repo_root or Path.cwd()) / R7_STATE_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_acknowledgment_path(
    override: Path | None = None,
    *,
    repo_root: Path | None = None,
) -> Path:
    """Resolved absolute/relative path for the mandatory ack artifact."""
    if override is not None:
        return override
    base = repo_root or Path.cwd()
    return base / DEFAULT_ACKNOWLEDGMENT_PATH


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def classify_authoritative_state(root: Path) -> list[Path]:
    found: list[Path] = []
    for rel in _AUTHORITATIVE_RELATIVE:
        p = root / rel
        if p.is_file():
            found.append(p)
    return found


def live_refuses_on_state_conflict(
    *,
    repo_root: Path | None = None,
    old_root: Path | None = None,
    new_root: Path | None = None,
) -> tuple[bool, list[str]]:
    """Return (refuse_live, conflict_paths) when old and new authoritative state disagree."""
    base = repo_root or Path.cwd()
    old = old_root or (base / LEGACY_STATE_ROOT)
    new = new_root or (base / STATE_ROOT)
    conflicts: list[str] = []
    for rel in _AUTHORITATIVE_RELATIVE:
        a = old / rel
        b = new / rel
        if a.is_file() and b.is_file() and _file_digest(a) != _file_digest(b):
            conflicts.append(rel.replace("\\", "/"))
    return (bool(conflicts), conflicts)


def migrate_state_to_runtime_state(
    repo_root: Path | None = None,
    *,
    dry_run: bool = False,
    old_root: Path | None = None,
    new_root: Path | None = None,
) -> StateMigrationResult:
    """Safe one-time ``var/state`` → ``var/runtime_state`` migration.

    Does not overwrite differing destination files. On conflict, preserves both
    and reports ``refused_live=True`` so LIVE must not start until resolved.
    No permanent alias is created linking the legacy and current state roots.
    """
    base = repo_root or Path.cwd()
    old = Path(old_root) if old_root is not None else base / LEGACY_STATE_ROOT
    new = Path(new_root) if new_root is not None else base / STATE_ROOT

    if not old.exists():
        new.mkdir(parents=True, exist_ok=True)
        (new / "r7").mkdir(parents=True, exist_ok=True)
        return StateMigrationResult(
            ok=True,
            migrated=[],
            skipped_identical=[],
            conflicts=[],
            refused_live=False,
            message="no legacy var/state; runtime_state ready",
        )

    migrated: list[str] = []
    skipped: list[str] = []
    conflicts: list[str] = []

    if not dry_run:
        new.mkdir(parents=True, exist_ok=True)

    for src in old.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(old).as_posix()
        dest = new / rel
        if dest.is_file():
            if _file_digest(src) == _file_digest(dest):
                skipped.append(rel)
                continue
            conflicts.append(rel)
            continue
        if dry_run:
            migrated.append(rel)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        if not dest.is_file() or _file_digest(src) != _file_digest(dest):
            return StateMigrationResult(
                ok=False,
                migrated=migrated,
                skipped_identical=skipped,
                conflicts=conflicts + [rel],
                refused_live=True,
                message=f"verification failed for {rel}",
            )
        migrated.append(rel)

    refuse, conflict_paths = live_refuses_on_state_conflict(
        repo_root=base, old_root=old, new_root=new
    )
    for c in conflict_paths:
        if c not in conflicts:
            conflicts.append(c)

    ok = not conflicts
    return StateMigrationResult(
        ok=ok,
        migrated=migrated,
        skipped_identical=skipped,
        conflicts=conflicts,
        refused_live=bool(conflicts) or refuse,
        message=(
            "migration complete"
            if ok
            else "authoritative state conflict; LIVE must refuse until owner resolves"
        ),
    )


def assert_live_runtime_state_ready(
    repo_root: Path | None = None,
    *,
    old_root: Path | None = None,
    new_root: Path | None = None,
) -> None:
    """Raise RuntimeError when LIVE must refuse due to state conflict."""
    refuse, conflicts = live_refuses_on_state_conflict(
        repo_root=repo_root, old_root=old_root, new_root=new_root
    )
    if refuse:
        raise RuntimeError(
            "LIVE refused: conflicting authoritative state between "
            f"var/state and var/runtime_state: {conflicts}"
        )
