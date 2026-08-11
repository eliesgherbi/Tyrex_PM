"""Framework-owned conversion from strategy intents to venue order contracts."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from tyrex_pm.core.intents import EnterIntent, ExitIntent, FlattenIntent
from tyrex_pm.execution.orders import MarketBuyOrderSpec, MarketSellOrderSpec


@dataclass(frozen=True)
class ExecutionRiskPolicy:
    maximum_total_debit: Decimal
    fee_reserve_rate: Decimal = Decimal("0.02")
    minimum_exit_price: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if self.maximum_total_debit <= 0:
            raise ValueError("maximum_total_debit must be positive")
        if self.fee_reserve_rate < 0:
            raise ValueError("fee_reserve_rate cannot be negative")


class IntentOrderPlanner:
    """Size orders without importing a strategy or a venue SDK."""

    def __init__(self, policy: ExecutionRiskPolicy) -> None:
        self.policy = policy

    def entry(
        self,
        intent: EnterIntent,
        *,
        token_id: str,
        candidate_monotonic_ns: int | None = None,
    ) -> MarketBuyOrderSpec:
        cap = self.policy.maximum_total_debit
        desired = min(intent.target_notional, cap)
        spend = desired / (Decimal("1") + self.policy.fee_reserve_rate)
        worst = intent.max_price
        if worst is None:
            raise ValueError("entry intent requires a strategy-owned max_price")
        return MarketBuyOrderSpec(
            order_id=f"entry-{uuid4()}",
            market_id=intent.market_id.value,
            instrument_id=intent.instrument_id.value,
            token_id=token_id,
            spend_amount=spend,
            maximum_total_debit=cap,
            worst_price=worst,
            estimated_shares=spend / worst,
            metadata={
                "intent_id": intent.intent_id.value,
                "reason_code": intent.reason_code,
                "candidate_at": intent.created_at.astimezone(timezone.utc).isoformat(),
                "candidate_monotonic_ns": (
                    time.monotonic_ns()
                    if candidate_monotonic_ns is None
                    else int(candidate_monotonic_ns)
                ),
            },
        )

    def exit(
        self,
        intent: ExitIntent | FlattenIntent,
        *,
        token_id: str,
        shares: Decimal,
        candidate_monotonic_ns: int | None = None,
    ) -> MarketSellOrderSpec:
        requested_min = getattr(intent, "min_price", None)
        minimum = self.policy.minimum_exit_price if requested_min is None else requested_min
        return MarketSellOrderSpec(
            order_id=f"exit-{uuid4()}",
            market_id=intent.market_id.value,
            instrument_id=intent.instrument_id.value,
            token_id=token_id,
            shares=shares,
            minimum_price=minimum,
            metadata={
                "intent_id": intent.intent_id.value,
                "reason_code": intent.reason_code,
                "candidate_at": datetime.now(timezone.utc).isoformat(),
                "candidate_monotonic_ns": (
                    time.monotonic_ns()
                    if candidate_monotonic_ns is None
                    else int(candidate_monotonic_ns)
                ),
            },
        )
