"""Protection monitor (P4 architecture_enhance).

Procedural tick loop: reads marks from ``MarketStateStore``, evaluates triggers,
and produces ``ExitIntent`` work units. It emits ``protection_register`` /
``protection_tick`` / ``protection_trigger`` facts. ``protection_tick`` is deduped
on observed price so periodic monitoring does not flood ``facts.jsonl``.

The monitor never submits/cancels and never mutates allocation. Returned work
units are run by the caller through the generic pipeline
(``process_intent_work_unit``).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.protection.config import ProtectionPolicy
from tyrex_pm.protection.lifecycle import build_exit_work_unit
from tyrex_pm.protection.registry import ProtectionEntry, ProtectionRegistry
from tyrex_pm.protection.sizing import compute_exit_sizing
from tyrex_pm.protection.trigger_eval import evaluate_trigger
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_PROTECTION_REGISTER,
    FACT_TYPE_PROTECTION_TICK,
    FACT_TYPE_PROTECTION_TRIGGER,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.intent_work import IntentWorkUnit


class ProtectionMonitor:
    def __init__(self, registry: ProtectionRegistry) -> None:
        self.registry = registry

    # --- registration ------------------------------------------------------
    def register(
        self,
        *,
        owner_id: str,
        token_id: TokenId,
        entry_price: Decimal,
        policy: ProtectionPolicy,
        parent_correlation_id: str,
        sink: JsonlSink | None = None,
        run_id: RunId | None = None,
    ) -> ProtectionEntry:
        entry = self.registry.register(
            owner_id=owner_id,
            token_id=token_id,
            entry_price=entry_price,
            policy=policy,
            parent_correlation_id=parent_correlation_id,
        )
        self._emit(
            sink,
            run_id,
            FACT_TYPE_PROTECTION_REGISTER,
            parent_correlation_id,
            {
                "owner_id": owner_id,
                "token_id": str(token_id),
                "entry_price": str(entry_price),
                "size_mode": policy.size_mode,
                **entry.threshold_evidence,
            },
        )
        return entry

    # --- tick --------------------------------------------------------------
    def tick(
        self,
        coord: RuntimeCoordinator,
        *,
        sink: JsonlSink | None = None,
        run_id: RunId | None = None,
        now: datetime | None = None,
    ) -> list[IntentWorkUnit]:
        work: list[IntentWorkUnit] = []
        market_state = coord.market_state
        for entry in self.registry.active_entries():
            observed = self._observe(market_state, entry, now=now)
            if observed is None:
                # Stale or missing book: never fabricate a trigger. Emit one
                # deduped tick noting the stale state for operator visibility.
                if entry.last_tick_price is not None:
                    self._emit(
                        sink,
                        run_id,
                        FACT_TYPE_PROTECTION_TICK,
                        entry.parent_correlation_id,
                        {
                            "owner_id": entry.owner_id,
                            "token_id": str(entry.token_id),
                            "stale_or_missing_book": True,
                        },
                    )
                    entry.last_tick_price = None
                continue

            if entry.last_tick_price is None or observed != entry.last_tick_price:
                self._emit(
                    sink,
                    run_id,
                    FACT_TYPE_PROTECTION_TICK,
                    entry.parent_correlation_id,
                    {
                        "owner_id": entry.owner_id,
                        "token_id": str(entry.token_id),
                        "observed_price": str(observed),
                        **entry.threshold_evidence,
                    },
                )
                entry.last_tick_price = observed

            trigger = evaluate_trigger(
                observed_price=observed,
                take_profit_trigger_price=entry.take_profit_trigger_price,
                stop_loss_trigger_price=entry.stop_loss_trigger_price,
            )
            if trigger is None:
                continue

            sizing = compute_exit_sizing(
                coord, owner_id=entry.owner_id, token_id=entry.token_id, policy=entry.policy
            )
            # Mark triggered BEFORE producing the exit so a duplicate tick in the
            # same loop / next loop cannot double-sell.
            entry.triggered = True
            entry.trigger_kind = trigger
            self._emit(
                sink,
                run_id,
                FACT_TYPE_PROTECTION_TRIGGER,
                entry.parent_correlation_id,
                {
                    "owner_id": entry.owner_id,
                    "token_id": str(entry.token_id),
                    "trigger": trigger,
                    "observed_price": str(observed),
                    **sizing.to_evidence(),
                    **entry.threshold_evidence,
                },
            )
            if sizing.final_size <= 0:
                # Nothing sellable (allocation/venue empty) — no exit, stay done.
                continue
            work.append(
                build_exit_work_unit(
                    entry, sizing, trigger_kind=trigger, observed_price=observed
                )
            )
        return work

    # --- helpers -----------------------------------------------------------
    def _observe(
        self, market_state, entry: ProtectionEntry, *, now: datetime | None
    ) -> Decimal | None:
        if market_state is None:
            return None
        if market_state.snapshot(entry.token_id) is None:
            return None
        if market_state.is_stale(entry.token_id, max_age_s=entry.policy.max_book_age_s, now=now):
            return None
        # SELL-side reference: the price we could exit into right now.
        return market_state.best_bid(entry.token_id)

    def _emit(
        self,
        sink: JsonlSink | None,
        run_id: RunId | None,
        fact_type: str,
        correlation_id: str,
        payload: dict,
    ) -> None:
        if sink is None or run_id is None:
            return
        sink.write(make_fact(fact_type, str(run_id), payload, correlation_id=correlation_id))
