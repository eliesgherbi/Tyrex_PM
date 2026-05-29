from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO


class JsonlSink:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh: TextIO | None = None

    def __enter__(self) -> JsonlSink:
        self._fh = self._path.open("w", encoding="utf-8")
        return self

    def __exit__(self, *args: Any) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def write(self, obj: dict[str, Any]) -> None:
        if not self._fh:
            raise RuntimeError("sink not open")
        self._fh.write(json.dumps(obj, default=str) + "\n")
        self._fh.flush()
