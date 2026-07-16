"""Append-only UTF-8 JSONL fact sink."""

from __future__ import annotations

import json
from pathlib import Path

from tyrex_pm.core.facts import FactEnvelope
from tyrex_pm.reporting.serialize import fact_to_jsonable


class JsonlFactSink:
    SCHEMA_VERSION = 1

    def __init__(self, path: Path | str, *, flush_every: bool = True) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._flush_every = flush_every
        self._fp = self._path.open("a", encoding="utf-8", newline="\n")
        self._count = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def count(self) -> int:
        return self._count

    def append(self, fact: FactEnvelope) -> None:
        if fact.schema_version != self.SCHEMA_VERSION:
            # Allow higher versions only if explicitly matching sink; R3 uses 1.
            if fact.schema_version < 1:
                raise ValueError("invalid schema_version")
        line = json.dumps(fact_to_jsonable(fact), ensure_ascii=False, separators=(",", ":"))
        self._fp.write(line + "\n")
        self._count += 1
        if self._flush_every:
            self.flush()

    def flush(self) -> None:
        self._fp.flush()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self._fp.close()

    def __enter__(self) -> JsonlFactSink:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
