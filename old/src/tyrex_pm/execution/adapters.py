from __future__ import annotations

from typing import Any, Protocol

from tyrex_pm.core.models import ApprovedCancel, ApprovedIntent


class OMSBackend(Protocol):
    async def submit(
        self,
        ap: ApprovedIntent,
        *,
        market_info: Any | None = None,
    ) -> str: ...

    async def cancel(self, ac: ApprovedCancel) -> str: ...


class ShadowOMS:
    async def submit(
        self,
        ap: ApprovedIntent,
        *,
        market_info: Any | None = None,
    ) -> str:
        del market_info  # shadow path needs no quantization
        return "shadow_ack"

    async def cancel(self, ac: ApprovedCancel) -> str:
        return "shadow_cancel_ack"


# LiveOMS imported lazily by callers that install py-clob-client-v2 (see execution.live_oms).
