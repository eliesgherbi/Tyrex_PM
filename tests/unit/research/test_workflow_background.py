"""Background workers run without visible windows; a dead replay process is resumable."""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool

import pytest

from tyrex_pm.research.workflows import worker
from tyrex_pm.research.workflows.artifacts import background_flags
from tyrex_pm.research.workflows.contracts import WorkflowError


@pytest.mark.skipif(os.name != "nt", reason="Windows creation flags")
def test_background_workers_get_a_hidden_console_their_children_inherit() -> None:
    flags = background_flags()
    assert flags & subprocess.CREATE_NO_WINDOW
    assert not flags & subprocess.DETACHED_PROCESS  # pool children would open windows


class _DeadPool:
    def __init__(self, max_workers: int) -> None:
        del max_workers

    def submit(self, fn, *args):
        future: Future = Future()
        future.set_exception(BrokenProcessPool("terminated abruptly"))
        return future

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        del wait, cancel_futures


def test_dead_replay_process_is_a_named_resumable_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _DeadPool)
    monkeypatch.setenv("TYREX_REPLAY_WORKERS", "2")
    units = [
        {"id": f"u{i}", "kind": "strategy_replay", "market_id": f"m{i}", "manifests": [1]}
        for i in range(2)
    ]
    with pytest.raises(WorkflowError) as caught:
        worker._run_parallel(tmp_path, {"units": units}, {}, 1)
    assert caught.value.code == "replay_worker_ended"
    assert "resume" in caught.value.reason
    assert caught.value.fields["windows"] in (["m0"], ["m1"])
