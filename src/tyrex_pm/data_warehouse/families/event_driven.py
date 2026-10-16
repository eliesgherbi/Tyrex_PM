"""Windowless question family. Storage stays generic; this module owns the classifier."""

from __future__ import annotations

from typing import Any

from ..contracts import canonical, decode, digest, instrument_id, market_id


class EventQuestion:
    def matches(self, metadata: dict[str, Any]) -> bool:
        return metadata.get("market_family") == "event_driven_question"

    def identify(self, metadata: dict[str, Any]) -> dict[str, Any]:
        condition = str(metadata.get("conditionId") or metadata.get("condition_id") or "")
        if not condition:
            raise ValueError("missing condition identity")
        labels = metadata.get("outcomes") or []
        tokens = metadata.get("clobTokenIds") or []
        if isinstance(labels, str):
            labels = decode(labels)
        if isinstance(tokens, str):
            tokens = decode(tokens)
        if not isinstance(labels, list) or not isinstance(tokens, list):
            raise ValueError("expected provider array")
        instruments = []
        if labels or tokens:
            if len(labels) != len(tokens) or len(set(map(str, tokens))) != len(tokens):
                raise ValueError("token mapping must be one-to-one")
            instruments = [
                {
                    "token_id": str(token),
                    "instrument_id": instrument_id(str(token)),
                    "outcome_index": index,
                    "venue_label": str(labels[index]),
                    "normalized_label": None,
                }
                for index, token in enumerate(tokens)
            ]
        return {
            "market_id": market_id(condition),
            "condition_id": condition,
            "venue_market_id": str(metadata.get("id", "")),
            "slug": str(metadata.get("slug") or condition),
            "family": "event_driven_question",
            "underlying_asset": None,
            "duration_s": None,
            "window_start": None,
            "window_end": None,
            "instruments": instruments,
            "rule_fingerprint": digest(canonical({"description": metadata.get("description")})),
            "closed": metadata.get("closed") if type(metadata.get("closed")) is bool else None,
            "rules": {
                "status": "UNSUPPORTED",
                "version": "event_question_v1",
                "reason": "event_family_has_no_scheduled_window",
            },
        }

    def coverage_plan(self, entity: dict[str, Any], warmup_s: int) -> dict[str, Any]:
        return {
            "market_from": None,
            "market_until": None,
            "external_from": None,
            "end_policy": "none",
        }

    def boundary_policy(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return {"status": "UNSUPPORTED", "reason": "event_family_has_no_price_boundary"}
