"""Fill / trade finality helper (P3.5 architecture_enhance).

Centralizes the meaning of Polymarket trade statuses so every consumer agrees on
what each status implies. This is intentionally *only* a classification helper:
no portfolio, no PnL math (that is deferred to Phase 5).

Finality table (authoritative):

==========  =========  ==============  ===============  ===================  ============
status      evidence?  position final  allocation final  releases reservation  realized PnL
==========  =========  ==============  ===============  ===================  ============
MATCHED     yes        no             no               no                   no
MINED       yes        no             no               no                   no
CONFIRMED   yes        yes            yes              yes                  yes
RETRYING    no         no             no               no                   no
FAILED      no         no             no               yes                  no
<unknown>   no         no             no               no                   no   (fail closed)
==========  =========  ==============  ===============  ===================  ============

Design rule: existing behavior for ``MATCHED``/``MINED``/``CONFIRMED`` is
preserved exactly (evidence is recorded on all three; wallet/allocation credit
only on ``CONFIRMED``). ``RETRYING``/``FAILED``/unknown get explicit safe
handling. Protection (Phase 4) registers only when ``is_allocation_final`` is
true for the originating BUY (``allocation_buy_applied``).
"""

from __future__ import annotations

from dataclasses import dataclass

STATUS_MATCHED = "MATCHED"
STATUS_MINED = "MINED"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_RETRYING = "RETRYING"
STATUS_FAILED = "FAILED"

#: Statuses that already record execution evidence today (unchanged behavior).
EVIDENCE_STATUSES = (STATUS_MATCHED, STATUS_MINED, STATUS_CONFIRMED)


@dataclass(frozen=True)
class FillClassification:
    status: str
    known: bool
    execution_evidence: bool
    position_final: bool
    allocation_final: bool
    releases_reservation: bool
    realized_pnl: bool


_TABLE: dict[str, FillClassification] = {
    STATUS_MATCHED: FillClassification(
        STATUS_MATCHED, True, True, False, False, False, False
    ),
    STATUS_MINED: FillClassification(
        STATUS_MINED, True, True, False, False, False, False
    ),
    STATUS_CONFIRMED: FillClassification(
        STATUS_CONFIRMED, True, True, True, True, True, True
    ),
    STATUS_RETRYING: FillClassification(
        STATUS_RETRYING, True, False, False, False, False, False
    ),
    STATUS_FAILED: FillClassification(
        STATUS_FAILED, True, False, False, False, True, False
    ),
}

#: Unknown statuses fail closed: no evidence, no finality, no PnL, no allocation.
_UNKNOWN_BASE = FillClassification("", False, False, False, False, False, False)


def _norm(status: str | None) -> str:
    return str(status or "").strip().upper()


def classify(status: str | None) -> FillClassification:
    s = _norm(status)
    known = _TABLE.get(s)
    if known is not None:
        return known
    return FillClassification(
        status=s,
        known=False,
        execution_evidence=False,
        position_final=False,
        allocation_final=False,
        releases_reservation=False,
        realized_pnl=False,
    )


def is_execution_evidence(status: str | None) -> bool:
    return classify(status).execution_evidence


def is_position_final(status: str | None) -> bool:
    return classify(status).position_final


def is_allocation_final(status: str | None) -> bool:
    return classify(status).allocation_final


def releases_reservation(status: str | None) -> bool:
    return classify(status).releases_reservation


def counts_for_realized_pnl(status: str | None) -> bool:
    return classify(status).realized_pnl
