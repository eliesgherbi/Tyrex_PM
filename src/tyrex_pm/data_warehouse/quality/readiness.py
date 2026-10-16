"""One conservative research gate, shared by verification, exports and replay."""

from __future__ import annotations


def readiness(report: dict) -> dict:
    reasons = []
    for key, expected in (
        ("capture_integrity", "VERIFIED"),
        ("normalization", "ACCOUNTED"),
        ("reconstruction_usability", "NO_DETECTED_GAP_WITH_STATED_ASSURANCE"),
        ("semantic_readiness", "READY"),
        ("settlement_readiness", "PROVIDER_TERMINAL"),
        ("closure_status", "OBSERVED"),
    ):
        if report.get(key) != expected:
            reasons.append(key)
    if report.get("normalization_complete") is not True:
        reasons.append("normalization_pending")
    feeds = report.get("feeds", {})
    if not feeds:
        reasons.append("required_feeds_unavailable")
    for name, feed in feeds.items():
        if feed.get("required", True) and (
            feed.get("required_duration_ns", 0) <= 0
            or feed.get("unusable_or_missing_duration_ns", 1) != 0
            or feed.get("status") not in ("UNKNOWN_CONTINUITY", "CAPTURED_NO_DETECTED_GAP")
        ):
            reasons.append(f"feed:{name}")
    if not report.get("scoring_labels"):
        reasons.append("qualified_payout_mapping_unavailable")
    return {
        "ready": not reasons,
        "reasons": sorted(set(reasons)),
        "policy_version": "2",
        "continuity_policy": "recorded_intervals_with_explicit_unknown_provider_continuity",
    }
