"""R7B two-level authorization: session envelope → exact execution artifact.

R7A.2 implements and dry-tests this model. It does not arm mutations or submit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


class SessionError(RuntimeError):
    pass


class SessionPhase(str, Enum):
    CREATED = "CREATED"
    OBSERVING = "OBSERVING"
    MARKET_BOUND = "MARKET_BOUND"
    ARTIFACT_READY = "ARTIFACT_READY"
    ENTRY_SUBMITTING = "ENTRY_SUBMITTING"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    COMPLETED_FLAT = "COMPLETED_FLAT"
    BLOCKED = "BLOCKED"


DEFAULT_SESSION_LIMITS = {
    "session_lifetime_s": 1800,  # 30 minutes
    "max_windows_observed": 3,
    "markets_bound": 1,
    "max_entry_economic_actions": 1,
    "max_buy_collateral_including_fee": "5.00",
    "max_working_entry_orders": 1,
    "max_positions_created": 1,
    "pyramiding": False,
    "automatic_next_market_after_binding": False,
    "entry_order_type": "FAK",
    "exit_order_type": "FAK",
    "max_entry_attempts": 2,  # 2 only if first definitively rejected/unfilled
    "prefer_entry_attempts": 1,
    "max_hold_s": 180.0,
    "flatten_before_close_s": 30.0,
    "normal_exit_attempts": 3,
    "exit_retry_interval_s": 2.0,
    "heartbeat": False,
    "post_lifecycle_continuation": False,
    "resting_gtc_entry": False,
    "cancel_all": False,
    "redemption": False,
    "on_chain": False,
    "allowance_approval": False,
    "credential_create_rotate": False,
}


@dataclass(frozen=True)
class R7BSessionAuthorization:
    """User-approved session envelope — no exact token before runtime binding."""

    schema_version: str
    session_id: str
    nonce: str
    created_at: str
    expires_at: str
    branch: str
    commit_identity: str
    strategy_id: str
    market_family: str
    acknowledged_position_set_id: str
    acknowledged_position_set_fingerprint: str
    limits: dict[str, Any]
    exit_policy_id: str
    consumed: bool = False
    user_authorization_present: bool = False

    def content_hash(self) -> str:
        payload = self.to_dict()
        payload.pop("consumed", None)
        payload.pop("user_authorization_present", None)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "nonce": self.nonce,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "branch": self.branch,
            "commit_identity": self.commit_identity,
            "strategy_id": self.strategy_id,
            "market_family": self.market_family,
            "acknowledged_position_set_id": self.acknowledged_position_set_id,
            "acknowledged_position_set_fingerprint": (
                self.acknowledged_position_set_fingerprint
            ),
            "limits": dict(self.limits),
            "exit_policy_id": self.exit_policy_id,
            "consumed": self.consumed,
            "user_authorization_present": self.user_authorization_present,
            "content_hash": None,  # filled by writer
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> R7BSessionAuthorization:
        return cls(
            schema_version=str(data["schema_version"]),
            session_id=str(data["session_id"]),
            nonce=str(data["nonce"]),
            created_at=str(data["created_at"]),
            expires_at=str(data["expires_at"]),
            branch=str(data["branch"]),
            commit_identity=str(data["commit_identity"]),
            strategy_id=str(data["strategy_id"]),
            market_family=str(data["market_family"]),
            acknowledged_position_set_id=str(data["acknowledged_position_set_id"]),
            acknowledged_position_set_fingerprint=str(
                data["acknowledged_position_set_fingerprint"]
            ),
            limits=dict(data["limits"]),
            exit_policy_id=str(data["exit_policy_id"]),
            consumed=bool(data.get("consumed", False)),
            user_authorization_present=bool(data.get("user_authorization_present", False)),
        )


def build_session_authorization(
    *,
    commit_identity: str,
    acknowledged_position_set_id: str,
    acknowledged_position_set_fingerprint: str,
    branch: str = "rest_project",
    strategy_id: str = "ReferenceMomentumStrategy",
    market_family: str = "btc_updown_5m",
    lifetime_s: int = 1800,
    now: datetime | None = None,
    limits: dict[str, Any] | None = None,
) -> R7BSessionAuthorization:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    lim = dict(DEFAULT_SESSION_LIMITS)
    if limits:
        lim.update(limits)
    # Hard envelope caps
    if Decimal(str(lim["max_buy_collateral_including_fee"])) > Decimal("5.00"):
        raise SessionError("MAX_BUY_EXCEEDS_5")
    if int(lim["markets_bound"]) != 1:
        raise SessionError("MARKETS_BOUND_MUST_BE_1")
    if int(lim["max_entry_economic_actions"]) != 1:
        raise SessionError("MAX_ENTRY_MUST_BE_1")
    if lim.get("pyramiding"):
        raise SessionError("PYRAMIDING_FORBIDDEN")
    if lim.get("automatic_next_market_after_binding"):
        raise SessionError("AUTOMATIC_CONTINUATION_FORBIDDEN")
    if lim.get("post_lifecycle_continuation"):
        raise SessionError("POST_LIFECYCLE_CONTINUATION_FORBIDDEN")
    if lim.get("heartbeat"):
        raise SessionError("HEARTBEAT_FORBIDDEN")
    return R7BSessionAuthorization(
        schema_version="r7b_session_auth_v1",
        session_id=str(uuid4()),
        nonce=str(uuid4()),
        created_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=int(lifetime_s))).isoformat(),
        branch=branch,
        commit_identity=commit_identity,
        strategy_id=strategy_id,
        market_family=market_family,
        acknowledged_position_set_id=acknowledged_position_set_id,
        acknowledged_position_set_fingerprint=acknowledged_position_set_fingerprint,
        limits=lim,
        exit_policy_id="EXIT_POLICY_V1",
        consumed=False,
        user_authorization_present=False,
    )


def write_session_authorization(path: Path, session: R7BSessionAuthorization) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = session.to_dict()
    ch = session.content_hash()
    data["content_hash"] = ch
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return ch


def read_session_authorization(path: Path) -> R7BSessionAuthorization:
    return R7BSessionAuthorization.from_dict(
        json.loads(path.read_text(encoding="utf-8"))
    )


def validate_session_authorization(
    session: R7BSessionAuthorization,
    *,
    commit_identity: str,
    branch: str = "rest_project",
    strategy_id: str = "ReferenceMomentumStrategy",
    now: datetime | None = None,
    require_user_auth: bool = False,
) -> None:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if session.consumed:
        raise SessionError("SESSION_CONSUMED")
    exp = datetime.fromisoformat(session.expires_at.replace("Z", "+00:00"))
    if exp <= now:
        raise SessionError("SESSION_EXPIRED")
    if session.commit_identity != commit_identity:
        raise SessionError("COMMIT_MISMATCH")
    if session.branch != branch:
        raise SessionError("BRANCH_MISMATCH")
    if session.strategy_id != strategy_id:
        raise SessionError("STRATEGY_MISMATCH")
    if session.market_family != "btc_updown_5m":
        raise SessionError("MARKET_FAMILY_MISMATCH")
    if Decimal(str(session.limits["max_buy_collateral_including_fee"])) > Decimal("5.00"):
        raise SessionError("MAX_BUY_EXCEEDS_5")
    if require_user_auth and not session.user_authorization_present:
        raise SessionError("USER_AUTHORIZATION_ABSENT")


@dataclass
class R7BExecutionArtifact:
    """Exact market-bound artifact — created immediately before future submit."""

    artifact_id: str
    session_id: str
    created_at: str
    expires_at: str
    commit_identity: str
    config_fingerprint: str
    acknowledged_position_set_id: str
    market_slug: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    selected_outcome: str
    selected_token_id: str
    signal_id: str
    decision_id: str
    market_start: str
    market_end: str
    entry_deadline: str
    flatten_deadline: str
    best_ask: str
    worst_entry_price: str
    buy_amount: str
    max_entry_fee: str
    max_entry_collateral: str
    max_estimated_shares: str
    visible_depth: str
    tick_size: str
    min_order_size: str
    fee_rate: str
    fee_exponent: str
    user_stream_ready: bool
    reconciliation_fingerprint: str
    balance_allowance_ready: bool
    exit_policy_id: str
    kill_switch_active: bool
    live_budget_unused: bool
    correlation_id: str

    def content_hash(self) -> str:
        raw = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "commit_identity": self.commit_identity,
            "config_fingerprint": self.config_fingerprint,
            "acknowledged_position_set_id": self.acknowledged_position_set_id,
            "market_slug": self.market_slug,
            "condition_id": self.condition_id,
            "yes_token_id": self.yes_token_id,
            "no_token_id": self.no_token_id,
            "selected_outcome": self.selected_outcome,
            "selected_token_id": self.selected_token_id,
            "signal_id": self.signal_id,
            "decision_id": self.decision_id,
            "market_start": self.market_start,
            "market_end": self.market_end,
            "entry_deadline": self.entry_deadline,
            "flatten_deadline": self.flatten_deadline,
            "best_ask": self.best_ask,
            "worst_entry_price": self.worst_entry_price,
            "buy_amount": self.buy_amount,
            "max_entry_fee": self.max_entry_fee,
            "max_entry_collateral": self.max_entry_collateral,
            "max_estimated_shares": self.max_estimated_shares,
            "shares_guaranteed": False,
            "visible_depth": self.visible_depth,
            "tick_size": self.tick_size,
            "min_order_size": self.min_order_size,
            "fee_rate": self.fee_rate,
            "fee_exponent": self.fee_exponent,
            "user_stream_ready": self.user_stream_ready,
            "reconciliation_fingerprint": self.reconciliation_fingerprint,
            "balance_allowance_ready": self.balance_allowance_ready,
            "exit_policy_id": self.exit_policy_id,
            "kill_switch_active": self.kill_switch_active,
            "live_budget_unused": self.live_budget_unused,
            "correlation_id": self.correlation_id,
            "order_type": "FAK",
        }


@dataclass
class SessionRuntimeState:
    phase: SessionPhase = SessionPhase.CREATED
    session: R7BSessionAuthorization | None = None
    windows_observed: int = 0
    bound_market_slug: str | None = None
    execution_artifact: R7BExecutionArtifact | None = None
    entry_attempts: int = 0
    any_fill: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)

    def _note(self, event: str, **extra: Any) -> None:
        self.history.append(
            {
                "event": event,
                "phase": self.phase.value,
                "ts": datetime.now(timezone.utc).isoformat(),
                **extra,
            }
        )

    def attach(self, session: R7BSessionAuthorization) -> None:
        self.session = session
        self.phase = SessionPhase.OBSERVING
        self._note("session_attached", session_id=session.session_id)

    def observe_window(self, slug: str, *, eligible: bool) -> None:
        if self.session is None:
            raise SessionError("NO_SESSION")
        if self.phase not in {SessionPhase.OBSERVING, SessionPhase.CREATED}:
            if self.phase is SessionPhase.MARKET_BOUND:
                raise SessionError("ALREADY_BOUND")
            raise SessionError(f"BAD_PHASE_{self.phase.value}")
        self.phase = SessionPhase.OBSERVING
        self.windows_observed += 1
        max_w = int(self.session.limits["max_windows_observed"])
        self._note("window_observed", slug=slug, eligible=eligible, n=self.windows_observed)
        if self.windows_observed > max_w:
            self.phase = SessionPhase.EXPIRED
            raise SessionError("MAX_WINDOWS_EXCEEDED")
        if not eligible:
            return

    def bind_market(self, slug: str) -> None:
        if self.session is None:
            raise SessionError("NO_SESSION")
        if self.phase is not SessionPhase.OBSERVING:
            raise SessionError("BIND_REQUIRES_OBSERVING")
        if self.bound_market_slug is not None:
            raise SessionError("ALREADY_BOUND")
        self.bound_market_slug = slug
        self.phase = SessionPhase.MARKET_BOUND
        self._note("market_bound", slug=slug)

    def set_artifact(self, artifact: R7BExecutionArtifact) -> None:
        if self.phase is not SessionPhase.MARKET_BOUND:
            raise SessionError("ARTIFACT_REQUIRES_BOUND")
        if artifact.market_slug != self.bound_market_slug:
            raise SessionError("ARTIFACT_MARKET_MISMATCH")
        if artifact.session_id != (self.session.session_id if self.session else None):
            raise SessionError("ARTIFACT_SESSION_MISMATCH")
        self.execution_artifact = artifact
        self.phase = SessionPhase.ARTIFACT_READY
        self._note("artifact_ready", artifact_id=artifact.artifact_id)

    def begin_entry_submit(self) -> None:
        """Consume session at the start of network submission."""
        if self.phase is not SessionPhase.ARTIFACT_READY:
            raise SessionError("SUBMIT_REQUIRES_ARTIFACT")
        if self.session is None or self.session.consumed:
            raise SessionError("SESSION_UNAVAILABLE")
        self.session = R7BSessionAuthorization(
            **{**self.session.__dict__, "consumed": True}
        )
        self.phase = SessionPhase.ENTRY_SUBMITTING
        self.entry_attempts += 1
        self._note("entry_submit_begun_session_consumed")

    def note_fill(self) -> None:
        self.any_fill = True
        self.phase = SessionPhase.CONSUMED
        self._note("entry_fill")

    def note_flat(self) -> None:
        self.phase = SessionPhase.COMPLETED_FLAT
        self._note("flat_confirmed")

    def can_second_entry(self) -> bool:
        if self.any_fill:
            return False
        if self.session is None:
            return False
        if self.entry_attempts >= int(self.session.limits["max_entry_attempts"]):
            return False
        # Only if first was definitive reject/unfilled and budget unused — caller checks budget
        return self.entry_attempts == 1 and not self.any_fill

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "windows_observed": self.windows_observed,
            "bound_market_slug": self.bound_market_slug,
            "entry_attempts": self.entry_attempts,
            "any_fill": self.any_fill,
            "session_consumed": bool(self.session and self.session.consumed),
            "artifact_id": (
                None if self.execution_artifact is None else self.execution_artifact.artifact_id
            ),
            "history": list(self.history),
        }


def build_execution_artifact(
    *,
    session: R7BSessionAuthorization,
    market_slug: str,
    condition_id: str,
    yes_token_id: str,
    no_token_id: str,
    selected_outcome: str,
    selected_token_id: str,
    signal_id: str,
    decision_id: str,
    market_start: str,
    market_end: str,
    entry_deadline: str,
    flatten_deadline: str,
    best_ask: Decimal,
    worst_entry_price: Decimal,
    buy_amount: Decimal,
    max_entry_fee: Decimal,
    max_entry_collateral: Decimal,
    max_estimated_shares: Decimal,
    visible_depth: Decimal,
    tick_size: str,
    min_order_size: str,
    fee_rate: str,
    fee_exponent: str,
    user_stream_ready: bool,
    reconciliation_fingerprint: str,
    balance_allowance_ready: bool,
    kill_switch_active: bool,
    live_budget_unused: bool,
    config_fingerprint: str = "r7b_live_once_v1",
    artifact_ttl_s: float = 20.0,
    now: datetime | None = None,
) -> R7BExecutionArtifact:
    if selected_token_id not in {yes_token_id, no_token_id}:
        raise SessionError("SELECTED_TOKEN_NOT_IN_MARKET")
    if max_entry_collateral > Decimal(
        str(session.limits["max_buy_collateral_including_fee"])
    ):
        raise SessionError("COLLATERAL_EXCEEDS_SESSION_CAP")
    if max_entry_collateral > Decimal("5.00"):
        raise SessionError("COLLATERAL_EXCEEDS_5")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return R7BExecutionArtifact(
        artifact_id=str(uuid4()),
        session_id=session.session_id,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=artifact_ttl_s)).isoformat(),
        commit_identity=session.commit_identity,
        config_fingerprint=config_fingerprint,
        acknowledged_position_set_id=session.acknowledged_position_set_id,
        market_slug=market_slug,
        condition_id=condition_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        selected_outcome=selected_outcome,
        selected_token_id=selected_token_id,
        signal_id=signal_id,
        decision_id=decision_id,
        market_start=market_start,
        market_end=market_end,
        entry_deadline=entry_deadline,
        flatten_deadline=flatten_deadline,
        best_ask=str(best_ask),
        worst_entry_price=str(worst_entry_price),
        buy_amount=str(buy_amount),
        max_entry_fee=str(max_entry_fee),
        max_entry_collateral=str(max_entry_collateral),
        max_estimated_shares=str(max_estimated_shares),
        visible_depth=str(visible_depth),
        tick_size=tick_size,
        min_order_size=min_order_size,
        fee_rate=fee_rate,
        fee_exponent=fee_exponent,
        user_stream_ready=user_stream_ready,
        reconciliation_fingerprint=reconciliation_fingerprint,
        balance_allowance_ready=balance_allowance_ready,
        exit_policy_id=session.exit_policy_id,
        kill_switch_active=kill_switch_active,
        live_budget_unused=live_budget_unused,
        correlation_id=str(uuid4()),
    )


def future_authorization_wording(session: R7BSessionAuthorization) -> str:
    """Exact statement the user must approve for a future R7B session."""
    lim = session.limits
    return (
        f"I authorize one-shot R7B session {session.session_id} "
        f"(hash-bound; nonce {session.nonce}) on branch {session.branch} "
        f"at commit {session.commit_identity} for strategy {session.strategy_id} "
        f"on market family {session.market_family} only. "
        f"Session lifetime ends at {session.expires_at} "
        f"(max {lim['session_lifetime_s']}s). "
        f"Runtime may observe at most {lim['max_windows_observed']} windows and "
        f"bind exactly one eligible BTC 5m market. "
        f"Maximum cumulative BUY collateral including entry fee is "
        f"${lim['max_buy_collateral_including_fee']}. "
        f"Allowed: one FAK BUY, cancel of owned order id if required, "
        f"FAK SELL of venue-confirmed acquired quantity, emergency risk-reducing "
        f"FAK SELL of that quantity only. "
        f"Forbidden: redemption or any action on acknowledged position set "
        f"{session.acknowledged_position_set_id}, heartbeat, cancel-all, "
        f"on-chain ops, allowance approval, credential create/rotate, "
        f"pyramiding, automatic next market, post-lifecycle continuation. "
        f"This is not a reusable live-trading switch. "
        f"Exact execution artifact will be generated at bind time and must be "
        f"revalidated before any submission."
    )
