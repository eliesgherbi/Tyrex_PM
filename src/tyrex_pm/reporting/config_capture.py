"""Frozen effective config for reporting (REC-06)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from typing import Any

from tyrex_pm.config.loaders import RiskSettings, RuntimeSettings, StrategySettings

_MATERIAL_ENV_KEYS = (
    "TYREX_MIN_BUY_NOTIONAL_USD",
    "POLYMARKET_SIGNATURE_TYPE",
    "POLYMARKET_FUNDER",
)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, float) and (obj == float("inf") or obj == float("-inf")):
        return "inf" if obj > 0 else "-inf"
    raise TypeError(f"not JSON serializable: {type(obj)}")


def _to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        d = dataclasses.asdict(obj)
        return {k: _to_jsonable(v) for k, v in d.items()}
    if isinstance(obj, tuple):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, float) and (obj == float("inf") or obj == float("-inf")):
        return "inf" if obj > 0 else "-inf"
    return obj


def build_config_snapshot(
    strategy: StrategySettings,
    risk: RiskSettings,
    runtime: RuntimeSettings,
) -> tuple[str, str]:
    """
    Return (config_json, sha256_hex).

    Secrets: only env *key names* for material vars; values redacted.
    """
    payload: dict[str, Any] = {
        "strategy": _to_jsonable(strategy),
        "risk": _to_jsonable(risk),
        "runtime": _to_jsonable(runtime),
        "material_env": {k: _redact_env(k) for k in _MATERIAL_ENV_KEYS},
    }
    text = json.dumps(payload, sort_keys=True, default=_json_default)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text, sha


def _redact_env(key: str) -> str:
    if key not in os.environ:
        return "<unset>"
    return "<set>"
