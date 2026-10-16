from __future__ import annotations

import re
from typing import Any

from tyrex_pm.domain.polymarket.outcome_map import map_up_down_outcomes

from ..contracts import canonical, decode, digest, epoch_ns, instrument_id, market_id


def array(value: Any) -> list:
    result = decode(value) if isinstance(value, str) else value
    if not isinstance(result, list):
        raise ValueError("expected provider array")
    return result


class BtcFiveMinute:
    def matches(self, metadata: dict[str, Any]) -> bool:
        return re.fullmatch(r"btc-updown-5m-\d+", str(metadata.get("slug", ""))) is not None

    def expected_windows(self, now_s: int, lookahead_s: int) -> list[tuple[str, int]]:
        current = now_s // 300 * 300
        return [
            (f"btc-updown-5m-{candidate}", candidate * 1_000_000_000)
            for candidate in range(current - 300, current + lookahead_s + 1, 300)
        ]

    def probe_windows(
        self, now_s: int, lookahead_s: int, cursor: int
    ) -> tuple[list[tuple[str, int]], int]:
        current = now_s // 300 * 300
        horizon = max(1, lookahead_s // 300)
        starts = sorted({current - 300, current, current + 300, current + cursor * 300})
        return (
            [(f"btc-updown-5m-{start}", start * 1_000_000_000) for start in starts],
            (cursor + 1) % horizon,
        )

    def identify(self, metadata: dict[str, Any]) -> dict[str, Any]:
        slug = str(metadata.get("slug", ""))
        match = re.fullmatch(r"btc-updown-5m-(\d+)", slug)
        if not match:
            raise ValueError("not a BTC five-minute slug")
        start = int(match[1]) * 1_000_000_000
        end = start + 300_000_000_000
        if metadata.get("endDate") and epoch_ns(metadata["endDate"], "iso") != end:
            raise ValueError("provider end and slug schedule disagree")
        condition = str(metadata.get("conditionId") or metadata.get("condition_id") or "")
        if not condition:
            raise ValueError("missing condition identity")
        labels = array(metadata.get("outcomes"))
        tokens = array(metadata.get("clobTokenIds"))
        # Validate labels independently of order; source outcome index remains authoritative.
        if len(labels) != 2 or len(tokens) != 2 or len(set(map(str, tokens))) != 2:
            raise ValueError("binary token mapping must be one-to-one")
        normalized = [str(label).strip().lower() for label in labels]
        if set(normalized) != {"up", "down"}:
            raise ValueError("missing explicit Up/Down labels")
        # The shared helper is exercised by its own domain tests; call with provider document.
        map_up_down_outcomes(labels, tokens)
        instruments = [
            {
                "token_id": str(token),
                "instrument_id": instrument_id(str(token)),
                "outcome_index": index,
                "venue_label": str(labels[index]),
                "normalized_label": normalized[index].upper(),
            }
            for index, token in enumerate(tokens)
        ]
        return {
            "market_id": market_id(condition),
            "condition_id": condition,
            "venue_market_id": str(metadata.get("id", "")),
            "slug": slug,
            "family": "recurring_price_window",
            "underlying_asset": "BTC",
            "duration_s": 300,
            "window_start": start,
            "window_end": end,
            "instruments": instruments,
            "rule_fingerprint": digest(
                canonical(
                    {
                        "description": metadata.get("description"),
                        "resolutionSource": metadata.get("resolutionSource"),
                    }
                )
            ),
            "closed": metadata.get("closed") if type(metadata.get("closed")) is bool else None,
            "rules": self.boundary_policy(metadata),
        }

    def coverage_plan(self, entity: dict[str, Any], warmup_s: int) -> dict[str, Any]:
        return {
            "market_from": entity["window_start"],
            "market_until": entity["window_end"],
            "external_from": entity["window_start"] - warmup_s * 1_000_000_000,
            "end_policy": "observed_trading_close",
        }

    def boundary_policy(self, metadata: dict[str, Any]) -> dict[str, Any]:
        # A slug or mention of Chainlink is not proof of the complete payoff functional.
        return {
            "status": "UNSUPPORTED",
            "version": "validated_market_rules_v1",
            "reason": "official_threshold_and_payoff_contract_not_attested",
            "description": metadata.get("description"),
            "resolution_source": metadata.get("resolutionSource"),
        }
