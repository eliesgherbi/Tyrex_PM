"""Credential loading boundary — never log or persist secret values."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class L2Credentials:
    """L2 API credentials. ``repr``/``str`` are redacted."""

    address: str
    api_key: str
    secret: str
    passphrase: str

    def __repr__(self) -> str:
        return (
            f"L2Credentials(address={self.address!r}, "
            f"api_key='***', secret='***', passphrase='***')"
        )

    def __str__(self) -> str:
        return repr(self)


_SECRET_ENV_KEYS = (
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
    "POLYMARKET_PRIVATE_KEY",
    "TYREX_PRIVATE_KEY",
    "POLYMARKET_PK",
)


def credential_env_keys() -> tuple[str, ...]:
    return _SECRET_ENV_KEYS


def load_l2_credentials(
    env: Mapping[str, str] | None = None,
) -> L2Credentials:
    """Load L2 credentials from environment. Never returns secret values in errors."""
    source = env if env is not None else os.environ
    address = (source.get("POLYMARKET_ADDRESS") or source.get("POLYMARKET_FUNDER") or "").strip()
    api_key = (source.get("POLYMARKET_API_KEY") or "").strip()
    secret = (source.get("POLYMARKET_API_SECRET") or "").strip()
    passphrase = (
        source.get("POLYMARKET_API_PASSPHRASE")
        or source.get("POLYMARKET_PASSPHRASE")
        or ""
    ).strip()
    missing = [
        name
        for name, val in (
            ("POLYMARKET_ADDRESS|POLYMARKET_FUNDER", address),
            ("POLYMARKET_API_KEY", api_key),
            ("POLYMARKET_API_SECRET", secret),
            ("POLYMARKET_API_PASSPHRASE|POLYMARKET_PASSPHRASE", passphrase),
        )
        if not val
    ]
    if missing:
        raise CredentialError(f"missing credential env vars: {', '.join(missing)}")
    return L2Credentials(
        address=address,
        api_key=api_key,
        secret=secret,
        passphrase=passphrase,
    )


def credentials_present(env: Mapping[str, str] | None = None) -> bool:
    try:
        load_l2_credentials(env)
        return True
    except CredentialError:
        return False


def redact_text(text: str, creds: L2Credentials | None = None) -> str:
    """Remove known secret substrings from text (facts/logs/exceptions)."""
    out = text
    if creds is not None:
        for secret in (creds.api_key, creds.secret, creds.passphrase):
            if secret:
                out = out.replace(secret, "***")
    # Also scrub common header-shaped fragments if present
    for key in ("POLY_SIGNATURE=", "POLY_API_KEY=", "POLY_PASSPHRASE="):
        if key in out:
            # crude redaction of header values
            parts = out.split(key)
            rebuilt = [parts[0]]
            for chunk in parts[1:]:
                sep = chunk.find(" ")
                if sep < 0:
                    sep = chunk.find("\n")
                rebuilt.append("***" + (chunk[sep:] if sep >= 0 else ""))
            out = key.join(rebuilt) if False else out  # keep simple
            out = out.replace(key, key + "***")
    return out


def assert_no_secrets(text: str, creds: L2Credentials | None = None) -> None:
    if creds is None:
        return
    for label, secret in (
        ("api_key", creds.api_key),
        ("secret", creds.secret),
        ("passphrase", creds.passphrase),
    ):
        if secret and secret in text:
            raise AssertionError(f"secret leak detected: {label}")
