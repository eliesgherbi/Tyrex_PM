"""Pre-submit guard against the venue's hard minimum-order-size floor.

Why this module exists
----------------------
Polymarket rejects any order whose ``size`` (in outcome shares) falls below a hard floor —
typically ``5`` for binary markets — with::

    Size (4.54) lower than the minimum: 5

Live evidence (``var/reporting/runs/live_test_reservation_life_cycle``) showed exactly this
case: a guru signal of $272 was clipped by ``notional_max_usd=4`` to ``4 / 0.88 ≈ 4.54``
shares, then submitted, then rejected. The clip math is correct; the bot just shouldn't
have sent the order in the first place.

This module is the *last* gate before :class:`tyrex_pm.core.models.ApprovedIntent` is built,
running after notional-min/max, deployment caps, capital, and inventory checks. By that
point the candidate ``size`` reflects every other constraint.

Two policies
~~~~~~~~~~~~
* ``deny`` (default): block locally with reason ``below_venue_min_size``.
* ``bump``: raise ``size`` to the configured floor and re-validate the higher-priority gates
  against the bumped intent. Only submit if those still pass; otherwise emit
  ``below_venue_min_size`` with ``venue_min_size_bump_unsafe=True`` so the audit trail
  shows *why* the bump was unsafe.

Ordering rationale (documented for future readers)
--------------------------------------------------
The check is intentionally last so it sees the **final** size that would otherwise be sent
to the OMS. Putting it earlier would mean re-running it after every clip, which would tangle
the fall-through logic in :func:`tyrex_pm.risk.engine.evaluate_intent` for no real benefit.
The cost is that the ``bump`` policy must re-call the deployment + capital evaluators with
the bumped size; those calls are cheap (pure functions over an already-built ``RiskContext``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.models import EnterIntent, ExitIntent, ReduceIntent
from tyrex_pm.risk.evidence_format import s_usd
from tyrex_pm.runtime.config import VenueMinSizeConfig


@dataclass(frozen=True)
class VenueMinSizeEvaluation:
    """Outcome of the venue minimum-size policy.

    ``intent`` is the (possibly bumped) intent to use downstream. ``deny_reason`` is set
    iff this gate produced a deny (either ``policy=deny`` short-circuit, or ``policy=bump``
    with the bumped size unsafe). ``evidence`` is always populated and is intended to be
    merged into ``risk_decision.extensions``.
    """

    ok: bool
    intent: EnterIntent | ExitIntent | ReduceIntent
    deny_reason: str | None
    evidence: dict[str, Any]


def _resolve_min_size(cfg: VenueMinSizeConfig, intent: EnterIntent | ExitIntent | ReduceIntent) -> Decimal:
    """Per-token overrides could be wired here later; today we use the global default.

    Kept as a function so the call-site contract is explicit and the evidence payload
    always reports the value that *was actually used*, not just the config default.
    """
    del intent  # placeholder for future per-token resolution
    return cfg.default_min_size


def evaluate_venue_min_size(
    intent: EnterIntent | ExitIntent | ReduceIntent,
    cfg: VenueMinSizeConfig,
) -> VenueMinSizeEvaluation:
    """Evaluate the venue minimum-size gate for an already-clipped intent.

    Inputs are the *post-clip* intent (after notional cap / deployment / capital have
    finished narrowing the size). The returned ``intent`` is the original when ``policy=deny``
    or when no bump was needed; it is ``replace(intent, size=floor)`` after a bump.

    Re-validation of caps/capital after a bump is **not** done here: it is the caller's
    responsibility (see :func:`tyrex_pm.risk.engine.evaluate_intent`) so this module stays
    a pure size-policy helper with no knowledge of deployment / capital wiring.
    """
    floor = _resolve_min_size(cfg, intent)
    evidence: dict[str, Any] = {
        "venue_min_size_check": cfg.enabled,
        "venue_min_size_policy": cfg.policy,
        "venue_min_size": str(floor),
        "venue_min_size_token_id": str(intent.token_id),
        "venue_min_size_limit_price": (
            str(intent.limit_price) if intent.limit_price is not None else None
        ),
        "venue_min_size_final_size": str(intent.size),
    }
    final_notional = (
        intent.size * intent.limit_price if intent.limit_price is not None else Decimal("0")
    )
    evidence["venue_min_size_final_notional_usd"] = s_usd(final_notional)

    if not cfg.enabled:
        evidence["venue_min_size_skipped"] = "disabled"
        return VenueMinSizeEvaluation(True, intent, None, evidence)

    if intent.size >= floor:
        evidence["venue_min_size_outcome"] = "above_floor"
        return VenueMinSizeEvaluation(True, intent, None, evidence)

    if cfg.policy == "deny":
        evidence["venue_min_size_outcome"] = "deny"
        return VenueMinSizeEvaluation(False, intent, rc.BELOW_VENUE_MIN_SIZE, evidence)

    bumped = replace(intent, size=floor)
    bumped_notional = (
        bumped.size * bumped.limit_price if bumped.limit_price is not None else Decimal("0")
    )
    evidence["venue_min_size_outcome"] = "bumped"
    evidence["venue_min_size_original_size"] = str(intent.size)
    evidence["venue_min_size_bumped_size"] = str(bumped.size)
    evidence["venue_min_size_bumped_notional_usd"] = s_usd(bumped_notional)
    return VenueMinSizeEvaluation(True, bumped, None, evidence)
