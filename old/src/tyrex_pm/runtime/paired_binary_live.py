"""Live preflight guards for paired binary (Phase 4.6)."""

from __future__ import annotations

from tyrex_pm.core.enums import ExecutionMode
from tyrex_pm.core.errors import ConfigError
from tyrex_pm.runtime.config import AppConfig, PairedBinaryStrategyConfig


def validate_paired_binary_live_config(app: AppConfig) -> None:
    if app.runtime.execution_mode != ExecutionMode.LIVE:
        return
    pb = app.paired_binary
    if pb is None:
        return
    if pb.use_fixture_book:
        raise ConfigError("live mode forbids paired_binary.use_fixture_book=true")
    if pb.seed_allocation_qty is not None and pb.seed_allocation_qty > 0:
        raise ConfigError("live mode forbids paired_binary.seed_allocation_qty")
    if pb.allow_seed_allocation:
        raise ConfigError("live mode forbids paired_binary.allow_seed_allocation=true")


def validate_paired_binary_required_wiring(app: AppConfig) -> None:
    pb = app.paired_binary
    if pb is None or not pb.enabled:
        return
    if not app.runtime.market_data.enabled:
        raise ConfigError("paired_binary requires runtime.market_data.enabled=true")
    if not app.execution.planner.enabled:
        raise ConfigError("paired_binary requires execution.planner.enabled=true")
