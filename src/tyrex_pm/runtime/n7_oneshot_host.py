"""N7 one-shot Scope A host — wraps generic N6LiveHost.

Z-Gap still emits intents only. This host binds one market/window, enforces
authorization, caps, daily limits, and the bounded exit ladder. Mutations
default OFF; fake-transport arming requires a consumed N7 envelope.
Real venue arming is gated separately for N7B (CI-blocked).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.core.clock import Clock
from tyrex_pm.core.intents import EnterIntent, ExitIntent, FlattenIntent, new_intent_id
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.execution.polymarket.fake_transport import FakeTransport
from tyrex_pm.execution.polymarket.transport import PolymarketTransport
from tyrex_pm.runtime.live_config import LiveConfig, LiveScope
from tyrex_pm.runtime.n6_authorization import MutationAuthorization
from tyrex_pm.runtime.n6_live_host import N6LiveHost
from tyrex_pm.runtime.n7_abort import N7AbortCode
from tyrex_pm.runtime.n7_authorization import N7AuthorizationEnvelope
from tyrex_pm.runtime.n7_sealed import N7SealedConfig


@dataclass
class N7OneShotHost:
    sealed: N7SealedConfig
    clock: Clock
    transport: PolymarketTransport
    market: BinaryMarket
    envelope: N7AuthorizationEnvelope | None = None
    persistence_path: Path | None = None
    min_valid_order_notional: Decimal = Decimal("1")
    inner: N6LiveHost | None = None
    entry_lineages: int = 0
    daily_entry_notional: Decimal = Decimal("0")
    daily_realized_loss: Decimal = Decimal("0")
    exit_attempts: int = 0
    exit_ladder_started_at: datetime | None = None
    terminated: bool = False
    mutations_force_off: bool = False
    window_id: str | None = None
    facts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    last_abort: N7AbortCode | None = None

    def __post_init__(self) -> None:
        if self.sealed.live.scope is not LiveScope.A:
            raise ValueError("N7OneShotHost requires scope=A")
        ladder = self.sealed.timing.build_ladder(event_end=self.market.event_end)
        # LiveConfig for inner host: enable flags only when envelope arms fake.
        live = LiveConfig(
            enabled=False,
            mutations_enabled=False,
            scope=LiveScope.A,
            ack_timeout_ms=self.sealed.timing.ack_timeout_ms,
            max_order_notional=self.sealed.max_buy_collateral,
            order_style=self.sealed.order_style,
            hard_collateral_cap=self.sealed.max_buy_collateral,
        )
        self.inner = N6LiveHost(
            live=live,
            clock=self.clock,
            transport=self.transport,
            market=self.market,
            ladder=ladder,
            authorization=None,
            persistence_path=self.persistence_path,
            min_valid_order_notional=self.min_valid_order_notional,
        )
        self.window_id = self.market.market_id.value
        self._fact(
            "n7_host_started",
            {
                "config_fingerprint": self.sealed.fingerprint(),
                "market_id": self.market.market_id.value,
                "mutations_armed": False,
            },
        )

    def _fact(self, fact_type: str, payload: dict[str, Any]) -> None:
        self.facts.append((fact_type, payload))
        if self.inner is not None:
            self.inner._fact(fact_type, payload)

    def _now(self) -> datetime:
        return self.clock.now_utc()

    def disable_mutations(self, *, reason: str = "terminal") -> None:
        self.mutations_force_off = True
        assert self.inner is not None
        self.inner.authorization = None
        # Rebuild OMS with mutations off by toggling live flags via new auth None
        self.inner.live = LiveConfig(
            enabled=False,
            mutations_enabled=False,
            scope=LiveScope.A,
            ack_timeout_ms=self.sealed.timing.ack_timeout_ms,
            max_order_notional=self.sealed.max_buy_collateral,
            order_style=self.sealed.order_style,
            hard_collateral_cap=self.sealed.max_buy_collateral,
        )
        # LiveOMS already constructed — force mutations_enabled False
        if self.inner.oms is not None:
            self.inner.oms.mutations_enabled = False
        self._fact("n7_mutations_disabled", {"reason": reason})

    def refuse_forbidden(self, action: str) -> dict[str, Any]:
        code_map = {
            "scope_b": N7AbortCode.SCOPE_B_REFUSED,
            "hold_to_resolution": N7AbortCode.HOLD_TO_RESOLUTION_REFUSED,
            "redeem": N7AbortCode.REDEEM_REFUSED,
            "cancel_all": N7AbortCode.CANCEL_ALL_REFUSED,
            "allowance_change": N7AbortCode.ALLOWANCE_MUTATION_REFUSED,
            "transfer": N7AbortCode.TRANSFER_REFUSED,
            "historical_as_strategy": N7AbortCode.HISTORICAL_AS_STRATEGY_REFUSED,
        }
        code = code_map.get(action, N7AbortCode.AUTHORIZATION_MISMATCH)
        self.last_abort = code
        out = {"refused": True, "action": action, "abort": code.value}
        self._fact("n7_forbidden_refused", out)
        return out

    def arm_from_envelope_fake(self) -> N7AbortCode | None:
        """Consume envelope for FakeTransport mutations (N7A rehearsal)."""
        if self.mutations_force_off:
            return N7AbortCode.CRASH_MUTATIONS_DISABLED
        if self.envelope is None:
            return N7AbortCode.AUTHORIZATION_ABSENT
        if not isinstance(self.transport, FakeTransport):
            return N7AbortCode.AUTHORIZATION_MISMATCH
        err = self.envelope.consume_for_fake()
        if err is not None:
            self.last_abort = err
            return err
        assert self.inner is not None
        self.inner.live = LiveConfig(
            enabled=True,
            mutations_enabled=True,
            scope=LiveScope.A,
            ack_timeout_ms=self.sealed.timing.ack_timeout_ms,
            max_order_notional=self.sealed.max_buy_collateral,
            order_style=self.sealed.order_style,
            hard_collateral_cap=self.sealed.max_buy_collateral,
        )
        self.inner.authorization = MutationAuthorization.for_fake_transport(
            reason="n7a_fake_oneshot"
        )
        if self.inner.oms is not None:
            self.inner.oms.mutations_enabled = True
        self._fact("n7_armed_fake", {"envelope_id": self.envelope.envelope_id})
        return None

    def bind_envelope_to_market(self) -> N7AbortCode | None:
        if self.envelope is None:
            self.last_abort = N7AbortCode.AUTHORIZATION_ABSENT
            return self.last_abort
        err = self.envelope.bind_market(
            market_id=self.market.market_id.value,
            window_id=self.window_id or self.market.market_id.value,
            market_family=self.sealed.market_family,
            now=self._now(),
        )
        if err is not None:
            self.last_abort = err
            return err
        return None

    def validate_git_and_config(
        self,
        *,
        git_head: str,
        worktree_clean: bool,
        config_fingerprint: str | None = None,
    ) -> N7AbortCode | None:
        if self.envelope is None:
            return N7AbortCode.AUTHORIZATION_ABSENT
        if git_head != self.envelope.git_head:
            self.last_abort = N7AbortCode.HEAD_MISMATCH
            return self.last_abort
        if not worktree_clean:
            self.last_abort = N7AbortCode.DIRTY_WORKTREE
            return self.last_abort
        fp = config_fingerprint or self.sealed.fingerprint()
        if fp != self.envelope.config_fingerprint:
            self.last_abort = N7AbortCode.CONFIGURATION_MISMATCH
            return self.last_abort
        if self.envelope.is_expired(self._now()):
            self.last_abort = N7AbortCode.AUTHORIZATION_EXPIRED
            return self.last_abort
        return None

    def try_enter(self, intent: EnterIntent, *, book: BookSnapshot) -> dict[str, Any]:
        assert self.inner is not None
        if self.terminated:
            self.last_abort = N7AbortCode.SECOND_WINDOW_REFUSED
            return {"status": "ABORT", "abort": self.last_abort.value}
        if self.envelope is None or not self.envelope.approved:
            self.last_abort = N7AbortCode.AUTHORIZATION_ABSENT
            return {"status": "ABORT", "abort": self.last_abort.value}
        if self.envelope.bound_market_id != self.market.market_id.value:
            self.last_abort = N7AbortCode.MARKET_OUTSIDE_ENVELOPE
            return {"status": "ABORT", "abort": self.last_abort.value}
        if self.entry_lineages >= self.sealed.max_entry_lineages:
            self.last_abort = N7AbortCode.REENTRY_REFUSED
            return {"status": "ABORT", "abort": self.last_abort.value}

        # Daily notional: entry exposure only
        if self.daily_entry_notional + intent.target_notional > self.sealed.max_daily_notional:
            self.last_abort = N7AbortCode.RISK_OR_DAILY_LIMIT_BREACH
            return {"status": "ABORT", "abort": self.last_abort.value}
        if self.daily_realized_loss >= self.sealed.max_daily_loss:
            self.last_abort = N7AbortCode.RISK_OR_DAILY_LIMIT_BREACH
            return {"status": "ABORT", "abort": self.last_abort.value}

        if (
            self.sealed.skip_if_min_exceeds_cap
            and self.min_valid_order_notional > self.sealed.max_buy_collateral
        ):
            self.last_abort = N7AbortCode.VENUE_MINIMUM_ABOVE_CAP
            return {
                "status": "SKIP",
                "abort": self.last_abort.value,
                "reasons": ["min_valid_order_exceeds_hard_cap"],
            }

        if not self.inner._mutations_armed() and not self.mutations_force_off:
            # Unarmed → no submission
            self.last_abort = N7AbortCode.AUTHORIZATION_ABSENT
            return {"status": "ABORT", "abort": self.last_abort.value}

        result = self.inner.try_enter(intent, book=book)
        if result.get("status") in {
            "ACKNOWLEDGED",
            "DISPATCHED",
            "AMBIGUOUS",
            "REJECTED",
        }:
            self.entry_lineages += 1
            # Count planned notional toward daily entry (fee-inclusive cap enforced inside)
            self.daily_entry_notional += min(
                intent.target_notional, self.sealed.max_buy_collateral
            )
        if result.get("status") == "SKIP" and "late_entry_skipped" in (
            result.get("reasons") or []
        ):
            self.last_abort = N7AbortCode.LATE_ENTRY
        if result.get("status") == "SKIP" and "min_valid_order_exceeds_hard_cap" in (
            result.get("reasons") or []
        ):
            self.last_abort = N7AbortCode.VENUE_MINIMUM_ABOVE_CAP
        if result.get("status") == "SKIP" and "kill_active" in (result.get("reasons") or []):
            self.last_abort = N7AbortCode.KILL_ACTIVE
        return result

    def try_exit(
        self,
        intent: ExitIntent | FlattenIntent,
        *,
        book: BookSnapshot,
        limit_price: Decimal,
        quantity: Decimal | None = None,
    ) -> dict[str, Any]:
        assert self.inner is not None
        # Exits do not consume daily entry notional.
        return self.inner.try_exit(
            intent, book=book, limit_price=limit_price, quantity=quantity
        )

    def run_bounded_exit_ladder(
        self,
        *,
        book: BookSnapshot,
        limit_price: Decimal,
        strategy_stale: bool = False,
    ) -> dict[str, Any]:
        """Ack-aware exit retries; may continue when strategy inputs are stale."""
        assert self.inner is not None
        assert self.inner.portfolio is not None
        if self.exit_ladder_started_at is None:
            self.exit_ladder_started_at = self._now()
        attempts: list[dict[str, Any]] = []
        budget = timedelta(milliseconds=self.sealed.timing.exit_retry_time_budget_ms)
        max_attempts = self.sealed.timing.exit_retry_max_attempts

        while True:
            if self.inner.portfolio.is_flat():
                self.last_abort = N7AbortCode.TERMINAL_FLAT
                out = {
                    "status": "FLAT",
                    "attempts": attempts,
                    "strategy_stale_ok": strategy_stale,
                }
                self._fact("n7_exit_ladder_flat", out)
                return out
            if self.inner.ladder.past_hard_stop(self._now()) or (
                self._now() >= self.inner.ladder.residual_operator_deadline_at
            ):
                self.last_abort = N7AbortCode.RESIDUAL_EXPOSURE_AT_DEADLINE
                out = {
                    "status": "RESIDUAL",
                    "abort": self.last_abort.value,
                    "attempts": attempts,
                    "flat": False,
                }
                self._fact("n7_exit_ladder_residual", out)
                return out
            if self.exit_attempts >= max_attempts:
                self.last_abort = N7AbortCode.EXIT_RETRY_BUDGET_EXHAUSTED
                out = {
                    "status": "BUDGET_EXHAUSTED",
                    "abort": self.last_abort.value,
                    "attempts": attempts,
                }
                self._fact("n7_exit_ladder_budget", out)
                return out
            if self._now() - self.exit_ladder_started_at > budget:
                self.last_abort = N7AbortCode.EXIT_RETRY_BUDGET_EXHAUSTED
                out = {
                    "status": "TIME_BUDGET_EXHAUSTED",
                    "abort": self.last_abort.value,
                    "attempts": attempts,
                }
                self._fact("n7_exit_ladder_time_budget", out)
                return out

            # Reconcile before each retry
            if self.inner._has_ambiguous_lineage():
                self.inner.resolve_ambiguous_via_recon()
                if self.inner._has_ambiguous_lineage():
                    self.last_abort = N7AbortCode.AMBIGUOUS_ACKNOWLEDGMENT
                    return {
                        "status": "AMBIGUOUS",
                        "abort": self.last_abort.value,
                        "attempts": attempts,
                    }

            if self.inner.inventory_unknown:
                self.last_abort = N7AbortCode.UNKNOWN_INVENTORY
                return {
                    "status": "UNKNOWN",
                    "abort": self.last_abort.value,
                    "attempts": attempts,
                }

            from tyrex_pm.lifecycle.trade_lifecycle import LifecycleState

            if self.inner.lifecycle is not None and self.inner.lifecycle.state in {
                LifecycleState.EXIT_PENDING,
                LifecycleState.ENTRY_PENDING,
            }:
                # Do not blind-submit while an owned order is pending.
                self.exit_attempts += 1
                attempts.append({"status": "WAIT_PENDING", "lifecycle": self.inner.lifecycle.state.value})
                if self.exit_attempts >= max_attempts:
                    self.last_abort = N7AbortCode.EXIT_RETRY_BUDGET_EXHAUSTED
                    return {
                        "status": "BUDGET_EXHAUSTED",
                        "abort": self.last_abort.value,
                        "attempts": attempts,
                    }
                continue

            intent = FlattenIntent(
                intent_id=new_intent_id(),
                strategy_id=self.inner.strategy_id,
                market_id=self.market.market_id,
                instrument_id=self.market.yes.instrument_id,
                created_at=self._now(),
                correlation_id=self.inner._correlation,
                causation_id=None,
                reason_code="n7_bounded_exit_ladder",
            )
            self.exit_attempts += 1
            result = self.try_exit(intent, book=book, limit_price=limit_price)
            attempts.append(result)
            # For fake transport tests: stop after one submit so caller can fill
            if result.get("status") in {"ACKNOWLEDGED", "DISPATCHED", "AMBIGUOUS"}:
                return {
                    "status": "EXIT_SUBMITTED",
                    "result": result,
                    "attempts": attempts,
                    "strategy_stale_ok": strategy_stale,
                }
            if result.get("status") == "SKIP":
                # no inventory
                if self.inner.portfolio.is_flat():
                    self.last_abort = N7AbortCode.TERMINAL_FLAT
                    return {"status": "FLAT", "attempts": attempts}
                continue

    def refuse_second_window(self, other_market_id: str) -> dict[str, Any]:
        self.last_abort = N7AbortCode.SECOND_WINDOW_REFUSED
        out = {
            "refused": True,
            "abort": self.last_abort.value,
            "bound": self.market.market_id.value,
            "requested": other_market_id,
        }
        self._fact("n7_second_window_refused", out)
        return out

    def terminate(self, *, reason: str = "one_shot_complete") -> None:
        self.terminated = True
        self.disable_mutations(reason=reason)
        self._fact("n7_terminated", {"reason": reason})

    def handle_ctrl_c(self, *, book: BookSnapshot, limit_price: Decimal) -> dict[str, Any]:
        self.last_abort = N7AbortCode.CTRL_C_ABORT
        assert self.inner is not None
        self.inner.activate_kill()
        if self.inner.portfolio is not None and not self.inner.portfolio.is_flat():
            ladder = self.run_bounded_exit_ladder(book=book, limit_price=limit_price)
        else:
            ladder = {"status": "FLAT"}
        self.disable_mutations(reason="ctrl_c")
        return {"abort": self.last_abort.value, "ladder": ladder}

    def on_crash(self) -> dict[str, Any]:
        self.disable_mutations(reason="crash")
        self.last_abort = N7AbortCode.CRASH_MUTATIONS_DISABLED
        return {"abort": self.last_abort.value, "mutations_armed": False}

    def status(self) -> dict[str, Any]:
        assert self.inner is not None
        return {
            "n7": self.sealed.to_dict(),
            "envelope": None if self.envelope is None else self.envelope.to_dict(),
            "entry_lineages": self.entry_lineages,
            "daily_entry_notional": str(self.daily_entry_notional),
            "daily_realized_loss": str(self.daily_realized_loss),
            "terminated": self.terminated,
            "mutations_force_off": self.mutations_force_off,
            "last_abort": None if self.last_abort is None else self.last_abort.value,
            "inner": self.inner.status(),
        }
