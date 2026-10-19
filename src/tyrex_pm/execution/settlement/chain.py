"""Read-only Polygon implementations of the settlement ports (no signing, no transfers).

* ``finalized_payout_reader`` — Conditional Tokens ``payoutNumerators/payoutDenominator``
  read at the ``finalized`` block tag: the payout unit is the contract's own
  denominator, so ``payout_per_share = numerator / denominator`` needs no guess.
* ``redemption_matches`` — every redemption a receipt *proves* for our wallet. Two
  routes are recognised, each with all of its facts required:

  - ``ctf_direct``: a Conditional Tokens ``PayoutRedemption`` log whose redeemer is our
    wallet and collateral the configured token (the wallet called ``redeemPositions``);
  - ``polymarket_adapter_pusd`` (what the Polymarket UI does since 2026-10, observed on
    tx 0x85983a7f…, block 95654969): within one operation of a bundle, our wallet's
    position tokens move to the redemption adapter (CTF ``TransferSingle/Batch``,
    operator = adapter), the adapter redeems them (CTF ``PayoutRedemption``, redeemer =
    adapter, our condition, payout X), Polymarket USD (pUSD) mints X to our wallet, and
    the adapter's per-user event names our wallet, our condition and X. The credited
    amount is X in pUSD (6 decimals).

  A balance increase, missing tokens or a ``redeemable`` flag are never evidence.
* ``receipt_redemption_verifier`` — the episode-level port used by
  ``confirm-redemption``: exactly one match for the episode's condition and outcome.

All need ``TYREX_POLYGON_RPC_URL``. Nothing here runs inside strategy evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial
from typing import Any

from tyrex_pm.adapters.polymarket.ctf_payout import CONDITIONAL_TOKENS_ADDRESS, read_binary_payout
from tyrex_pm.adapters.polymarket.polygon_rpc import PolygonRpcError, PolygonRpcReader

from .ledger import Episode
from .resolution import PayoutObservation, RedemptionEvidence

# Polymarket CTF collateral on Polygon (USDC.e), 6 decimals.
DEFAULT_COLLATERAL = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
COLLATERAL_DECIMALS = 6
PAYOUT_REDEMPTION_SIGNATURE = "PayoutRedemption(address,address,bytes32,bytes32,uint256[],uint256)"
TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
TRANSFER_BATCH_TOPIC = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS = "0x" + "0" * 40


def payout_redemption_topic() -> str:
    try:
        from eth_utils import keccak
    except ImportError as exc:  # pragma: no cover - optional live dependency
        raise RuntimeError("eth_utils is required to verify redemption receipts") from exc
    return "0x" + keccak(text=PAYOUT_REDEMPTION_SIGNATURE).hex()


@dataclass(frozen=True)
class AdapterRoute:
    """A redemption adapter route, with every address and topic it must match."""

    name: str
    adapter: str
    # Adapter event: (address indexed user, bytes32 indexed condition, uint256 amount).
    user_event_topic: str
    credited_token: str
    credited_decimals: int


POLYMARKET_ADAPTER_PUSD = AdapterRoute(
    name="polymarket_adapter_pusd",
    adapter="0xa1200000d0002264c9a1698e001292d00e1b00af",
    user_event_topic="0xb434294b5904213c83a167af0068ab82637c6fd4fac945e2abc74ed8d3f4d52a",
    credited_token="0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb",  # "Polymarket USD" (pUSD)
    credited_decimals=6,
)
DEFAULT_ADAPTER_ROUTES = (POLYMARKET_ADAPTER_PUSD,)


@dataclass(frozen=True)
class RedemptionMatch:
    """One redemption proven for our wallet by a finalized-or-not receipt."""

    route: str
    tx_hash: str
    block_number: int
    log_index: int  # attributable log: PayoutRedemption (direct) or adapter user event
    condition_id: str
    credited_raw: int
    credited_asset: str
    decimals: int
    index_sets: tuple[int, ...] = ()
    # Adapter route: position tokens (decimal id) our wallet surrendered -> raw amount.
    positions: dict[str, int] = field(default_factory=dict)

    @property
    def credited(self) -> Decimal:
        return Decimal(self.credited_raw) / (Decimal(10) ** self.decimals)

    @property
    def key(self) -> str:
        return f"{self.tx_hash.lower()}:{self.log_index}"

    def covers(self, condition_id: str, outcome_index: int, token_id: str) -> bool:
        if self.condition_id.lower() != condition_id.lower():
            return False
        if self.route == "ctf_direct":
            return (1 << outcome_index) in self.index_sets
        try:
            token = str(int(token_id))
        except (TypeError, ValueError):
            return False
        return self.positions.get(token, 0) > 0

    def to_dict(self) -> dict:
        return {"route": self.route, "tx_hash": self.tx_hash, "block": self.block_number,
                "log_index": self.log_index, "condition_id": self.condition_id,
                "credited_raw": self.credited_raw, "credited": str(self.credited),
                "credited_asset": self.credited_asset, "index_sets": list(self.index_sets),
                "positions": dict(self.positions)}

    @classmethod
    def from_dict(cls, data: dict) -> "RedemptionMatch":
        return cls(route=data["route"], tx_hash=data["tx_hash"], block_number=data["block"],
                   log_index=data["log_index"], condition_id=data["condition_id"],
                   credited_raw=int(data["credited_raw"]), credited_asset=data["credited_asset"],
                   decimals=int(data.get("decimals", 6)),
                   index_sets=tuple(data.get("index_sets") or ()),
                   positions={k: int(v) for k, v in (data.get("positions") or {}).items()})


class SettlementChainReader(PolygonRpcReader):
    """Read-only reader: finalized ``eth_call``, receipts, blocks, logs, transfer index."""

    allowed_methods = frozenset(
        {"eth_chainId", "eth_call", "eth_getTransactionReceipt", "eth_getBlockByNumber",
         "eth_getLogs", "alchemy_getAssetTransfers"}
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

    async def block_timestamp(self, number: int) -> int | None:
        await self._ensure_chain()
        block = await self._rpc("eth_getBlockByNumber", [hex(number), False])
        if not isinstance(block, dict) or not isinstance(block.get("timestamp"), str):
            return None
        return int(block["timestamp"], 16)

    async def logs(self, *, address: str | list[str], topics: list, from_block: int,
                   to_block: int) -> list[dict]:
        await self._ensure_chain()
        result = await self._rpc("eth_getLogs", [{"address": address, "topics": topics,
                                                  "fromBlock": hex(from_block),
                                                  "toBlock": hex(to_block)}])
        if not isinstance(result, list):
            raise PolygonRpcError("polygon_rpc_malformed")
        return result

    async def asset_transfers(self, params: dict) -> dict:
        await self._ensure_chain()
        result = await self._rpc("alchemy_getAssetTransfers", [params])
        if not isinstance(result, dict):
            raise PolygonRpcError("polygon_rpc_malformed")
        return result


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


def _address(topic: str) -> str:
    return "0x" + topic.lower()[-40:]


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
        "redeemer": _address(topics[1]),
        "collateral": _address(topics[2]),
        "parent_collection": topics[3],
        "condition_id": condition,
        "index_sets": index_sets,
        "payout": payout,
        "address": str(log.get("address") or "").lower(),
    }


def decode_erc1155_transfer(log: dict) -> dict[str, Any] | None:
    """TransferSingle / TransferBatch -> operator, from, to, {token_id: amount}."""
    topics = [t.lower() for t in log.get("topics") or []]
    if len(topics) != 4 or topics[0] not in (TRANSFER_SINGLE_TOPIC, TRANSFER_BATCH_TOPIC):
        return None
    data = str(log.get("data") or "0x")
    try:
        if topics[0] == TRANSFER_SINGLE_TOPIC:
            amounts = {str(int(_word(data, 0), 16)): int(_word(data, 1), 16)}
        else:
            ids_at, values_at = int(_word(data, 0), 16) // 32, int(_word(data, 1), 16) // 32
            count = int(_word(data, ids_at), 16)
            if int(_word(data, values_at), 16) != count:
                return None
            amounts: dict[str, int] = {}
            for i in range(count):
                token = str(int(_word(data, ids_at + 1 + i), 16))
                amounts[token] = amounts.get(token, 0) + int(_word(data, values_at + 1 + i), 16)
    except ValueError:
        return None
    return {"address": str(log.get("address") or "").lower(), "operator": _address(topics[1]),
            "from": _address(topics[2]), "to": _address(topics[3]), "amounts": amounts}


def _erc20_transfer(log: dict) -> dict[str, Any] | None:
    topics = [t.lower() for t in log.get("topics") or []]
    if len(topics) != 3 or topics[0] != ERC20_TRANSFER_TOPIC:
        return None
    try:
        value = int(_word(str(log.get("data") or "0x"), 0), 16)
    except ValueError:
        return None
    return {"address": str(log.get("address") or "").lower(), "from": _address(topics[1]),
            "to": _address(topics[2]), "value": value}


def redemption_matches(
    receipt: dict,
    *,
    wallet: str,
    contract: str = CONDITIONAL_TOKENS_ADDRESS,
    collateral: str = DEFAULT_COLLATERAL,
    decimals: int = COLLATERAL_DECIMALS,
    routes: tuple[AdapterRoute, ...] = DEFAULT_ADAPTER_ROUTES,
    rejections: list[dict] | None = None,
) -> list[RedemptionMatch]:
    """Every redemption this receipt proves for ``wallet`` (successful receipts only)."""
    if receipt.get("status") != "0x1":
        return []
    wallet, ctf, collateral = wallet.lower(), contract.lower(), collateral.lower()
    topic = payout_redemption_topic()
    tx_hash = str(receipt.get("transactionHash") or "")
    block = int(str(receipt.get("blockNumber") or "0x0"), 16)
    logs = [dict(log, logIndex=log.get("logIndex", hex(position)))
            for position, log in enumerate(receipt.get("logs") or [])]
    logs.sort(key=lambda log: int(str(log["logIndex"]), 16))
    out: list[RedemptionMatch] = []

    def reject(reason: str, log_index: int, **extra: Any) -> None:
        if rejections is not None:
            rejections.append({"tx_hash": tx_hash, "log_index": log_index, "reason": reason,
                               **extra})

    for log in logs:
        decoded = decode_payout_redemption(log)
        if decoded is None or decoded["topic0"] != topic or decoded["address"] != ctf:
            continue
        if decoded["redeemer"] == wallet and decoded["collateral"] == collateral:
            out.append(RedemptionMatch(
                route="ctf_direct", tx_hash=tx_hash, block_number=block,
                log_index=int(str(log["logIndex"]), 16),
                condition_id=decoded["condition_id"], credited_raw=decoded["payout"],
                credited_asset=collateral, decimals=decimals,
                index_sets=tuple(decoded["index_sets"])))

    for route in routes:
        adapter = route.adapter.lower()
        user_events = [
            (i, log) for i, log in enumerate(logs)
            if str(log.get("address") or "").lower() == adapter
            and [t.lower() for t in log.get("topics") or []][:1] == [route.user_event_topic]
        ]
        previous_event = -1
        for position, (i, log) in enumerate(user_events):
            start_floor = previous_event
            previous_event = i
            topics = [t.lower() for t in log["topics"]]
            if len(topics) != 3 or _address(topics[1]) != wallet:
                continue
            log_index = int(str(log["logIndex"]), 16)
            condition = topics[2]
            try:
                amount = int(_word(str(log.get("data") or "0x"), 0), 16)
            except ValueError:
                reject("ADAPTER_EVENT_MALFORMED", log_index)
                continue
            # The operation: logs after the previous user's adapter event, up to this one.
            segment = logs[start_floor + 1 : i]
            surrendered: dict[str, int] = {}
            transfers = 0
            for item in segment:
                moved = decode_erc1155_transfer(item)
                if (moved is not None and moved["address"] == ctf and moved["from"] == wallet
                        and moved["to"] == adapter and moved["operator"] == adapter):
                    transfers += 1
                    for token, value in moved["amounts"].items():
                        surrendered[token] = surrendered.get(token, 0) + value
            redemptions = [
                d for d in (decode_payout_redemption(item) for item in segment)
                if d is not None and d["topic0"] == topic and d["address"] == ctf
                and d["redeemer"] == adapter and d["condition_id"] == condition
            ]
            credits = [
                t for t in (_erc20_transfer(item) for item in segment)
                if t is not None and t["address"] == route.credited_token.lower()
                and t["to"] == wallet
            ]
            reasons = []
            if transfers == 0 or not any(surrendered.values()):
                reasons.append("NO_POSITION_SURRENDERED_BY_WALLET")
            if len(redemptions) != 1:
                reasons.append("ADAPTER_REDEMPTION_LOG_COUNT_" + str(len(redemptions)))
            elif redemptions[0]["collateral"] != collateral or redemptions[0]["payout"] != amount:
                reasons.append("ADAPTER_REDEMPTION_MISMATCH")
            if len(credits) != 1 or credits[0]["value"] != amount:
                reasons.append("CREDIT_TRANSFER_MISMATCH")
            if reasons:
                reject(";".join(reasons), log_index, route=route.name)
                continue
            out.append(RedemptionMatch(
                route=route.name, tx_hash=tx_hash, block_number=block, log_index=log_index,
                condition_id=condition, credited_raw=amount,
                credited_asset=route.credited_token.lower(), decimals=route.credited_decimals,
                index_sets=tuple(redemptions[0]["index_sets"]),
                positions={k: v for k, v in surrendered.items() if v > 0}))
    return out


def receipt_redemption_verifier(
    reader: SettlementChainReader,
    *,
    collateral: str = DEFAULT_COLLATERAL,
    decimals: int = COLLATERAL_DECIMALS,
    contract: str = CONDITIONAL_TOKENS_ADDRESS,
    routes: tuple[AdapterRoute, ...] = DEFAULT_ADAPTER_ROUTES,
):
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
        matches = [
            m for m in redemption_matches(receipt, wallet=wallet, contract=contract,
                                          collateral=collateral, decimals=decimals,
                                          routes=routes)
            if m.covers(episode.condition_id, episode.outcome_index, episode.token_id)
        ]
        if len(matches) != 1:
            reasons.append("NO_MATCHING_REDEMPTION_LOG" if not matches else "AMBIGUOUS_LOGS")
        match = matches[0] if len(matches) == 1 else None
        return RedemptionEvidence(
            attributable=not reasons,
            finalized=finalized,
            credited_amount=None if match is None else match.credited,
            reasons=tuple(reasons),
            tx_hash=tx_hash,
            block_number=block,
            source="ctf_payout_redemption_receipt" if match is None or match.route == "ctf_direct"
            else f"{match.route}_receipt",
            log_index=None if match is None else match.log_index,
            route="" if match is None else match.route,
            credited_asset="" if match is None else match.credited_asset,
        )

    return verify
