"""Payout observations. A ``latest`` read is not reusable clearance evidence.

Polygon ``eth_call`` at block tag ``latest`` has no block hash, no confirmation
depth, and no reorg check in the current adapter. Process lifetime does not
make that observation final. ``lookup`` therefore returns nothing until a proof
is explicitly finalized with a non-``latest`` block identity. Inspection always
performs a fresh payout read on its own client. Holdings and open orders are
not stored here. Neg-risk is never stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.adapters.polymarket.ctf_payout import CONDITIONAL_TOKENS_ADDRESS, POLYGON_CHAIN_ID

PROOF_SOURCE = "payoutNumerators/payoutDenominator"
BLOCK_TAG_LATEST = "latest"


@dataclass(frozen=True, kw_only=True)
class SettlementProof:
    chain_id: int
    contract: str
    condition_id: str
    outcome_index: int
    payout_per_share: Decimal
    proof_source: str
    block_tag: str
    finality: str
    block_identity: str = ""
    token_id: str = ""


def proof_is_reusable(proof: SettlementProof) -> bool:
    """Indefinite reuse requires proven finality and a token/outcome mapping.

    ``latest`` and ``provisional_latest`` never qualify.
    """
    if proof.finality != "finalized":
        return False
    if proof.block_tag in {"", BLOCK_TAG_LATEST}:
        return False
    if not proof.block_identity or proof.outcome_index not in (0, 1):
        return False
    if not proof.condition_id or not proof.token_id or not proof.contract:
        return False
    if proof.payout_per_share < 0 or proof.payout_per_share > 1:
        return False
    return proof.proof_source == PROOF_SOURCE


class SettlementCache:
    def __init__(self) -> None:
        self._proofs: dict[tuple[int, str, str, int, str], SettlementProof] = {}

    def lookup(
        self,
        *,
        chain_id: int,
        contract: str,
        condition_id: str,
        outcome_index: int,
        token_id: str,
        proof_source: str = PROOF_SOURCE,
    ) -> SettlementProof | None:
        proof = self._proofs.get(
            (
                int(chain_id),
                contract.lower(),
                condition_id.lower(),
                int(outcome_index),
                proof_source,
            )
        )
        if proof is None or not proof_is_reusable(proof):
            return None
        if proof.token_id != token_id:
            return None
        return proof

    def store_provisional_latest(
        self,
        *,
        condition_id: str,
        outcome_index: int,
        payout_per_share: Decimal,
        contract: str = CONDITIONAL_TOKENS_ADDRESS,
        chain_id: int = POLYGON_CHAIN_ID,
        token_id: str = "",
    ) -> SettlementProof | None:
        """Record a ``latest`` observation. It is not returned by ``lookup``."""
        if payout_per_share is None or payout_per_share < 0 or payout_per_share > 1:
            return None
        if outcome_index not in (0, 1) or not condition_id:
            return None
        proof = SettlementProof(
            chain_id=int(chain_id),
            contract=contract,
            condition_id=condition_id,
            outcome_index=int(outcome_index),
            payout_per_share=payout_per_share,
            proof_source=PROOF_SOURCE,
            block_tag=BLOCK_TAG_LATEST,
            finality="provisional_latest",
            block_identity="",
            token_id=token_id,
        )
        self._proofs[self._key(proof)] = proof
        return proof

    def invalidate(
        self,
        *,
        condition_id: str | None = None,
        contract: str | None = None,
        chain_id: int | None = None,
    ) -> None:
        def keep(proof: SettlementProof) -> bool:
            if condition_id is not None and proof.condition_id.lower() != condition_id.lower():
                return True
            if contract is not None and proof.contract.lower() != contract.lower():
                return True
            if chain_id is not None and proof.chain_id != int(chain_id):
                return True
            return False

        self._proofs = {key: proof for key, proof in self._proofs.items() if keep(proof)}

    @staticmethod
    def _key(proof: SettlementProof) -> tuple[int, str, str, int, str]:
        return (
            proof.chain_id,
            proof.contract.lower(),
            proof.condition_id.lower(),
            proof.outcome_index,
            proof.proof_source,
        )
