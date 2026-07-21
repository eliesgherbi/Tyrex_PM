"""Label-based outcome mapping for binary Polymarket markets (N2).

BTC 5m Up/Down markets must map by venue label only — never by array position.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from tyrex_pm.core.ids import TokenId


class NormalizedLeg(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    YES = "YES"
    NO = "NO"


class OutcomeMapError(ValueError):
    """Rejected outcome/token binding."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True, kw_only=True)
class OutcomeLegBinding:
    """Validated token binding for one normalized leg."""

    leg: NormalizedLeg
    venue_label: str
    token_id: TokenId
    outcome_index: int


@dataclass(frozen=True, kw_only=True)
class OutcomeMap:
    """Complete label-validated outcome map for a binary market."""

    legs: tuple[OutcomeLegBinding, ...]
    mapping_method: str = "label_index_only"

    def token_for(self, leg: NormalizedLeg) -> TokenId:
        for binding in self.legs:
            if binding.leg is leg:
                return binding.token_id
        raise KeyError(leg)

    def as_up_down(self) -> tuple[TokenId, TokenId]:
        return self.token_for(NormalizedLeg.UP), self.token_for(NormalizedLeg.DOWN)

    def as_yes_no(self) -> tuple[TokenId, TokenId]:
        return self.token_for(NormalizedLeg.YES), self.token_for(NormalizedLeg.NO)

    @property
    def token_ids(self) -> tuple[str, ...]:
        return tuple(b.token_id.value for b in self.legs)


def _parse_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise OutcomeMapError("incomplete_gamma", "outcomes/tokens not a list")
    return list(value)


def map_yes_no_outcomes(
    outcomes: Sequence[Any] | str | None,
    token_ids: Sequence[Any] | str | None,
) -> OutcomeMap:
    labels = [str(o) for o in _parse_json_list(outcomes)]
    tokens = [str(t) for t in _parse_json_list(token_ids)]
    return _map_binary(
        labels,
        tokens,
        required={NormalizedLeg.YES: "yes", NormalizedLeg.NO: "no"},
    )


def map_up_down_outcomes(
    outcomes: Sequence[Any] | str | None,
    token_ids: Sequence[Any] | str | None,
) -> OutcomeMap:
    """Map venue ``Up``/``Down`` labels to ``UP``/``DOWN`` by label only."""
    labels = [str(o) for o in _parse_json_list(outcomes)]
    tokens = [str(t) for t in _parse_json_list(token_ids)]
    return _map_binary(
        labels,
        tokens,
        required={NormalizedLeg.UP: "up", NormalizedLeg.DOWN: "down"},
    )


def _map_binary(
    labels: list[str],
    tokens: list[str],
    *,
    required: Mapping[NormalizedLeg, str],
) -> OutcomeMap:
    if not labels or not tokens:
        raise OutcomeMapError("incomplete_gamma", "missing outcomes or tokens")
    if len(labels) != len(tokens):
        raise OutcomeMapError(
            "token_count_mismatch",
            f"outcomes={len(labels)} tokens={len(tokens)}",
        )
    if len(set(tokens)) != len(tokens):
        raise OutcomeMapError("duplicate_token_ids", str(tokens))

    lowered = [x.lower() for x in labels]
    known = set(required.values())
    unknown = [lab for lab, low in zip(labels, lowered) if low not in known]
    if unknown:
        raise OutcomeMapError("unknown_label", str(unknown))

    bindings: list[OutcomeLegBinding] = []
    for leg, needle in required.items():
        count = lowered.count(needle)
        if count == 0:
            raise OutcomeMapError("missing_outcome", needle)
        if count > 1:
            raise OutcomeMapError("duplicate_outcome", needle)
        idx = lowered.index(needle)
        bindings.append(
            OutcomeLegBinding(
                leg=leg,
                venue_label=labels[idx],
                token_id=TokenId(tokens[idx]),
                outcome_index=idx,
            )
        )

    return OutcomeMap(legs=tuple(bindings), mapping_method="label_index_only")


def rule_fingerprint(*, resolution_source: str | None, description: str | None) -> str:
    payload = {
        "resolution_source": (resolution_source or "").strip(),
        "description": (description or "").strip(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()[:16]
