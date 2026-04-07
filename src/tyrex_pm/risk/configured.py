"""
Fail-closed risk gate from :class:`~tyrex_pm.config.loaders.RiskSettings`.

**Deployment-budget model:** Per-order, per-token, and portfolio caps share one accounting basis —
pending deployment (resting ``leaves ×`` limit price on Polymarket) plus filled deployment
(``abs(signed_qty) × avg_px_open`` per open position). No live mark / ``net_exposure`` valuation
for caps. See :mod:`tyrex_pm.runtime.deployment_budget`.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import UTC, datetime
from typing import Any, Callable

from tyrex_pm.config.loaders import RiskSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent
from tyrex_pm.reporting.capital_observability import (
    compute_buy_headroom_usd,
    trim_json_text,
)
from tyrex_pm.runtime.clob_collateral_money import parse_clob_collateral_usd
from tyrex_pm.runtime.deployment_budget import NautilusDeploymentBudget
from tyrex_pm.runtime.nautilus_cash_extract import extract_nautilus_cash_free_usd
from tyrex_pm.runtime.state_readers import (
    POLYMARKET_VENUE_ID,
    AccountSnapshotSource,
    AllowanceSnapshotSource,
    ExecutionStateReader,
)

_LOG = logging.getLogger(__name__)

# Grep-friendly operational prefix (Tyrex-owned; distinct from Nautilus component names).
_TYREX_RISK_OPS_EVENT = "tyrex_risk_ops"


def _ops_snippet(text: str | None, max_len: int = 220) -> str:
    """Single-line, length-bounded fragment for operator logs (no secrets)."""
    if not text:
        return ""
    s = " ".join(text.split())
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class ConfiguredRiskPolicy:
    """
    Pre-trade risk gate with **deployment-budget** caps on live runs.

    **Live:** :class:`~tyrex_pm.runtime.deployment_budget.NautilusDeploymentBudget` reads
    open orders and positions from the same node’s ``Cache`` / ``Portfolio``. **Shadow:**
    deployment budget is ``None`` (finite portfolio cap is not valid in shadow; see runtime contract).

    Guru concurrent resting cap and collateral reserve behave as in :class:`~tyrex_pm.config.loaders.RiskSettings`.
    """

    def __init__(
        self,
        settings: RiskSettings,
        *,
        execution_reader: ExecutionStateReader | None = None,
        account_snapshot: AccountSnapshotSource | None = None,
        allowance_provider: AllowanceSnapshotSource | None = None,
        deployment_budget: NautilusDeploymentBudget | None = None,
        fact_emit: Callable[[str, dict[str, Any]], None] | None = None,
        reporting_capital_observability_enabled: bool = False,
        reporting_capital_snapshot_period_seconds: float = 0.0,
        allowance_observability_enabled: bool = False,
    ) -> None:
        self._s = settings
        self._execution_reader = execution_reader
        self._account_snapshot = account_snapshot
        self._allowance_provider = allowance_provider
        self._deployment_budget = deployment_budget
        self._account_cache = None
        self._allowance_cache = None
        self._fact_emit = fact_emit
        self._reporting_account_seq = 0
        self._reporting_capital_observability_enabled = bool(
            reporting_capital_observability_enabled,
        )
        self._reporting_capital_period_s = float(reporting_capital_snapshot_period_seconds)
        self._allowance_observability_enabled = bool(allowance_observability_enabled)
        self._last_periodic_capital_mono: float | None = None

    @property
    def execution_reader(self):
        """Nautilus cache-backed reader (same node's ``Cache``); never owned by strategy."""
        return self._execution_reader

    @property
    def account_snapshot_provider(self):
        """Portfolio-backed account snapshots (may be empty until exec connects)."""
        return self._account_snapshot

    @property
    def allowance_provider(self):
        """py-clob collateral snapshot owner; ``None`` in shadow mode."""
        return self._allowance_provider

    @property
    def deployment_budget(self):
        """Deployment accounting; ``None`` in shadow (no framework portfolio cap)."""
        return self._deployment_budget

    def framework_open_order_count(self) -> int:
        """Count of orders Nautilus marks open — for tests and operator diagnostics."""
        if self._execution_reader is None:
            return 0
        return len(self._execution_reader.list_open_orders())

    def evaluate(self, intent: OrderIntent) -> tuple[bool, str, OrderIntent | None]:
        self._maybe_periodic_capital_snapshot()
        if self._s.kill_switch:
            meta = _deploy_adjust_base_meta(intent, self._s)
            self._emit_risk_and_deployment(
                intent_strategy=intent,
                intent_at_eval=intent,
                allowed=False,
                reason_code=str(ReasonCode.RISK_KILL_SWITCH),
                deploy_adjust_meta=meta,
            )
            return False, str(ReasonCode.RISK_KILL_SWITCH), None

        adjusted, d_reason, meta = self._apply_order_deploy_policies(intent)
        if adjusted is None:
            self._emit_risk_and_deployment(
                intent_strategy=intent,
                intent_at_eval=intent,
                allowed=False,
                reason_code=str(d_reason),
                deploy_adjust_meta=meta,
            )
            return False, str(d_reason), None

        ok, reason, final_intent = self._evaluate_impl(adjusted)
        self._emit_risk_and_deployment(
            intent_strategy=intent,
            intent_at_eval=final_intent,
            allowed=ok,
            reason_code=str(reason),
            deploy_adjust_meta=meta,
        )
        if ok:
            return True, str(reason), final_intent
        return False, str(reason), None

    def emit_capital_observation(
        self,
        snapshot_trigger: str,
        *,
        correlation_id: str | None = None,
        intent: OrderIntent | None = None,
    ) -> None:
        """
        Reporting hook: emit ``account_snapshot`` on submit / venue denial, etc.

        Best-effort refresh when :attr:`_reporting_capital_observability_enabled` is true.
        """
        if not self._reporting_capital_observability_enabled or self._fact_emit is None:
            return
        self._observability_refresh_caches_best_effort()
        self._emit_account_snapshot_row(
            snapshot_trigger=snapshot_trigger,
            correlation_id=correlation_id,
            intent=intent,
        )

    def _evaluate_impl(self, intent: OrderIntent) -> tuple[bool, str, OrderIntent]:
        """
        Gates after per-order deploy clip/bump. ``intent`` is already risk-adjusted for min/max.
        """
        order_deploy = _order_deploy_usd(intent)
        if order_deploy is None:
            if self._s.fail_on_missing_price_for_notional:
                return False, str(ReasonCode.RISK_MISSING_PRICE), intent
        ok_cap, rc_cap = self._capital_gate_eval(_utc_now(), intent, order_deploy)
        if not ok_cap:
            return False, str(rc_cap), intent

        if not math.isinf(self._s.max_token_notional_usd_open):
            if order_deploy is None:
                if self._s.fail_on_missing_price_for_notional:
                    return False, str(ReasonCode.RISK_MISSING_PRICE), intent
            else:
                ok_t, rc_t = self._token_deployment_cap_eval(intent, order_deploy)
                if not ok_t:
                    return False, str(rc_t), intent

        if not math.isinf(self._s.max_portfolio_notional_usd_open):
            ok_p, rc_p = self._portfolio_deployment_cap_eval(intent, order_deploy)
            if not ok_p:
                return False, str(rc_p), intent

        if self._s.max_concurrent_guru_resting_orders is not None:
            ok_cg, rc_cg = self._guru_concurrent_resting_cap_eval(intent)
            if not ok_cg:
                return False, str(rc_cg), intent

        return True, "approved", intent

    def _apply_order_deploy_policies(
        self,
        intent: OrderIntent,
    ) -> tuple[OrderIntent | None, str, dict[str, Any]]:
        """
        Clip to max deploy and/or bump to min deploy (BUY) per operator policy.
        Returns ``(None, reason, meta)`` on deny from these policies.
        """
        s = self._s
        meta = _deploy_adjust_base_meta(intent, s)
        p = intent.price_ref
        qty = float(intent.quantity)
        if p is None:
            return intent, "", meta
        pr = float(p)
        if pr <= 0.0 or qty < 0.0:
            return intent, "", meta

        max_n = float(s.max_notional_usd_per_order)
        min_n = float(s.min_notional_usd_per_order)
        max_pol = str(s.max_notional_policy).strip().lower()
        min_pol = str(s.min_notional_policy).strip().lower()
        eps = 1e-9

        deploy = pr * qty
        clipped = False
        bumped = False

        if deploy > max_n + eps:
            if max_pol == "deny":
                _LOG.info(
                    "event=%s gate=max_order_deploy reason=%s correlation_id=%s "
                    "order_deploy=%.6g max=%.6g policy=deny",
                    _TYREX_RISK_OPS_EVENT,
                    ReasonCode.RISK_ORDER_DEPLOYMENT_EXCEEDED,
                    intent.correlation_id,
                    float(deploy),
                    max_n,
                )
                return None, str(ReasonCode.RISK_ORDER_DEPLOYMENT_EXCEEDED), meta
            qty = max_n / pr
            deploy = pr * qty
            clipped = True

        if min_n > 0 and intent.side.upper() == "BUY":
            if deploy + eps < min_n:
                if min_pol == "deny":
                    _LOG.info(
                        "event=%s gate=min_order_notional reason=%s correlation_id=%s "
                        "order_deploy=%.6g min=%.6g policy=deny",
                        _TYREX_RISK_OPS_EVENT,
                        ReasonCode.RISK_MIN_ORDER_NOTIONAL,
                        intent.correlation_id,
                        float(deploy),
                        min_n,
                    )
                    return None, str(ReasonCode.RISK_MIN_ORDER_NOTIONAL), meta
                qty = min_n / pr
                deploy = pr * qty
                bumped = True
                if deploy > max_n + eps:
                    _LOG.info(
                        "event=%s gate=order_deploy_infeasible reason=%s correlation_id=%s "
                        "after_min_bump_deploy=%.6g max=%.6g",
                        _TYREX_RISK_OPS_EVENT,
                        ReasonCode.RISK_ORDER_DEPLOYMENT_INFEASIBLE,
                        intent.correlation_id,
                        float(deploy),
                        max_n,
                    )
                    return None, str(ReasonCode.RISK_ORDER_DEPLOYMENT_INFEASIBLE), meta

        out = _intent_with_qty(intent, qty)
        meta["risk_approved_quantity"] = float(out.quantity)
        meta["risk_approved_order_deploy_usd"] = float(pr * out.quantity)
        meta["max_notional_policy_clipped"] = clipped
        meta["min_notional_policy_bumped"] = bumped
        return out, "", meta

    def _token_deployment_cap_eval(
        self,
        intent: OrderIntent,
        order_deploy: float,
    ) -> tuple[bool, str]:
        db = self._deployment_budget
        if db is None:
            return False, ReasonCode.RISK_TOKEN_DEPLOYMENT_UNRESOLVED
        tok_dep, ok, err = db.token_deployment_usd_with_policy(
            intent.token_id,
            strict_filled=self._s.fail_on_unresolved_token_deployment,
        )
        if not ok:
            _LOG.info(
                "event=%s gate=token_deployment reason=%s correlation_id=%s detail=%s",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_TOKEN_DEPLOYMENT_UNRESOLVED,
                intent.correlation_id,
                _ops_snippet(err) or "(none)",
            )
            return False, ReasonCode.RISK_TOKEN_DEPLOYMENT_UNRESOLVED
        cap = self._s.max_token_notional_usd_open
        if tok_dep + order_deploy > cap:
            _LOG.info(
                "event=%s gate=token_deployment_cap reason=%s correlation_id=%s "
                "token_deploy=%.6g order_deploy=%.6g cap=%.6g",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_TOKEN_DEPLOYMENT_EXCEEDED,
                intent.correlation_id,
                float(tok_dep),
                float(order_deploy),
                float(cap),
            )
            return False, ReasonCode.RISK_TOKEN_DEPLOYMENT_EXCEEDED
        return True, ""

    def _portfolio_deployment_cap_eval(
        self,
        intent: OrderIntent,
        order_deploy: float | None,
    ) -> tuple[bool, str]:
        db = self._deployment_budget
        if db is None:
            _LOG.info(
                "event=%s gate=portfolio_deployment reason=%s correlation_id=%s detail=no_reader",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_PORTFOLIO_DEPLOYMENT_UNRESOLVED,
                intent.correlation_id,
            )
            return False, ReasonCode.RISK_PORTFOLIO_DEPLOYMENT_UNRESOLVED

        pf, ok, err = db.portfolio_deployment_usd_with_policy(
            strict_filled=self._s.fail_on_unresolved_portfolio_deployment,
        )
        if not ok:
            _LOG.info(
                "event=%s gate=portfolio_deployment reason=%s correlation_id=%s detail=%s",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_PORTFOLIO_DEPLOYMENT_UNRESOLVED,
                intent.correlation_id,
                _ops_snippet(err) or "(none)",
            )
            return False, ReasonCode.RISK_PORTFOLIO_DEPLOYMENT_UNRESOLVED

        if order_deploy is None:
            return False, ReasonCode.RISK_MISSING_PRICE

        cap = self._s.max_portfolio_notional_usd_open
        if pf + order_deploy > cap:
            _LOG.info(
                "event=%s gate=portfolio_deployment_cap reason=%s correlation_id=%s "
                "portfolio_deploy=%.6g order_deploy=%.6g cap=%.6g",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_PORTFOLIO_DEPLOYMENT_EXCEEDED,
                intent.correlation_id,
                float(pf),
                float(order_deploy),
                float(cap),
            )
            return False, ReasonCode.RISK_PORTFOLIO_DEPLOYMENT_EXCEEDED
        return True, ""

    def _emit_risk_and_deployment(
        self,
        *,
        intent_strategy: OrderIntent,
        intent_at_eval: OrderIntent,
        allowed: bool,
        reason_code: str,
        deploy_adjust_meta: dict[str, Any],
    ) -> None:
        fe = self._fact_emit
        if fe is None:
            return
        rc = str(reason_code)
        order_deploy = _order_deploy_usd(intent_at_eval)
        if (
            self._reporting_capital_observability_enabled
            and not self._s.capital_gate_enabled
        ):
            self._observability_refresh_caches_best_effort()

        snap_seq = 0
        if self._reporting_capital_observability_enabled or self._s.capital_gate_enabled:
            snap_seq = self._emit_account_snapshot_row(
                snapshot_trigger="risk_eval",
                correlation_id=intent_strategy.correlation_id,
                intent=intent_at_eval,
            )

        cap_m = self._capital_metrics_for_facts(intent_at_eval)
        token_dep: float | None = None
        portfolio_dep: float | None = None
        db = self._deployment_budget
        if db is not None:
            t_acc, t_ok, _ = db.token_deployment_usd_with_policy(
                intent_strategy.token_id,
                strict_filled=self._s.fail_on_unresolved_token_deployment,
            )
            if t_ok:
                token_dep = t_acc
            p_acc, p_ok, _ = db.portfolio_deployment_usd_with_policy(
                strict_filled=self._s.fail_on_unresolved_portfolio_deployment,
            )
            if p_ok:
                portfolio_dep = p_acc

        rd: dict[str, Any] = {
            "correlation_id": intent_strategy.correlation_id,
            "allowed": bool(allowed),
            "reason_code": rc,
            "gate": "",
            "capital_gate_enabled": self._s.capital_gate_enabled,
            "pre_venue_collateral_check_active": self._s.capital_gate_enabled,
            "account_snapshot_seq": snap_seq if snap_seq > 0 else None,
            "collateral_reserve_usd": float(self._s.collateral_reserve_usd),
            "intent_notional_usd": order_deploy,
            "order_deploy_usd_at_eval": order_deploy,
            "token_deploy_at_eval": token_dep,
            "portfolio_deploy_at_eval": portfolio_dep,
            "intent_side": str(intent_strategy.side),
            "observability_mode_enabled": self._reporting_capital_observability_enabled,
            **deploy_adjust_meta,
            **cap_m,
        }
        fe("risk_decision", rd)

        if db is not None and order_deploy is not None:
            pend_t, pend_ok, _ = db.pending_usd_for_token(intent_strategy.token_id)
            fill_t, fill_ok = db.filled_usd_for_token(intent_strategy.token_id)
            pend_all, pend_all_ok, _ = db.pending_polymarket_usd()
            fill_all, fill_all_ok = db.filled_polymarket_usd()
            fe(
                "deployment_budget",
                {
                    "correlation_id": intent_strategy.correlation_id,
                    "order_deploy_usd": order_deploy,
                    "token_pending_usd": pend_t if pend_ok else None,
                    "token_filled_usd": fill_t if fill_ok else None,
                    "token_deploy_usd": token_dep,
                    "portfolio_pending_usd": pend_all if pend_all_ok else None,
                    "portfolio_filled_usd": fill_all if fill_all_ok else None,
                    "portfolio_deploy_usd": portfolio_dep,
                },
            )

    def _maybe_periodic_capital_snapshot(self) -> None:
        if (
            self._fact_emit is None
            or not self._reporting_capital_observability_enabled
            or self._reporting_capital_period_s <= 0
        ):
            return
        now_m = time.monotonic()
        last = self._last_periodic_capital_mono
        if last is not None and (now_m - last) < self._reporting_capital_period_s:
            return
        self._last_periodic_capital_mono = now_m
        self._observability_refresh_caches_best_effort()
        self._emit_account_snapshot_row(
            snapshot_trigger="periodic",
            correlation_id=None,
            intent=None,
        )

    def _observability_refresh_caches_best_effort(self) -> None:
        if self._account_snapshot is not None:
            try:
                self._account_cache = self._account_snapshot.snapshot()
            except Exception:  # noqa: BLE001
                pass
        if (
            self._allowance_observability_enabled
            and self._allowance_provider is not None
        ):
            try:
                self._allowance_cache = self._allowance_provider.snapshot()
            except Exception:  # noqa: BLE001
                pass

    def _capital_metrics_for_facts(self, intent: OrderIntent | None) -> dict[str, Any]:
        """
        Normalized py-clob money, optional Nautilus cash free, canonical balance for headroom.

        ``balance_canonical_usd`` prefers Nautilus cash ``free`` when extractable (framework truth);
        otherwise normalized CLOB balance. Headroom uses canonical only.
        """
        acct = self._account_cache
        balances = None
        if acct is not None and acct.account_present:
            balances = acct.balances
        n_free, n_note = extract_nautilus_cash_free_usd(balances)

        clob_p = None
        bal_py: float | None = None
        allow_py: float | None = None
        raw_bal_s: str | None = None
        raw_allow_s: str | None = None
        b_note = "no_allowance_cache"
        a_note = "no_allowance_cache"
        if self._allowance_cache is not None:
            clob_p = parse_clob_collateral_usd(self._allowance_cache.raw)
            bal_py = clob_p.balance_usd
            allow_py = clob_p.allowance_usd
            raw_bal_s = clob_p.balance_raw
            raw_allow_s = clob_p.allowance_raw
            b_note = clob_p.balance_parse_note
            a_note = clob_p.allowance_parse_note

        if n_free is not None:
            canonical = n_free
            c_src = "nautilus_cash_account"
        elif bal_py is not None:
            canonical = bal_py
            c_src = "py_clob_balance_allowance"
        else:
            canonical = None
            c_src = "none"

        n = _order_deploy_usd(intent) if intent is not None else None
        headroom: float | None = None
        if intent is not None and intent.side.upper() == "BUY":
            headroom = compute_buy_headroom_usd(
                canonical,
                float(self._s.collateral_reserve_usd),
                n,
            )

        disagree = False
        disc_abs: float | None = None
        if n_free is not None and bal_py is not None:
            disc_abs = abs(float(n_free) - float(bal_py))
            if disc_abs > 0.005:
                disagree = True

        business_ok = canonical is not None and (n_free is not None or not disagree)

        return {
            "py_clob_balance_usd": bal_py,
            "py_clob_allowance_usd": allow_py,
            "py_clob_balance_raw": raw_bal_s,
            "py_clob_allowance_raw": raw_allow_s,
            "py_clob_balance_parse_note": b_note,
            "py_clob_allowance_parse_note": a_note,
            "nautilus_cash_free_usd": n_free,
            "nautilus_cash_extract_note": n_note,
            "balance_canonical_usd": canonical,
            "capital_canonical_balance_source": c_src,
            "capital_balance_sources_disagree": disagree,
            "capital_balance_abs_discrepancy_usd": disc_abs,
            "capital_balance_business_trusted": business_ok,
            "estimated_buy_headroom_usd": headroom,
            "wallet_collateral_numeric_known": canonical is not None,
        }

    def _emit_account_snapshot_row(
        self,
        *,
        snapshot_trigger: str,
        correlation_id: str | None,
        intent: OrderIntent | None,
    ) -> int:
        """
        Return monotonic ``account_snapshot_seq`` (0 when skipped).
        """
        fe = self._fact_emit
        if fe is None:
            return 0
        if not self._reporting_capital_observability_enabled and not self._s.capital_gate_enabled:
            return 0

        self._reporting_account_seq += 1
        seq = self._reporting_account_seq
        acct = self._account_cache
        captured = datetime.now(tz=UTC)
        acct_present = False
        venue_s = ""
        balances_txt: str | None = None
        if acct is not None:
            captured = acct.captured_at_utc
            acct_present = bool(acct.account_present)
            venue_s = str(acct.venue)
            if acct.balances is not None:
                balances_txt = trim_json_text(acct.balances)
        if self._allowance_cache is not None:
            if self._allowance_cache.captured_at_utc > captured:
                captured = self._allowance_cache.captured_at_utc

        cap_m = self._capital_metrics_for_facts(intent)
        n = _order_deploy_usd(intent) if intent is not None else None
        payload: dict[str, Any] = {
            "account_snapshot_seq": seq,
            "account_present": acct_present,
            "snapshot_trigger": snapshot_trigger,
            "captured_at_utc": captured.isoformat(),
            "capital_gate_enabled": self._s.capital_gate_enabled,
            "pre_venue_collateral_check_active": self._s.capital_gate_enabled,
            "observability_mode_enabled": self._reporting_capital_observability_enabled,
            "allowance_pull_enabled": self._allowance_observability_enabled,
            "nautilus_venue": venue_s or None,
            "collateral_reserve_usd": float(self._s.collateral_reserve_usd),
            "intent_notional_usd": n,
            "intent_side": str(intent.side) if intent is not None else None,
            "nautilus_balances_json": balances_txt,
            "deployment_budget_wired": self._deployment_budget is not None,
            **cap_m,
        }
        if correlation_id is not None:
            payload["correlation_id"] = correlation_id
        if not self._s.capital_gate_enabled and self._reporting_capital_observability_enabled:
            payload["operator_note"] = (
                "capital_gate_enabled=false: wallet snapshots may still be recorded but "
                "pre-venue checks were not required for approvals"
            )
        fe("account_snapshot", payload)
        return seq

    def _guru_concurrent_resting_cap_eval(self, intent: OrderIntent) -> tuple[bool, str]:
        """Deny when open guru-origin resting orders already at ``max_concurrent_guru_resting_orders``."""
        lim = self._s.max_concurrent_guru_resting_orders
        if lim is None:
            return True, ""
        cid = intent.correlation_id
        er = self._execution_reader
        if er is None:
            _LOG.info(
                "event=%s gate=guru_concurrent reason=%s correlation_id=%s "
                "guru_resting_count=(no_reader) limit=%s",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT,
                cid,
                lim,
            )
            return False, ReasonCode.RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT
        try:
            cnt = er.count_guru_resting_orders_open(venue=POLYMARKET_VENUE_ID)
        except (TypeError, AttributeError):
            _LOG.info(
                "event=%s gate=guru_concurrent reason=%s correlation_id=%s "
                "guru_resting_count=(count_failed) limit=%s",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT,
                cid,
                lim,
            )
            return False, ReasonCode.RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT
        if cnt >= lim:
            _LOG.info(
                "event=%s gate=guru_concurrent reason=%s correlation_id=%s guru_resting_count=%s limit=%s",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT,
                cid,
                cnt,
                lim,
            )
            return False, ReasonCode.RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT
        return True, ""

    def _capital_gate_eval(
        self,
        now: datetime,
        intent: OrderIntent,
        intent_notional: float | None,
    ) -> tuple[bool, str]:
        if not self._s.capital_gate_enabled:
            if self._s.collateral_reserve_usd > 0:
                return False, ReasonCode.RISK_ALLOWANCE_UNAVAILABLE
            return True, ""

        if self._account_snapshot is None:
            return False, ReasonCode.RISK_ACCOUNT_UNAVAILABLE

        need_acct = (
            self._account_cache is None
            or (now - self._account_cache.captured_at_utc).total_seconds()
            >= self._s.max_account_snapshot_age_seconds
        )
        if need_acct:
            self._account_cache = self._account_snapshot.snapshot()
        if self._account_cache is None or not self._account_cache.account_present:
            return False, ReasonCode.RISK_ACCOUNT_UNAVAILABLE

        need_py_clob_snapshot = (
            self._s.min_collateral_balance_usd is not None
            or self._s.min_allowance_usd is not None
            or self._s.collateral_reserve_usd > 0
        )
        bal: float | None = None
        allow: float | None = None

        if need_py_clob_snapshot:
            if self._allowance_provider is None:
                return False, ReasonCode.RISK_ALLOWANCE_UNAVAILABLE
            need_alw = (
                self._allowance_cache is None
                or (now - self._allowance_cache.captured_at_utc).total_seconds()
                >= self._s.max_allowance_snapshot_age_seconds
            )
            if need_alw:
                self._allowance_cache = self._allowance_provider.snapshot()
            if self._allowance_cache is None:
                return False, ReasonCode.RISK_ALLOWANCE_UNAVAILABLE
            clob_p = parse_clob_collateral_usd(self._allowance_cache.raw)
            bal, allow = clob_p.balance_usd, clob_p.allowance_usd

            if self._s.min_collateral_balance_usd is not None:
                if bal is None:
                    return False, ReasonCode.RISK_ALLOWANCE_UNAVAILABLE
                if bal < self._s.min_collateral_balance_usd:
                    _LOG.info(
                        "event=%s gate=min_collateral reason=%s py_clob_balance=%.6g min_required=%.6g",
                        _TYREX_RISK_OPS_EVENT,
                        ReasonCode.RISK_INSUFFICIENT_COLLATERAL_BALANCE,
                        float(bal),
                        float(self._s.min_collateral_balance_usd),
                    )
                    return False, ReasonCode.RISK_INSUFFICIENT_COLLATERAL_BALANCE
            if self._s.min_allowance_usd is not None:
                if allow is None:
                    return False, ReasonCode.RISK_ALLOWANCE_UNAVAILABLE
                if allow < self._s.min_allowance_usd:
                    _LOG.info(
                        "event=%s gate=min_allowance reason=%s py_clob_allowance=%.6g min_required=%.6g",
                        _TYREX_RISK_OPS_EVENT,
                        ReasonCode.RISK_INSUFFICIENT_ALLOWANCE,
                        float(allow),
                        float(self._s.min_allowance_usd),
                    )
                    return False, ReasonCode.RISK_INSUFFICIENT_ALLOWANCE

        if self._s.collateral_reserve_usd > 0:
            if bal is None:
                return False, ReasonCode.RISK_ALLOWANCE_UNAVAILABLE
            if intent.side.upper() == "BUY":
                if intent_notional is None:
                    return False, ReasonCode.RISK_MISSING_PRICE
                reserve = float(self._s.collateral_reserve_usd)
                need = reserve + float(intent_notional)
                if bal < need:
                    _LOG.info(
                        "event=%s gate=reserve reason=%s correlation_id=%s "
                        "py_clob_balance=%.6g reserve_usd=%.6g intent_notional=%.6g required_free=%.6g",
                        _TYREX_RISK_OPS_EVENT,
                        ReasonCode.RISK_INSUFFICIENT_FREE_COLLATERAL_AFTER_RESERVE,
                        intent.correlation_id,
                        float(bal),
                        reserve,
                        float(intent_notional),
                        need,
                    )
                    return False, ReasonCode.RISK_INSUFFICIENT_FREE_COLLATERAL_AFTER_RESERVE

        return True, ""


def _intent_with_qty(intent: OrderIntent, qty: float) -> OrderIntent:
    return OrderIntent(
        correlation_id=intent.correlation_id,
        token_id=intent.token_id,
        side=intent.side,
        quantity=float(qty),
        signal_kind=intent.signal_kind,
        reason_code=intent.reason_code,
        price_ref=intent.price_ref,
    )


def _deploy_adjust_base_meta(intent: OrderIntent, s: RiskSettings) -> dict[str, Any]:
    od = _order_deploy_usd(intent)
    q = float(intent.quantity)
    return {
        "strategy_quantity": q,
        "strategy_order_deploy_usd": od,
        "risk_approved_quantity": q,
        "risk_approved_order_deploy_usd": od,
        "max_notional_policy_clipped": False,
        "min_notional_policy_bumped": False,
        "max_notional_policy": str(s.max_notional_policy),
        "min_notional_policy": str(s.min_notional_policy),
    }


def _order_deploy_usd(intent: OrderIntent) -> float | None:
    if intent.price_ref is None:
        return None
    return float(intent.price_ref) * float(intent.quantity)
