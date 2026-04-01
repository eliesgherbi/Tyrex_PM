"""Validated application YAML (v1.03)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class AppConfig:
    mode: str  # live | backtest
    trader_id: str
    log_level: str = "INFO"


def load_app_config(path: str | Path) -> AppConfig:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: root must be a mapping")
    missing = [k for k in ("mode", "trader_id") if k not in raw or raw[k] in (None, "")]
    if missing:
        raise ValueError(f"{p}: missing required key(s): {', '.join(missing)}")
    mode = str(raw["mode"]).lower().strip()
    if mode not in ("live", "backtest"):
        raise ValueError(f"{p}: mode must be 'live' or 'backtest', got {mode!r}")
    log_level = str(raw.get("log_level") or "INFO").upper()
    return AppConfig(mode=mode, trader_id=str(raw["trader_id"]).strip(), log_level=log_level)
