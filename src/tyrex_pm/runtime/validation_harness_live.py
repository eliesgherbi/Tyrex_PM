"""Live validation harness guards (P4.5 wiring).

Preflight and fixture-policy helpers for true live validation runs.
"""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import ExecutionMode, Side
from tyrex_pm.core.errors import ConfigError
from tyrex_pm.core.ids import TokenId
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_HEALTH
from tyrex_pm.runtime.config import (
    VALIDATION_MODE_PROTECTION_SL,
    VALIDATION_MODE_PROTECTION_TP,
    VALIDATION_MODE_STALE_BOOK_DENY,
    AppConfig,
    ValidationHarnessStrategyConfig,
)
from tyrex_pm.runtime.exit_lifecycle import inventory_snapshot


def validate_validation_harness_live_config(app: AppConfig) -> None:
    """Fail closed: no synthetic shortcuts in live mode."""
    if app.runtime.execution_mode != ExecutionMode.LIVE:
        return
    vh = app.validation_harness
    if vh is None:
        return
    if vh.seed_allocation_qty is not None and vh.seed_allocation_qty > 0:
        raise ConfigError("live mode forbids validation.seed_allocation_qty")
    if vh.allow_seed_allocation:
        raise ConfigError("live mode forbids validation.allow_seed_allocation=true")
    if vh.use_fixture_book:
        raise ConfigError("live mode forbids validation.use_fixture_book=true")
    if vh.fixture_book_bid is not None or vh.fixture_book_ask is not None:
        raise ConfigError("live mode forbids validation.fixture_book_bid/ask")
    if vh.mode in (
        VALIDATION_MODE_STALE_BOOK_DENY,
        VALIDATION_MODE_PROTECTION_TP,
        VALIDATION_MODE_PROTECTION_SL,
    ):
        raise ConfigError(
            f"live mode forbids validation.mode={vh.mode!r} (shadow/fixture synthetic only)"
        )


def may_use_fixture_book(app: AppConfig, cfg: ValidationHarnessStrategyConfig) -> bool:
    """Fixtures allowed only in shadow or when explicitly opted in (never silently in live)."""
    if app.runtime.execution_mode == ExecutionMode.LIVE:
        return False
    return bool(cfg.use_fixture_book)


def may_seed_allocation(app: AppConfig, cfg: ValidationHarnessStrategyConfig) -> bool:
    if app.runtime.execution_mode == ExecutionMode.LIVE:
        return False
    if cfg.allow_seed_allocation and cfg.seed_allocation_qty is not None:
        return cfg.seed_allocation_qty > 0
    return cfg.seed_allocation_qty is not None and cfg.seed_allocation_qty > 0


def preflight_urgent_exit(coord, cfg: ValidationHarnessStrategyConfig) -> tuple[bool, dict]:
    """Check allocation + venue inventory before live urgent SELL."""
    tid = TokenId(cfg.token_id)
    ledger = coord.allocation_ledger
    allocated = Decimal("0")
    if ledger is not None:
        allocated = ledger.get_available_allocated(cfg.owner_id, tid)
    snap = inventory_snapshot(coord, tid)
    venue_qty = Decimal(str(snap.get("available_to_sell", "0")))
    ok = allocated > 0 and venue_qty > 0
    return ok, {
        "event": "validation_harness_preflight",
        "mode": "urgent_exit",
        "ok": ok,
        "owner_id": cfg.owner_id,
        "token_id": str(tid),
        "allocated_available": str(allocated),
        "venue_available_to_sell": str(venue_qty),
    }


def emit_preflight(sink, run_id: str, payload: dict) -> None:
    sink.write(make_fact(FACT_TYPE_HEALTH, run_id, payload))
