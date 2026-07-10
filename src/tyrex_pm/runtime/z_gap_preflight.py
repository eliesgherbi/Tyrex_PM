"""Z-Gap Phase A preflight gate model and artifact readers (A0.1)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_Z_GAP_PREFLIGHT_DIR = Path("var/reporting/z_gap")

GATE_FEE_CURVE_SPIKE = "fee_curve_spike_passed"
GATE_BINANCE_CONNECTIVITY = "binance_connectivity_passed"
GATE_PTB_ATTESTATION = "ptb_attestation_passed"
GATE_CLOCK_SANITY = "clock_sanity_passed"
GATE_CALIBRATION_LITE = "calibration_lite_reviewed"
GATE_OPERATOR_APPROVED = "operator_approved_enforce"

ALL_ENFORCE_GATES = (
    GATE_FEE_CURVE_SPIKE,
    GATE_BINANCE_CONNECTIVITY,
    GATE_PTB_ATTESTATION,
    GATE_CLOCK_SANITY,
    GATE_CALIBRATION_LITE,
    GATE_OPERATOR_APPROVED,
)


@dataclass(frozen=True)
class ZGapPreflightGates:
    """Machine-readable enforce gate status for Z-Gap Phase A."""

    fee_curve_spike_passed: bool = False
    binance_connectivity_passed: bool = False
    ptb_attestation_passed: bool = False
    clock_sanity_passed: bool = False
    calibration_lite_reviewed: bool = False
    operator_approved_enforce: bool = False
    blockers: tuple[str, ...] = ()
    artifacts_dir: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def enforce_allowed(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["enforce_allowed"] = self.enforce_allowed
        return out


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _fee_curve_spike_passed(data: dict[str, Any] | None) -> tuple[bool, str]:
    if data is None:
        return False, "missing fee_curve_spike.json"
    if data.get("spike_passed") is True:
        return True, ""
    outcome = str(data.get("outcome", "")).strip().lower()
    if outcome in {"full_curve_available", "category_1", "1"}:
        return True, ""
    fd = data.get("fd")
    if isinstance(fd, dict) and fd.get("r") is not None and fd.get("e") is not None:
        return True, ""
    return False, "fee_curve_spike artifact does not confirm dynamic fd curve"


def _binance_connectivity_passed(data: dict[str, Any] | None) -> tuple[bool, str]:
    if data is None:
        return False, "missing binance_connectivity.json"
    if data.get("ok") is True:
        return True, ""
    return False, f"binance_connectivity not ok ({data.get('error') or 'ok=false'})"


def _ptb_attestation_passed(data: dict[str, Any] | None) -> tuple[bool, str]:
    if data is None:
        return False, "missing ptb_attestation.json"
    if data.get("attestation_pass") is not True:
        reason = data.get("attestation_fail_reason") or "attestation_pass is not true"
        return False, f"ptb attestation failed: {reason}"
    if data.get("golden_fixture_only") is True:
        return False, "ptb attestation from golden_day fixtures only (enforce requires fresh live windows)"
    if data.get("enforce_unlock_allowed") is False:
        return False, "ptb attestation marked enforce_unlock_allowed=false"
    err = data.get("ptb_error_bps")
    if err is not None:
        try:
            if float(err) > 0.5:
                return False, f"ptb_error_bps {err} exceeds 0.5 bps tolerance"
        except (TypeError, ValueError):
            return False, f"invalid ptb_error_bps: {err!r}"
    return True, ""


def _clock_sanity_passed(data: dict[str, Any] | None) -> tuple[bool, str]:
    if data is None:
        return False, "missing clock_sanity.json"
    if data.get("sync_status") == "synced" and data.get("enforce_gate_pass") is True:
        return True, ""
    sync_status = data.get("sync_status")
    uncertainty = data.get("time_authority_uncertainty_ms")
    if sync_status != "synced":
        return False, f"time_authority sync_status={sync_status!r} (authoritative enforce gate)"
    if not data.get("enforce_gate_pass"):
        return False, f"time_authority uncertainty_ms={uncertainty} failed enforce gate"
    reason = data.get("fail_reason") or "clock sanity enforce gate failed"
    return False, reason


def _calibration_lite_reviewed(data: dict[str, Any] | None) -> tuple[bool, str]:
    if data is None:
        return False, "missing calibration_lite_review.json"
    if data.get("reviewed") is True and data.get("operator_signoff") is True:
        return True, ""
    return False, "calibration_lite not reviewed / operator sign-off missing"


def _operator_approved_enforce(data: dict[str, Any] | None) -> tuple[bool, str]:
    if data is None:
        return False, "missing operator_enforce_approval.json"
    if data.get("approved") is True:
        return True, ""
    return False, "operator enforce approval missing"


def load_z_gap_preflight_gates(
    artifacts_dir: Path | str | None = None,
) -> ZGapPreflightGates:
    """Read preflight artifacts and evaluate enforce gate contract."""
    base = Path(artifacts_dir) if artifacts_dir is not None else DEFAULT_Z_GAP_PREFLIGHT_DIR
    base = base.resolve()

    checks: list[tuple[str, tuple[bool, str]]] = [
        (GATE_FEE_CURVE_SPIKE, _fee_curve_spike_passed(_read_json(base / "fee_curve_spike.json"))),
        (
            GATE_BINANCE_CONNECTIVITY,
            _binance_connectivity_passed(_read_json(base / "binance_connectivity.json")),
        ),
        (GATE_PTB_ATTESTATION, _ptb_attestation_passed(_read_json(base / "ptb_attestation.json"))),
        (GATE_CLOCK_SANITY, _clock_sanity_passed(_read_json(base / "clock_sanity.json"))),
        (
            GATE_CALIBRATION_LITE,
            _calibration_lite_reviewed(_read_json(base / "calibration_lite_review.json")),
        ),
        (
            GATE_OPERATOR_APPROVED,
            _operator_approved_enforce(_read_json(base / "operator_enforce_approval.json")),
        ),
    ]

    blockers: list[str] = []
    details: dict[str, Any] = {}
    gate_values: dict[str, bool] = {}
    for gate_name, (passed, reason) in checks:
        gate_values[gate_name] = passed
        details[gate_name] = {"passed": passed, "reason": reason}
        if not passed and reason:
            blockers.append(f"{gate_name}: {reason}")

    return ZGapPreflightGates(
        fee_curve_spike_passed=gate_values[GATE_FEE_CURVE_SPIKE],
        binance_connectivity_passed=gate_values[GATE_BINANCE_CONNECTIVITY],
        ptb_attestation_passed=gate_values[GATE_PTB_ATTESTATION],
        clock_sanity_passed=gate_values[GATE_CLOCK_SANITY],
        calibration_lite_reviewed=gate_values[GATE_CALIBRATION_LITE],
        operator_approved_enforce=gate_values[GATE_OPERATOR_APPROVED],
        blockers=tuple(blockers),
        artifacts_dir=str(base),
        details=details,
    )


def enforce_blockers(
    *,
    artifacts_dir: Path | str | None = None,
) -> list[str]:
    """Return human-readable blockers for entry_mode: enforce (empty if allowed)."""
    return list(load_z_gap_preflight_gates(artifacts_dir).blockers)
