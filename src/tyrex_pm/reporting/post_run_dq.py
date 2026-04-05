"""Post-run data quality heuristics (REC-07, SCH-05, operational cut line)."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tyrex_pm.reporting.context import RunContext

from tyrex_pm.reporting.schema.data_quality import RunDataQuality


def _iter_fact_rows(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def apply_fact_file_heuristics(
    dq: RunDataQuality,
    facts_path: Path,
    *,
    run_context: RunContext | None = None,
) -> None:
    """
    Inspect ``facts.jsonl`` after the sink is closed.

    **Framework live:** if we recorded submit outcomes but zero lifecycle/fill facts, raise
    ``order_events_sparse`` (ADP-01 / WBS — do not silently claim execution truth).
    """
    execution_path = "unknown"
    if run_context is not None:
        execution_path = run_context.execution_path

    types: Counter[str] = Counter()
    outcome_submits = 0
    seen_signal_corr: set[str] = set()
    dup_signal_corr: set[str] = set()

    for row in _iter_fact_rows(facts_path):
        ft = str(row.get("fact_type") or "")
        types[ft] += 1
        if ft == "execution_outcome" and str(row.get("outcome")) == "submit":
            outcome_submits += 1
        if ft == "guru_signal":
            cid = str(row.get("correlation_id") or "")
            if cid:
                if cid in seen_signal_corr:
                    dup_signal_corr.add(cid)
                seen_signal_corr.add(cid)

    lifecycle_n = types.get("order_lifecycle", 0)
    fill_n = types.get("fill", 0)

    if execution_path == "framework_submit":
        if outcome_submits > 0 and lifecycle_n == 0 and fill_n == 0:
            dq.order_events_sparse = True

    if dup_signal_corr:
        dq.extra["duplicate_guru_signal_correlation_ids"] = sorted(dup_signal_corr)[:100]
