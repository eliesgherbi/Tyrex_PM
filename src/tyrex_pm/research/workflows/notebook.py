"""Notebook adapter. Frames are built after the shared operation returns."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .api import call


def resolve_experiment(request: dict, *, root: Path) -> dict[str, Any]:
    return call("experiment.resolve", request, root=root)


def open_results(request: dict, *, root: Path) -> tuple[dict[str, Any], Any]:
    envelope = call("results.open", request, root=root)
    frame = None
    if envelope["ok"]:
        import pandas as pd

        frame = pd.DataFrame(envelope["result"]["windows"])
    return envelope, frame
