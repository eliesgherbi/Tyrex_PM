from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class PolymarketAuth:
    """Non-secret config; secrets from env only."""

    api_key: str | None = None
    api_secret: str | None = None
    passphrase: str | None = None

    @staticmethod
    def from_env() -> PolymarketAuth:
        return PolymarketAuth(
            api_key=os.environ.get("POLYMARKET_API_KEY"),
            api_secret=os.environ.get("POLYMARKET_API_SECRET"),
            passphrase=os.environ.get("POLYMARKET_PASSPHRASE"),
        )
