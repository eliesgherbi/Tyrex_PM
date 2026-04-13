"""Scenario A validation harness constants — not used by production :class:`~tyrex_pm.strategy.copy_strategy.CopyStrategy`."""

from __future__ import annotations

# Polymarket token balance in atomic units can sit ~1–2% below Nautilus ``net_position`` / BUY fill float;
# default bps shaves long inventory before validation SELL submit.
DEFAULT_VALIDATION_SELL_INVENTORY_HAIRCUT_BPS = 200.0
