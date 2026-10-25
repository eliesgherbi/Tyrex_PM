from __future__ import annotations

from typing import Any

from tyrex_pm.domain.polymarket.outcome_map import map_up_down_outcomes
from tyrex_pm.domain.polymarket.window_families import WindowFamily, window_family

from ..contracts import canonical, decode, digest, epoch_ns, instrument_id, market_id


def array(value: Any) -> list:
    result = decode(value) if isinstance(value, str) else value
    if not isinstance(result, list):
        raise ValueError("expected provider array")
    return result


class RecurringWindow:
    """One recurring up/down window family (domain.polymarket.window_families) to collect.

    The BTC 5-minute family reproduces the original collector exactly (same slugs, ids,
    windows and metadata). Normalized metadata names the family's asset and duration.
    """

    def __init__(self, family: WindowFamily) -> None:
        self.spec = family

    def matches(self, metadata: dict[str, Any]) -> bool:
        return self.spec.slug_epoch(str(metadata.get("slug", ""))) is not None

    def expected_windows(self, now_s: int, lookahead_s: int) -> list[tuple[str, int]]:
        step = self.spec.window_duration_s
        current = now_s // step * step
        return [
            (self.spec.slug(candidate), candidate * 1_000_000_000)
            for candidate in range(current - step, current + lookahead_s + 1, step)
        ]

    def probe_windows(
        self, now_s: int, lookahead_s: int, cursor: int
    ) -> tuple[list[tuple[str, int]], int]:
        step = self.spec.window_duration_s
        current = now_s // step * step
        horizon = max(1, lookahead_s // step)
        starts = sorted({current - step, current, current + step, current + cursor * step})
        return (
            [(self.spec.slug(start), start * 1_000_000_000) for start in starts],
            (cursor + 1) % horizon,
        )

    def identify(self, metadata: dict[str, Any]) -> dict[str, Any]:
        slug = str(metadata.get("slug", ""))
        epoch = self.spec.slug_epoch(slug)
        if epoch is None:
            raise ValueError(f"not a {self.spec.slug_prefix} slug")
        start = epoch * 1_000_000_000
        end = start + self.spec.window_duration_s * 1_000_000_000
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
            "underlying_asset": self.spec.asset,
            "duration_s": self.spec.window_duration_s,
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


def BtcFiveMinute() -> RecurringWindow:  # noqa: N802 - name kept for existing importers
    """The original BTC 5-minute family."""
    return RecurringWindow(window_family("btc_updown_5m"))


def normalized_families() -> list[RecurringWindow]:
    """Families whose metadata the normalizer recognizes: the enabled ones.

    Enabling a family changes what the normalizer extracts from the same raw records, so
    it goes with a PARSER_VERSION bump (builds are immutable per parser version).
    """
    from tyrex_pm.domain.polymarket.window_families import window_families

    return [RecurringWindow(f) for f in window_families().values() if f.enabled and f.supported]
