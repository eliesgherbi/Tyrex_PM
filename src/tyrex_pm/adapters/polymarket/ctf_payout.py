"""Read-only standard-binary payouts from the Polygon Conditional Tokens contract.

The address is the Conditional Tokens (CTF) entry published at
https://docs.polymarket.com/developers/CTF/deployment-resources
(Polygon chain id 137): ``0x4D97DCd97eC945f40cF65F87097ACe5EA0476045``.

This module calls ``payoutDenominator(bytes32)`` and
``payoutNumerators(bytes32,uint256)`` through an injected ``eth_call``.
It does not sign, redeem, or open a network connection by itself.
Official docs say neg-risk positions also live on this contract. Startup still
refuses them until the held token's outcome slot is proven.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Awaitable, Callable

POLYGON_CHAIN_ID = 137
CONDITIONAL_TOKENS_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
# keccak256("payoutDenominator(bytes32)")[:4]
PAYOUT_DENOMINATOR_SELECTOR = bytes.fromhex("dd34de67")
# keccak256("payoutNumerators(bytes32,uint256)")[:4]
PAYOUT_NUMERATOR_SELECTOR = bytes.fromhex("0504c814")

EthCall = Callable[..., Awaitable[str]]


def condition_id_word(condition_id: str) -> bytes | None:
    text = str(condition_id).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if len(text) != 64:
        return None
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def encode_call(selector: bytes, *words: bytes) -> str:
    return "0x" + selector.hex() + b"".join(words).hex()


def decode_uint256(raw: str) -> int | None:
    text = str(raw).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not text or len(text) > 64:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


async def read_binary_payout(
    eth_call: EthCall,
    *,
    condition_id: str,
    outcome_index: int,
    contract: str = CONDITIONAL_TOKENS_ADDRESS,
) -> Decimal | None:
    """Return numerator/denominator for outcome 0 or 1, or None when unresolved.

    A zero denominator means the condition is not finalized. Values above 1
    are rejected. This does not interpret price, ``closed``, UMA status, or
    ``redeemable`` as a payout.
    """
    word = condition_id_word(condition_id)
    if word is None or outcome_index not in (0, 1):
        return None
    denominator_raw = await eth_call(
        to=contract,
        data=encode_call(PAYOUT_DENOMINATOR_SELECTOR, word),
    )
    denominator = decode_uint256(denominator_raw)
    if denominator is None or denominator <= 0:
        return None
    numerator_raw = await eth_call(
        to=contract,
        data=encode_call(
            PAYOUT_NUMERATOR_SELECTOR,
            word,
            outcome_index.to_bytes(32, "big"),
        ),
    )
    numerator = decode_uint256(numerator_raw)
    if numerator is None or numerator > denominator:
        return None
    return Decimal(numerator) / Decimal(denominator)
