"""R7 directory contract: durable state vs disposable reports.

``var/reporting/`` — disposable run reports, facts, recon dumps.
``var/state/`` — required local runtime/safety artifacts (must survive report cleanup).
``config/r7/`` — sealed acknowledgment policy (committed source of truth).

Deleting historical reports under ``var/reporting/`` must never delete
acknowledgment, policy, or residual-registry state under ``var/state/`` / ``config/r7/``.
"""

from __future__ import annotations

from pathlib import Path

# Disposable
REPORTING_ROOT = Path("var/reporting")
R7B_REPORT_DIR = REPORTING_ROOT / "r7b"
R7C_REPORT_DIR = REPORTING_ROOT / "r7c"

# Durable required state
STATE_ROOT = Path("var/state")
R7_STATE_DIR = STATE_ROOT / "r7"
DEFAULT_ACKNOWLEDGMENT_PATH = R7_STATE_DIR / "position_acknowledgment.json"
DEFAULT_LIFECYCLE_DUST_PATH = R7_STATE_DIR / "lifecycle_dust.json"  # legacy scalar
DEFAULT_LIFECYCLE_RESIDUALS_PATH = R7_STATE_DIR / "lifecycle_residuals.json"
DEFAULT_ACK_POLICY_STATE_PATH = R7_STATE_DIR / "acknowledgment_policy.json"

# Committed sealed policy (not disposable reports)
CONFIG_ACK_POLICY_PATH = Path("config/r7/acknowledgment_policy.json")

DISPOSABLE_GLOBS = (
    "var/reporting/**",
)
DURABLE_STATE_GLOBS = (
    "var/state/r7/position_acknowledgment.json",
    "var/state/r7/lifecycle_residuals.json",
    "var/state/r7/lifecycle_dust.json",  # legacy; migrated into residuals
    "var/state/r7/acknowledgment_policy.json",
    "config/r7/acknowledgment_policy.json",
)


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
