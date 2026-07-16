"""Deterministic R4 risk policies (no side effects except via engine-owned dedup register)."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from tyrex_pm.core.intents import EnterIntent, IntentKind
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.domain.polymarket.market import MarketStatus
from tyrex_pm.market_data.executable import executable_vwap
from tyrex_pm.market_data.freshness import FreshnessReason
from tyrex_pm.risk.context import RiskContext
from tyrex_pm.risk.decision import PolicyResult
from tyrex_pm.risk.reasons import RiskReason


class RiskPolicy(Protocol):
    policy_id: str

    def evaluate(self, intent: EnterIntent, context: RiskContext) -> PolicyResult: ...


class SchemaValidityPolicy:
    policy_id = "schema_validity"

    def evaluate(self, intent: EnterIntent, context: RiskContext) -> PolicyResult:
        if intent.kind is not IntentKind.ENTER:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.UNSUPPORTED_INTENT,
            )
        if intent.target_notional <= 0:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.INVALID_NOTIONAL,
            )
        return PolicyResult(
            policy_id=self.policy_id,
            approved=True,
            reason_code=RiskReason.APPROVED,
        )


class RuntimeModePolicy:
    policy_id = "runtime_mode"

    def evaluate(self, intent: EnterIntent, context: RiskContext) -> PolicyResult:
        if context.mode is RuntimeMode.LIVE_TINY:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.LIVE_NOT_SUPPORTED,
                evidence={"mode": context.mode.value, "note": "R4 has no OMS/portfolio"},
            )
        if context.mode is RuntimeMode.OBSERVE:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=True,
                reason_code=RiskReason.APPROVED,
                evidence={"mode": context.mode.value, "note": "hypothetical dry evaluation"},
            )
        if context.mode is RuntimeMode.SHADOW:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=True,
                reason_code=RiskReason.APPROVED,
                evidence={"mode": context.mode.value, "note": "dry plan only — no OMS"},
            )
        return PolicyResult(
            policy_id=self.policy_id,
            approved=False,
            reason_code=RiskReason.MODE_NOT_EXECUTABLE,
            evidence={"mode": context.mode.value},
        )


class KillSwitchPolicy:
    policy_id = "kill_switch"

    def evaluate(self, intent: EnterIntent, context: RiskContext) -> PolicyResult:
        if context.risk_config.kill_switch_active:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.KILL_SWITCH_ACTIVE,
            )
        return PolicyResult(
            policy_id=self.policy_id,
            approved=True,
            reason_code=RiskReason.APPROVED,
        )


class DuplicateIntentPolicy:
    policy_id = "duplicate_intent"

    def evaluate(self, intent: EnterIntent, context: RiskContext) -> PolicyResult:
        key = intent.semantic_key()
        existing = context.lookup_duplicate(key)
        if existing is not None and existing.first_intent_id != intent.intent_id.value:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.DUPLICATE_INTENT,
                evidence={
                    "semantic_key": key,
                    "original_intent_id": existing.first_intent_id,
                },
            )
        return PolicyResult(
            policy_id=self.policy_id,
            approved=True,
            reason_code=RiskReason.APPROVED,
            evidence={"semantic_key": key},
        )


class InstrumentAllowlistPolicy:
    policy_id = "instrument_allowlist"

    def evaluate(self, intent: EnterIntent, context: RiskContext) -> PolicyResult:
        market = context.market
        if intent.market_id != market.market_id:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.MARKET_MISMATCH,
            )
        known = {
            market.yes.instrument_id,
            market.no.instrument_id,
        }
        if intent.instrument_id not in known:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.UNKNOWN_INSTRUMENT,
            )
        return PolicyResult(
            policy_id=self.policy_id,
            approved=True,
            reason_code=RiskReason.APPROVED,
        )


class MarketTimingPolicy:
    policy_id = "market_timing"

    def evaluate(self, intent: EnterIntent, context: RiskContext) -> PolicyResult:
        market = context.market
        if market.status is MarketStatus.CLOSED:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.MARKET_NOT_ACTIVE,
                evidence={"status": market.status.value},
            )
        if market.event_end is not None:
            boundary = market.event_end - context.risk_config.no_entry_before_close
            if context.now >= boundary:
                return PolicyResult(
                    policy_id=self.policy_id,
                    approved=False,
                    reason_code=RiskReason.ENTRY_WINDOW_CLOSED,
                    evidence={
                        "now": context.now.isoformat(),
                        "boundary": boundary.isoformat(),
                        "event_end": market.event_end.isoformat(),
                    },
                )
        return PolicyResult(
            policy_id=self.policy_id,
            approved=True,
            reason_code=RiskReason.APPROVED,
        )


class DataReadinessPolicy:
    policy_id = "data_readiness"

    def evaluate(self, intent: EnterIntent, context: RiskContext) -> PolicyResult:
        snap = context.snapshot
        for label, fresh in (
            ("yes", snap.yes_freshness),
            ("no", snap.no_freshness),
            ("reference", snap.reference_freshness),
        ):
            if fresh.reason_code is FreshnessReason.UNINITIALIZED:
                return PolicyResult(
                    policy_id=self.policy_id,
                    approved=False,
                    reason_code=RiskReason.DATA_UNINITIALIZED,
                    evidence={"feed": label},
                )
            if fresh.reason_code is FreshnessReason.STALE:
                return PolicyResult(
                    policy_id=self.policy_id,
                    approved=False,
                    reason_code=RiskReason.DATA_STALE,
                    evidence={"feed": label, "age_ms": fresh.age_ms},
                )
            if fresh.reason_code is FreshnessReason.FUTURE_TIMESTAMP:
                return PolicyResult(
                    policy_id=self.policy_id,
                    approved=False,
                    reason_code=RiskReason.FUTURE_TIMESTAMP,
                    evidence={"feed": label},
                )
            if not fresh.is_fresh:
                return PolicyResult(
                    policy_id=self.policy_id,
                    approved=False,
                    reason_code=RiskReason.DATA_STALE,
                    evidence={"feed": label, "reason": fresh.reason_code.value},
                )
        try:
            ready = context.readiness_for_instrument(intent.instrument_id.value)
        except KeyError:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.UNKNOWN_INSTRUMENT,
            )
        if ready.recovery_required or not ready.initialized:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.BOOK_RECOVERY_REQUIRED,
            )
        # Both books must be ready for binary market entries.
        if (
            context.yes_book.recovery_required
            or context.no_book.recovery_required
            or not context.yes_book.initialized
            or not context.no_book.initialized
        ):
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.BOOK_RECOVERY_REQUIRED,
            )
        return PolicyResult(
            policy_id=self.policy_id,
            approved=True,
            reason_code=RiskReason.APPROVED,
        )


class PriceSpreadLiquidityPolicy:
    policy_id = "price_spread_liquidity"

    def evaluate(self, intent: EnterIntent, context: RiskContext) -> PolicyResult:
        try:
            quote = context.quote_for_instrument(intent.instrument_id.value)
        except KeyError:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.UNKNOWN_INSTRUMENT,
            )
        if quote.best_ask is None or quote.best_bid is None or quote.spread is None:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.BOOK_ONE_SIDED_OR_EMPTY,
            )
        cfg = context.risk_config
        if quote.best_ask < cfg.min_price or quote.best_ask > cfg.max_price:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.PRICE_OUT_OF_BOUNDS,
                evidence={"ask": str(quote.best_ask)},
            )
        if intent.max_price is not None and quote.best_ask > intent.max_price:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.PRICE_OUT_OF_BOUNDS,
                evidence={"ask": str(quote.best_ask), "max_price": str(intent.max_price)},
            )
        if quote.spread > cfg.max_spread:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.SPREAD_TOO_WIDE,
                evidence={"spread": str(quote.spread)},
            )
        # Liquidity: enough ask depth for target notional at touch VWAP.
        book = (
            context.snapshot.yes_book
            if intent.instrument_id == context.market.yes.instrument_id
            else context.snapshot.no_book
        )
        if book is None or not book.asks:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.INSUFFICIENT_LIQUIDITY,
            )
        # Approximate required qty at best ask; check VWAP fill for that qty.
        if quote.best_ask <= 0:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.PRICE_OUT_OF_BOUNDS,
                evidence={"ask": str(quote.best_ask)},
            )
        qty = intent.target_notional / quote.best_ask
        vwap = executable_vwap(book.asks, qty, side="BUY")
        if not vwap.sufficient:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.INSUFFICIENT_LIQUIDITY,
                evidence={"requested_qty": str(qty), "filled_qty": str(vwap.filled_qty)},
            )
        touch_notional = quote.ask_size_at_touch * quote.best_ask
        if touch_notional < cfg.min_liquidity_notional:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.INSUFFICIENT_LIQUIDITY,
                evidence={"touch_notional": str(touch_notional)},
            )
        return PolicyResult(
            policy_id=self.policy_id,
            approved=True,
            reason_code=RiskReason.APPROVED,
            evidence={"ask": str(quote.best_ask), "spread": str(quote.spread)},
        )


class NotionalCapPolicy:
    policy_id = "notional_cap"

    def evaluate(self, intent: EnterIntent, context: RiskContext) -> PolicyResult:
        if intent.target_notional <= 0:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.INVALID_NOTIONAL,
            )
        if intent.target_notional > context.risk_config.max_notional:
            return PolicyResult(
                policy_id=self.policy_id,
                approved=False,
                reason_code=RiskReason.NOTIONAL_LIMIT_EXCEEDED,
                evidence={
                    "requested": str(intent.target_notional),
                    "max": str(context.risk_config.max_notional),
                },
            )
        return PolicyResult(
            policy_id=self.policy_id,
            approved=True,
            reason_code=RiskReason.APPROVED,
        )


DEFAULT_POLICY_ORDER: tuple[RiskPolicy, ...] = (
    SchemaValidityPolicy(),
    RuntimeModePolicy(),
    KillSwitchPolicy(),
    DuplicateIntentPolicy(),
    InstrumentAllowlistPolicy(),
    MarketTimingPolicy(),
    DataReadinessPolicy(),
    PriceSpreadLiquidityPolicy(),
    NotionalCapPolicy(),
)
