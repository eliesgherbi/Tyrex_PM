"""Structured reporting / observability (facts, ETL, summaries)."""

from tyrex_pm.reporting.context import RunContext, create_run_context
from tyrex_pm.reporting.etl import build_sqlite_from_jsonl
from tyrex_pm.reporting.recorder import FactRecorder, NoOpFactRecorder
from tyrex_pm.reporting.summarize import build_summary, write_summary_artifacts

__all__ = [
    "FactRecorder",
    "NoOpFactRecorder",
    "RunContext",
    "build_sqlite_from_jsonl",
    "build_summary",
    "create_run_context",
    "write_summary_artifacts",
]
