"""Framework-owned conversion from strategy intents to venue order contracts."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from tyrex_pm.core.intents import EnterIntent, ExitIntent, FlattenIntent, LiquidityRole
from tyrex_pm.execution.orders import (
    LimitOrderSpec,
    MarketBuyOrderSpec,
    MarketSellOrderSpec,
    OrderSide,
    TimeInForce,
)


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
    ) -> MarketBuyOrderSpec | LimitOrderSpec:
        cap = self.policy.maximum_total_debit
        desired = min(intent.target_notional, cap)
        spend = desired / (Decimal("1") + self.policy.fee_reserve_rate)
        worst = intent.max_price
        if worst is None:
            raise ValueError("entry intent requires a strategy-owned max_price")
        metadata = {
            "intent_id": intent.intent_id.value,
            "reason_code": intent.reason_code,
            "liquidity_role": intent.liquidity_role.value,
            "candidate_at": intent.created_at.astimezone(timezone.utc).isoformat(),
            "candidate_monotonic_ns": (
                time.monotonic_ns()
                if candidate_monotonic_ns is None
                else int(candidate_monotonic_ns)
            ),
        }
        checks = (getattr(intent, "evidence", None) or {}).get("dispatch_checks")
        if checks:
            # Strategy-declared checks the shared pre-send gate repeats on the freshest book.
            metadata["dispatch_checks"] = dict(checks)
        if intent.liquidity_role is LiquidityRole.MAKER:
            shares = spend / worst
            return LimitOrderSpec(
                order_id=f"entry-{uuid4()}",
                market_id=intent.market_id.value,
                instrument_id=intent.instrument_id.value,
                token_id=token_id,
                side=OrderSide.BUY,
                shares=shares,
                limit_price=worst,
                time_in_force=TimeInForce.GTC,
                metadata={**metadata, "post_only": True, "worst_price": str(worst)},
            )
        return MarketBuyOrderSpec(
            order_id=f"entry-{uuid4()}",
            market_id=intent.market_id.value,
            instrument_id=intent.instrument_id.value,
            token_id=token_id,
            spend_amount=spend,
            maximum_total_debit=cap,
            worst_price=worst,
            estimated_shares=spend / worst,
            metadata=metadata,
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

    def resting_take_profit(
        self,
        *,
        market_id: str,
        instrument_id: str,
        token_id: str,
        shares: Decimal,
        limit_price: Decimal,
        time_in_force: TimeInForce = TimeInForce.GTC,
        expiration_epoch_s: int | None = None,
        reason_code: str = "PROTECTION_TP_RESTING",
        candidate_monotonic_ns: int | None = None,
    ) -> LimitOrderSpec:
        return LimitOrderSpec(
            order_id=f"resting-tp-{uuid4()}",
            market_id=market_id,
            instrument_id=instrument_id,
            token_id=token_id,
            side=OrderSide.SELL,
            shares=shares,
            limit_price=limit_price,
            time_in_force=time_in_force,
            expiration_epoch_s=expiration_epoch_s,
            metadata={
                "reason_code": reason_code,
                "post_only": True,
                "candidate_at": datetime.now(timezone.utc).isoformat(),
                "candidate_monotonic_ns": (
                    time.monotonic_ns()
                    if candidate_monotonic_ns is None
                    else int(candidate_monotonic_ns)
                ),
            },
        )
