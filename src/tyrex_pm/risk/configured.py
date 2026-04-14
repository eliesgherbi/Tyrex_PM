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
from tyrex_pm.runtime.capital import CapitalState, CapitalStateProvider
from tyrex_pm.runtime.capital.policy import CapitalSnapshotPolicy
from tyrex_pm.runtime.deployment_budget import NautilusDeploymentBudget
from tyrex_pm.runtime.state_readers import POLYMARKET_VENUE_ID, ExecutionStateReader
from tyrex_pm.runtime.tradable_state import (
    TradableStateHealthSnapshot,
    TradableStateHealthSource,
    tradable_health_allows_intent,
)
from tyrex_pm.runtime.tradable_state.synthetic import synthetic_snapshot_health_source_missing

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

    **SELL:** exit inventory is verified against ``filled_usd_for_token`` before the additive
    token/portfolio open-cap formulas apply; eligible exits may skip only those formulas
    (see :meth:`_sell_exit_inventory_gate`).

    Guru concurrent resting cap and collateral reserve behave as in :class:`~tyrex_pm.config.loaders.RiskSettings`.
    """

    def __init__(
        self,
        settings: RiskSettings,
        *,
        execution_reader: ExecutionStateReader | None = None,
        capital_provider: CapitalStateProvider | None = None,
        deployment_budget: NautilusDeploymentBudget | None = None,
        tradable_state_health_source: TradableStateHealthSource | None = None,
        fact_emit: Callable[[str, dict[str, Any]], None] | None = None,
        reporting_capital_observability_enabled: bool = False,
        reporting_capital_snapshot_period_seconds: float = 0.0,
    ) -> None:
        self._s = settings
        self._execution_reader = execution_reader
        self._capital_provider = capital_provider
        self._deployment_budget = deployment_budget
        self._tradable_state_health_source = tradable_state_health_source
        self._last_health_snap: TradableStateHealthSnapshot | None = None
        self._capital_cache: CapitalState | None = None
        self._fact_emit = fact_emit
        self._reporting_account_seq = 0
        self._reporting_capital_observability_enabled = bool(
            reporting_capital_observability_enabled,
        )
        self._reporting_capital_period_s = float(reporting_capital_snapshot_period_seconds)
        self._last_periodic_capital_mono: float | None = None

    @property
    def execution_reader(self):
        """Nautilus cache-backed reader (same node's ``Cache``); never owned by strategy."""
        return self._execution_reader

    @property
    def capital_state_provider(self) -> CapitalStateProvider | None:
        """Single capital read path (framework account + optional CLOB inside provider)."""
        return self._capital_provider

    @property
    def deployment_budget(self):
        """Deployment accounting; ``None`` in shadow (no framework portfolio cap)."""
        return self._deployment_budget

    def _capital_policy(self) -> CapitalSnapshotPolicy:
        return CapitalSnapshotPolicy.from_risk_settings(self._s)

    def framework_open_order_count(self) -> int:
        """Count of orders Nautilus marks open — for tests and operator diagnostics."""
        if self._execution_reader is None:
            return 0
        return len(self._execution_reader.list_open_orders())

    def evaluate(self, intent: OrderIntent) -> tuple[bool, str, OrderIntent | None]:
        self._maybe_periodic_capital_snapshot()
        self._last_health_snap = None
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

        if self._s.tradable_state_health_gate_enabled:
            if self._tradable_state_health_source is None:
                # WP4 — joinable health reporting without implying a real framework producer exists.
                self._last_health_snap = synthetic_snapshot_health_source_missing(
                    observed_at_utc=_utc_now(),
                )
                meta = _deploy_adjust_base_meta(intent, self._s)
                self._emit_risk_and_deployment(
                    intent_strategy=intent,
                    intent_at_eval=intent,
                    allowed=False,
                    reason_code=str(ReasonCode.RISK_HEALTH_UNKNOWN_BOOTSTRAP),
                    deploy_adjust_meta=meta,
                )
                return False, str(ReasonCode.RISK_HEALTH_UNKNOWN_BOOTSTRAP), None
            h_snap = self._tradable_state_health_source.snapshot()
            self._last_health_snap = h_snap
            ok_h, rc_h = tradable_health_allows_intent(
                h_snap,
                side_upper=intent.side.upper(),
                allow_exit_when_degraded_oms=self._s.allow_exit_when_degraded_oms,
            )
            if not ok_h:
                meta = _deploy_adjust_base_meta(intent, self._s)
                self._emit_risk_and_deployment(
                    intent_strategy=intent,
                    intent_at_eval=intent,
                    allowed=False,
                    reason_code=rc_h,
                    deploy_adjust_meta=meta,
                )
                return False, rc_h, None

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
        self._refresh_capital_cache_observability()
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

        side_u = intent.side.upper()
        # SELL: verify reducible long on token before any open-cap bypass; blocks naked sells.
        # Then bypass only the **additive** token/portfolio open-cap checks (not kill_switch,
        # per-order limits, capital gate, guru concurrent cap, etc.).
        sell_bypass_additive_open_caps = False
        if side_u == "SELL":
            deny_rc = self._sell_exit_inventory_gate(intent, order_deploy)
            if deny_rc is not None:
                return False, deny_rc, intent
            sell_bypass_additive_open_caps = self._sell_additive_open_cap_bypass_enabled()

        # Token/portfolio *open* caps: additive model matches BUY accumulation. For eligible SELL
        # exits, that sum double-counts resting sells vs inventory — bypass **only** those checks.
        if not math.isinf(self._s.max_token_notional_usd_open) and not sell_bypass_additive_open_caps:
            if order_deploy is None:
                if self._s.fail_on_missing_price_for_notional:
                    return False, str(ReasonCode.RISK_MISSING_PRICE), intent
            else:
                ok_t, rc_t = self._token_deployment_cap_eval(intent, order_deploy)
                if not ok_t:
                    return False, str(rc_t), intent

        if not math.isinf(self._s.max_portfolio_notional_usd_open) and not sell_bypass_additive_open_caps:
            ok_p, rc_p = self._portfolio_deployment_cap_eval(intent, order_deploy)
            if not ok_p:
                return False, str(rc_p), intent

        if self._s.max_concurrent_guru_resting_orders is not None:
            ok_cg, rc_cg = self._guru_concurrent_resting_cap_eval(intent)
            if not ok_cg:
                return False, str(rc_cg), intent

        return True, "approved", intent

    def _sell_additive_open_cap_bypass_enabled(self) -> bool:
        """True when SELL may skip only the additive token/portfolio *open* cap formulas."""
        return not math.isinf(self._s.max_token_notional_usd_open) or not math.isinf(
            self._s.max_portfolio_notional_usd_open,
        )

    def _sell_exit_inventory_gate(
        self,
        intent: OrderIntent,
        order_deploy: float | None,
    ) -> str | None:
        """
        SELL-only: require resolved positive ``filled_usd_for_token`` (deployment_budget basis)
        and ``order_deploy <=`` that inventory (prevents naked / oversized exit intents). When
        ``deployment_budget`` is missing but finite open caps need the exit bypass path,
        fail closed.
        """
        db = self._deployment_budget
        if db is None:
            if self._sell_additive_open_cap_bypass_enabled():
                _LOG.info(
                    "event=%s gate=sell_inventory reason=%s correlation_id=%s detail=no_deployment_budget",
                    _TYREX_RISK_OPS_EVENT,
                    ReasonCode.RISK_SELL_INVENTORY_UNVERIFIED,
                    intent.correlation_id,
                )
                return str(ReasonCode.RISK_SELL_INVENTORY_UNVERIFIED)
            return None

        filled, f_ok = db.filled_usd_for_token(intent.token_id)
        if not f_ok:
            _LOG.info(
                "event=%s gate=sell_inventory reason=%s correlation_id=%s detail=filled_unresolved",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_TOKEN_DEPLOYMENT_UNRESOLVED,
                intent.correlation_id,
            )
            return str(ReasonCode.RISK_TOKEN_DEPLOYMENT_UNRESOLVED)
        if filled <= 1e-12:
            _LOG.info(
                "event=%s gate=sell_inventory reason=%s correlation_id=%s filled_usd=%.6g",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_SELL_WITHOUT_FILLED_INVENTORY,
                intent.correlation_id,
                float(filled),
            )
            return str(ReasonCode.RISK_SELL_WITHOUT_FILLED_INVENTORY)
        if order_deploy is None:
            return str(ReasonCode.RISK_MISSING_PRICE)
        tol = max(1e-9, abs(float(filled)) * 1e-9)
        if float(order_deploy) > float(filled) + tol:
            _LOG.info(
                "event=%s gate=sell_inventory reason=%s correlation_id=%s "
                "order_deploy=%.6g filled_usd=%.6g",
                _TYREX_RISK_OPS_EVENT,
                ReasonCode.RISK_SELL_EXCEEDS_FILLED_INVENTORY,
                intent.correlation_id,
                float(order_deploy),
                float(filled),
            )
            return str(ReasonCode.RISK_SELL_EXCEEDS_FILLED_INVENTORY)
        return None

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
                "portfolio_deploy=%.6g order_deploy=%.6g cap=%.6g "
                "hint=portfolio_uses_nautilus_positions_not_wallet_cash",
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
        self._maybe_refresh_capital_cache_for_emit()

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
            "tradable_state_health_gate_enabled": self._s.tradable_state_health_gate_enabled,
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
        hs = self._last_health_snap
        if hs is not None:
            rd["tradable_state_health_level"] = hs.level.value
            rd["tradable_state_health_reason_code"] = hs.reason_code
            if hs.framework_detail is not None:
                rd["tradable_state_health_framework_detail"] = hs.framework_detail
        fe("risk_decision", rd)

        if hs is not None:
            self._emit_tradable_state_health_fact(
                hs,
                correlation_id=intent_strategy.correlation_id,
                risk_allowed=bool(allowed),
                risk_reason_code=rc,
                reporting_only_synthetic=(hs.reason_code == "health_source_missing"),
            )

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

    def _emit_tradable_state_health_fact(
        self,
        snap: TradableStateHealthSnapshot,
        *,
        correlation_id: str,
        risk_allowed: bool,
        risk_reason_code: str,
        reporting_only_synthetic: bool = False,
    ) -> None:
        fe = self._fact_emit
        if fe is None:
            return
        payload: dict[str, Any] = {
            "correlation_id": correlation_id,
            "level": snap.level.value,
            "reason_code": snap.reason_code,
            "observed_at_utc": snap.observed_at_utc.isoformat(),
            "risk_allowed": risk_allowed,
            "risk_reason_code": risk_reason_code,
        }
        if snap.framework_detail is not None:
            payload["framework_detail"] = snap.framework_detail
        if reporting_only_synthetic:
            payload["reporting_only_synthetic"] = True
        fe("tradable_state_health", payload)

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
        self._refresh_capital_cache_observability()
        self._emit_account_snapshot_row(
            snapshot_trigger="periodic",
            correlation_id=None,
            intent=None,
        )

    def _refresh_capital_cache_observability(self) -> None:
        if self._capital_provider is None:
            return
        try:
            self._capital_cache = self._capital_provider.snapshot(
                purpose="observability",
                policy=self._capital_policy(),
            )
        except Exception:  # noqa: BLE001
            pass

    def _maybe_refresh_capital_cache_for_emit(self) -> None:
        """Prefer observability snapshot for fact richness when enabled."""
        if self._capital_provider is None:
            return
        if self._reporting_capital_observability_enabled:
            self._refresh_capital_cache_observability()
        elif self._s.capital_gate_enabled and self._capital_cache is None:
            try:
                self._capital_cache = self._capital_provider.snapshot(
                    purpose="risk_gate",
                    policy=self._capital_policy(),
                )
            except Exception:  # noqa: BLE001
                pass

    def _capital_metrics_for_facts(self, intent: OrderIntent | None) -> dict[str, Any]:
        """
        Capital metrics from the unified :class:`~tyrex_pm.runtime.capital.CapitalState` cache.

        ``balance_canonical_usd`` prefers Nautilus cash ``free`` when extractable (framework truth);
        otherwise normalized CLOB balance. Headroom uses canonical only.
        """
        cap = self._capital_cache
        n_free = cap.nautilus_cash_free_usd if cap is not None else None
        n_note = cap.nautilus_cash_extract_note if cap is not None else "no_capital_cache"
        bal_py = cap.py_clob_balance_usd if cap is not None else None
        allow_py = cap.py_clob_allowance_usd if cap is not None else None
        raw_bal_s = cap.py_clob_balance_raw if cap is not None else None
        raw_allow_s = cap.py_clob_allowance_raw if cap is not None else None
        b_note = cap.py_clob_balance_parse_note if cap is not None else "no_capital_cache"
        a_note = cap.py_clob_allowance_parse_note if cap is not None else "no_capital_cache"

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

        out: dict[str, Any] = {
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
        if cap is not None:
            out["capital_state_source"] = cap.source.value
            out["capital_state_ok"] = cap.ok
            out["capital_state_merged_clob"] = cap.merged_clob
            pol = self._capital_policy()
            out["capital_fresh_enough"] = (
                self._capital_provider.freshness_ok(cap, policy=pol)
                if self._capital_provider is not None
                else None
            )
            out["capital_attrib_free_collateral_usd"] = (
                "nautilus_cash_free_usd"
                if cap.nautilus_cash_free_usd is not None
                else (
                    "py_clob_balance_usd"
                    if cap.py_clob_balance_usd is not None
                    else "unavailable"
                )
            )
            allow_raw = cap.py_clob_allowance_raw
            out["capital_attrib_allowance_usd"] = (
                "py_clob_allowance_usd"
                if cap.py_clob_allowance_usd is not None
                or (allow_raw is not None and str(allow_raw).strip() != "")
                else "unavailable"
            )
        return out

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
        cap = self._capital_cache
        captured = datetime.now(tz=UTC)
        acct_present = False
        venue_s = ""
        balances_txt: str | None = None
        if cap is not None:
            captured = cap.captured_at_utc
            acct_present = cap.account_present
            venue_s = cap.venue
            if cap.nautilus_balances is not None:
                balances_txt = trim_json_text(cap.nautilus_balances)

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
            "allowance_pull_enabled": bool(cap.merged_clob) if cap is not None else False,
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

        if self._capital_provider is None:
            return False, ReasonCode.RISK_ACCOUNT_UNAVAILABLE

        try:
            cap = self._capital_provider.snapshot(
                purpose="risk_gate",
                policy=self._capital_policy(),
            )
        except Exception:  # noqa: BLE001
            return False, ReasonCode.RISK_ACCOUNT_UNAVAILABLE
        self._capital_cache = cap

        if not cap.ok:
            if cap.error == "allowance_source_unavailable":
                return False, ReasonCode.RISK_ALLOWANCE_UNAVAILABLE
            return False, ReasonCode.RISK_ACCOUNT_UNAVAILABLE

        if not cap.account_present:
            return False, ReasonCode.RISK_ACCOUNT_UNAVAILABLE

        bal = cap.free_collateral_usd
        allow = cap.allowance_usd

        if self._s.min_collateral_balance_usd is not None:
            if bal is None:
                return False, ReasonCode.RISK_ALLOWANCE_UNAVAILABLE
            if bal < self._s.min_collateral_balance_usd:
                _LOG.info(
                    "event=%s gate=min_collateral reason=%s free_collateral_usd=%.6g min_required=%.6g",
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
                    "event=%s gate=min_allowance reason=%s allowance_usd=%.6g min_required=%.6g",
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
                        "free_collateral_usd=%.6g reserve_usd=%.6g intent_notional=%.6g required_free=%.6g",
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
