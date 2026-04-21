from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.ids import ClientOrderId, RunId
from tyrex_pm.core.models import (
    ApprovedCancel,
    ApprovedIntent,
    CancelIntent,
    EnterIntent,
    ExitIntent,
    Intent,
    ReduceIntent,
    RiskContext,
    RiskDecision,
)
from tyrex_pm.risk import (
    capital,
    concurrency,
    deployment,
    health,
    inventory,
    kill_switch,
    pretrade,
    venue_min_size,
)
from tyrex_pm.risk.deployment import RiskConfigCaps
from tyrex_pm.risk.evidence_format import s_usd, s_usd_map
from tyrex_pm.runtime.config import AppConfig


def evaluate_intent(
    intent: Intent,
    ctx: RiskContext,
    *,
    app: AppConfig,
    run_id: RunId,
) -> RiskDecision:
    r = app.risk

    ok, reason = kill_switch.check_kill_switch(enabled=r.kill_switch.enabled)
    if not ok:
        return RiskDecision(False, (reason or rc.KILL_SWITCH,), None, None)

    if isinstance(intent, CancelIntent):
        if intent.venue_order_id is None and intent.client_order_id is None:
            return RiskDecision(False, (rc.UNKNOWN,), None, "cancel missing venue/client id")
        ac = ApprovedCancel(
            venue_order_id=intent.venue_order_id,
            client_order_id=intent.client_order_id,
            run_id=run_id,
            intent_id=intent.intent_id,
        )
        return RiskDecision(True, (rc.APPROVED,), None, None, ac)

    ok, reason = concurrency.check_concurrency(ctx, max_in_flight=r.concurrency.max_orders_in_flight)
    if not ok:
        return RiskDecision(False, (reason or rc.CONCURRENCY_LIMIT,), None, None)

    ok, reason = health.check_aggressive_readiness(ctx, runtime=app.runtime, readiness=r.readiness)
    if not ok:
        code = reason or rc.NOT_READY
        return RiskDecision(False, (code,), None, None)

    if isinstance(intent, (EnterIntent, ExitIntent, ReduceIntent)):
        work, ext, deny = pretrade.apply_notional_min_max(
            intent,
            min_usd=r.notional.min_usd,
            max_usd=r.notional.max_usd,
            max_policy=r.notional.max_policy,
        )
        # Always carry the in-flight reservation summary on the decision (approve OR deny)
        # so the ``risk_decision`` fact is self-contained for operator audit. The deployment +
        # capital gates further down use the same set; here we just surface the totals
        # unconditionally so an "approved" fact also shows what was already reserved.
        _in_flight_total = sum(
            (r.remaining_size * r.limit_price for r in ctx.in_flight_buy_reservations),
            start=Decimal("0"),
        )
        _in_flight_by_token_dec: dict[str, Decimal] = {}
        for r_ in ctx.in_flight_buy_reservations:
            usd = r_.remaining_size * r_.limit_price
            _in_flight_by_token_dec[str(r_.token_id)] = (
                _in_flight_by_token_dec.get(str(r_.token_id), Decimal("0")) + usd
            )
        ext = {
            **ext,
            "in_flight_reserved_usd_total": s_usd(_in_flight_total),
            "in_flight_reservation_count": len(ctx.in_flight_buy_reservations),
            "in_flight_reserved_usd_by_token": s_usd_map(_in_flight_by_token_dec),
        }
        if deny:
            return RiskDecision(False, (deny,), None, None, None, ext)

        caps = RiskConfigCaps(
            token_cap_usd=r.deployment.token_cap_usd,
            portfolio_cap_usd=r.deployment.portfolio_cap_usd,
        )
        ok_d, reason_d, dep_evidence = deployment.evaluate_deployment_caps(
            caps, ctx, pending_intent=work
        )
        if not ok_d:
            ext_dep = {**ext, **dep_evidence}
            return RiskDecision(
                False, (reason_d or rc.TOKEN_DEPLOYMENT_CAP,), None, None, None, ext_dep
            )

        if isinstance(work, EnterIntent):
            cap_eval = capital.evaluate_capital_buy(work, ctx, enabled=r.capital.enabled)
            # Merge capital evidence (incl. in-flight reservation netting) into BOTH
            # approve and deny paths so risk_decision facts always show why a BUY was
            # gated against the *effective* free balance, not the raw wallet figure.
            ext = {**ext, **cap_eval.evidence}
            if not cap_eval.ok:
                return RiskDecision(
                    False,
                    (cap_eval.reason or rc.INSUFFICIENT_CAPITAL,),
                    None,
                    None,
                    None,
                    ext,
                )

        if isinstance(work, ExitIntent | ReduceIntent):
            ok_i, reason_i = inventory.check_inventory_sell(
                work,
                ctx,
                require_position=r.inventory.sell_requires_venue_position,
            )
            if not ok_i:
                return RiskDecision(False, (reason_i or rc.NAKED_SELL,), None, None, None, ext)

        # Venue minimum-size gate (last gate before submit). Runs after notional/deployment/
        # capital/inventory so it sees the *final* size that would otherwise reach the OMS.
        # Without this gate, a clipped order such as 4.54 shares hits the venue's hard
        # 5-share floor and is rejected with "Size lower than the minimum: 5".
        vms = venue_min_size.evaluate_venue_min_size(work, r.venue_min_size, ctx)
        ext = {**ext, **vms.evidence}
        if not vms.ok:
            return RiskDecision(
                False, (vms.deny_reason or rc.BELOW_VENUE_MIN_SIZE,), None, None, None, ext
            )
        if vms.intent is not work:
            # ``policy=bump`` produced a larger size — re-validate the higher-priority gates
            # against the bumped intent. Skipping this would let the bump silently push the
            # order past deployment/capital limits the original size respected.
            work = vms.intent
            ok_d2, reason_d2, dep_evidence2 = deployment.evaluate_deployment_caps(
                caps, ctx, pending_intent=work
            )
            if not ok_d2:
                ext_dep = {
                    **ext,
                    **dep_evidence2,
                    "venue_min_size_bump_unsafe": True,
                    "venue_min_size_bump_unsafe_reason": reason_d2 or rc.TOKEN_DEPLOYMENT_CAP,
                }
                return RiskDecision(
                    False,
                    (rc.BELOW_VENUE_MIN_SIZE,),
                    None,
                    None,
                    None,
                    ext_dep,
                )
            if isinstance(work, EnterIntent):
                cap_eval2 = capital.evaluate_capital_buy(
                    work, ctx, enabled=r.capital.enabled
                )
                ext = {**ext, **cap_eval2.evidence}
                if not cap_eval2.ok:
                    ext = {
                        **ext,
                        "venue_min_size_bump_unsafe": True,
                        "venue_min_size_bump_unsafe_reason": (
                            cap_eval2.reason or rc.INSUFFICIENT_CAPITAL
                        ),
                    }
                    return RiskDecision(
                        False, (rc.BELOW_VENUE_MIN_SIZE,), None, None, None, ext
                    )

        cid = ClientOrderId(str(uuid4()))
        approved = ApprovedIntent(intent=work, client_order_id=cid, run_id=run_id)
        return RiskDecision(True, (rc.APPROVED,), approved, None, None, ext)

    return RiskDecision(False, (rc.UNKNOWN,), None, "unsupported intent")
