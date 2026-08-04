"""Reporting YAML profiles (optional analytics only)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

DEFAULT_REPORTING_PATH = Path("config/reporting/full.yaml")


@dataclass(frozen=True, kw_only=True)
class ReportingConfig:
    schema_version: int
    profile: str
    per_evaluation: bool
    indicators: bool
    signals: bool
    debug_host_trace: bool
    closest_candidates_per_reason: int
    raw_recording_enabled: bool
    # Internal writer defaults (not user-facing knobs in v1 YAML)
    analytics_max_queue: int = 1024
    critical_max_queue: int = 4096
    batch_size: int = 32
    checkpoint_every_n_events: int = 25
    source_path: Path | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("reporting schema_version must be 1")
        if self.profile not in {"full", "minimal"}:
            raise ValueError(f"unknown reporting profile: {self.profile!r}")
        if self.closest_candidates_per_reason < 0:
            raise ValueError("closest_candidates_per_reason must be >= 0")
        if self.analytics_max_queue < 1 or self.critical_max_queue < 1:
            raise ValueError("queue sizes must be >= 1")


def default_reporting_config(*, profile: str = "full") -> ReportingConfig:
    if profile == "minimal":
        return ReportingConfig(
            schema_version=1,
            profile="minimal",
            per_evaluation=False,
            indicators=False,
            signals=False,
            debug_host_trace=False,
            closest_candidates_per_reason=5,
            raw_recording_enabled=False,
        )
    return ReportingConfig(
        schema_version=1,
        profile="full",
        per_evaluation=True,
        indicators=True,
        signals=True,
        debug_host_trace=False,
        closest_candidates_per_reason=5,
        raw_recording_enabled=False,
    )


def load_reporting_config(path: Path | str | None = None) -> ReportingConfig:
    if path is None:
        path = DEFAULT_REPORTING_PATH
    path = Path(path)
    if not path.is_file():
        # Fall back to in-code defaults when files not yet present (tests).
        name = path.stem.lower()
        cfg = default_reporting_config(profile="minimal" if "minimal" in name else "full")
        return ReportingConfig(
            schema_version=cfg.schema_version,
            profile=cfg.profile,
            per_evaluation=cfg.per_evaluation,
            indicators=cfg.indicators,
            signals=cfg.signals,
            debug_host_trace=cfg.debug_host_trace,
            closest_candidates_per_reason=cfg.closest_candidates_per_reason,
            raw_recording_enabled=cfg.raw_recording_enabled,
            source_path=path,
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("reporting config root must be a mapping")
    return reporting_config_from_mapping(raw, source_path=path)


def reporting_config_from_mapping(
    data: Mapping[str, Any], *, source_path: Path | None = None
) -> ReportingConfig:
    _reject_mandatory_disable_attempts(data)
    analytics = data.get("analytics") if isinstance(data.get("analytics"), Mapping) else {}
    raw_rec = (
        data.get("raw_recording")
        if isinstance(data.get("raw_recording"), Mapping)
        else {}
    )
    profile = str(data.get("profile") or "full").strip().lower()
    defaults = default_reporting_config(profile=profile)
    return ReportingConfig(
        schema_version=int(data.get("schema_version", 1)),
        profile=profile,
        per_evaluation=bool(analytics.get("per_evaluation", defaults.per_evaluation)),
        indicators=bool(analytics.get("indicators", defaults.indicators)),
        signals=bool(analytics.get("signals", defaults.signals)),
        debug_host_trace=bool(
            analytics.get("debug_host_trace", defaults.debug_host_trace)
        ),
        closest_candidates_per_reason=int(
            analytics.get(
                "closest_candidates_per_reason",
                defaults.closest_candidates_per_reason,
            )
        ),
        raw_recording_enabled=bool(raw_rec.get("enabled", False)),
        source_path=source_path,
    )


def _reject_mandatory_disable_attempts(data: Mapping[str, Any]) -> None:
    forbidden_false = {
        "manifest",
        "run_summary",
        "audit_events",
        "audit",
        "mandatory_audit",
        "reporting_health",
    }
    outputs = data.get("outputs")
    if isinstance(outputs, Mapping):
        for key in forbidden_false:
            if key in outputs and outputs[key] is False:
                raise ValueError(
                    f"cannot disable mandatory reporting output {key!r}"
                )
    channels = data.get("channels")
    if isinstance(channels, Mapping) and channels.get("operational") is False:
        raise ValueError("cannot disable mandatory operational/audit channel")
    if data.get("audit_events") is False or data.get("mandatory_audit") is False:
        raise ValueError("cannot disable mandatory audit evidence")
