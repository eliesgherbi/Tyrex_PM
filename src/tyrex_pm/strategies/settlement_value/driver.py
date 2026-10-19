"""settlement_value driver: the generic evaluate() adapter used offline and live.

Decision flow per evaluation:

1. Episode: the window's episode state machine (see ``state.py``). An emitted entry is
   pending at once; the host's execution outcome (``on_entry_outcome``) decides whether
   a retry may follow (``retry``), whether the window is done, or whether a position
   exists. A position follows the selected lifecycle policy (``exit.policy``):
   hold-to-resolution commits it once; managed exits may sell it. After a confirmed
   complete exit a new episode may start (``reentry``). Nothing ever tops up.
2. Readiness: entry band, K quality, reference, volatility, books, time.
3. Probability: endpoint diffusion point estimate with a conservative band.
4. Economics: per side, walk asks while the marginal conservative edge clears the
   minimum; choose the best feasible side or wait with a stable reason.

With retry disabled, ``exit.policy: hold_to_resolution`` and one trade per window the
decisions and intents are exactly those of the original single-attempt driver.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from tyrex_pm.core.ids import CorrelationId, EventId, StrategyId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.intents import (
    EnterIntent,
    ExitIntent,
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
from tyrex_pm.strategies.context import DecisionContext, StrategyContext, StrategyPositionPhase
from tyrex_pm.strategies.decisions import StrategyAction, StrategyDecision
from tyrex_pm.strategies.evaluation import StrategyEvaluation
from tyrex_pm.strategies.outcomes import (
    EntryOutcome,
    EntryOutcomeKind,
    ExitOutcome,
    ExitOutcomeKind,
    RetryClass,
    is_retryable,
)
from tyrex_pm.strategies.settlement_value import reasons as R
from tyrex_pm.strategies.settlement_value.config import SettlementValueConfig
from tyrex_pm.strategies.settlement_value.economics import (
    ProbabilityEstimate,
    SideCandidate,
    book_implied_up,
    buffered_limit,
    choose_side,
    endpoint_probability,
    exit_walk,
    size_side,
)
from tyrex_pm.strategies.settlement_value.state import EpisodePhase, EpisodeState
from tyrex_pm.strategies.settlement_value.twap_proxy import BACKEND as TWAP_BACKEND
from tyrex_pm.strategies.settlement_value.twap_proxy import twap_proxy_probability

STRATEGY_ID = StrategyId("settlement_value")
P = EpisodePhase

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


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


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

    # -- execution feedback ----------------------------------------------------------

    def on_entry_outcome(self, outcome: EntryOutcome) -> None:
        """Host-delivered result of an entry intent (replay and live)."""
        st = self.state
        if outcome.intent_id != st.entry_intent_id:
            # Late evidence for an earlier attempt. Exposure is picked up from the
            # host's position context; budgets were already counted for that attempt.
            return
        if outcome.posted and not outcome.resolved_from_uncertain:
            st.episode_posts += 1
            st.window_posts += 1
        if outcome.prepare_failed:
            st.episode_prepare_failures += 1
        service = outcome.prepare_failed or outcome.retry_class is RetryClass.RETRY_WHEN_CLEARED
        st.consecutive_service_failures = st.consecutive_service_failures + 1 if service else 0
        st.last_outcome = outcome.kind.value
        st.last_outcome_reason = outcome.reason
        if outcome.exposure:
            self._mark_filled(outcome.cost)
            st.phase = P.POSITION
            return
        if outcome.kind is EntryOutcomeKind.UNCERTAIN:
            st.phase = P.UNCERTAIN
            return
        retry = self.config.retry
        if not retry.enabled:
            self._close(R.RETRY_DISABLED)
            return
        if not is_retryable(outcome.retry_class):
            self._close(f"{R.ENTRY_NOT_RETRYABLE}:{outcome.reason or outcome.retry_class.value}")
            return
        delay_ms = retry.min_retry_interval_ms
        if service and st.consecutive_service_failures > 0:
            backoff = retry.service_backoff_ms * 2 ** (st.consecutive_service_failures - 1)
            delay_ms = max(delay_ms, min(backoff, retry.service_backoff_max_ms))
        st.next_attempt_not_before = _iso(outcome.at + timedelta(milliseconds=delay_ms))
        st.phase = P.REARMABLE

    def on_hold_committed(self) -> None:
        """The host committed the filled episode to resolution (hold profile).

        Recorded so a later empty position (the hold was detached to the settlement
        ledger) is not mistaken for a sale, and no second hold intent is emitted.
        """
        st = self.state
        st.hold_requested = True
        if st.phase in (P.POSITION, P.PENDING, P.UNCERTAIN, P.REARMABLE):
            st.phase = P.HOLD_COMMITTED

    def on_exit_outcome(self, outcome: ExitOutcome) -> None:
        st = self.state
        if outcome.intent_id != st.exit_intent_id:
            return
        if outcome.kind is ExitOutcomeKind.UNCERTAIN:
            st.phase = P.EXITING
            return
        if outcome.kind is ExitOutcomeKind.FLAT:
            # Closure is confirmed by the host's position context on the next evaluation.
            st.phase = P.EXITING
            return
        # Not sent, no fill or partial: the remaining position is managed again.
        st.phase = P.POSITION
        st.exit_intent_id = None

    def restore_window_usage(
        self,
        *,
        episodes: int,
        attempts: int,
        posts: int,
        filled_episodes: int,
        latest_attempts: int,
        latest_state: str | None,
    ) -> None:
        """Seed counters from the host's durable ledger after a restart in this window.

        The ledger stays the authority (the host re-checks every budget); this only keeps
        the strategy from proposing attempts the ledger would refuse, and from
        restarting a used window as if it were fresh.
        """
        st = self.state
        if episodes <= 0 or st.window_attempts >= attempts:
            return
        st.episode_seq = episodes
        st.attempt_seq = latest_attempts
        st.window_attempts = attempts
        st.window_posts = posts
        st.filled_episodes = filled_episodes
        if latest_state in ("NO_FILL", "NO_DISPATCH"):
            if self.config.retry.enabled:
                st.phase = P.REARMABLE
                # The pre-restart start time is not durable: the elapsed budget restarts,
                # while the durable attempt / POST ceilings still bound the episode.
                st.episode_started_at = None
            else:
                self._close(R.RETRY_DISABLED)
        elif latest_state == "SOLD":
            st.phase = P.IDLE
            st.episode_filled = True
        elif latest_state in ("RESERVED", "DISPATCH_UNKNOWN", "MANUAL_INTERVENTION"):
            st.phase = P.UNCERTAIN
        else:
            # A holding (filled / committed): the host's position context drives it.
            st.phase = P.POSITION
            st.episode_filled = True

    def _mark_filled(self, cost: Decimal) -> None:
        st = self.state
        if st.episode_filled:
            return
        st.episode_filled = True
        st.filled_episodes += 1
        st.window_debit = str(Decimal(st.window_debit) + cost)

    def _close(self, reason: str) -> None:
        self.state.phase = P.CLOSED
        self.state.closed_reason = reason

    def _start_episode(self, now: datetime, side: str) -> None:
        st = self.state
        st.episode_seq += 1
        st.attempt_seq = 0
        st.episode_posts = 0
        st.episode_prepare_failures = 0
        st.consecutive_service_failures = 0
        st.episode_started_at = _iso(now)
        st.next_attempt_not_before = None
        st.entry_side = side
        st.episode_filled = False
        st.hold_requested = False
        st.exit_intent_id = None
        st.exit_attempts = 0
        st.closed_reason = None

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
        *,
        debit_cap: Decimal | None = None,
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
        sizing = cfg.sizing
        if debit_cap is not None:
            sizing = dataclasses.replace(
                sizing,
                max_debit_usdc=min(sizing.max_debit_usdc, debit_cap),
                max_loss_usdc=min(sizing.max_loss_usdc, debit_cap),
            )
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
                sizing=sizing,
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

    def _attempt_evidence(self, *, exit_attempt: bool = False) -> dict[str, Any]:
        """Episode identity on intents; ``attempt_id`` only beyond the first attempt so
        single-attempt semantic keys stay exactly as before."""
        st = self.state
        out: dict[str, Any] = {"episode_seq": st.episode_seq, "attempt_seq": st.attempt_seq}
        if exit_attempt:
            out["attempt_id"] = f"{st.attempt_id}x{st.exit_attempts}"
        elif st.episode_seq > 1 or st.attempt_seq > 1:
            out["attempt_id"] = st.attempt_id
        return out

    def _sync_position(self, held: bool, context: DecisionContext, now: datetime) -> None:
        """Align the episode phase with the host's confirmed position."""
        st = self.state
        if held:
            if st.phase in (P.IDLE, P.PENDING, P.UNCERTAIN, P.REARMABLE, P.CLOSED):
                if st.phase in (P.IDLE, P.CLOSED) and st.episode_seq == 0:
                    st.episode_seq = 1
                self._mark_filled(context.position_cost_total)
                st.phase = P.HOLD_COMMITTED if st.hold_requested else P.POSITION
            return
        phase = None if context.lifecycle is None else context.lifecycle.phase
        busy = phase in (
            StrategyPositionPhase.ENTRY_PENDING,
            StrategyPositionPhase.EXIT_REQUESTED,
            StrategyPositionPhase.EXIT_PENDING,
        )
        if st.phase in (P.POSITION, P.EXITING) and not busy and st.episode_filled:
            # A filled episode whose inventory is gone: closed by a sale.
            st.phase = P.IDLE
            st.last_close_at = _iso(now)
            st.last_close_reason = context.last_exit_reason or st.last_exit_reason
            st.last_closed_side = st.entry_side
            st.rearm_reset_seen = False
            st.exit_intent_id = None

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
        st = self.state
        now = market_snapshot.observed_at
        market = market_snapshot.market
        st.decision_epoch += 1
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
        obs: _Observation | None = None

        def finish(
            action: StrategyAction, reason: str, *, model_ready: bool = False
        ) -> StrategyEvaluation:
            row["action"] = action.value
            row["reason"] = reason
            row["blockers"] = list(dict.fromkeys(blockers))
            # Model readiness describes the strategy's inputs, never the host's entry
            # permission: the host gates entries on it, so tying it to entry_allowed
            # would deadlock (host waits for a ready model, model waits for permission).
            model_ready = model_ready or _observed_ready(obs)
            row.update(
                episode_seq=st.episode_seq,
                attempt_seq=st.attempt_seq,
                episode_phase=st.phase.value,
                window_attempts=st.window_attempts,
                filled_episodes=st.filled_episodes,
            )
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

        held = context.position_quantity > 0
        if not context.unknown_inventory:
            self._sync_position(held, context, now)
        reentry = st.phase is P.IDLE and st.filled_episodes > 0
        remaining_window_debit = cfg.reentry.max_window_debit_usdc - Decimal(st.window_debit)
        debit_cap = remaining_window_debit if reentry else None

        # Observe first: model state is recorded on every evaluation (holding, outside
        # the band, pending) so decision records stay comparable; gating comes after.
        obs = self._observe(market_snapshot, context, features, tau_s, row, debit_cap=debit_cap)

        # 1. Episode handling.
        if context.unknown_inventory:
            blockers.append(R.UNKNOWN_INVENTORY)
            return finish(StrategyAction.BLOCKED, R.UNKNOWN_INVENTORY)
        if held:
            return self._manage_position(
                context=context,
                market=market,
                obs=obs,
                tau_s=tau_s,
                now=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
                intents=intents,
                row=row,
                finish=finish,
            )
        if st.phase in (P.PENDING, P.CLOSED, P.POSITION, P.EXITING, P.HOLD_COMMITTED):
            if st.phase is P.CLOSED and st.closed_reason not in (None, R.RETRY_DISABLED):
                return finish(StrategyAction.WAIT, st.closed_reason)
            if st.phase is P.EXITING:
                return finish(StrategyAction.WAIT, R.EXIT_PENDING)
            reason = R.PENDING_ORDER if not context.entry_allowed else R.EPISODE_CONSUMED
            return finish(StrategyAction.WAIT, reason)
        if st.phase is P.UNCERTAIN:
            blockers.append(R.OUTCOME_UNCERTAIN)
            return finish(StrategyAction.WAIT, R.OUTCOME_UNCERTAIN)
        retrying = st.phase is P.REARMABLE
        if retrying:
            exhausted = self._retry_exhausted(now)
            if exhausted is not None:
                self._close(exhausted)
                return finish(StrategyAction.WAIT, exhausted)
        if reentry:
            closed = self._reentry_closed(tau_s)
            if closed is not None:
                self._close(closed)
                return finish(StrategyAction.WAIT, closed)
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
        if reentry and not st.rearm_reset_seen and st.last_closed_side in candidates:
            if not candidates[st.last_closed_side].feasible:
                st.rearm_reset_seen = True
        if retrying and st.entry_side in candidates:
            not_before = _parse(st.next_attempt_not_before)
            if not_before is not None and now < not_before:
                return finish(StrategyAction.WAIT, R.RETRY_BACKOFF, model_ready=True)
            locked = candidates[st.entry_side]
            if not locked.feasible:
                # The episode's side is fixed; when it loses eligibility pursuit stops.
                self._close(R.RETRY_SIDE_NOT_ELIGIBLE)
                return finish(StrategyAction.WAIT, R.RETRY_SIDE_NOT_ELIGIBLE, model_ready=True)
            chosen: SideCandidate | None = locked
        else:
            chosen = choose_side(candidates["UP"], candidates["DOWN"])
        if chosen is None:
            reason = _skip_reason(candidates["UP"], candidates["DOWN"])
            return finish(StrategyAction.WAIT, reason, model_ready=True)
        if reentry:
            waiting = self._reentry_wait(chosen, now)
            if waiting is not None:
                return finish(StrategyAction.WAIT, waiting, model_ready=True)

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
        if not retrying:
            self._start_episode(now, chosen.side)
        st.attempt_seq += 1
        st.window_attempts += 1
        st.next_attempt_not_before = None
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
            decision_epoch=st.decision_epoch,
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
                **self._attempt_evidence(),
            },
        )
        intents.append(intent)
        st.phase = P.PENDING
        st.entry_intent_id = intent.intent_id.value
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

    # -- retry / re-entry budgets ------------------------------------------------------

    def _retry_exhausted(self, now: datetime) -> str | None:
        retry = self.config.retry
        st = self.state
        if st.attempt_seq >= retry.max_attempts_per_episode:
            return R.RETRY_ATTEMPTS_EXHAUSTED
        if st.window_attempts >= retry.max_attempts_per_window:
            return R.RETRY_ATTEMPTS_EXHAUSTED
        if st.episode_posts >= retry.max_posts_per_episode:
            return R.RETRY_POSTS_EXHAUSTED
        if st.window_posts >= retry.max_posts_per_window:
            return R.RETRY_POSTS_EXHAUSTED
        if st.episode_prepare_failures >= retry.max_prepare_failures_per_episode:
            return R.RETRY_PREPARE_FAILURES_EXHAUSTED
        started = _parse(st.episode_started_at)
        if started is not None and (now - started).total_seconds() > retry.max_retry_duration_s:
            return R.RETRY_DURATION_EXHAUSTED
        return None

    def _reentry_closed(self, tau_s: float | None) -> str | None:
        """Conditions that end re-entry for the rest of the window."""
        cfg = self.config
        st = self.state
        if st.filled_episodes >= cfg.reentry.max_trades_per_window:
            return R.MAX_TRADES_PER_WINDOW
        if self._locked_by_stop(st.last_close_reason):
            return R.WINDOW_LOCKED_AFTER_STOP
        if Decimal(st.window_debit) >= cfg.reentry.max_window_debit_usdc:
            return R.REENTRY_WINDOW_DEBIT_EXHAUSTED
        if cfg.retry.enabled and st.window_attempts >= cfg.retry.max_attempts_per_window:
            return R.RETRY_ATTEMPTS_EXHAUSTED
        if tau_s is None or tau_s < cfg.reentry.last_entry_tau_s:
            return R.REENTRY_CUTOFF
        return None

    def _locked_by_stop(self, reason: str | None) -> bool:
        cfg = self.config.reentry
        if not reason:
            return False
        if reason.startswith(R.THESIS_STOP):
            return cfg.lock_after_thesis_stop
        if any(reason.startswith(prefix) for prefix in R.PROTECTIVE_EXIT_PREFIXES):
            return cfg.lock_after_protective_stop
        return False

    def _reentry_wait(self, chosen: SideCandidate, now: datetime) -> str | None:
        """Re-entry conditions that may still clear later in the window."""
        cfg = self.config
        st = self.state
        closed_at = _parse(st.last_close_at)
        if closed_at is not None and (now - closed_at).total_seconds() < cfg.reentry.cooldown_s:
            return R.REENTRY_COOLDOWN
        if cfg.reentry.rearm == "signal_reset" and not st.rearm_reset_seen:
            return R.REENTRY_AWAITING_SIGNAL_RESET
        same = chosen.side == st.last_closed_side
        if (same and not cfg.reentry.same_side_allowed) or (
            not same and not cfg.reentry.opposite_side_allowed
        ):
            return R.REENTRY_SIDE_NOT_ALLOWED
        required = cfg.entry.min_edge_per_share + cfg.reentry.min_extra_edge_per_share
        if chosen.first_level_edge is None or chosen.first_level_edge < required:
            return R.REENTRY_EDGE_INSUFFICIENT
        return None

    # -- positions ---------------------------------------------------------------------

    def _hold_intent(self, *, context, market, now, correlation_id, causation_id, reason):
        st = self.state
        cfg = self.config
        instrument = (
            context.lifecycle.instrument_id
            if context.lifecycle is not None and context.lifecycle.instrument_id
            else None
        )
        if instrument is None:
            up = st.entry_side == "UP"
            instrument = market.yes.instrument_id if up else market.no.instrument_id
        evidence = {
            "confirmed_quantity": str(context.position_quantity),
            "cost_total": str(context.position_cost_total),
            "settlement_functional": cfg.settlement.functional_id,
        }
        if st.episode_seq > 1:
            evidence.update(self._attempt_evidence())
        return HoldToResolutionIntent(
            intent_id=new_intent_id(),
            strategy_id=STRATEGY_ID,
            instrument_id=instrument,
            market_id=market.market_id,
            created_at=now,
            correlation_id=correlation_id,
            causation_id=causation_id,
            reason_code=reason,
            window_id=self.window_id,
            evidence=evidence,
        )

    def _manage_position(
        self,
        *,
        context: DecisionContext,
        market,
        obs: _Observation,
        tau_s: float | None,
        now: datetime,
        correlation_id: CorrelationId,
        causation_id: EventId | None,
        intents: list,
        row: dict[str, Any],
        finish,
    ) -> StrategyEvaluation:
        cfg = self.config
        st = self.state
        if cfg.exit.policy == "hold_to_resolution":
            if not st.hold_requested:
                intents.append(
                    self._hold_intent(
                        context=context,
                        market=market,
                        now=now,
                        correlation_id=correlation_id,
                        causation_id=causation_id,
                        reason=R.HOLD_TO_SETTLEMENT,
                    )
                )
                st.hold_requested = True
                st.phase = P.HOLD_COMMITTED
                return finish(StrategyAction.HOLD, R.HOLD_TO_SETTLEMENT, model_ready=True)
            return finish(StrategyAction.HOLD, R.HOLDING, model_ready=True)

        # managed_exit
        if st.hold_requested:
            return finish(StrategyAction.HOLD, R.HOLDING, model_ready=True)
        phase = None if context.lifecycle is None else context.lifecycle.phase
        if st.phase is P.EXITING or phase in (
            StrategyPositionPhase.EXIT_REQUESTED,
            StrategyPositionPhase.EXIT_PENDING,
        ):
            return finish(StrategyAction.HOLD, R.EXIT_PENDING, model_ready=True)
        if tau_s is None or tau_s <= cfg.exit.exit_cutoff_tau_s:
            # Declared residual policy: whatever is still held is kept to resolution.
            intents.append(
                self._hold_intent(
                    context=context,
                    market=market,
                    now=now,
                    correlation_id=correlation_id,
                    causation_id=causation_id,
                    reason=R.EXIT_CUTOFF_RESIDUAL_HOLD,
                )
            )
            st.hold_requested = True
            st.phase = P.HOLD_COMMITTED
            return finish(StrategyAction.HOLD, R.EXIT_CUTOFF_RESIDUAL_HOLD, model_ready=True)
        if st.exit_attempts >= cfg.exit.max_exit_attempts_per_episode:
            return finish(StrategyAction.HOLD, R.EXIT_ATTEMPTS_EXHAUSTED, model_ready=True)
        prob = obs.prob
        side = st.entry_side
        if side is None and context.lifecycle is not None and context.lifecycle.instrument_id:
            side = "UP" if context.lifecycle.instrument_id == market.yes.instrument_id else "DOWN"
        if prob is None or not prob.ready or side is None:
            return finish(StrategyAction.HOLD, R.MANAGING_POSITION)
        up = side == "UP"
        point = prob.point(up=up)
        row["held_side"] = side
        row["held_p_point"] = point
        exit_reason: str | None = None
        min_price = cfg.exit.min_exit_price
        if cfg.exit.thesis_stop_enabled and point is not None:
            if point < cfg.exit.thesis_stop_probability:
                exit_reason = R.THESIS_STOP
        if exit_reason is None and cfg.exit.value_exit_enabled:
            book = obs.legs.up_book if up else obs.legs.down_book
            ready = obs.legs.up_ready if up else obs.legs.down_ready
            if cfg.exit.value_exit_basis == "upper":
                hold_value = prob.p_up_high if up else (
                    None if prob.p_up_low is None else 1.0 - prob.p_up_low
                )
            else:
                hold_value = point
            minimum = (obs.legs.up_min_order if up else obs.legs.down_min_order)
            walk = exit_walk(
                () if book is None or not ready else book.bids,
                held_shares=context.position_quantity,
                hold_value=hold_value,
                margin=cfg.exit.value_exit_margin_per_share,
                fee_curve=self.fee_curve,
                min_order_shares=(
                    cfg.sizing.default_min_order_shares if minimum is None else minimum
                ),
                min_price=cfg.exit.min_exit_price,
                share_increment=cfg.sizing.share_increment,
                max_levels=cfg.sizing.max_levels,
            )
            row.update(
                exit_hold_value=None
                if walk.hold_value_per_share is None
                else str(walk.hold_value_per_share),
                exit_first_level_gain=None
                if walk.first_level_gain is None
                else str(walk.first_level_gain),
                exit_feasible=walk.feasible,
                exit_reason=walk.reason,
                exit_sell_shares=str(walk.sell_shares),
            )
            if walk.feasible and walk.worst_price is not None:
                exit_reason = R.VALUE_EXIT
                min_price = max(min_price, walk.worst_price)
        if exit_reason is None:
            return finish(StrategyAction.HOLD, R.MANAGING_POSITION, model_ready=True)
        st.exit_attempts += 1
        instrument = market.yes.instrument_id if up else market.no.instrument_id
        intent = ExitIntent(
            intent_id=new_intent_id(),
            strategy_id=STRATEGY_ID,
            instrument_id=instrument,
            market_id=market.market_id,
            created_at=now,
            correlation_id=correlation_id,
            causation_id=causation_id,
            reason_code=exit_reason,
            min_price=min_price,
            evidence={
                "side": side,
                "held_shares": str(context.position_quantity),
                "p_point": point,
                **self._attempt_evidence(exit_attempt=True),
            },
        )
        intents.append(intent)
        st.phase = P.EXITING
        st.exit_intent_id = intent.intent_id.value
        st.last_exit_reason = exit_reason
        row.update(exit_intent_id=intent.intent_id.value, exit_min_price=str(min_price))
        return finish(StrategyAction.EXIT, exit_reason, model_ready=True)


def _observed_ready(obs: _Observation | None) -> bool:
    """Probability and both executable books are available for this evaluation."""
    return bool(
        obs is not None and obs.prob is not None and obs.prob.ready and obs.candidates is not None
    )


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
