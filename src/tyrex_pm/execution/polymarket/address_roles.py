"""Signer EOA vs funder/proxy address discipline (R7C.1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tyrex_pm.execution.polymarket.auth import L2Credentials, positions_wallet_address


class AddressRoleError(RuntimeError):
    pass


@dataclass(frozen=True)
class AddressRoles:
    signer_eoa: str
    funder_proxy: str | None
    signature_type: int
    positions_wallet: str

    @property
    def proxy_mode(self) -> bool:
        return self.signature_type == 1 and bool(self.funder_proxy)

    @property
    def conditional_owner(self) -> str:
        """Wallet that owns conditional tokens / must be queried for balance."""
        if self.proxy_mode:
            assert self.funder_proxy is not None
            return self.funder_proxy
        return self.funder_proxy or self.signer_eoa

    def assert_conditional_query_target(self, queried_as: str | None = None) -> None:
        """Refuse signer-as-owner substitution when signature_type=1."""
        if self.proxy_mode:
            if not self.funder_proxy:
                raise AddressRoleError("FUNDER_REQUIRED_FOR_SIGNATURE_TYPE_1")
            if queried_as and queried_as.lower() == self.signer_eoa.lower():
                if queried_as.lower() != self.funder_proxy.lower():
                    raise AddressRoleError(
                        "REFUSING_SIGNER_CONDITIONAL_BALANCE_WHEN_PROXY_MODE"
                    )

    def to_safe_dict(self) -> dict[str, Any]:
        def fp(a: str | None) -> str | None:
            if not a:
                return None
            x = a.lower()
            return f"{x[:6]}…{x[-4:]}" if len(x) >= 10 else "***"

        return {
            "signer_eoa_fp": fp(self.signer_eoa),
            "funder_proxy_fp": fp(self.funder_proxy),
            "positions_wallet_fp": fp(self.positions_wallet),
            "conditional_owner_fp": fp(self.conditional_owner),
            "signature_type": self.signature_type,
            "proxy_mode": self.proxy_mode,
            "signer_eq_funder": (
                bool(self.funder_proxy)
                and self.signer_eoa.lower() == self.funder_proxy.lower()
            ),
        }


def roles_from_credentials(creds: L2Credentials) -> AddressRoles:
    return AddressRoles(
        signer_eoa=creds.address,
        funder_proxy=creds.funder,
        signature_type=int(creds.signature_type or 0),
        positions_wallet=positions_wallet_address(creds),
    )
