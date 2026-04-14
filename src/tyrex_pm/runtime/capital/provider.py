"""
Single :class:`CapitalState` producer — framework account first, optional py-clob inside only here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from tyrex_pm.runtime.clob_collateral_money import parse_clob_collateral_usd
from tyrex_pm.runtime.capital.policy import CapitalSnapshotPolicy
from tyrex_pm.runtime.capital.state import CapitalState, CapitalStateSource
from tyrex_pm.runtime.nautilus_cash_extract import extract_nautilus_cash_free_usd
from tyrex_pm.runtime.state_readers import (
    AccountSnapshot,
    AccountSnapshotSource,
    AllowanceSnapshot,
    AllowanceSnapshotSource,
)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _need_clob_for_risk_gate(policy: CapitalSnapshotPolicy) -> bool:
    return (
        policy.min_collateral_balance_usd is not None
        or policy.min_allowance_usd is not None
        or policy.collateral_reserve_usd > 0
    )


def _merge_capital_state(
    *,
    acct: AccountSnapshot,
    clob_snap: AllowanceSnapshot | None,
    policy: CapitalSnapshotPolicy,
    purpose: Literal["risk_gate", "observability"],
    merged_clob: bool,
) -> CapitalState:
    n_free, n_note = extract_nautilus_cash_free_usd(
        acct.balances if acct.account_present else None,
    )
    clob_p = parse_clob_collateral_usd(clob_snap.raw) if clob_snap is not None else None

    bal_py = clob_p.balance_usd if clob_p else None
    allow_py = clob_p.allowance_usd if clob_p else None
    raw_bal = clob_p.balance_raw if clob_p else None
    raw_allow = clob_p.allowance_raw if clob_p else None
    b_note = clob_p.balance_parse_note if clob_p else "no_clob_snapshot"
    a_note = clob_p.allowance_parse_note if clob_p else "no_clob_snapshot"

    if n_free is not None:
        free = n_free
    elif bal_py is not None:
        free = bal_py
    else:
        free = None

    allowance = allow_py

    if merged_clob:
        source = CapitalStateSource.EXPLICIT_REFRESH
        stale_after = min(
            policy.max_account_snapshot_age_seconds,
            policy.max_allowance_snapshot_age_seconds,
        )
    else:
        source = CapitalStateSource.ADAPTER_ACCOUNT
        stale_after = policy.max_account_snapshot_age_seconds

    captured = acct.captured_at_utc
    if clob_snap is not None and clob_snap.captured_at_utc > captured:
        captured = clob_snap.captured_at_utc

    ok = True
    err: str | None = None
    if purpose == "risk_gate":
        if not acct.account_present:
            ok = False
            err = "account_unavailable"
        elif _need_clob_for_risk_gate(policy) and clob_snap is None:
            ok = False
            err = "allowance_source_unavailable"

    return CapitalState(
        free_collateral_usd=free,
        allowance_usd=allowance,
        captured_at_utc=captured,
        source=source,
        stale_after_seconds=stale_after,
        ok=ok,
        error=err,
        account_present=acct.account_present,
        venue=str(acct.venue),
        nautilus_balances=acct.balances,
        nautilus_cash_free_usd=n_free,
        nautilus_cash_extract_note=n_note,
        py_clob_balance_usd=bal_py,
        py_clob_allowance_usd=allow_py,
        py_clob_balance_raw=raw_bal,
        py_clob_allowance_raw=raw_allow,
        py_clob_balance_parse_note=b_note,
        py_clob_allowance_parse_note=a_note,
        merged_clob=merged_clob,
    )


@runtime_checkable
class CapitalStateProvider(Protocol):
    """Tyrex-wide capital read contract (Phase 1 — risk + reporting)."""

    def snapshot(
        self,
        *,
        purpose: Literal["risk_gate", "observability"],
        policy: CapitalSnapshotPolicy,
    ) -> CapitalState: ...

    def freshness_ok(self, state: CapitalState, *, policy: CapitalSnapshotPolicy) -> bool:
        """True when ``state`` is still fresh enough for ``policy`` TTL thresholds."""
        ...


class DefaultCapitalStateProvider:
    """
    Framework-first capital: :class:`~tyrex_pm.runtime.state_readers.NautilusAccountSnapshotProvider`
    plus optional :class:`~tyrex_pm.runtime.state_readers.ClobAllowanceStateProvider` **only** here.

    Maintains **separate** TTL for account vs CLOB snapshots (matches historical risk caching).
    """

    __slots__ = (
        "_account",
        "_clob",
        "_acct_cache",
        "_clob_cache",
        "_observability_include_clob",
    )

    def __init__(
        self,
        account: AccountSnapshotSource,
        clob: AllowanceSnapshotSource | None,
        *,
        observability_include_clob: bool = True,
    ) -> None:
        self._account = account
        self._clob = clob
        self._acct_cache: AccountSnapshot | None = None
        self._clob_cache: AllowanceSnapshot | None = None
        self._observability_include_clob = observability_include_clob

    def _fresh_account(self, policy: CapitalSnapshotPolicy, now: datetime) -> AccountSnapshot:
        need = (
            self._acct_cache is None
            or (now - self._acct_cache.captured_at_utc).total_seconds()
            >= policy.max_account_snapshot_age_seconds
        )
        if need:
            self._acct_cache = self._account.snapshot()
        assert self._acct_cache is not None
        return self._acct_cache

    def _fresh_clob(self, policy: CapitalSnapshotPolicy, now: datetime) -> AllowanceSnapshot | None:
        if self._clob is None:
            return None
        need = (
            self._clob_cache is None
            or (now - self._clob_cache.captured_at_utc).total_seconds()
            >= policy.max_allowance_snapshot_age_seconds
        )
        if need:
            raw = self._clob.snapshot()
            self._clob_cache = raw if isinstance(raw, AllowanceSnapshot) else None
        return self._clob_cache

    def snapshot(
        self,
        *,
        purpose: Literal["risk_gate", "observability"],
        policy: CapitalSnapshotPolicy,
    ) -> CapitalState:
        now = _utc_now()
        acct = self._fresh_account(policy, now)

        pull_clob = False
        if self._clob is not None:
            if purpose == "observability" and self._observability_include_clob:
                pull_clob = True
            elif purpose == "risk_gate" and _need_clob_for_risk_gate(policy):
                pull_clob = True

        clob_snap = self._fresh_clob(policy, now) if pull_clob else None
        merged_clob = clob_snap is not None
        return _merge_capital_state(
            acct=acct,
            clob_snap=clob_snap,
            policy=policy,
            purpose=purpose,
            merged_clob=merged_clob,
        )

    def freshness_ok(self, state: CapitalState, *, policy: CapitalSnapshotPolicy) -> bool:
        if not state.ok:
            return False
        age = (_utc_now() - state.captured_at_utc).total_seconds()
        if state.source == CapitalStateSource.ADAPTER_ACCOUNT:
            return age <= policy.max_account_snapshot_age_seconds
        return age <= min(
            policy.max_account_snapshot_age_seconds,
            policy.max_allowance_snapshot_age_seconds,
        )
