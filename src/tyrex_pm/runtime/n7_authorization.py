"""N7 single-use operator authorization envelope (not reusable from R7).

CLI may generate an authorization *request*. The approval phrase must come
from the operator. CI cannot construct a real-venue mutation authorization.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.runtime.n7_abort import N7AbortCode
from tyrex_pm.runtime.n7_sealed import N7SealedConfig

PURPOSE = "N7 Z-Gap one-shot"
MARKET_FAMILY = "btc_updown_5m"
APPROVAL_PHRASE_PREFIX = "I AUTHORIZE N7 Z-GAP ONE-SHOT"

# Forbidden actions (recorded on every envelope).
FORBIDDEN_ACTIONS = (
    "cancel_all",
    "heartbeat_cancel_all",
    "redeem",
    "transfer",
    "allowance_change",
    "unrelated_orders",
    "scope_b",
    "hold_to_resolution",
    "second_window_rollover",
    "reentry",
    "reversal",
)


@dataclass(frozen=True, kw_only=True)
class N7AuthorizationRequest:
    """Operator-facing request artifact (not yet approved)."""

    envelope_id: str
    purpose: str
    operator_label: str
    git_head: str
    worktree_clean_required: bool
    config_fingerprint: str
    scope: str
    max_buy_collateral: str
    max_daily_notional: str
    max_daily_loss: str
    order_style: str
    timing_fingerprint: str
    exit_retry_max_attempts: int
    exit_retry_time_budget_ms: int
    market_family: str
    valid_from_utc: str
    valid_until_utc: str
    max_markets: int
    max_entry_lineages: int
    allowed_actions: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    nonce: str
    approval_phrase_template: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope_id": self.envelope_id,
            "purpose": self.purpose,
            "operator_label": self.operator_label,
            "git_head": self.git_head,
            "worktree_clean_required": self.worktree_clean_required,
            "config_fingerprint": self.config_fingerprint,
            "scope": self.scope,
            "max_buy_collateral": self.max_buy_collateral,
            "max_daily_notional": self.max_daily_notional,
            "max_daily_loss": self.max_daily_loss,
            "order_style": self.order_style,
            "timing_fingerprint": self.timing_fingerprint,
            "exit_retry_max_attempts": self.exit_retry_max_attempts,
            "exit_retry_time_budget_ms": self.exit_retry_time_budget_ms,
            "market_family": self.market_family,
            "valid_from_utc": self.valid_from_utc,
            "valid_until_utc": self.valid_until_utc,
            "max_markets": self.max_markets,
            "max_entry_lineages": self.max_entry_lineages,
            "allowed_actions": list(self.allowed_actions),
            "forbidden_actions": list(self.forbidden_actions),
            "nonce": self.nonce,
            "approval_phrase_template": self.approval_phrase_template,
            "approved": False,
            "mutations_enabled": False,
        }


@dataclass
class N7AuthorizationEnvelope:
    """Mutable runtime envelope: single-use, bind-once."""

    envelope_id: str
    purpose: str
    operator_label: str
    git_head: str
    config_fingerprint: str
    scope: str
    max_buy_collateral: Decimal
    max_daily_notional: Decimal
    max_daily_loss: Decimal
    order_style: str
    timing_fingerprint: str
    exit_retry_max_attempts: int
    exit_retry_time_budget_ms: int
    market_family: str
    valid_from: datetime
    valid_until: datetime
    max_markets: int
    max_entry_lineages: int
    nonce: str
    expected_phrase: str
    approved: bool = False
    consumed: bool = False
    bound_market_id: str | None = None
    bound_window_id: str | None = None
    bound_at: datetime | None = None
    allows_real_venue_mutation: bool = False
    transport_kind: str = "none"
    facts: list[dict[str, Any]] = field(default_factory=list)

    def is_expired(self, now: datetime) -> bool:
        return now >= self.valid_until or now < self.valid_from

    def approve(self, phrase: str, *, now: datetime | None = None) -> N7AbortCode | None:
        now = now or datetime.now(timezone.utc)
        if self.consumed:
            return N7AbortCode.AUTHORIZATION_CONSUMED
        if self.is_expired(now):
            return N7AbortCode.AUTHORIZATION_EXPIRED
        if phrase.strip() != self.expected_phrase:
            return N7AbortCode.AUTHORIZATION_MISMATCH
        self.approved = True
        self.facts.append({"event": "approved", "at": now.isoformat()})
        return None

    def bind_market(
        self,
        *,
        market_id: str,
        window_id: str,
        market_family: str,
        now: datetime | None = None,
    ) -> N7AbortCode | None:
        now = now or datetime.now(timezone.utc)
        if not self.approved:
            return N7AbortCode.AUTHORIZATION_ABSENT
        if self.bound_market_id is not None:
            return N7AbortCode.BINDING_ALREADY_SET
        if self.consumed:
            return N7AbortCode.AUTHORIZATION_CONSUMED
        if self.is_expired(now):
            return N7AbortCode.AUTHORIZATION_EXPIRED
        if market_family != self.market_family:
            return N7AbortCode.MARKET_OUTSIDE_ENVELOPE
        self.bound_market_id = market_id
        self.bound_window_id = window_id
        self.bound_at = now
        self.facts.append(
            {
                "event": "bound",
                "market_id": market_id,
                "window_id": window_id,
                "at": now.isoformat(),
            }
        )
        return None

    def consume_for_fake(self) -> N7AbortCode | None:
        """Arm fake-transport mutations for N7A deterministic acceptance."""
        if not self.approved or self.bound_market_id is None:
            return N7AbortCode.AUTHORIZATION_ABSENT
        if self.consumed:
            return N7AbortCode.AUTHORIZATION_CONSUMED
        self.consumed = True
        self.allows_real_venue_mutation = False
        self.transport_kind = "fake"
        self.facts.append({"event": "consumed_fake"})
        return None

    def consume_for_real(self, *, ci_guard: bool = True) -> N7AbortCode | None:
        """Arm real venue mutations — operator N7B only.

        ``ci_guard`` blocks construction in CI (``CI`` / ``PYTEST_CURRENT_TEST``).
        """
        import os

        if ci_guard and (
            os.environ.get("CI")
            or os.environ.get("PYTEST_CURRENT_TEST")
            or os.environ.get("TYREX_N7_FORBID_REAL") == "1"
        ):
            return N7AbortCode.AUTHORIZATION_MISMATCH
        if not self.approved or self.bound_market_id is None:
            return N7AbortCode.AUTHORIZATION_ABSENT
        if self.consumed:
            return N7AbortCode.AUTHORIZATION_CONSUMED
        self.consumed = True
        self.allows_real_venue_mutation = True
        self.transport_kind = "real"
        self.facts.append({"event": "consumed_real"})
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope_id": self.envelope_id,
            "purpose": self.purpose,
            "operator_label": self.operator_label,
            "git_head": self.git_head,
            "config_fingerprint": self.config_fingerprint,
            "scope": self.scope,
            "max_buy_collateral": str(self.max_buy_collateral),
            "approved": self.approved,
            "consumed": self.consumed,
            "bound_market_id": self.bound_market_id,
            "bound_window_id": self.bound_window_id,
            "bound_at": None if self.bound_at is None else self.bound_at.isoformat(),
            "allows_real_venue_mutation": self.allows_real_venue_mutation,
            "transport_kind": self.transport_kind,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "nonce": self.nonce,
            "max_entry_lineages": self.max_entry_lineages,
            "market_family": self.market_family,
        }


def _timing_fingerprint(cfg: N7SealedConfig) -> str:
    raw = json.dumps(cfg.timing.fingerprint_payload(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_approval_phrase(*, envelope_id: str, git_head: str, nonce: str) -> str:
    short = git_head[:12]
    return (
        f"{APPROVAL_PHRASE_PREFIX} "
        f"envelope={envelope_id} head={short} nonce={nonce}"
    )


def create_authorization_request(
    *,
    sealed: N7SealedConfig,
    git_head: str,
    operator_label: str = "operator",
    valid_for: timedelta = timedelta(minutes=20),
    now: datetime | None = None,
) -> tuple[N7AuthorizationRequest, N7AuthorizationEnvelope]:
    """Create request + unapproved envelope. Does not enable mutations."""
    now = now or datetime.now(timezone.utc)
    envelope_id = str(uuid.uuid4())
    nonce = secrets.token_hex(8)
    phrase = build_approval_phrase(
        envelope_id=envelope_id, git_head=git_head, nonce=nonce
    )
    valid_until = now + valid_for
    timing_fp = _timing_fingerprint(sealed)
    req = N7AuthorizationRequest(
        envelope_id=envelope_id,
        purpose=PURPOSE,
        operator_label=operator_label,
        git_head=git_head,
        worktree_clean_required=True,
        config_fingerprint=sealed.fingerprint(),
        scope="A",
        max_buy_collateral=str(sealed.max_buy_collateral),
        max_daily_notional=str(sealed.max_daily_notional),
        max_daily_loss=str(sealed.max_daily_loss),
        order_style=sealed.order_style,
        timing_fingerprint=timing_fp,
        exit_retry_max_attempts=sealed.timing.exit_retry_max_attempts,
        exit_retry_time_budget_ms=sealed.timing.exit_retry_time_budget_ms,
        market_family=sealed.market_family,
        valid_from_utc=now.isoformat(),
        valid_until_utc=valid_until.isoformat(),
        max_markets=1,
        max_entry_lineages=1,
        allowed_actions=(
            "one_entry_lineage",
            "inventory_reducing_exit",
            "cancel_owned_order",
            "bounded_exit_ladder",
        ),
        forbidden_actions=FORBIDDEN_ACTIONS,
        nonce=nonce,
        approval_phrase_template=phrase,
    )
    env = N7AuthorizationEnvelope(
        envelope_id=envelope_id,
        purpose=PURPOSE,
        operator_label=operator_label,
        git_head=git_head,
        config_fingerprint=sealed.fingerprint(),
        scope="A",
        max_buy_collateral=sealed.max_buy_collateral,
        max_daily_notional=sealed.max_daily_notional,
        max_daily_loss=sealed.max_daily_loss,
        order_style=sealed.order_style,
        timing_fingerprint=timing_fp,
        exit_retry_max_attempts=sealed.timing.exit_retry_max_attempts,
        exit_retry_time_budget_ms=sealed.timing.exit_retry_time_budget_ms,
        market_family=sealed.market_family,
        valid_from=now,
        valid_until=valid_until,
        max_markets=1,
        max_entry_lineages=1,
        nonce=nonce,
        expected_phrase=phrase,
    )
    return req, env


def write_authorization_request(path: Path, request: N7AuthorizationRequest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(request.to_dict(), indent=2) + "\n", encoding="utf-8")


def make_test_envelope(
    *,
    sealed: N7SealedConfig,
    git_head: str = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    approve: bool = True,
    valid_for: timedelta = timedelta(hours=1),
    now: datetime | None = None,
) -> N7AuthorizationEnvelope:
    """Deterministic test helper — fake path only; never arms real venue."""
    req, env = create_authorization_request(
        sealed=sealed,
        git_head=git_head,
        operator_label="test",
        valid_for=valid_for,
        now=now,
    )
    if approve:
        err = env.approve(req.approval_phrase_template, now=now)
        assert err is None, err
    return env
