"""R6C dry signing vectors — never submit, never use real private keys in logs."""

from __future__ import annotations

import json

from tyrex_pm.execution.polymarket.signing_dry import (
    CLOB_V2_DOMAIN,
    build_unsigned_order_payload,
    default_synthetic_order,
    dry_sign_hmac_l2_message,
    validate_dry_signing_vector,
)


def test_v2_domain_and_schema() -> None:
    report = validate_dry_signing_vector()
    assert report["ok"]
    assert report["domain"] == CLOB_V2_DOMAIN
    assert report["chain_id"] == 137
    assert report["version"] == "2"
    assert report["would_send"] is False
    assert report["real_private_key_used"] is False


def test_serialized_request_shape() -> None:
    payload = build_unsigned_order_payload(default_synthetic_order())
    assert payload["transport"]["would_send"] is False
    assert payload["transport"]["path"] == "/order"
    assert set(payload["order"]) >= {
        "tokenId",
        "price",
        "size",
        "side",
        "signatureType",
        "funder",
    }


def test_hmac_deterministic_and_secret_redacted() -> None:
    import base64

    secret = base64.urlsafe_b64encode(b"\x33" * 32).decode()
    a = dry_sign_hmac_l2_message(
        secret_b64=secret, timestamp="1", method="GET", path="/data/orders"
    )
    b = dry_sign_hmac_l2_message(
        secret_b64=secret, timestamp="1", method="GET", path="/data/orders"
    )
    assert a == b
    report = validate_dry_signing_vector()
    blob = json.dumps(report)
    assert secret not in blob
    assert "POLY_SIGNATURE" not in blob
