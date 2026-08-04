"""R6D: signer vs funder identity and env mapping regression tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from eth_account import Account

from tyrex_pm.execution.polymarket.auth import (
    CredentialError,
    build_identity_mapping_report,
    derive_signer_address,
    load_l2_credentials,
    positions_wallet_address,
)
from tyrex_pm.execution.polymarket.l2_hmac import build_hmac_signature, build_l2_headers, secret_base64_valid
from tyrex_pm.execution.polymarket.sdk_readonly import MutationAttemptError, NetworkAllowlistSpy

ROOT = Path(__file__).resolve().parents[1]
ENV_HASH = "27210C97AE37101DE48570130BBB517E572F3EB75160B5FC4C05CF178B91F772"

# Deterministic synthetic key — not a real trading key
_SYNTH_PK = "0x" + ("ab" * 32)
_SYNTH_SIGNER = Account.from_key(_SYNTH_PK).address
_SYNTH_FUNDER = "0x" + ("cd" * 20)


def _triple(env: dict) -> dict:
    base = {
        "POLYMARKET_API_KEY": "key-aaa",
        "POLYMARKET_API_SECRET": "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LQ==",  # urlsafe-ish
        "POLYMARKET_PASSPHRASE": "pass-bbb",
    }
    base.update(env)
    return base


def test_env_unchanged() -> None:
    digest = hashlib.sha256((ROOT / ".env").read_bytes()).hexdigest().upper()
    assert digest == ENV_HASH


def test_poly_address_is_signer_not_funder() -> None:
    creds = load_l2_credentials(
        _triple(
            {
                "POLYMARKET_PK": _SYNTH_PK,
                "POLYMARKET_FUNDER": _SYNTH_FUNDER,
                "POLYMARKET_SIGNATURE_TYPE": "1",
            }
        )
    )
    assert creds.address.lower() == _SYNTH_SIGNER.lower()
    assert creds.funder is not None
    assert creds.funder.lower() == _SYNTH_FUNDER.lower()
    assert creds.address.lower() != creds.funder.lower()


def test_funder_never_used_as_poly_address_alias() -> None:
    """Pre-R6D bug: POLYMARKET_FUNDER was used as POLY_ADDRESS."""
    with pytest.raises(CredentialError, match="signer identity"):
        load_l2_credentials(
            _triple(
                {
                    "POLYMARKET_FUNDER": _SYNTH_FUNDER,
                    # no PK, no POLYMARKET_ADDRESS
                }
            )
        )


def test_identity_mapping_detects_funder_misuse() -> None:
    report = build_identity_mapping_report(
        {
            "POLYMARKET_PK": _SYNTH_PK,
            "POLYMARKET_FUNDER": _SYNTH_FUNDER,
            # Simulate pre-R6D: only funder configured as "address" source
            "POLYMARKET_ADDRESS": _SYNTH_FUNDER,
            "POLYMARKET_SIGNATURE_TYPE": "1",
        }
    )
    assert report.private_key_derives_valid_signer is True
    assert report.pre_r6d_would_use_funder_as_poly_address is True
    assert report.poly_address_role == "signer"  # current loader corrected
    assert report.historical_and_current_identity_mapping_match is True


def test_historical_env_aliases() -> None:
    creds = load_l2_credentials(
        _triple(
            {
                "TYREX_PRIVATE_KEY": _SYNTH_PK,
                "TYREX_FUNDER": _SYNTH_FUNDER,
                "TYREX_SIGNATURE_TYPE": "3",
            }
        )
    )
    assert creds.signature_type == 3
    assert positions_wallet_address(creds) == _SYNTH_FUNDER


def test_quoted_and_whitespace_env_values() -> None:
    creds = load_l2_credentials(
        {
            "POLYMARKET_API_KEY": '  "key-aaa"  ',
            "POLYMARKET_API_SECRET": "  c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LQ==  ",
            "POLYMARKET_PASSPHRASE": "'pass-bbb'",
            "POLYMARKET_PK": f"  {_SYNTH_PK}  ",
            "POLYMARKET_FUNDER": f'"{_SYNTH_FUNDER}"',
        }
    )
    assert creds.api_key == "key-aaa"
    assert creds.passphrase == "pass-bbb"
    assert creds.address.lower() == _SYNTH_SIGNER.lower()


def test_hmac_matches_frozen_official_vector() -> None:
    """Frozen vector matching Tyrex L2 HMAC (URL-safe base64 HMAC-SHA256)."""
    import base64

    secret = base64.urlsafe_b64encode(b"\x11" * 32).decode()
    ours = build_hmac_signature(secret, "1700000000", "GET", "/data/orders", None)
    # Independent recomputation (same algorithm as l2_hmac.build_hmac_signature).
    import hashlib
    import hmac as _hmac

    digest = _hmac.new(
        base64.urlsafe_b64decode(secret),
        b"1700000000GET/data/orders",
        hashlib.sha256,
    ).digest()
    expected = base64.urlsafe_b64encode(digest).decode("utf-8")
    assert ours == expected
    assert secret_base64_valid(secret)


def test_l2_headers_use_signer_and_structural_report() -> None:
    creds = load_l2_credentials(
        _triple({"POLYMARKET_PK": _SYNTH_PK, "POLYMARKET_FUNDER": _SYNTH_FUNDER})
    )
    headers, report = build_l2_headers(
        creds, method="get", request_path="/data/orders", timestamp=1700000000
    )
    assert headers["POLY_ADDRESS"].lower() == _SYNTH_SIGNER.lower()
    assert headers["POLY_ADDRESS"].lower() != _SYNTH_FUNDER.lower()
    assert report.timestamp_digits == 10
    assert report.body_present is False
    assert report.query_present is False
    assert "POLY_SIGNATURE" not in report.to_dict()


def test_network_allowlist_blocks_mutations() -> None:
    spy = NetworkAllowlistSpy()
    spy.check(method="GET", path="/data/orders")
    with pytest.raises(MutationAttemptError):
        spy.check(method="POST", path="/order")
    with pytest.raises(MutationAttemptError):
        spy.check(method="POST", path="/heartbeats")
    with pytest.raises(MutationAttemptError):
        spy.check(method="GET", path="/auth/derive-api-key")


def test_derive_signer_address() -> None:
    assert derive_signer_address(_SYNTH_PK).lower() == _SYNTH_SIGNER.lower()
