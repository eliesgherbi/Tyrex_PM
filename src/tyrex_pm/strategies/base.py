from __future__ import annotations

from typing import Any, Protocol

from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import Intent
from tyrex_pm.signals.base import GuruCopySignal


class Strategy(Protocol):
    def on_guru_signal(
        self,
        sig: GuruCopySignal,
        holdings: dict[TokenId, Decimal],
    ) -> tuple[list[Intent], str | None, dict[str, Any] | None]: ...
