"""Thin Tyrex boundary over official ``polymarket`` Secure / AsyncSecure clients.

Existing L2 credentials are injected — never create/derive API keys at runtime.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.execution.polymarket.auth import (
    CredentialError,
    L2Credentials,
    _funder_from_env,
    _private_key_from_env,
    _signature_type_from_env,
    _strip_env,
    load_l2_credentials,
)


def _wallet_address(source: Mapping[str, str], creds: L2Credentials) -> str | None:
    """Funder/proxy address when present; else None (SDK defaults to deposit wallet)."""
    funder = creds.funder or _funder_from_env(source) or None
    return funder


def build_secure_client(
    *,
    env: dict[str, str] | None = None,
    creds: L2Credentials | None = None,
    validate_credentials: bool = True,
) -> Any:
    """Build official SecureClient with pre-existing API creds only (no derive)."""
    try:
        from polymarket import PRODUCTION, SecureClient
        from polymarket.models.clob.api_key import ApiKeyCreds
    except ImportError as exc:  # pragma: no cover
        raise CredentialError("polymarket-client not installed") from exc

    source = env if env is not None else dict(os.environ)
    loaded = creds or load_l2_credentials(source)
    pk = _private_key_from_env(source)
    if not pk:
        raise CredentialError("official client requires TYREX_PRIVATE_KEY|POLYMARKET_PK")
    if not pk.startswith("0x"):
        pk = "0x" + pk

    chain = int(_strip_env(source.get("TYREX_CHAIN_ID")) or "137")
    if chain != 137:
        # Production environment is pinned to Polygon mainnet in the SDK.
        raise CredentialError(f"unsupported_chain_id:{chain}")

    api_creds = ApiKeyCreds(
        key=loaded.api_key,
        secret=loaded.secret,
        passphrase=loaded.passphrase,
    )
    wallet = _wallet_address(source, loaded)
    # Signature type is encoded by acting-as wallet (funder/proxy) in the unified SDK.
    _ = _signature_type_from_env(source)

    if validate_credentials:
        return SecureClient.create(
            private_key=pk,
            wallet=wallet,
            environment=PRODUCTION,
            credentials=api_creds,
        )
    # Internal path for deterministic tests that inject a fake client instead.
    return SecureClient.create(
        private_key=pk,
        wallet=wallet,
        environment=PRODUCTION,
        credentials=api_creds,
    )


async def build_async_secure_client(
    *,
    env: dict[str, str] | None = None,
    creds: L2Credentials | None = None,
) -> Any:
    """Await ``AsyncSecureClient.create`` exactly once; return an initialized client.

    Callers must ``await`` this function. It never returns a bare coroutine factory
    result and never bridges event loops.
    """
    try:
        from polymarket import PRODUCTION, AsyncSecureClient
        from polymarket.models.clob.api_key import ApiKeyCreds
    except ImportError as exc:  # pragma: no cover
        raise CredentialError("polymarket-client not installed") from exc

    source = env if env is not None else dict(os.environ)
    loaded = creds or load_l2_credentials(source)
    pk = _private_key_from_env(source)
    if not pk:
        raise CredentialError("official client requires TYREX_PRIVATE_KEY|POLYMARKET_PK")
    if not pk.startswith("0x"):
        pk = "0x" + pk
    api_creds = ApiKeyCreds(
        key=loaded.api_key,
        secret=loaded.secret,
        passphrase=loaded.passphrase,
    )
    client = await AsyncSecureClient.create(
        private_key=pk,
        wallet=_wallet_address(source, loaded),
        environment=PRODUCTION,
        credentials=api_creds,
    )
    return client


def micro_to_decimal(raw: int | str | Decimal | None, *, scale: int = 6) -> Decimal:
    if raw is None:
        return Decimal("0")
    return Decimal(str(raw)) / (Decimal(10) ** scale)


def balance_allowance_to_decimal(
    raw: Any,
    *,
    conditional: bool,
) -> tuple[Decimal, Decimal | None]:
    """Translate SDK BalanceAllowance ints into share / USDC decimals."""
    if raw is None:
        return Decimal("0"), None
    bal_raw = getattr(raw, "balance", 0)
    allowances = getattr(raw, "allowances", None) or {}
    # Unified SDK returns integer micro-units for both collateral and conditional.
    bal = micro_to_decimal(bal_raw)
    allowance: Decimal | None = None
    if isinstance(allowances, dict) and allowances:
        first = next(iter(allowances.values()))
        allowance = micro_to_decimal(first)
    _ = conditional  # same scale today; kept for call-site clarity
    return bal, allowance
