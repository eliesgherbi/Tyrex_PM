"""Explicit strategy-level token universe (optional allowlist)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TokenFilterSpec:
    """
    When ``enabled`` is False, all non-empty guru token ids pass the filter stage.

    When ``enabled`` is True, only ids in ``allowlisted`` pass (fail-closed vs missing token).
    """

    enabled: bool
    allowlisted: frozenset[str]

    def allows_token(self, token_id: str | None) -> bool:
        if not token_id:
            return False
        if not self.enabled:
            return True
        return token_id in self.allowlisted
