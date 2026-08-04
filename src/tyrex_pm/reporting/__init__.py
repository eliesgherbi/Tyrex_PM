"""Public reporting API."""

from tyrex_pm.reporting.config import (
    DEFAULT_REPORTING_PATH,
    ReportingConfig,
    default_reporting_config,
    load_reporting_config,
)
from tyrex_pm.reporting.contracts import (
    SCHEMA_VERSION,
    DiagnosticsContract,
    EventFamily,
    LineageIds,
    ReportingEvent,
    ReportingHealth,
    ReportingLane,
    RunLifecycle,
    StrategyDiagnosticsBlob,
)
from tyrex_pm.reporting.paths import (
    ensure_run_layout,
    ops_run_dir,
    recordings_dir,
    runtime_state_dir,
    strategy_run_dir,
)
from tyrex_pm.reporting.reporter import (
    ReportingPort,
    RunReporter,
    inspect_run_manifest,
    open_run_reporter,
)
from tyrex_pm.reporting.summary import classify_stale_running
from tyrex_pm.reporting.writer import atomic_write_json

__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_REPORTING_PATH",
    "DiagnosticsContract",
    "EventFamily",
    "LineageIds",
    "ReportingConfig",
    "ReportingEvent",
    "ReportingHealth",
    "ReportingLane",
    "ReportingPort",
    "RunLifecycle",
    "RunReporter",
    "StrategyDiagnosticsBlob",
    "atomic_write_json",
    "classify_stale_running",
    "default_reporting_config",
    "ensure_run_layout",
    "inspect_run_manifest",
    "load_reporting_config",
    "open_run_reporter",
    "ops_run_dir",
    "recordings_dir",
    "runtime_state_dir",
    "strategy_run_dir",
]
