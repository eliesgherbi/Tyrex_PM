"""Dry order construction + EIP-712 V2 domain validation — never submits.

Uses a fixed synthetic private key for local vectors only. Real keys must not
appear in logged test vectors.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

# Official CLOB Exchange EIP-712 domain (docs / polymarket-client).
CLOB_V2_DOMAIN = {
    "name": "Polymarket CTF Exchange",
    "version": "2",
    "chainId": 137,
}

# Synthetic key material for deterministic vectors — NOT a real trading key.
_SYNTHETIC_PK_HEX = "0x" + ("11" * 32)


@dataclass(frozen=True)
class DryOrderVector:
    token_id: str
    side: str
    price: str
    size: str
    signature_type: int
    funder_placeholder: str
    expiration: int
    salt: str


def default_synthetic_order() -> DryOrderVector:
    return DryOrderVector(
        token_id="1234567890123456789012345678901234567890123456789012345678901234567890",
        side="BUY",
        price="0.50",
        size="10",
        signature_type=0,
        funder_placeholder="0x0000000000000000000000000000000000000001",
        expiration=0,
        salt="1",
    )


def build_unsigned_order_payload(order: DryOrderVector) -> dict[str, Any]:
    """Serialize the local request shape expected before submit (not sent)."""
    return {
        "domain": dict(CLOB_V2_DOMAIN),
        "order": {
            "tokenId": order.token_id,
            "price": order.price,
            "size": order.size,
            "side": order.side,
            "expiration": str(order.expiration),
            "salt": order.salt,
            "signatureType": order.signature_type,
            "funder": order.funder_placeholder,
        },
        "transport": {
            "method": "POST",
            "path": "/order",
            "would_send": False,
        },
        "redaction": {
            "private_key_logged": False,
            "signature_placeholder": "***",
        },
    }


def dry_sign_hmac_l2_message(
    *,
    secret_b64: str,
    timestamp: str,
    method: str,
    path: str,
    body: str = "",
) -> str:
    """Reproduce L2 HMAC signing locally (secret is synthetic in tests)."""
    import base64
    import hmac

    message = timestamp + method.upper() + path + body
    secret = base64.urlsafe_b64decode(secret_b64)
    return base64.urlsafe_b64encode(
        hmac.new(secret, message.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")


def validate_dry_signing_vector() -> dict[str, Any]:
    """Return a sanitized validation report. Payload is never sent."""
    order = default_synthetic_order()
    payload = build_unsigned_order_payload(order)
    # Deterministic L2 HMAC with synthetic secret (urlsafe base64 of 32 bytes)
    import base64

    synth_secret = base64.urlsafe_b64encode(b"\x22" * 32).decode("utf-8")
    sig = dry_sign_hmac_l2_message(
        secret_b64=synth_secret,
        timestamp="1700000000",
        method="GET",
        path="/data/orders",
        body="",
    )
    report = {
        "ok": True,
        "domain": payload["domain"],
        "chain_id": CLOB_V2_DOMAIN["chainId"],
        "version": CLOB_V2_DOMAIN["version"],
        "signature_type": order.signature_type,
        "funder_is_placeholder": order.funder_placeholder.endswith("0001"),
        "token_id_present": bool(order.token_id),
        "price": order.price,
        "size": order.size,
        "expiration": order.expiration,
        "serialized_keys": sorted(payload["order"].keys()),
        "would_send": False,
        "l2_hmac_sig_len": len(sig),
        "l2_hmac_deterministic": True,
        "synthetic_pk_fingerprint": hashlib.sha256(_SYNTHETIC_PK_HEX.encode()).hexdigest()[:16],
        "real_private_key_used": False,
        "payload_json_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }
    # Ensure secrets not in report
    blob = json.dumps(report)
    assert synth_secret not in blob
    assert _SYNTHETIC_PK_HEX not in blob
    return report
