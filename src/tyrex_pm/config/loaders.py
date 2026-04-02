"""YAML loaders with validation (secrets stay in ``.env``)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def _root(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be a mapping")
    return data


@dataclass(frozen=True, slots=True)
class StrategySettings:
    guru_wallet_address: str
    allowlisted_token_ids: tuple[str, ...]
    copy_scale: float
    strategy_dedup_state_path: str | None


@dataclass(frozen=True, slots=True)
class RiskSettings:
    max_order_quantity: float
    max_notional_usd_per_order: float
    max_token_notional_usd_open: float
    kill_switch: bool
    fail_on_missing_price_for_notional: bool


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    trader_id: str
    execution_mode: str
    guru_poll_interval_seconds: float
    data_api_base_url: str
    guru_dedup_state_path: str
    guru_state_path: str
    guru_activity_limit: int
    guru_startup_backfill_seconds: float
    guru_max_activity_pages_per_poll: int
    logging_level: str
    clob_host: str
    chain_id: int


def load_strategy_settings(path: str | Path) -> StrategySettings:
    p = Path(path)
    raw = _root(yaml.safe_load(p.read_text(encoding="utf-8")), p)
    missing = [k for k in ("guru_wallet_address", "allowlisted_token_ids") if k not in raw]
    if missing:
        raise ValueError(f"{p}: missing required key(s): {', '.join(missing)}")
    gw = str(raw["guru_wallet_address"]).strip()
    if not gw.startswith("0x") or len(gw) != 42:
        raise ValueError(f"{p}: guru_wallet_address must be 0x + 40 hex chars")

    tokens = raw["allowlisted_token_ids"]
    if not isinstance(tokens, list):
        raise ValueError(f"{p}: allowlisted_token_ids must be a list")
    if not tokens:
        raise ValueError(f"{p}: allowlisted_token_ids must be non-empty")
    norm = tuple(str(x).strip() for x in tokens if str(x).strip())
    if len(norm) != len(set(norm)):
        raise ValueError(f"{p}: duplicate token ids in allowlisted_token_ids")

    scale = float(raw.get("copy_scale", 1.0))
    if scale < 0:
        raise ValueError(f"{p}: copy_scale must be >= 0")

    dedup = raw.get("strategy_dedup_state_path")
    dedup_s = str(dedup).strip() if dedup else None

    return StrategySettings(
        guru_wallet_address=gw,
        allowlisted_token_ids=norm,
        copy_scale=scale,
        strategy_dedup_state_path=dedup_s,
    )


def load_risk_settings(path: str | Path) -> RiskSettings:
    p = Path(path)
    raw = _root(yaml.safe_load(p.read_text(encoding="utf-8")), p)
    mqty = raw.get("max_order_quantity")
    if mqty is None:
        raise ValueError(f"{p}: max_order_quantity is required")
    max_order_quantity = float(mqty)
    if max_order_quantity <= 0:
        raise ValueError(f"{p}: max_order_quantity must be positive")

    mn = raw.get("max_notional_usd_per_order")
    if mn is None:
        raise ValueError(f"{p}: max_notional_usd_per_order is required")
    max_notional_usd_per_order = float(mn)
    if max_notional_usd_per_order <= 0:
        raise ValueError(f"{p}: max_notional_usd_per_order must be positive")

    mt = raw.get("max_token_notional_usd_open")
    max_token = float("inf") if mt is None else float(mt)
    if max_token <= 0:
        raise ValueError(f"{p}: max_token_notional_usd_open must be positive or null (unlimited)")

    kill_switch = bool(raw.get("kill_switch", False))
    fail_on_missing_price_for_notional = bool(
        raw.get("fail_on_missing_price_for_notional", True)
    )

    return RiskSettings(
        max_order_quantity=max_order_quantity,
        max_notional_usd_per_order=max_notional_usd_per_order,
        max_token_notional_usd_open=max_token,
        kill_switch=kill_switch,
        fail_on_missing_price_for_notional=fail_on_missing_price_for_notional,
    )


def load_runtime_settings(path: str | Path) -> RuntimeSettings:
    p = Path(path)
    raw = _root(yaml.safe_load(p.read_text(encoding="utf-8")), p)
    tid = str(raw.get("trader_id") or "").strip()
    if not tid or "-" not in tid:
        raise ValueError(f"{p}: trader_id must look like NAME-001")

    mode = str(raw.get("execution_mode", "shadow")).lower().strip()
    if mode not in ("shadow", "live"):
        raise ValueError(f"{p}: execution_mode must be shadow or live")

    poll = float(raw.get("guru_poll_interval_seconds", 30.0))
    if poll <= 0:
        raise ValueError(f"{p}: guru_poll_interval_seconds must be positive")

    api = str(raw.get("data_api_base_url", "https://data-api.polymarket.com")).rstrip("/")
    dedup = raw.get("guru_dedup_state_path")
    dedup_s = str(dedup).strip() if dedup else "var/guru_dedup.json"

    state = raw.get("guru_state_path")
    state_s = str(state).strip() if state else "var/guru_watermark.json"

    activity_limit = int(raw.get("guru_activity_limit", 200))
    if not (1 <= activity_limit <= 500):
        raise ValueError(f"{p}: guru_activity_limit must be between 1 and 500")

    backfill = float(raw.get("guru_startup_backfill_seconds", 0.0))
    if backfill < 0:
        raise ValueError(f"{p}: guru_startup_backfill_seconds must be >= 0")

    max_pages = int(raw.get("guru_max_activity_pages_per_poll", 4))
    if not (1 <= max_pages <= 20):
        raise ValueError(f"{p}: guru_max_activity_pages_per_poll must be between 1 and 20")

    log_level = str(raw.get("logging_level", "INFO")).upper()
    clob = str(raw.get("clob_host", "https://clob.polymarket.com")).rstrip("/")
    chain_id = int(raw.get("chain_id", 137))

    return RuntimeSettings(
        trader_id=tid,
        execution_mode=mode,
        guru_poll_interval_seconds=poll,
        data_api_base_url=api,
        guru_dedup_state_path=dedup_s,
        guru_state_path=state_s,
        guru_activity_limit=activity_limit,
        guru_startup_backfill_seconds=backfill,
        guru_max_activity_pages_per_poll=max_pages,
        logging_level=log_level,
        clob_host=clob,
        chain_id=chain_id,
    )
