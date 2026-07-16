"""R6 credential loading and redaction."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tyrex_pm.execution.polymarket.auth import (
    CredentialError,
    assert_no_secrets,
    credentials_present,
    load_l2_credentials,
    redact_text,
)

ROOT = Path(__file__).resolve().parents[1]
ENV_HASH = "27210C97AE37101DE48570130BBB517E572F3EB75160B5FC4C05CF178B91F772"


def test_env_unchanged() -> None:
    digest = hashlib.sha256((ROOT / ".env").read_bytes()).hexdigest().upper()
    assert digest == ENV_HASH


def test_env_example_sanitized() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "sk-" not in text.lower()
    assert "POLYMARKET_API_SECRET=" not in text or "your_" in text.lower()
    # No real private-key material
    assert "BEGIN PRIVATE" not in text


def test_missing_credentials_fail_closed() -> None:
    with pytest.raises(CredentialError, match="missing credential"):
        load_l2_credentials(env={})
    assert credentials_present(env={}) is False


def test_secrets_absent_from_repr_and_redaction() -> None:
    creds = load_l2_credentials(
        env={
            "POLYMARKET_ADDRESS": "0xabc",
            "POLYMARKET_API_KEY": "key-secret-value",
            "POLYMARKET_API_SECRET": "super-secret-hmac",
            "POLYMARKET_API_PASSPHRASE": "pass-secret-value",
        }
    )
    r = repr(creds)
    assert "key-secret-value" not in r
    assert "super-secret-hmac" not in r
    assert "pass-secret-value" not in r
    leaked = f"header POLY_API_KEY={creds.api_key} body={creds.secret}"
    cleaned = redact_text(leaked, creds)
    assert_no_secrets(cleaned, creds)
    with pytest.raises(AssertionError, match="secret leak"):
        assert_no_secrets(leaked, creds)
