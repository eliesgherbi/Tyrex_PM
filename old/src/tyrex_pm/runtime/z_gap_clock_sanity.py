"""Clock sanity helpers for Z-Gap Phase A preflight (A0.1)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from tyrex_pm.runtime.time_authority import TimeAuthority

# Legacy informational threshold for raw OS drift reporting only.
CLOCK_DRIFT_ENFORCE_THRESHOLD_MS = 500


@dataclass(frozen=True)
class ClockSanityReport:
    local_time_utc: str
    reference_time_utc: str
    clock_drift_ms: float
    reference_source: str
    pass_: bool
    fail_reason: str | None
    enforce_blocked: bool
    observe_warning: str | None
    checked_at_utc: str
    os_drift_ms: float | None = None
    time_authority_offset_ms: float | None = None
    time_authority_uncertainty_ms: float | None = None
    sync_status: str | None = None
    enforce_gate_pass: bool | None = None
    observe_warning_flag: bool | None = None
    source: str | None = None
    sntp_offset_ms: float | None = None
    binance_offset_ms: float | None = None
    offset_disagreement_ms: float | None = None
    samples_kept: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["pass"] = self.pass_
        del out["pass_"]
        return out


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def evaluate_clock_sanity(
    *,
    local_dt: datetime,
    reference_dt: datetime,
    reference_source: str,
    threshold_ms: float = CLOCK_DRIFT_ENFORCE_THRESHOLD_MS,
    time_authority: TimeAuthority | None = None,
) -> ClockSanityReport:
    """Evaluate clock sanity. TimeAuthority sync_status + uncertainty are authoritative for enforce."""
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=timezone.utc)
    if reference_dt.tzinfo is None:
        reference_dt = reference_dt.replace(tzinfo=timezone.utc)

    drift_ms = (local_dt - reference_dt).total_seconds() * 1000.0
    os_drift_ms = round(drift_ms, 3)

    ta_offset = time_authority.offset_ms if time_authority else None
    ta_uncertainty = time_authority.uncertainty_ms if time_authority else None
    sync_status = time_authority.sync_status if time_authority else None
    enforce_gate_pass = time_authority.enforce_gate_pass if time_authority else False
    observe_warning_flag = time_authority.observe_warning if time_authority else True
    source = time_authority.source if time_authority else reference_source
    sntp_offset = time_authority.sntp_offset_ms if time_authority else None
    binance_offset = time_authority.binance_offset_ms if time_authority else None
    disagreement = time_authority.offset_disagreement_ms if time_authority else None
    samples_kept = time_authority.samples_kept if time_authority else None

    passed = bool(enforce_gate_pass)
    enforce_blocked = not passed
    fail_reason: str | None = None
    observe_warning: str | None = None

    if not passed:
        if sync_status != "synced":
            fail_reason = f"time_authority sync_status={sync_status!r} (authoritative enforce gate)"
        elif ta_uncertainty is not None:
            fail_reason = (
                f"time_authority uncertainty_ms={ta_uncertainty:.2f} exceeds enforce gate "
                f"(authoritative; os_drift_ms={os_drift_ms:.2f} informational)"
            )
        else:
            fail_reason = "time_authority sync failed (authoritative enforce gate)"
        observe_warning = "observe_only may proceed with clock sync warning; enforce blocked"
    elif abs(os_drift_ms) > threshold_ms:
        observe_warning = (
            f"os_drift_ms={abs(os_drift_ms):.2f} exceeds {threshold_ms}ms informational threshold; "
            "TimeAuthority enforce gate passed"
        )

    return ClockSanityReport(
        local_time_utc=local_dt.isoformat(),
        reference_time_utc=reference_dt.isoformat(),
        clock_drift_ms=os_drift_ms,
        reference_source=reference_source,
        pass_=passed,
        fail_reason=fail_reason,
        enforce_blocked=enforce_blocked,
        observe_warning=observe_warning,
        checked_at_utc=_utc_now().isoformat(),
        os_drift_ms=os_drift_ms,
        time_authority_offset_ms=ta_offset,
        time_authority_uncertainty_ms=ta_uncertainty,
        sync_status=sync_status,
        enforce_gate_pass=enforce_gate_pass,
        observe_warning_flag=observe_warning_flag,
        source=source,
        sntp_offset_ms=sntp_offset,
        binance_offset_ms=binance_offset,
        offset_disagreement_ms=disagreement,
        samples_kept=samples_kept,
    )


def write_clock_sanity_report(path: Any, report: ClockSanityReport) -> None:
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
