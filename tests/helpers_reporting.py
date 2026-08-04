"""Helpers for unified reporting run directories in tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def run_dir_from_output(tmp: Path, stem: str = "facts") -> Path:
    """Map legacy ``tmp/facts.jsonl`` output_path to ``tmp/facts/`` run dir."""
    return tmp / stem


def load_run_events(run_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for name in ("audit_events.jsonl", "analytics_events.jsonl"):
        path = run_dir / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


def load_events_for_legacy_jsonl(tmp: Path, filename: str = "facts.jsonl") -> list[dict[str, Any]]:
    stem = Path(filename).stem
    return load_run_events(tmp / stem)


def legacy_fact_types(events: list[dict[str, Any]]) -> set[str]:
    """Derive a set comparable to old FactEnvelope.fact_type values."""
    types: set[str] = set()
    for ev in events:
        et = str(ev.get("event_type") or "")
        if et.startswith("legacy."):
            types.add(et[len("legacy.") :])
        elif et == "decision.evaluated":
            types.add("observe_decision")
            diag = ev.get("strategy_diagnostics") or {}
            values = diag.get("values") or {}
            if values.get("schema") == "z_gap_calibration_v1":
                types.add("zgap_calibration_row")
                types.add("zgap_model_snapshot")
                types.add("zgap_entry_valuation")
            if values.get("held_leg") is not None or values.get("position"):
                types.add("zgap_active_position_context")
        elif et == "intent.created":
            types.add("intent_created")
        elif et == "risk.decided":
            payload = ev.get("payload") or {}
            if payload.get("approved") is False:
                types.add("risk_denied")
            else:
                types.add("risk_approved")
        elif et in {"plan.created", "legacy.execution_plan_created"}:
            types.add("execution_plan_created")
        elif et.startswith("signal.") or et == "legacy.signal":
            types.add("signal")
        elif et.startswith("intent."):
            types.add(et.replace(".", "_"))
    return types


def payloads_by_legacy_type(
    events: list[dict[str, Any]], fact_type: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in events:
        et = str(ev.get("event_type") or "")
        if et == f"legacy.{fact_type}":
            out.append(dict(ev.get("payload") or {}))
        elif fact_type == "zgap_calibration_row" and et == "decision.evaluated":
            diag = ev.get("strategy_diagnostics") or {}
            values = diag.get("values") or {}
            if values.get("schema") == "z_gap_calibration_v1":
                out.append(dict(values))
        elif fact_type == "intent_created" and et in {
            "legacy.intent_created",
            "intent.created",
        }:
            out.append(dict(ev.get("payload") or {}))
    return out


def legacy_events_with_payload(
    tmp: Path, fact_type: str, *, filename: str = "facts.jsonl"
) -> list[dict[str, Any]]:
    """Return list of {payload: ...} shaped like old fact lines for one type."""
    events = load_events_for_legacy_jsonl(tmp, filename)
    return [{"payload": p} for p in payloads_by_legacy_type(events, fact_type)]
