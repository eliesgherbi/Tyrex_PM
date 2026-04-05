"""VAL-06: new :class:`~tyrex_pm.core.reason_codes.ReasonCode` values map to taxonomy."""

from __future__ import annotations

from tyrex_pm.reporting.taxonomy import unmapped_reason_codes


def test_all_reason_codes_mapped_or_excluded() -> None:
    missing = unmapped_reason_codes()
    assert missing == [], f"Unmapped ReasonCode values for delta taxonomy: {missing}"
