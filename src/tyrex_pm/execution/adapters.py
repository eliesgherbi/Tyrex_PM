from __future__ import annotations

from typing import Protocol

from tyrex_pm.core.models import ApprovedCancel, ApprovedIntent


class OMSBackend(Protocol):
    async def submit(self, ap: ApprovedIntent) -> str: ...

    async def cancel(self, ac: ApprovedCancel) -> str: ...


class ShadowOMS:
    async def submit(self, ap: ApprovedIntent) -> str:
        return "shadow_ack"

    async def cancel(self, ac: ApprovedCancel) -> str:
        return "shadow_cancel_ack"


# LiveOMS imported lazily by callers that install py-clob-client (see execution.live_oms).
