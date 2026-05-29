from __future__ import annotations

from typing import Any, Protocol

from tyrex_pm.core.models import Intent
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.signals.base import GuruCopySignal


class Strategy(Protocol):
    def on_guru_signal(
        self,
        sig: GuruCopySignal,
        coord: RuntimeCoordinator,
    ) -> tuple[list[Intent], str | None, dict[str, Any] | None]: ...
