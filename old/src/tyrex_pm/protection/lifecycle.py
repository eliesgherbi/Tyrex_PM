"""Protection exit lifecycle (P4 architecture_enhance).

Builds the ``ExitIntent`` work unit when a trigger fires. The exit is marked
``urgency="urgent"`` so the ExecutionPlanner chooses a marketable FAK using fresh
book evidence. Provenance fields carry the allocation owner so the existing
allocation/exit machinery attributes the SELL correctly.
"""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.models import ExitIntent, URGENCY_URGENT
from tyrex_pm.protection.registry import ProtectionEntry
from tyrex_pm.protection.sizing import ExitSizing
from tyrex_pm.runtime.allocation_ids import PROTECTION_INTENT_SOURCE
from tyrex_pm.runtime.intent_work import IntentWorkUnit


def build_exit_work_unit(
    entry: ProtectionEntry,
    sizing: ExitSizing,
    *,
    trigger_kind: str,
    observed_price: Decimal,
) -> IntentWorkUnit:
    # The risk pre-check computes notional from the intent's limit price, so an
    # urgent exit must carry a sane reference price (the observed exitable price)
    # even though the ExecutionPlanner recomputes the marketable worst price from
    # the fresh book. An explicit policy fallback wins if configured.
    reference_price = entry.policy.exit_limit_price or observed_price
    exit_intent = ExitIntent(
        token_id=entry.token_id,
        side=Side.SELL,
        size=sizing.final_size,
        limit_price=reference_price,
        order_style=entry.policy.exit_order_style,
        urgency=URGENCY_URGENT,
    )
    ext: dict[str, object] = {
        "source": PROTECTION_INTENT_SOURCE,
        "allocation_owner_id": entry.owner_id,
        "parent_correlation_id": entry.parent_correlation_id,
        "protection_trigger": trigger_kind,
        "protection_observed_price": str(observed_price),
        "protection_sizing": sizing.to_evidence(),
        **{f"threshold_{k}": v for k, v in entry.threshold_evidence.items()},
    }
    return IntentWorkUnit(
        intent=exit_intent,
        correlation_id=entry.parent_correlation_id,
        intent_fact_extensions=ext,
    )
