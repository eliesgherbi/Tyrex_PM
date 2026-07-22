"""N7 PTB trust-policy helpers (Chainlink sealed K vs optional SSR match)."""

from __future__ import annotations

from typing import Any


def ptb_trust_fields(
    *,
    sealed_k: str | None,
    require_ssr_price_match: bool,
    ptb_ready: bool,
    ssr_check_status: str | None = None,
) -> dict[str, Any]:
    """Explicit report fields for the active PTB authority policy."""
    if not isinstance(require_ssr_price_match, bool):
        raise ValueError("require_ssr_price_match must be a boolean")
    status = ssr_check_status
    if status is None:
        status = "REQUIRED" if require_ssr_price_match else "DISABLED"
    return {
        "ptb_authority": (
            "chainlink_sealed_k_and_ssr_match"
            if require_ssr_price_match
            else "chainlink_sealed_k"
        ),
        "sealed_k": sealed_k,
        "ssr_match_required": require_ssr_price_match,
        "ssr_check_status": status,
        "ptb_ready": bool(ptb_ready),
    }


def no_entry_reason(
    *,
    evals: int,
    last_skip_reasons: list[str] | None,
    require_ssr_price_match: bool,
) -> str:
    """Truthful no-entry reason (never blame disabled SSR)."""
    skips = list(last_skip_reasons or [])
    if not require_ssr_price_match:
        skips = [
            s
            for s in skips
            if s not in {"attestation_unavailable", "attestation_mismatch"}
        ]
    if evals <= 0:
        if skips:
            return f"eval_blocked:{','.join(skips[:4])}"
        return "no_aligned_evaluation"
    return "evaluated_no_enter_signal"
