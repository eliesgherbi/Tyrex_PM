"""Credential and identity loading — never log or persist secret values.

R6D: ``POLY_ADDRESS`` is the **signer EOA** derived from the private key
(historical ``old/…/clob_env.try_create_clob_client`` + official V2 SDK).
The funder/deposit wallet is a separate field and must never be used as
``POLY_ADDRESS``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping


class CredentialError(RuntimeError):
    pass


def _strip_env(value: str | None) -> str:
    """Strip whitespace and optional surrounding quotes (historical .env hygiene)."""
    if value is None:
        return ""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return text


def derive_signer_address(private_key: str) -> str:
    """Derive checksummed EOA from a private key. Never log the key."""
    pk = _strip_env(private_key)
    if not pk:
        raise CredentialError("private key empty")
    if not pk.startswith("0x"):
        pk = "0x" + pk
    try:
        from eth_account import Account
    except ImportError as exc:  # pragma: no cover
        raise CredentialError("eth_account required to derive signer address") from exc
    try:
        return Account.from_key(pk).address
    except Exception as exc:  # noqa: BLE001
        raise CredentialError(
            f"private_key_derives_valid_signer=false ({type(exc).__name__})"
        ) from None


@dataclass(frozen=True)
class L2Credentials:
    """L2 API credentials + identity roles. ``repr``/``str`` are redacted."""

    address: str  # signer EOA — used as POLY_ADDRESS
    api_key: str
    secret: str
    passphrase: str
    funder: str | None = None
    signature_type: int = 0
    private_key_present: bool = False

    def __repr__(self) -> str:
        return (
            "L2Credentials("
            "address='***', api_key='***', secret='***', passphrase='***', "
            f"funder_present={bool(self.funder)}, signature_type={self.signature_type}, "
            f"private_key_present={self.private_key_present})"
        )

    def __str__(self) -> str:
        return repr(self)


@dataclass(frozen=True)
class IdentityMappingReport:
    """Sanitized identity comparison — no addresses."""

    private_key_derives_valid_signer: bool
    configured_poly_address_matches_signer: bool
    configured_poly_address_matches_funder: bool
    signer_equals_funder: bool
    funder_present: bool
    signature_type_present: bool
    historical_and_current_identity_mapping_match: bool
    poly_address_role: str  # current role used for POLY_ADDRESS
    pre_r6d_would_use_funder_as_poly_address: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "private_key_derives_valid_signer": self.private_key_derives_valid_signer,
            "configured_poly_address_matches_signer": self.configured_poly_address_matches_signer,
            "configured_poly_address_matches_funder": self.configured_poly_address_matches_funder,
            "signer_equals_funder": self.signer_equals_funder,
            "funder_present": self.funder_present,
            "signature_type_present": self.signature_type_present,
            "historical_and_current_identity_mapping_match": (
                self.historical_and_current_identity_mapping_match
            ),
            "poly_address_role": self.poly_address_role,
            "pre_r6d_would_use_funder_as_poly_address": (
                self.pre_r6d_would_use_funder_as_poly_address
            ),
        }


_SECRET_ENV_KEYS = (
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
    "POLYMARKET_PASSPHRASE",
    "POLYMARKET_PRIVATE_KEY",
    "TYREX_PRIVATE_KEY",
    "POLYMARKET_PK",
)


def credential_env_keys() -> tuple[str, ...]:
    return _SECRET_ENV_KEYS


def _private_key_from_env(source: Mapping[str, str]) -> str:
    # Historical precedence: TYREX_PRIVATE_KEY > POLYMARKET_PK
    return _strip_env(source.get("TYREX_PRIVATE_KEY")) or _strip_env(source.get("POLYMARKET_PK"))


def _funder_from_env(source: Mapping[str, str]) -> str:
    return _strip_env(source.get("TYREX_FUNDER")) or _strip_env(source.get("POLYMARKET_FUNDER"))


def _signature_type_from_env(source: Mapping[str, str]) -> int | None:
    raw = _strip_env(source.get("TYREX_SIGNATURE_TYPE")) or _strip_env(
        source.get("POLYMARKET_SIGNATURE_TYPE")
    )
    if not raw:
        return None
    return int(raw)


def build_identity_mapping_report(env: Mapping[str, str] | None = None) -> IdentityMappingReport:
    """Compare signer/funder roles without exposing addresses."""
    source = env if env is not None else os.environ
    pk = _private_key_from_env(source)
    funder = _funder_from_env(source)
    explicit_address_only = _strip_env(source.get("POLYMARKET_ADDRESS"))
    sig_t = _signature_type_from_env(source)

    signer: str | None = None
    pk_ok = False
    if pk:
        try:
            signer = derive_signer_address(pk)
            pk_ok = True
        except CredentialError:
            pk_ok = False

    # What pre-R6D would have put in POLY_ADDRESS (ADDRESS alias or FUNDER)
    pre_r6d_poly = explicit_address_only or funder
    pre_r6d_is_signer = bool(signer and pre_r6d_poly and pre_r6d_poly.lower() == signer.lower())
    pre_r6d_is_funder = bool(
        funder and pre_r6d_poly and pre_r6d_poly.lower() == funder.lower() and not pre_r6d_is_signer
    )
    signer_eq_funder = bool(signer and funder and signer.lower() == funder.lower())

    # Current loader: POLY_ADDRESS = signer when PK present
    if pk_ok and signer:
        role = "signer"
        hist_match = True  # matches historical SDK + old/clob_env
    elif explicit_address_only and not pk:
        role = "signer_override"
        hist_match = True
    else:
        role = "missing"
        hist_match = False

    return IdentityMappingReport(
        private_key_derives_valid_signer=pk_ok,
        configured_poly_address_matches_signer=pre_r6d_is_signer,
        configured_poly_address_matches_funder=pre_r6d_is_funder,
        signer_equals_funder=signer_eq_funder,
        funder_present=bool(funder),
        signature_type_present=sig_t is not None,
        historical_and_current_identity_mapping_match=hist_match,
        poly_address_role=role,
        pre_r6d_would_use_funder_as_poly_address=pre_r6d_is_funder,
    )


def load_l2_credentials(
    env: Mapping[str, str] | None = None,
) -> L2Credentials:
    """Load L2 credentials. ``address`` is always the signer for POLY_ADDRESS."""
    source = env if env is not None else os.environ
    api_key = _strip_env(source.get("POLYMARKET_API_KEY"))
    secret = _strip_env(source.get("POLYMARKET_API_SECRET"))
    passphrase = _strip_env(source.get("POLYMARKET_API_PASSPHRASE")) or _strip_env(
        source.get("POLYMARKET_PASSPHRASE")
    )
    pk = _private_key_from_env(source)
    funder = _funder_from_env(source) or None
    sig_t = _signature_type_from_env(source)
    # Explicit POLYMARKET_ADDRESS is allowed only as a *signer* override for tests
    # when no private key is available — never as a funder alias.
    explicit_signer = _strip_env(source.get("POLYMARKET_ADDRESS"))

    missing = [
        name
        for name, val in (
            ("POLYMARKET_API_KEY", api_key),
            ("POLYMARKET_API_SECRET", secret),
            ("POLYMARKET_API_PASSPHRASE|POLYMARKET_PASSPHRASE", passphrase),
        )
        if not val
    ]
    if missing:
        raise CredentialError(f"missing credential env vars: {', '.join(missing)}")

    if pk:
        address = derive_signer_address(pk)
    elif explicit_signer:
        # Test/synthetic path — must not be the funder when funder differs
        if funder and explicit_signer.lower() == funder.lower():
            # Ambiguous: treat as signer==funder EOA mode only when equal
            address = explicit_signer
        else:
            address = explicit_signer
    else:
        raise CredentialError(
            "missing signer identity: set TYREX_PRIVATE_KEY|POLYMARKET_PK "
            "(preferred) or POLYMARKET_ADDRESS for synthetic tests"
        )

    return L2Credentials(
        address=address,
        api_key=api_key,
        secret=secret,
        passphrase=passphrase,
        funder=funder,
        signature_type=0 if sig_t is None else sig_t,
        private_key_present=bool(pk),
    )


def credentials_present(env: Mapping[str, str] | None = None) -> bool:
    try:
        load_l2_credentials(env)
        return True
    except CredentialError:
        return False


def positions_wallet_address(creds: L2Credentials) -> str:
    """Data API positions address: funder preferred (historical), else signer."""
    if creds.funder:
        return creds.funder
    return creds.address


def redact_text(text: str, creds: L2Credentials | None = None) -> str:
    """Remove known secret substrings from text (facts/logs/exceptions)."""
    out = text
    if creds is not None:
        for secret in (creds.api_key, creds.secret, creds.passphrase):
            if secret:
                out = out.replace(secret, "***")
        if creds.address:
            out = out.replace(creds.address, "0x***")
        if creds.funder:
            out = out.replace(creds.funder, "0x***")
    for key in ("POLY_SIGNATURE=", "POLY_API_KEY=", "POLY_PASSPHRASE=", "POLY_ADDRESS="):
        if key in out:
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
    if creds.address and creds.address in text:
        raise AssertionError("secret leak detected: address")
    if creds.funder and creds.funder in text:
        raise AssertionError("secret leak detected: funder")
