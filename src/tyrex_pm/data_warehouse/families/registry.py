from __future__ import annotations

from typing import Any, Protocol


class CollectionFamily(Protocol):
    def matches(self, metadata: dict[str, Any]) -> bool: ...
    def identify(self, metadata: dict[str, Any]) -> dict[str, Any]: ...
    def coverage_plan(self, entity: dict[str, Any], warmup_s: int) -> dict[str, Any]: ...
    def boundary_policy(self, metadata: dict[str, Any]) -> dict[str, Any]: ...


class FamilyRegistry:
    def __init__(self) -> None:
        self._families: dict[str, CollectionFamily] = {}

    def register(self, name: str, family: CollectionFamily) -> None:
        if name in self._families:
            raise ValueError("duplicate family")
        self._families[name] = family

    def get(self, name: str) -> CollectionFamily:
        try:
            return self._families[name]
        except KeyError as exc:
            raise ValueError(f"unsupported family: {name}") from exc
