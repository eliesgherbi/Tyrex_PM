"""Minimal Z-Gap strategy object for pipeline/OMS integration (A0.7)."""

from __future__ import annotations

from tyrex_pm.core.models import ApprovedIntent
from tyrex_pm.runtime.config import ZGapStrategyConfig
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.core.enums import ExecutionMode


class ZGapStrategy:
    """Duck-typed strategy handle for ``process_intent_work_unit``."""

    def __init__(self, cfg: ZGapStrategyConfig) -> None:
        self.cfg = cfg
        self.owner_id = cfg.owner_id
        self.last_entry_client_order_id: str | None = None
        self.last_exit_client_order_id: str | None = None

    def on_buy_submit_ack(
        self,
        *,
        ap: ApprovedIntent,
        parent_correlation_id: str,
        coord: RuntimeCoordinator,
        execution_mode: ExecutionMode,
        apply_local_shadow_fill: bool,
        match_evidence: dict | None = None,
    ) -> None:
        _ = (parent_correlation_id, coord, execution_mode, apply_local_shadow_fill, match_evidence)
        self.last_entry_client_order_id = str(ap.client_order_id)

    def on_exit_submit_ack(self, *, ap: ApprovedIntent) -> None:
        self.last_exit_client_order_id = str(ap.client_order_id)
