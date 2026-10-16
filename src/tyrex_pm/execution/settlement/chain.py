"""Read-only Polygon implementations of the settlement ports (no signing, no transfers).

* ``finalized_payout_reader`` — Conditional Tokens ``payoutNumerators/payoutDenominator``
  read at the ``finalized`` block tag: the payout unit is the contract's own
  denominator, so ``payout_per_share = numerator / denominator`` needs no guess.
* ``receipt_redemption_verifier`` — one redemption transaction's receipt: status 1,
  block at or before the finalized block, and a Conditional Tokens ``PayoutRedemption``
  log whose redeemer is our wallet, collateral the configured token, condition ours and
  index sets containing our outcome. Credited amount = the log's payout / 10^decimals.

Both need ``TYREX_POLYGON_RPC_URL``. They are operator tools; nothing here runs inside
strategy evaluation.
"""

from __future__ import annotations

from decimal import Decimal
from functools import partial
from typing import Any

from tyrex_pm.adapters.polymarket.ctf_payout import CONDITIONAL_TOKENS_ADDRESS, read_binary_payout
from tyrex_pm.adapters.polymarket.polygon_rpc import PolygonRpcError, PolygonRpcReader

from .ledger import Episode
from .resolution import PayoutObservation, RedemptionEvidence

# Polymarket collateral on Polygon (USDC.e), 6 decimals.
DEFAULT_COLLATERAL = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
COLLATERAL_DECIMALS = 6
PAYOUT_REDEMPTION_SIGNATURE = "PayoutRedemption(address,address,bytes32,bytes32,uint256[],uint256)"


def payout_redemption_topic() -> str:
    try:
        from eth_utils import keccak
    except ImportError as exc:  # pragma: no cover - optional live dependency
        raise RuntimeError("eth_utils is required to verify redemption receipts") from exc
    return "0x" + keccak(text=PAYOUT_REDEMPTION_SIGNATURE).hex()


class SettlementChainReader(PolygonRpcReader):
    """Read-only reader: finalized ``eth_call``, receipts and the finalized block."""

    allowed_methods = frozenset(
        {"eth_chainId", "eth_call", "eth_getTransactionReceipt", "eth_getBlockByNumber"}
    )

    async def eth_call_at(self, *, to: str, data: str, block: str = "finalized") -> str:
        await self._ensure_chain()
        result = await self._rpc("eth_call", [{"to": to, "data": data}, block])
        if not isinstance(result, str) or not result.startswith("0x") or len(result) != 66:
            raise PolygonRpcError("polygon_rpc_malformed")
        return result

    async def receipt(self, tx_hash: str) -> dict | None:
        await self._ensure_chain()
        result = await self._rpc("eth_getTransactionReceipt", [tx_hash])
        return result if isinstance(result, dict) else None

    async def finalized_block_number(self) -> int | None:
        await self._ensure_chain()
        block = await self._rpc("eth_getBlockByNumber", ["finalized", False])
        if not isinstance(block, dict) or not isinstance(block.get("number"), str):
            return None
        return int(block["number"], 16)


def finalized_payout_reader(reader: SettlementChainReader):
    async def read(condition_id: str, outcome_index: int) -> PayoutObservation:
        call = partial(reader.eth_call_at, block="finalized")
        payout = await read_binary_payout(
            call, condition_id=condition_id, outcome_index=outcome_index
        )
        if payout is None:
            return PayoutObservation(False, None, "ctf_finalized_block", "unresolved_or_unreadable")
        return PayoutObservation(True, payout, "ctf_finalized_block", "numerator/denominator")

    return read


def _word(data: str, index: int) -> str:
    body = data[2:] if data.startswith("0x") else data
    return body[index * 64 : (index + 1) * 64]


def decode_payout_redemption(log: dict) -> dict[str, Any] | None:
    """Decode a PayoutRedemption log (redeemer, collateral, parent indexed)."""
    topics = [t.lower() for t in log.get("topics") or []]
    data = str(log.get("data") or "0x")
    if len(topics) != 4:
        return None
    try:
        condition = "0x" + _word(data, 0)
        offset = int(_word(data, 1), 16) // 32
        payout = int(_word(data, 2), 16)
        count = int(_word(data, offset), 16)
        index_sets = [int(_word(data, offset + 1 + i), 16) for i in range(count)]
    except ValueError:
        return None
    return {
        "topic0": topics[0],
        "redeemer": "0x" + topics[1][-40:],
        "collateral": "0x" + topics[2][-40:],
        "parent_collection": topics[3],
        "condition_id": condition,
        "index_sets": index_sets,
        "payout": payout,
        "address": str(log.get("address") or "").lower(),
    }


def receipt_redemption_verifier(
    reader: SettlementChainReader,
    *,
    collateral: str = DEFAULT_COLLATERAL,
    decimals: int = COLLATERAL_DECIMALS,
    contract: str = CONDITIONAL_TOKENS_ADDRESS,
):
    topic = payout_redemption_topic()

    async def verify(tx_hash: str, episode: Episode, wallet: str) -> RedemptionEvidence:
        receipt = await reader.receipt(tx_hash)
        if receipt is None:
            return RedemptionEvidence(False, False, None, ("RECEIPT_NOT_FOUND",), tx_hash)
        reasons: list[str] = []
        if receipt.get("status") != "0x1":
            reasons.append("TRANSACTION_FAILED")
        block = int(str(receipt.get("blockNumber") or "0x0"), 16)
        finalized_block = await reader.finalized_block_number()
        finalized = finalized_block is not None and block <= finalized_block
        matches = []
        for log in receipt.get("logs") or []:
            decoded = decode_payout_redemption(log)
            if decoded is None or decoded["topic0"] != topic:
                continue
            if decoded["address"] != contract.lower():
                continue
            if (
                decoded["redeemer"].lower() == wallet.lower()
                and decoded["collateral"].lower() == collateral.lower()
                and decoded["condition_id"].lower() == episode.condition_id.lower()
                and (1 << episode.outcome_index) in decoded["index_sets"]
            ):
                matches.append(decoded)
        if len(matches) != 1:
            reasons.append("NO_MATCHING_REDEMPTION_LOG" if not matches else "AMBIGUOUS_LOGS")
        credited = (
            Decimal(matches[0]["payout"]) / (Decimal(10) ** decimals) if len(matches) == 1 else None
        )
        return RedemptionEvidence(
            attributable=not reasons,
            finalized=finalized,
            credited_amount=credited,
            reasons=tuple(reasons),
            tx_hash=tx_hash,
            block_number=block,
            source="ctf_payout_redemption_receipt",
        )

    return verify
