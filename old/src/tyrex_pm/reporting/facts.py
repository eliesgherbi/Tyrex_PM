from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any

from tyrex_pm.core.time import utc_now
from tyrex_pm.reporting.schema_v2 import FACT_SCHEMA_VERSION


def _serialize(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _serialize(v) for k, v in asdict(obj).items()}
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def make_fact(
    fact_type: str,
    run_id: str,
    payload: dict[str, Any],
    *,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": FACT_SCHEMA_VERSION,
        "fact_type": fact_type,
        "ts": utc_now().isoformat(),
        "run_id": run_id,
        "correlation_id": correlation_id,
        "payload": {k: _serialize(v) for k, v in payload.items()},
    }
