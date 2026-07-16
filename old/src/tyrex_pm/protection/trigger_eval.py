"""Pure trigger evaluation for protection (P4 architecture_enhance).

No I/O, no state mutation — given an entry price, an observed price and a policy,
decide whether a take-profit or stop-loss trigger has fired. Kept pure so it is
trivially unit-testable and reusable by both the monitor and regression harness.
"""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.protection.config import ProtectionPolicy

TRIGGER_TAKE_PROFIT = "take_profit"
TRIGGER_STOP_LOSS = "stop_loss"


def resolve_thresholds(
    policy: ProtectionPolicy, entry_price: Decimal
) -> tuple[Decimal | None, Decimal | None, dict[str, str]]:
    """Return ``(tp_trigger_price, sl_trigger_price, evidence)``.

    Absolute prices win over percentage thresholds when both are present.
    """
    evidence: dict[str, str] = {"entry_price": str(entry_price)}
    tp: Decimal | None = None
    sl: Decimal | None = None
    if policy.take_profit_pct is not None:
        evidence["take_profit_pct"] = str(policy.take_profit_pct)
        tp = entry_price * (Decimal("1") + policy.take_profit_pct)
    if policy.stop_loss_pct is not None:
        evidence["stop_loss_pct"] = str(policy.stop_loss_pct)
        sl = entry_price * (Decimal("1") - policy.stop_loss_pct)
    if policy.take_profit_price is not None:
        tp = policy.take_profit_price
    if policy.stop_loss_price is not None:
        sl = policy.stop_loss_price
    if tp is not None:
        evidence["take_profit_trigger_price"] = str(tp)
    if sl is not None:
        evidence["stop_loss_trigger_price"] = str(sl)
    return tp, sl, evidence


def evaluate_trigger(
    *,
    observed_price: Decimal,
    take_profit_trigger_price: Decimal | None,
    stop_loss_trigger_price: Decimal | None,
) -> str | None:
    if take_profit_trigger_price is not None and observed_price >= take_profit_trigger_price:
        return TRIGGER_TAKE_PROFIT
    if stop_loss_trigger_price is not None and observed_price <= stop_loss_trigger_price:
        return TRIGGER_STOP_LOSS
    return None
