"""Session-scoped protection registry."""

from __future__ import annotations

from tyrex_pm.protection.state import ArmedProtection, ProtectionPhase


class ProtectionRegistry:
    def __init__(self) -> None:
        self._by_session: dict[str, ArmedProtection] = {}

    def get(self, session_id: str) -> ArmedProtection | None:
        return self._by_session.get(session_id)

    def put(self, armed: ArmedProtection) -> None:
        self._by_session[armed.session_id] = armed

    def disarm(self, session_id: str) -> None:
        armed = self._by_session.get(session_id)
        if armed is None:
            return
        armed.phase = ProtectionPhase.DISARMED

    def clear(self, session_id: str) -> None:
        self._by_session.pop(session_id, None)

    def clear_all(self) -> None:
        self._by_session.clear()
