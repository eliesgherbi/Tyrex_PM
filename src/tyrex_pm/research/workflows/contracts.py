"""Public workflow envelope. Internal helpers stay out of the operation catalog."""

from __future__ import annotations

from typing import Any

SCHEMA = "workflow_result_v1"


class WorkflowError(Exception):
    def __init__(self, code: str, reason: str, fields: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.fields = dict(fields or {})


def ok(operation: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "operation": operation,
        "ok": True,
        "result": result,
        "error": None,
    }


def fail(
    operation: str, code: str, reason: str, fields: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "operation": operation,
        "ok": False,
        "result": None,
        "error": {"code": code, "reason": reason, "fields": dict(fields or {})},
    }


def from_error(operation: str, exc: WorkflowError) -> dict[str, Any]:
    return fail(operation, exc.code, exc.reason, exc.fields)
