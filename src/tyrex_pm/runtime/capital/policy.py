"""Narrow capital refresh/gating inputs — WP3 decouples provider from full :class:`RiskSettings`."""

from __future__ import annotations

from dataclasses import dataclass

from tyrex_pm.config.loaders import RiskSettings


@dataclass(frozen=True, slots=True)
class CapitalSnapshotPolicy:
    """
    Subset of risk settings that affect **only** capital snapshot merge, TTL, and CLOB pull.

    Callers derive this from :class:`~tyrex_pm.config.loaders.RiskSettings` at boundaries;
    :class:`~tyrex_pm.runtime.capital.DefaultCapitalStateProvider` does not read full risk.
    """

    min_collateral_balance_usd: float | None
    min_allowance_usd: float | None
    collateral_reserve_usd: float
    max_account_snapshot_age_seconds: float
    max_allowance_snapshot_age_seconds: float

    @classmethod
    def from_risk_settings(cls, risk: RiskSettings) -> CapitalSnapshotPolicy:
        return cls(
            min_collateral_balance_usd=risk.min_collateral_balance_usd,
            min_allowance_usd=risk.min_allowance_usd,
            collateral_reserve_usd=float(risk.collateral_reserve_usd),
            max_account_snapshot_age_seconds=float(risk.max_account_snapshot_age_seconds),
            max_allowance_snapshot_age_seconds=float(risk.max_allowance_snapshot_age_seconds),
        )
