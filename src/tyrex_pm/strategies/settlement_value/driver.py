"""settlement_value driver: the generic evaluate() adapter used offline and live.

Decision flow per evaluation:

1. Episode: once an entry intent is emitted the window's single attempt is spent.
   A confirmed position then requests hold-to-resolution once; nothing tops up.
2. Readiness: entry band, K quality, reference, volatility, books, time.
3. Probability: endpoint diffusion point estimate with a conservative band.
4. Economics: per side, walk asks while the marginal conservative edge clears the
   minimum; choose the best feasible side or wait with a stable reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from tyrex_pm.core.ids import CorrelationId, EventId, StrategyId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.intents import (
    EnterIntent,
    HoldToResolutionIntent,
    LiquidityRole,
    new_intent_id,
)
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.core.time_authority import ClockTimeAuthority, FakeTimeAuthority, TimeAuthority
from tyrex_pm.domain.polymarket.fees import FeeCurveParams
from tyrex_pm.domain.polymarket.ptb import PtbSnapshot
from tyrex_pm.facts.contract import EligibilityFacts
from tyrex_pm.indicators.ewma_volatility import VolatilitySnapshot
from tyrex_pm.indicators.graph import FeatureBundle
from tyrex_pm.indicators.spot_path import SpotPathSnapshot
from tyrex_pm.market_data.book_health import SyncHealth
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.strategies.context import DecisionContext, StrategyContext
from tyrex_pm.strategies.decisions import StrategyAction, StrategyDecision
from tyrex_pm.strategies.evaluation import StrategyEvaluation
from tyrex_pm.strategies.settlement_value import reasons as R
from tyrex_pm.strategies.settlement_value.config import SettlementValueConfig
from tyrex_pm.strategies.settlement_value.economics import (
    ProbabilityEstimate,
    SideCandidate,
    book_implied_up,
    buffered_limit,
    choose_side,
    endpoint_probability,
    size_side,
)
from tyrex_pm.strategies.settlement_value.state import EpisodeState
from tyrex_pm.strategies.settlement_value.twap_proxy import BACKEND as TWAP_BACKEND
from tyrex_pm.strategies.settlement_value.twap_proxy import twap_proxy_probability

STRATEGY_ID = StrategyId("settlement_value")

__all__ = ["STRATEGY_ID", "SettlementValueDriver", "create_settlement_value_driver"]


def fee_curve_from_config(cfg: SettlementValueConfig) -> FeeCurveParams:
    return FeeCurveParams(fee_rate=cfg.fees.rate, exponent=cfg.fees.exponent)


@dataclass
class _Legs:
    up_book: BookSnapshot | None
    down_book: BookSnapshot | None
    up_ready: bool
    down_ready: bool
    up_min_order: Decimal | None
    down_min_order: Decimal | None
    reasons: tuple[str, ...]
    up_tick: Decimal | None = None
    down_tick: Decimal | None = None


def _legs(snapshot: DecisionSnapshot) -> _Legs:
    view = snapshot.book_view
    if view is None:
        return _Legs(
            up_book=snapshot.yes_book,
            down_book=snapshot.no_book,
            up_ready=snapshot.yes_book is not None and snapshot.yes_freshness.is_fresh,
            down_ready=snapshot.no_book is not None and snapshot.no_freshness.is_fresh,
            up_min_order=None,
            down_min_order=None,
            reasons=(),
        )
    reasons = []
    for name, leg in (("up", view.up), ("down", view.down)):
        if leg.sync_health is not SyncHealth.READY:
            reasons.append(f"{name}_book_{leg.sync_health.value.lower()}")
    return _Legs(
        up_book=view.up.book,
        down_book=view.down.book,
        up_ready=view.up.sync_health is SyncHealth.READY and view.up.book is not None,
        down_ready=view.down.sync_health is SyncHealth.READY and view.down.book is not None,
        up_min_order=view.up.min_order_size,
        down_min_order=view.down.min_order_size,
        reasons=tuple(reasons),
        up_tick=view.up.tick_size,
        down_tick=view.down.tick_size,
    )


@dataclass
class _Observation:
    """Model inputs and outputs for one evaluation; recorded whether or not it trades."""

    legs: _Legs
    k: Decimal | None = None
    k_quality: str | None = None
    spot: Decimal | None = None
    spot_problem: str | None = None
    vol_problem: str | None = None
    prob: ProbabilityEstimate | None = None
    candidates: dict[str, SideCandidate] | None = None


@dataclass
class SettlementValueDriver:
    config: SettlementValueConfig
    time_authority: TimeAuthority
    fee_curve: FeeCurveParams
    target_notional: Decimal
    window_id: str
    state: EpisodeState = field(init=False)
    ptb: PtbSnapshot | None = None
    market_context: StrategyContext | None = None

    def __post_init__(self) -> None:
        self.state = EpisodeState(window_id=self.window_id)

    @property
    def strategy_id(self) -> StrategyId:
        return STRATEGY_ID

    def on_start(self, context: StrategyContext) -> None:
        self.market_context = context

    def on_stop(self, reason: str) -> None:
        del reason

    def configure_ptb(self, ptb: PtbSnapshot) -> None:
        self.ptb = ptb

    def persistence_slice(self) -> dict[str, Any]:
        return self.state.as_slice()

    def restore_persistence_slice(self, data: dict[str, Any]) -> None:
        self.state.restore(data)

    # -- evaluation -------------------------------------------------------------------

    def _spot_path(self, features: FeatureBundle) -> SpotPathSnapshot | None:
        for key, value in features.values.items():
            if key.startswith("spot_path") and isinstance(value, SpotPathSnapshot):
                return value
        return None

    def _volatility(self, features: FeatureBundle) -> VolatilitySnapshot | None:
        for key, value in features.values.items():
            if key.startswith("ewma_vol") and isinstance(value, VolatilitySnapshot):
                return value
        return None

    def _observe(
        self,
        snapshot: DecisionSnapshot,
        context: DecisionContext,
        features: FeatureBundle | None,
        tau_s: float | None,
        row: dict[str, Any],
    ) -> _Observation:
        cfg = self.config
        legs = _legs(snapshot)
        obs = _Observation(legs=legs)
        up_bid, up_ask = _top(legs.up_book)
        down_bid, down_ask = _top(legs.down_book)
        row.update(
            up_bid=None if up_bid is None else str(up_bid),
            up_ask=None if up_ask is None else str(up_ask),
            down_bid=None if down_bid is None else str(down_bid),
            down_ask=None if down_ask is None else str(down_ask),
            book_implied_up=book_implied_up(up_bid, up_ask),
        )
        ptb = self.ptb
        obs.k = None if ptb is None else ptb.k
        obs.k_quality = None if ptb is None else ptb.quality.value
        row["k"] = None if obs.k is None else str(obs.k)
        row["k_quality"] = obs.k_quality
        reference = snapshot.reference
        raw = context.references.trading_reference_raw
        row["aligned_reference"] = None if reference is None else str(reference.price)
        row["trading_reference_raw"] = None if raw is None else str(raw)
        row["spot_input"] = cfg.model.spot_input
        if reference is None or not snapshot.reference_freshness.is_fresh:
            obs.spot_problem = R.REFERENCE_MISSING
        elif cfg.model.spot_input == "binance_spot":
            obs.spot = raw
            obs.spot_problem = None if raw is not None else "trading_reference_raw_missing"
        else:
            obs.spot = reference.price
        row["reference_price"] = None if obs.spot is None else str(obs.spot)
        vol = self._volatility(features or FeatureBundle())
        row["sigma_raw"] = None if vol is None else vol.sigma
        if vol is None or not vol.ready or vol.sigma is None:
            reject = "missing" if vol is None else (vol.reject_reason or "not_ready")
            obs.vol_problem = f"vol:{reject}"
        if obs.spot is None or obs.k is None or obs.vol_problem is not None or tau_s is None:
            return obs
        assert vol is not None and vol.sigma is not None
        sigma = vol.sigma * cfg.volatility.sigma_scale
        endpoint = endpoint_probability(
            S=obs.spot, K=obs.k, sigma=sigma, tau_s=tau_s, model=cfg.model
        )
        twap, twap_diag = twap_proxy_probability(
            spot=context.references.trading_reference_raw,
            k=obs.k,
            sigma=sigma,
            now=snapshot.observed_at,
            window_end=snapshot.market.event_end or snapshot.observed_at,
            path=self._spot_path(features or FeatureBundle()),
            twap_obs=context.references.chainlink_raw,
            twap_ts=context.references.chainlink_source_ts,
            model=cfg.model,
        )
        row.update(
            endpoint_p_up=endpoint.p_up_point,
            endpoint_p_up_low=endpoint.p_up_low,
            endpoint_p_up_high=endpoint.p_up_high,
            twap_p_up=twap.p_up_point,
            twap_p_up_low=twap.p_up_low,
            twap_p_up_high=twap.p_up_high,
            twap_reason=twap.reason,
            twap_basis_usd=twap_diag.get("basis_usd"),
            twap_mean=twap_diag.get("twap_mean"),
            twap_sd=twap_diag.get("twap_sd"),
        )
        prob = twap if cfg.model.backend == TWAP_BACKEND else endpoint
        obs.prob = prob
        row.update(
            sigma=sigma,
            z=prob.z,
            p_up_point=prob.p_up_point,
            p_up_low=prob.p_up_low,
            p_up_high=prob.p_up_high,
            interval_method=prob.interval_method,
        )
        if not prob.ready or not (legs.up_ready and legs.down_ready):
            return obs
        default_min = cfg.sizing.default_min_order_shares
        candidates: dict[str, SideCandidate] = {}
        for up, book, minimum in (
            (True, legs.up_book, legs.up_min_order),
            (False, legs.down_book, legs.down_min_order),
        ):
            candidates["UP" if up else "DOWN"] = size_side(
                () if book is None else book.asks,
                up=up,
                p_conservative=prob.conservative(up=up),
                p_point=prob.point(up=up),
                entry=cfg.entry,
                sizing=cfg.sizing,
                fee_curve=self.fee_curve,
                min_order_shares=default_min if minimum is None else minimum,
            )
        for name, cand in candidates.items():
            prefix = name.lower()
            row.update(
                {
                    f"{prefix}_p_conservative": cand.p_conservative,
                    f"{prefix}_first_level_edge": (
                        None if cand.first_level_edge is None else str(cand.first_level_edge)
                    ),
                    f"{prefix}_feasible": cand.feasible,
                    f"{prefix}_reason": cand.reason,
                    f"{prefix}_binding": cand.binding_constraint,
                    f"{prefix}_gross_shares": str(cand.gross_shares),
                    f"{prefix}_debit": str(cand.debit),
                    f"{prefix}_ev_conservative": str(cand.ev_conservative),
                    f"{prefix}_ev_point": str(cand.ev_point),
                }
            )
        row["both_sides_feasible"] = all(c.feasible for c in candidates.values())
        obs.candidates = candidates
        return obs

    def _decision(
        self,
        action: StrategyAction,
        reason: str,
        *,
        at: datetime,
        correlation_id: CorrelationId,
        causation_id: EventId | None,
        evidence: dict[str, Any],
    ) -> StrategyDecision:
        return StrategyDecision(
            action=action,
            reason_code=reason,
            decided_at=at,
            correlation_id=correlation_id,
            causation_id=causation_id,
            evidence=evidence,
            strategy_id=STRATEGY_ID,
        )

    def evaluate(
        self,
        *,
        market_snapshot: DecisionSnapshot,
        causation_id: EventId | None,
        correlation_id: CorrelationId,
        context: DecisionContext,
        trigger: str,
        features: FeatureBundle | None = None,
    ) -> StrategyEvaluation:
        cfg = self.config
        now = market_snapshot.observed_at
        market = market_snapshot.market
        self.state.decision_epoch += 1
        tau_s = None if market.event_end is None else (market.event_end - now).total_seconds()
        row: dict[str, Any] = {
            "observed_at": now.isoformat(),
            "trigger": trigger,
            "window_id": self.window_id,
            "tau_s": tau_s,
            "backend": cfg.model.backend,
            "settlement_functional": cfg.settlement.functional_id,
            "settlement_provenance": cfg.settlement.provenance_status,
            "reference_series": cfg.settlement.reference_series,
            "fee_provenance": cfg.fees.provenance,
            "target_mode": cfg.entry.target_mode,
            "position_quantity": str(context.position_quantity),
        }
        blockers: list[str] = []
        intents: list[Any] = []

        def finish(
            action: StrategyAction, reason: str, *, model_ready: bool = False
        ) -> StrategyEvaluation:
            row["action"] = action.value
            row["reason"] = reason
            row["blockers"] = list(dict.fromkeys(blockers))
            decision = self._decision(
                action,
                reason,
                at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
                evidence={"blockers": row["blockers"], "decision_row": dict(row)},
            )
            return StrategyEvaluation(
                decision=decision,
                intents=tuple(intents),
                strategy_id=STRATEGY_ID,
                eligibility=EligibilityFacts(
                    strategy_inputs_eligible=model_ready and not blockers,
                    model_ready=model_ready,
                    blockers=tuple(row["blockers"]),
                ),
                reporting_context={"decision_row": dict(row)},
            )

        # Observe first: model state is recorded on every evaluation (holding, outside
        # the band, pending) so decision records stay comparable; gating comes after.
        obs = self._observe(market_snapshot, context, features, tau_s, row)

        # 1. Episode handling: the single attempt is spent once an intent is emitted.
        if context.unknown_inventory:
            blockers.append(R.UNKNOWN_INVENTORY)
            return finish(StrategyAction.BLOCKED, R.UNKNOWN_INVENTORY)
        held = context.position_quantity > 0
        if self.state.entry_emitted or held:
            if held and not self.state.hold_requested:
                instrument = (
                    context.lifecycle.instrument_id
                    if context.lifecycle is not None and context.lifecycle.instrument_id
                    else None
                )
                if instrument is None:
                    instrument = (
                        market.yes.instrument_id
                        if self.state.entry_side == "UP"
                        else market.no.instrument_id
                    )
                intents.append(
                    HoldToResolutionIntent(
                        intent_id=new_intent_id(),
                        strategy_id=STRATEGY_ID,
                        instrument_id=instrument,
                        market_id=market.market_id,
                        created_at=now,
                        correlation_id=correlation_id,
                        causation_id=causation_id,
                        reason_code=R.HOLD_TO_SETTLEMENT,
                        window_id=self.window_id,
                        evidence={
                            "confirmed_quantity": str(context.position_quantity),
                            "cost_total": str(context.position_cost_total),
                            "settlement_functional": cfg.settlement.functional_id,
                        },
                    )
                )
                self.state.hold_requested = True
                return finish(StrategyAction.HOLD, R.HOLD_TO_SETTLEMENT, model_ready=True)
            if held:
                return finish(StrategyAction.HOLD, R.HOLDING, model_ready=True)
            reason = R.PENDING_ORDER if not context.entry_allowed else R.EPISODE_CONSUMED
            return finish(StrategyAction.WAIT, reason)
        if not context.entry_allowed:
            blockers.append(context.entry_block_reason or R.ENTRY_NOT_ALLOWED)
            return finish(StrategyAction.BLOCKED, R.ENTRY_NOT_ALLOWED)

        # 2. Readiness.
        if tau_s is None:
            blockers.append(R.TIME_NOT_READY)
            return finish(StrategyAction.BLOCKED, R.TIME_NOT_READY)
        if tau_s < cfg.entry.tau_min_s:
            return finish(StrategyAction.SKIP, R.ENTRY_WINDOW_CLOSED)
        if tau_s > cfg.entry.tau_max_s:
            return finish(StrategyAction.WAIT, R.OUTSIDE_ENTRY_BAND)
        time_view = self.time_authority.view()
        if not time_view.ready or time_view.uncertainty_ms > cfg.quality.max_clock_uncertainty_ms:
            blockers.append(time_view.reason_code or R.TIME_NOT_READY)
            return finish(StrategyAction.BLOCKED, R.TIME_NOT_READY)
        if (
            cfg.quality.require_verified_settlement_spec
            and cfg.settlement.provenance_status != "VERIFIED"
        ):
            blockers.append(R.REFERENCE_UNVERIFIED)
            return finish(StrategyAction.BLOCKED, R.REFERENCE_UNVERIFIED)
        if obs.k is None:
            blockers.append(R.K_MISSING)
            return finish(StrategyAction.WAIT, R.K_MISSING)
        if obs.k_quality is not None and obs.k_quality not in cfg.quality.accepted_k_qualities:
            blockers.append(f"k_quality:{obs.k_quality}")
            return finish(StrategyAction.BLOCKED, R.K_NOT_ACCEPTED)
        if obs.spot is None:
            blockers.append(obs.spot_problem or R.REFERENCE_MISSING)
            return finish(StrategyAction.WAIT, R.REFERENCE_MISSING)
        if obs.vol_problem is not None:
            blockers.append(obs.vol_problem)
            return finish(StrategyAction.WAIT, R.MODEL_NOT_READY)
        prob = obs.prob
        if prob is None or not prob.ready:
            blockers.append("model_not_ready" if prob is None else (prob.reason or "model"))
            return finish(StrategyAction.WAIT, R.MODEL_NOT_READY)
        if obs.candidates is None:
            blockers.extend(obs.legs.reasons or ("book_not_ready",))
            return finish(StrategyAction.WAIT, R.BOOK_NOT_READY, model_ready=True)

        # 3. Economics: best feasible side, or wait with the binding reason.
        candidates = obs.candidates
        chosen = choose_side(candidates["UP"], candidates["DOWN"])
        if chosen is None:
            reason = _skip_reason(candidates["UP"], candidates["DOWN"])
            return finish(StrategyAction.WAIT, reason, model_ready=True)

        instrument = market.yes if chosen.up else market.no
        assert chosen.worst_price is not None and chosen.p_conservative is not None
        limit, limit_bound = buffered_limit(
            chosen.worst_price,
            p_conservative=chosen.p_conservative,
            buffer_ticks=cfg.entry.limit_buffer_ticks,
            tick=obs.legs.up_tick if chosen.up else obs.legs.down_tick,
            max_entry_price=cfg.entry.max_entry_price,
            fee_curve=self.fee_curve,
        )
        intent = EnterIntent(
            intent_id=new_intent_id(),
            strategy_id=STRATEGY_ID,
            instrument_id=instrument.instrument_id,
            market_id=market.market_id,
            created_at=now,
            correlation_id=correlation_id,
            causation_id=causation_id,
            reason_code=R.ENTER,
            target_notional=chosen.debit,
            outcome=OutcomeSide.YES if chosen.up else OutcomeSide.NO,
            decision_epoch=self.state.decision_epoch,
            max_price=limit,
            liquidity_role=LiquidityRole.TAKER,
            evidence={
                "side": chosen.side,
                "intended_gross_shares": str(chosen.gross_shares),
                "intended_net_shares": str(chosen.net_shares),
                "intended_debit": str(chosen.debit),
                "vwap": None if chosen.vwap is None else str(chosen.vwap),
                "ev_conservative": str(chosen.ev_conservative),
                "ev_point": str(chosen.ev_point),
                "p_conservative": chosen.p_conservative,
                "p_point": chosen.p_point,
                "target_mode": cfg.entry.target_mode,
                "target_feasible": chosen.target_feasible,
                "binding_constraint": chosen.binding_constraint,
                "worst_walked_price": str(chosen.worst_price),
                "limit_bound": limit_bound,
                "settlement_functional": cfg.settlement.functional_id,
                "settlement_provenance": cfg.settlement.provenance_status,
                "k_quality": row.get("k_quality"),
            },
        )
        intents.append(intent)
        self.state.entry_emitted = True
        self.state.entry_side = chosen.side
        self.state.entry_intent_id = intent.intent_id.value
        row.update(
            chosen_side=chosen.side,
            intended_gross_shares=str(chosen.gross_shares),
            intended_debit=str(chosen.debit),
            max_price=str(limit),
            worst_walked_price=str(chosen.worst_price),
            limit_bound=limit_bound,
            ev_conservative=str(chosen.ev_conservative),
            ev_point=str(chosen.ev_point),
            target_feasible=chosen.target_feasible,
            intent_id=intent.intent_id.value,
        )
        return finish(StrategyAction.ENTER, R.ENTER, model_ready=True)


def _top(book: BookSnapshot | None) -> tuple[Decimal | None, Decimal | None]:
    if book is None:
        return None, None
    bid = None if book.best_bid is None else book.best_bid.price
    ask = None if book.best_ask is None else book.best_ask.price
    return bid, ask


def _skip_reason(up: SideCandidate, down: SideCandidate) -> str:
    order = (
        R.TARGET_INFEASIBLE,
        R.BELOW_MINIMUM_SIZE,
        R.DEPTH_INSUFFICIENT,
        R.EDGE_BELOW_COST,
    )
    present = {up.reason, down.reason}
    for reason in order:
        if reason in present:
            return reason
    return R.EDGE_BELOW_COST


def create_settlement_value_driver(
    *,
    config: SettlementValueConfig,
    time_authority: TimeAuthority | None = None,
    clock=None,
    target_notional: Decimal = Decimal("5"),
    window_id: str = "unbound",
    fee_curve: FeeCurveParams | None = None,
) -> SettlementValueDriver:
    if time_authority is None:
        if clock is None:
            raise ValueError("settlement_value driver requires a time authority or clock")
        from tyrex_pm.core.clock import FakeClock

        time_authority = (
            FakeTimeAuthority(clock=clock)
            if isinstance(clock, FakeClock)
            else ClockTimeAuthority(clock=clock)
        )
    return SettlementValueDriver(
        config=config,
        time_authority=time_authority,
        fee_curve=fee_curve or fee_curve_from_config(config),
        target_notional=target_notional,
        window_id=window_id,
    )
