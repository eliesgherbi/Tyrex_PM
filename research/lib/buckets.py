"""Per-bucket sufficiency helpers for M2B.4 decision outputs."""

from __future__ import annotations

from typing import Any

BUCKET_DEFAULTS: dict[str, int] = {
    "jump": 200,
    "survivor_episode": 50,
    "depth_slippage": 100,
    "leadlag_fast_move": 30,
}


def bucket_sufficient(bucket_type: str, n_observations: int, *, thresholds: dict[str, int] | None = None) -> bool:
    th = thresholds or BUCKET_DEFAULTS
    minimum = th.get(bucket_type, 30)
    return n_observations >= minimum


def annotate_bucket_row(
    *,
    bucket_id: str,
    n_observations: int,
    n_markets: int,
    bucket_type: str,
    thresholds: dict[str, int] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "bucket_id": bucket_id,
        "n_observations": n_observations,
        "n_markets": n_markets,
        "sufficient": bucket_sufficient(bucket_type, n_observations, thresholds=thresholds),
    }
    if extra:
        row.update(extra)
    return row


def format_decision_output(
    *,
    decisions: dict[str, Any],
    confidence: str,
    total_sample_size: int,
    per_bucket_samples: dict[str, int] | None = None,
    provisional: bool = True,
    provisional_reason: str | None = None,
    follow_up: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Render standard DECISION OUTPUT markdown block."""
    lines = ["DECISION OUTPUT:"]
    for key, value in decisions.items():
        lines.append(f"- {key}: {value}")
    lines.append(f"- confidence: {confidence}")
    lines.append(f"- total_sample_size: {total_sample_size}")
    if per_bucket_samples:
        for bid, n in per_bucket_samples.items():
            lines.append(f"- per_bucket_sample[{bid}]: {n}")
    lines.append(f"- provisional: {str(provisional).lower()}")
    if provisional_reason:
        lines.append(f"- provisional_reason: {provisional_reason}")
    if follow_up:
        lines.append(f"- required_follow_up: {follow_up}")
    if extra:
        for key, value in extra.items():
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)
