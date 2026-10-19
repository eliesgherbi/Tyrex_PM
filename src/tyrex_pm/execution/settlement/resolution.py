"""Resolution, payout proof and operator-assisted redemption over the settlement ledger.

Provider access goes through two injectable ports (real read-only Polygon
implementations live in ``settlement.chain``; tests use fakes):

* ``PayoutReader(condition_id, outcome_index)`` — the Conditional Tokens payout for one
  outcome, read at a *finalized* block. A zero denominator means unresolved. Scheduled
  close, a displayed winner, an outcome price or a ``redeemable`` flag are never payout.
* ``RedemptionVerifier(tx_hash, episode, wallet)`` — evidence that one redemption
  transaction redeemed *this* condition and outcome for *this* wallet, with the credited
  amount taken from that transaction. An unrelated balance increase is never proof.

Redemption itself is an operator action (``REDEMPTION_PENDING_OPERATOR`` worklist);
proceeds stay unavailable until ``CASH_CONFIRMED``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal

from .ledger import Episode, EpisodeState, SettlementLedger

S = EpisodeState


@dataclass(frozen=True)
class PayoutObservation:
    finalized: bool
    payout_per_share: Decimal | None
    source: str
    detail: str = ""


@dataclass(frozen=True)
class RedemptionEvidence:
    attributable: bool
    finalized: bool
    credited_amount: Decimal | None
    reasons: tuple[str, ...]
    tx_hash: str
    block_number: int | None = None
    source: str = ""
    # The attributable log (``chain.RedemptionMatch``): one log is credited at most once.
    log_index: int | None = None
    route: str = ""
    credited_asset: str = ""


PayoutReader = Callable[[str, int], Awaitable[PayoutObservation]]
RedemptionVerifier = Callable[[str, Episode, str], Awaitable[RedemptionEvidence]]

# Credited collateral may differ from shares * payout only by USDC base-unit rounding.
CREDIT_TOLERANCE = Decimal("0.000001") * 2


@dataclass
class ReconcileReport:
    proven: list[str] = field(default_factory=list)
    completed_zero_payout: list[str] = field(default_factory=list)
    uncertain: dict[str, str] = field(default_factory=dict)


class ResolutionManager:
    def __init__(
        self,
        ledger: SettlementLedger,
        *,
        payout_reader: PayoutReader,
        redemption_verifier: RedemptionVerifier | None = None,
        wallet: str | None = None,
    ) -> None:
        self.ledger = ledger
        self.payout_reader = payout_reader
        self.redemption_verifier = redemption_verifier
        self.wallet = wallet

    async def reconcile(self) -> ReconcileReport:
        report = ReconcileReport()
        for ep in list(self.ledger.episodes.values()):
            if ep.state not in (S.HOLD_COMMITTED, S.RESOLUTION_UNCERTAIN):
                continue
            try:
                obs = await self.payout_reader(ep.condition_id, ep.outcome_index)
            except Exception as exc:  # noqa: BLE001 - provider failure is recorded, not fatal
                obs = PayoutObservation(False, None, "error", f"{type(exc).__name__}")
            if not obs.finalized or obs.payout_per_share is None:
                reason = obs.detail or "unresolved"
                report.uncertain[ep.episode_id] = reason
                # Dedupe key per reason: repeated identical observations append nothing.
                self.ledger.record(
                    "RESOLUTION_UNCERTAIN",
                    ep.episode_id,
                    key=f"RESOLUTION_UNCERTAIN:{ep.episode_id}:{obs.source}:{reason}",
                    reason=reason,
                    source=obs.source,
                )
                continue
            if not Decimal(0) <= obs.payout_per_share <= Decimal(1):
                report.uncertain[ep.episode_id] = "payout_out_of_range"
                continue
            self.ledger.record(
                "PAYOUT_PROVEN",
                ep.episode_id,
                payout_per_share=str(obs.payout_per_share),
                evidence={"source": obs.source, "detail": obs.detail},
            )
            report.proven.append(ep.episode_id)
            if obs.payout_per_share == 0:
                self.ledger.record("EPISODE_COMPLETED", ep.episode_id, reason="zero_payout")
                report.completed_zero_payout.append(ep.episode_id)
            else:
                self.ledger.record("REDEMPTION_REQUESTED", ep.episode_id, mode="operator")
        return report

    def worklist(self) -> list[dict]:
        return [
            {
                "episode_id": ep.episode_id,
                "window_id": ep.window_id,
                "condition_id": ep.condition_id,
                "token_id": ep.token_id,
                "outcome_index": ep.outcome_index,
                "net_shares": str(ep.net_shares),
                "payout_per_share": str(ep.payout_per_share),
                "expected_credit": str(ep.receivable),
                "action": "Redeem this position in the venue UI. A running runner (or "
                "`tyrex-pm settlement discover-redemptions --connect-chain --apply`) "
                "recognises the finalized redemption automatically; "
                "`confirm-redemption` with the tx hash remains available.",
            }
            for ep in self.ledger.episodes.values()
            if ep.state is S.REDEMPTION_PENDING_OPERATOR
        ]

    def credited_redemptions(self) -> dict[str, str]:
        """Redemption logs already credited -> episode. ``tx:*`` = whole tx (legacy record
        without a log index: every log of that tx is treated as already used)."""
        used: dict[str, str] = {}
        for ep in self.ledger.episodes.values():
            for item in ep.evidence:
                if item.get("event") != "CASH_CONFIRMED" or not item.get("tx_hash"):
                    continue
                tx = str(item["tx_hash"]).lower()
                index = item.get("log_index")
                used[f"{tx}:{'*' if index is None else index}"] = ep.episode_id
        return used

    def already_credited(self, tx_hash: str, log_index: int | None) -> str | None:
        used = self.credited_redemptions()
        tx = tx_hash.lower()
        return used.get(f"{tx}:*") or (None if log_index is None else used.get(f"{tx}:{log_index}"))

    async def confirm_redemption(
        self, episode_id: str, tx_hash: str
    ) -> tuple[bool, tuple[str, ...]]:
        ep = self.ledger.episodes.get(episode_id)
        if ep is None or ep.state is not S.REDEMPTION_PENDING_OPERATOR:
            return False, ("EPISODE_NOT_AWAITING_REDEMPTION",)
        if self.redemption_verifier is None or not self.wallet:
            return False, ("REDEMPTION_VERIFIER_UNAVAILABLE",)
        evidence = await self.redemption_verifier(tx_hash, ep, self.wallet)
        reasons = list(evidence.reasons)
        if not evidence.attributable:
            reasons.append("REDEMPTION_NOT_ATTRIBUTABLE")
        if not evidence.finalized:
            reasons.append("REDEMPTION_NOT_FINALIZED")
        credited = evidence.credited_amount
        if credited is None or credited <= 0:
            reasons.append("CREDIT_AMOUNT_UNKNOWN")
        elif abs(credited - ep.receivable) > CREDIT_TOLERANCE:
            reasons.append("CREDIT_AMOUNT_MISMATCH")
        if evidence.attributable and self.already_credited(tx_hash, evidence.log_index):
            reasons.append("REDEMPTION_ALREADY_CREDITED")
        if reasons:
            return False, tuple(dict.fromkeys(reasons))
        record = {"tx_hash": tx_hash, "block": evidence.block_number, "source": evidence.source}
        if evidence.log_index is not None:
            record.update(log_index=evidence.log_index, route=evidence.route,
                          credited_asset=evidence.credited_asset)
        self.ledger.record("CASH_CONFIRMED", episode_id, credited_amount=str(credited),
                           evidence=record)
        self.ledger.record("EPISODE_COMPLETED", episode_id, reason="cash_confirmed")
        return True, ()

    def apply_redemption(self, match, episode_ids: list[str], *,
                         discovery: dict | None = None) -> tuple[bool, tuple[str, ...]]:
        """Credit one proven, finalized redemption (``chain.RedemptionMatch``) to the
        episodes whose shares it redeemed. Each episode is credited its own receivable;
        the receivables must add up to the proven credit. Refused (nothing recorded) on
        any doubt; the caller keeps the item unresolved."""
        reasons: list[str] = []
        episodes = [self.ledger.episodes.get(e) for e in episode_ids]
        if not episodes or any(
            ep is None or ep.state is not S.REDEMPTION_PENDING_OPERATOR for ep in episodes
        ):
            reasons.append("EPISODE_NOT_AWAITING_REDEMPTION")
        elif any(not match.covers(ep.condition_id, ep.outcome_index, ep.token_id)
                 for ep in episodes):
            reasons.append("REDEMPTION_DOES_NOT_COVER_EPISODE")
        else:
            total = sum((ep.receivable for ep in episodes), Decimal(0))
            if abs(total - match.credited) > CREDIT_TOLERANCE * len(episodes):
                reasons.append("CREDIT_AMOUNT_MISMATCH")
        if self.already_credited(match.tx_hash, match.log_index):
            reasons.append("REDEMPTION_ALREADY_CREDITED")
        if reasons:
            return False, tuple(dict.fromkeys(reasons))
        group = sorted(episode_ids)
        for ep in episodes:
            self.ledger.record(
                "CASH_CONFIRMED",
                ep.episode_id,
                credited_amount=str(ep.receivable),
                evidence={
                    "tx_hash": match.tx_hash, "block": match.block_number,
                    "log_index": match.log_index, "route": match.route,
                    "credited_asset": match.credited_asset,
                    "source": f"{match.route}_receipt",
                    "redemption_credit_total": str(match.credited),
                    "allocated_to": group,
                    **dict(discovery or {}),
                },
            )
            self.ledger.record("EPISODE_COMPLETED", ep.episode_id, reason="cash_confirmed")
        return True, ()
