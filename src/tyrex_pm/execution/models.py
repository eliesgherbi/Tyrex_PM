"""ExecutionPlanner data model (P3 architecture_enhance).

``ExecutionPlan`` deliberately lives here (not in ``core/models.py``) so ``core``
stays small and stable. It carries the *final* concrete order (style, price,
size) chosen by the planner while preserving the ``client_order_id`` minted by
the pre-check approval so the whole chain
(``intent_created → risk_decision → execution_plan → risk_decision[planned] →
oms_submit``) shares one correlation/client-order identity.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from tyrex_pm.core.enums import OrderStyle
from tyrex_pm.core.ids import ClientOrderId, RunId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, ExitIntent, ReduceIntent

PlannableIntent = EnterIntent | ExitIntent | ReduceIntent


@dataclass(frozen=True)
class ExecutionPlan:
    """A validated-or-pending concrete order produced from an ApprovedIntent."""

    intent: PlannableIntent
    client_order_id: ClientOrderId
    run_id: RunId
    planner_reason: str
    urgency: str
    #: Pre-check approved limit price, kept so final validation can detect a
    #: planner that worsened the price after risk approved the intent.
    reference_limit_price: Decimal | None = None
    #: Operator evidence: best bid/ask, estimated fill price, staleness, etc.
    book_evidence: dict[str, Any] | None = None

    @property
    def order_style(self) -> OrderStyle:
        return self.intent.order_style

    @property
    def limit_price(self) -> Decimal | None:
        return self.intent.limit_price

    @property
    def size(self) -> Decimal:
        return self.intent.size

    def to_approved_intent(self) -> ApprovedIntent:
        """Rebuild an ApprovedIntent for the OMS, preserving the client order id."""
        return ApprovedIntent(
            intent=self.intent,
            client_order_id=self.client_order_id,
            run_id=self.run_id,
        )


def restyle_intent(
    intent: PlannableIntent,
    *,
    order_style: OrderStyle,
    limit_price: Decimal | None,
    size: Decimal | None = None,
) -> PlannableIntent:
    """Return a copy of ``intent`` with planner-chosen style/price/size.

    ``intent_id`` is preserved so correlation across facts is stable.
    """
    changes: dict[str, Any] = {"order_style": order_style, "limit_price": limit_price}
    if size is not None:
        changes["size"] = size
    return replace(intent, **changes)


@dataclass(frozen=True)
class ExecutionPlanResult:
    """Planner outcome: an approved plan, or a denial with a reason."""

    approved: bool
    reason: str
    plan: ExecutionPlan | None = None
    evidence: dict[str, Any] | None = None
