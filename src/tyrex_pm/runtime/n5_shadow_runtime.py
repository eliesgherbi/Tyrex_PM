"""N5A offline deterministic Z-Gap SHADOW composition.

This module composes N4's sealed/aligned inputs with the existing ShadowHost
OMS pipeline.  It deliberately contains no Z-Gap valuation formulas and does
not provide a live execution path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.events import BookUpdated, EventSource
from tyrex_pm.core.ids import new_event_id
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.domain.polymarket.boundary_candidates import BoundaryTickView
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.domain.polymarket.ptb_attestation import PtbAttestationPort
from tyrex_pm.execution.shadow_fill_model import (
    ECONOMICS_LABEL,
    FEES_LABEL,
    FILL_MODEL_DEPTH_WALK_V1,
)
from tyrex_pm.indicators.causal_pairing import PriceTickView
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleState
from tyrex_pm.runtime.config import ObserveConfig
from tyrex_pm.runtime.n4_observe_runtime import (
    AlignedEvalReady,
    MarketSession,
    N4ObserveRuntime,
    SessionSlot,
    config_fingerprint,
)
from tyrex_pm.runtime.shadow_host import ShadowHost
from tyrex_pm.runtime.strategy_binding import ZGapBinding, zgap_config_from_runtime
from tyrex_pm.strategies.context import DecisionContext


@dataclass(frozen=True, kw_only=True)
class ShadowRecord:
    """Replayable N5 evaluation fact, including N4 evidence and fill labels."""

    kind: str  # evaluated | skipped | promotion_blocked
    window_id: str
    market_id: str
    evaluated_at: datetime
    decision: str
    skip_reasons: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "window_id": self.window_id,
            "market_id": self.market_id,
            "evaluated_at": self.evaluated_at.isoformat(),
            "decision": self.decision,
            "skip_reasons": list(self.skip_reasons),
            "payload": dict(self.payload),
        }


@dataclass
class N5ShadowRuntime:
    """Fixture-only composition of N4's sealed path and ShadowHost's OMS path."""

    config: ObserveConfig
    _n4: N4ObserveRuntime
    _host: ShadowHost
    records: list[ShadowRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.config.shadow is None or not self.config.shadow.enable_oms:
            raise ValueError("N5 SHADOW requires shadow.enable_oms=true")
        if self.config.risk is None or self.config.risk.runtime_mode is not RuntimeMode.SHADOW:
            raise ValueError("N5 SHADOW requires risk.runtime_mode=SHADOW")
        if self.config.z_gap is None:
            raise ValueError("N5 SHADOW requires z_gap configuration")
        if self.config.shadow.fill_model_id != FILL_MODEL_DEPTH_WALK_V1:
            raise ValueError(
                f"N5A requires fills.model_id={FILL_MODEL_DEPTH_WALK_V1!r}"
            )

    @classmethod
    def create(
        cls,
        config: ObserveConfig,
        *,
        clock: FakeClock | None = None,
        attestation_port: PtbAttestationPort | None = None,
        basis_ewma_half_life_s: float | None = None,
    ) -> "N5ShadowRuntime":
        if config.z_gap is None:
            raise ValueError("N5 SHADOW requires z_gap configuration")
        n4 = N4ObserveRuntime.create(
            clock=clock,
            attestation_port=attestation_port,
            basis_ewma_half_life_s=basis_ewma_half_life_s,
            zgap_config=zgap_config_from_runtime(config.z_gap),
        )
        host = ShadowHost(config, clock=n4.clock)
        host._attach()
        return cls(config=config, _n4=n4, _host=host)

    @property
    def n4(self) -> N4ObserveRuntime:
        return self._n4

    @property
    def host(self) -> ShadowHost:
        return self._host

    @property
    def facts(self) -> list[ShadowRecord]:
        return self.records

    @property
    def orders_submitted(self) -> int:
        return len(self._host.commands)

    def open_session(self, **kwargs: Any) -> MarketSession:
        session = self._n4.open_session(**kwargs)
        if session.slot is SessionSlot.ACTIVE:
            self._activate_session(session)
        return session

    def ingest_binance(self, tick: PriceTickView):
        return self._n4.ingest_binance(tick)

    def ingest_chainlink(
        self, *, window_id: str, market_id, tick: BoundaryTickView, **kwargs: Any
    ) -> None:
        self._n4.ingest_chainlink(
            window_id=window_id, market_id=market_id, tick=tick, **kwargs
        )

    def attest_and_seal(self, **kwargs: Any):
        return self._n4.attest_and_seal(**kwargs)

    def publish_book(
        self, book: BookSnapshot, *, available_at: datetime | None = None
    ) -> None:
        """Publish a deterministic book to both host state and ShadowOMS history."""
        now = available_at or book.ts_event
        self._host.dispatcher.publish(
            BookUpdated(
                event_id=new_event_id(),
                correlation_id=self._host.correlation_id,
                ts_event=book.ts_event,
                ts_received=now,
                source=EventSource.TEST,
                book=book,
            )
        )
        active = self._n4.active
        if active is not None:
            quote = book.asks[0].price if book.asks else None
            bid = book.bids[0].price if book.bids else None
            if book.instrument_id == active.market.yes.instrument_id:
                active.up_ask, active.up_bid = quote, bid
            elif book.instrument_id == active.market.no.instrument_id:
                active.down_ask, active.down_bid = quote, bid

    def evaluate_shadow(self, *, trigger: str = "feed") -> ShadowRecord:
        """Evaluate only N4's active sealed session, then route intents to ShadowOMS."""
        yes_book, no_book = self._active_books()
        ready = self._n4.prepare_aligned_eval(yes_book=yes_book, no_book=no_book)
        if not ready.ok:
            return self._record_skip(ready)
        assert ready.session is not None and ready.snapshot is not None
        assert ready.binding is not None and ready.dyn is not None
        assert ready.sealed is not None and ready.binance_raw is not None
        assert ready.binance_source_ts is not None

        # The host's lifecycle/portfolio context is authoritative for SHADOW.
        self._host.binding = ready.binding
        self._host.strategy = ready.binding.strategy
        context = self._host._build_decision_context(ready.snapshot)
        if context is None:
            raise RuntimeError("SHADOW DecisionContext unavailable")
        result = ready.binding.evaluate(
            market_snapshot=ready.snapshot,
            causation_id=ready.snapshot.causation_id,
            correlation_id=ready.snapshot.correlation_id,
            trigger=trigger,
            decision_context=context,
            momentum_value=None,
            momentum_ready=False,
            momentum_reason="n5_unused",
            momentum_threshold=Decimal("0"),
            max_book_spread=Decimal("1"),
            settlement_ref=ready.dyn.chainlink_raw,
            settlement_ref_fresh=ready.dyn.chainlink_raw is not None,
            volatility_price=ready.binance_raw,
            volatility_ts=ready.binance_source_ts,
        )
        self._host.decisions.append(result.decision)
        for fact_type, payload in result.extra_facts:
            self._host._emit(
                fact_type,
                {
                    **payload,
                    "runtime_mode": RuntimeMode.SHADOW.value,
                    "fill_model_id": self.config.shadow.fill_model_id,
                    "economics_label": ECONOMICS_LABEL,
                    "fees_label": FEES_LABEL,
                },
                causation_id=ready.snapshot.causation_id,
                strategy_id=result.strategy_id,
            )
        self._host._dispatch_eval_result(result, ready.snapshot)
        decision = result.decision.action.value
        rec = ShadowRecord(
            kind="evaluated",
            window_id=ready.session.window_id,
            market_id=ready.session.market_id.value,
            evaluated_at=ready.snapshot.observed_at,
            decision=decision,
            skip_reasons=ready.skip_reasons,
            payload=self._evidence_payload(ready, trigger=trigger),
        )
        self.records.append(rec)
        self._host._emit("n5_shadow_evaluation", rec.to_dict())
        self._persist()
        return rec

    def promote_prepared_next(self, *, at: datetime | None = None) -> MarketSession | ShadowRecord:
        prepared = self._n4.prepared_next
        if prepared is None:
            raise ValueError("no prepared_next session")
        pending = bool(self._host.order_store.working_orders())
        requires_flat = self.config.shadow is not None and self.config.shadow.require_flat_for_promote
        if requires_flat and (
            not self._host.portfolio.is_flat()
            or self._host.lifecycle.state is not LifecycleState.FLAT
            or pending
        ):
            reasons = tuple(
                reason
                for reason, active in (
                    ("portfolio_not_flat", not self._host.portfolio.is_flat()),
                    ("lifecycle_not_flat", self._host.lifecycle.state is not LifecycleState.FLAT),
                    ("pending_orders", pending),
                )
                if active
            )
            rec = ShadowRecord(
                kind="promotion_blocked",
                window_id=prepared.window_id,
                market_id=prepared.market_id.value,
                evaluated_at=self._n4.clock.now_utc(),
                decision="blocked",
                skip_reasons=reasons,
                payload={"require_flat_for_promote": True},
            )
            self.records.append(rec)
            self._host._emit("n5_promotion_blocked", rec.to_dict())
            return rec
        promoted = self._n4.promote_prepared_next(at=at)
        self._activate_session(promoted)
        self._persist()
        return promoted

    def try_recover(self) -> bool:
        recovered = self._host.try_recover()
        if not recovered or self._host._persist is None:
            return recovered
        payload = self._host._persist.load(
            expected_market_id=self._host.registry.require_market().market_id.value,
            expected_config_fingerprint=self.config.fingerprint(),
            expected_runtime_mode=RuntimeMode.SHADOW.value,
        )
        ids = payload.get("n5_sessions") or {}
        active = self._n4.active
        if active is not None and ids.get("active_window_id") not in (None, active.window_id):
            raise ValueError("N5 recovery active window mismatch")
        return recovered

    def mark_unknown_inventory(self, active: bool = True) -> None:
        self._host.mark_unknown_inventory(active=active)

    def set_kill_switch(self, active: bool) -> None:
        self._host.set_kill_switch(active)
        self._persist()

    def persist(self) -> None:
        """Persist host state plus N5 active/prepared-window metadata."""
        self._persist()

    def _activate_session(self, session: MarketSession) -> None:
        self._host.registry.set_market(session.market)
        self._host.portfolio.set_market_id(session.market_id)
        binding = self._n4._strategy_by_window[session.window_id]
        assert isinstance(binding, ZGapBinding)
        self._host.binding = binding
        self._host.strategy = binding.strategy

    def _active_books(self) -> tuple[BookSnapshot | None, BookSnapshot | None]:
        active = self._n4.active
        if active is None:
            return None, None
        return (
            self._host.book_store.get(active.market.yes.instrument_id).book,
            self._host.book_store.get(active.market.no.instrument_id).book,
        )

    def _record_skip(self, ready: AlignedEvalReady) -> ShadowRecord:
        session = ready.session
        rec = ShadowRecord(
            kind="skipped",
            window_id="none" if session is None else session.window_id,
            market_id="none" if session is None else session.market_id.value,
            evaluated_at=self._n4.clock.now_utc(),
            decision="skipped",
            skip_reasons=ready.skip_reasons,
            payload=self._evidence_payload(ready, trigger="skip"),
        )
        self.records.append(rec)
        self._host._emit("n5_shadow_evaluation", rec.to_dict())
        return rec

    def _evidence_payload(self, ready: AlignedEvalReady, *, trigger: str) -> dict[str, Any]:
        dyn = ready.dyn
        sealed = ready.sealed
        assert self.config.shadow is not None
        oms = self._host.oms
        fill_trace = None if oms is None else oms.last_match_trace
        return {
            "trigger": trigger,
            "runtime_mode": RuntimeMode.SHADOW.value,
            "fill_model_id": self.config.shadow.fill_model_id,
            "economics_label": ECONOMICS_LABEL,
            "fees_label": FEES_LABEL,
            "pnl_label": "simulated_shadow_pnl",
            "k": None if sealed is None else str(sealed.ptb_k),
            "model_anchor": None if ready.model_anchor is None else str(ready.model_anchor),
            "model_spot": None if ready.model_spot is None else str(ready.model_spot),
            "model_spot_source": "aligned_c_hat",
            "model_anchor_source": "sealed_chainlink_ptb",
            "boundary_rule": None if sealed is None else sealed.boundary_rule_id.value,
            "attestation_result": None if sealed is None else sealed.ptb_attestation_result.value,
            "chainlink_raw": None if dyn is None or dyn.chainlink_raw is None else str(dyn.chainlink_raw),
            "chainlink_source_ts": (
                None
                if dyn is None or dyn.chainlink_source_ts is None
                else dyn.chainlink_source_ts.isoformat()
            ),
            "binance_raw": None if ready.binance_raw is None else str(ready.binance_raw),
            "binance_raw_price": None if ready.binance_raw is None else str(ready.binance_raw),
            "binance_source_ts": None if ready.binance_source_ts is None else ready.binance_source_ts.isoformat(),
            "c_hat": None if dyn is None or dyn.c_hat is None else str(dyn.c_hat),
            "aligned_model_price": None if dyn is None or dyn.c_hat is None else str(dyn.c_hat),
            "basis_estimate_used_ln": (
                None
                if dyn is None or dyn.basis_estimate_used_ln is None
                else str(dyn.basis_estimate_used_ln)
            ),
            "basis_estimate_as_of_ts": (
                None
                if dyn is None or dyn.basis_estimate_as_of_ts is None
                else dyn.basis_estimate_as_of_ts.isoformat()
            ),
            "alignment_mode": None if dyn is None else dyn.alignment_mode.value,
            "zgap_S_equals_c_hat": True,
            "sigma_source": "binance_raw_returns",
            "lifecycle": self._host.lifecycle.state.value,
            "portfolio_flat": self._host.portfolio.is_flat(),
            "orders_submitted": self.orders_submitted,
            "fill_assumptions": None if oms is None else oms.assumptions_fact(),
            "last_fill_trace": None
            if fill_trace is None
            else {
                "outcome": fill_trace.outcome,
                "filled_qty": str(fill_trace.filled_qty),
                "residual_qty": str(fill_trace.residual_qty),
                "simulated_arrival": None
                if fill_trace.simulated_arrival is None
                else fill_trace.simulated_arrival.isoformat(),
                "selected_book_available_at": None
                if fill_trace.selected_book_available_at is None
                else fill_trace.selected_book_available_at.isoformat(),
                "latency_ms": fill_trace.latency_ms,
            },
            "config_fingerprint": config_fingerprint(self._n4.zgap.config),
        }

    def _persist(self) -> None:
        self._host._maybe_persist()
        store = self._host._persist
        if store is None or not store.path.exists():
            return
        import json

        payload = json.loads(store.path.read_text(encoding="utf-8"))
        payload["n5_sessions"] = {
            "active_window_id": None if self._n4.active is None else self._n4.active.window_id,
            "prepared_window_id": None if self._n4.prepared_next is None else self._n4.prepared_next.window_id,
            "fill_model_id": self.config.shadow.fill_model_id,
        }
        store.save(payload)
