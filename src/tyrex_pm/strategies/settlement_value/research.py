"""How settlement_value is offered in the research workspace (form layout and policy)."""

from __future__ import annotations

from tyrex_pm.strategies.research import ResearchSchema
from tyrex_pm.strategies.settlement_value.config import (
    COMPARISONS,
    K_QUALITIES,
    PROVENANCE_STATUSES,
    SPOT_INPUTS,
    SUPPORTED_BACKENDS,
    TARGET_MODES,
)
from tyrex_pm.strategies.settlement_value.schema import settlement_value_config_as_parameters

ESSENTIALS = (
    "entry.min_edge_per_share",
    "entry.tau_min_s",
    "entry.tau_max_s",
    "sizing.max_debit_usdc",
    "entry.max_entry_price",
    "exit.policy",
)
POLICY_FIELDS = {
    "quality.accepted_k_qualities": ["CONFIRMED_CANONICAL", "PROVISIONAL"],
    "quality.require_verified_settlement_spec": False,
}
GROUPS = (
    {
        "id": "entry",
        "name": "Entry & position size",
        "description": "When to enter, price limits, spending and order sizing.",
        "sections": ["entry", "sizing"],
    },
    {
        "id": "model",
        "name": "Model & volatility",
        "description": "Probability inputs, uncertainty bands and volatility estimator.",
        "sections": ["model", "volatility"],
    },
    {
        "id": "exit",
        "name": "Exit, retry & re-entry",
        "description": "Lifecycle choices and bounded recovery of orders and positions.",
        "sections": ["exit", "retry", "reentry"],
    },
    {
        "id": "quality",
        "name": "Settlement & research policy",
        "description": "Payoff interpretation, input quality and the visible set policy.",
        "sections": ["settlement", "quality"],
    },
    {
        "id": "fees",
        "name": "Strategy fee estimates",
        "description": "Strategy fee curve. Simulation fees stay with the execution scenario.",
        "sections": ["fees"],
    },
    {
        "id": "veto",
        "name": "Model / market disagreement",
        "description": "Optional entry veto. It does not alter an existing hold or exit.",
        "sections": ["disagreement_veto"],
    },
)
CHOICES = {
    "model.backend": sorted(SUPPORTED_BACKENDS),
    "model.spot_input": sorted(SPOT_INPUTS),
    "settlement.comparison": sorted(COMPARISONS),
    "settlement.provenance_status": sorted(PROVENANCE_STATUSES),
    "entry.target_mode": sorted(TARGET_MODES),
    "quality.accepted_k_qualities": sorted(K_QUALITIES),
    "exit.policy": ["hold_to_resolution", "managed_exit"],
}
LABELS = {
    "entry.min_edge_per_share": "Minimum conservative edge",
    "entry.tau_min_s": "Stop new entries this close to expiry",
    "entry.tau_max_s": "Start entries with this much time left",
    "sizing.max_debit_usdc": "Maximum acquisition spending",
    "entry.max_entry_price": "Maximum entry price",
    "exit.policy": "Position lifecycle",
    "quality.accepted_k_qualities": "Accepted price-to-beat quality",
    "quality.require_verified_settlement_spec": "Require verified settlement specification",
}
LABELS.update(
    {
        "model.backend": "Probability model",
        "volatility.half_life_s": "Volatility half-life",
        "exit.value_exit_enabled": "Enable value exit",
        "retry.enabled": "Allow bounded entry retry",
        "reentry.max_trades_per_window": "Maximum trades per window",
    }
)


SETTLEMENT_VALUE_RESEARCH = ResearchSchema(
    label="Settlement value",
    as_parameters=lambda config: settlement_value_config_as_parameters(config, full=True),
    identity_parameters=settlement_value_config_as_parameters,
    groups=GROUPS,
    labels=LABELS,
    choices=CHOICES,
    essentials=ESSENTIALS,
    policy_fields=POLICY_FIELDS,
)
