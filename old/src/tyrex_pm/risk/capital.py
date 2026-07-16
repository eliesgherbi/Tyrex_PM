"""USDC balance / allowance gate for live BUYs.

The venue locks collateral the moment a BUY is accepted (HTTP 200 from POST /order). The
merged ``WalletStore.usdc_balance`` only reflects that lock once the venue's
``/balance-allowance`` endpoint catches up — typically 100 ms to several seconds later.

Without netting in-flight reservations against the wallet view, this gate happily approves
a fresh BUY that the venue rejects with::

    not enough balance / allowance: the balance is not enough -> balance: X,
    sum of matched orders: Y, order amount: Z

See :mod:`tyrex_pm.risk.in_flight` for how the reservation set is derived.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import Side
from tyrex_pm.core.models import EnterIntent, RiskContext
from tyrex_pm.risk.evidence_format import s_usd


@dataclass(frozen=True)
class CapitalEvaluation:
    """Result of an evaluated capital gate, with full evidence for risk_decision facts."""

    ok: bool
    reason: str | None
    evidence: dict[str, Any]


def _reservation_total_usd(ctx: RiskContext) -> Decimal:
    total = Decimal("0")
    for r in ctx.in_flight_buy_reservations:
        total += r.remaining_size * r.limit_price
    return total


def evaluate_capital_buy(
    intent: EnterIntent, ctx: RiskContext, *, enabled: bool
) -> CapitalEvaluation:
    """Evaluate the USDC balance + allowance gate including in-flight reservations.

    Lifecycle of the reservation netting (mirrors :mod:`tyrex_pm.risk.in_flight`):

    * a successful submit creates a provisional ``LocalOrder`` → reservation is added
    * the merged wallet view eventually reflects the order → reservation is dropped
      (dedup-by-vid in the derivation helper) so the wallet figure is the source of truth
    * a venue reject / fill / cancel removes the ``LocalOrder`` → reservation is released

    Without this netting, ``need <= ctx.usdc_balance`` is a stale comparison: the venue
    has already locked ``in_flight_reserved_usd_total`` worth of collateral that the
    balance figure has not yet caught up with.
    """
    evidence: dict[str, Any] = {
        "capital_gate_checked": True,
        "capital_gate_enabled": enabled,
    }
    if not enabled or intent.side != Side.BUY:
        evidence["capital_gate_skipped_reason"] = (
            "disabled" if not enabled else "non_buy_intent"
        )
        return CapitalEvaluation(True, None, evidence)
    if ctx.usdc_balance is None or ctx.usdc_allowance is None:
        evidence["wallet_balance_known"] = False
        return CapitalEvaluation(False, rc.INSUFFICIENT_CAPITAL, evidence)

    need = intent.size * (intent.limit_price or Decimal("0"))
    reserved = _reservation_total_usd(ctx)
    effective_balance = ctx.usdc_balance - reserved
    effective_allowance = ctx.usdc_allowance - reserved

    evidence.update(
        {
            "wallet_balance_known": True,
            "wallet_usdc_balance": s_usd(ctx.usdc_balance),
            "wallet_usdc_allowance": s_usd(ctx.usdc_allowance),
            "in_flight_reserved_usd_total": s_usd(reserved),
            "effective_free_balance_usd": s_usd(effective_balance),
            "effective_free_allowance_usd": s_usd(effective_allowance),
            "intent_need_usd": s_usd(need),
        }
    )

    if need > effective_balance:
        evidence["capital_deny_kind"] = "balance"
        return CapitalEvaluation(False, rc.INSUFFICIENT_CAPITAL, evidence)
    if need > effective_allowance:
        evidence["capital_deny_kind"] = "allowance"
        return CapitalEvaluation(False, rc.INSUFFICIENT_ALLOWANCE, evidence)
    return CapitalEvaluation(True, None, evidence)


def check_capital_buy(intent: EnterIntent, ctx: RiskContext, *, enabled: bool) -> tuple[bool, str | None]:
    """Back-compat wrapper around :func:`evaluate_capital_buy` (drops evidence)."""
    res = evaluate_capital_buy(intent, ctx, enabled=enabled)
    return res.ok, res.reason
