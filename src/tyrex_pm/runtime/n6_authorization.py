"""Explicit lower-level mutation authorization for N6 deterministic tests.

Real venue mutations remain structurally disabled in N6 operator paths.
Fake-transport acceptance may construct this token to exercise LiveOMS
against ``FakeTransport`` only — never against SdkMutationTransport.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True, kw_only=True)
class MutationAuthorization:
    """Capability object required in addition to ``live.mutations_enabled``.

    ``transport_kind`` must be ``fake`` for N6 acceptance. Real SDK mutation
    transports refuse this token.
    """

    reason: str
    transport_kind: str  # "fake" only in N6
    issued_at: datetime
    allows_real_venue_mutation: bool = False

    def __post_init__(self) -> None:
        if self.allows_real_venue_mutation:
            raise ValueError(
                "N6 MutationAuthorization must not allow real venue mutation"
            )
        if self.transport_kind != "fake":
            raise ValueError(
                "N6 MutationAuthorization transport_kind must be 'fake'"
            )

    @classmethod
    def for_fake_transport(cls, *, reason: str = "n6_deterministic_acceptance") -> "MutationAuthorization":
        return cls(
            reason=reason,
            transport_kind="fake",
            issued_at=datetime.now(timezone.utc),
            allows_real_venue_mutation=False,
        )
