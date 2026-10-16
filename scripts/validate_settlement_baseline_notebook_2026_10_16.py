"""Execute disarmed notebook 04 through IPython without a separate kernel process."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import nbformat


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    scratch = repo / "scratchpad/baseline_v2_closeout_2026-10-16"
    for name in ("ipython", "matplotlib"):
        (scratch / name).mkdir(parents=True, exist_ok=True)
    os.environ["IPYTHONDIR"] = str(scratch / "ipython")
    os.environ["MPLCONFIGDIR"] = str(scratch / "matplotlib")
    os.environ["MPLBACKEND"] = "module://matplotlib_inline.backend_inline"
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(repo / "src"))
    os.chdir(repo)
    from IPython.core.interactiveshell import InteractiveShell
    from IPython.utils.capture import capture_output
    from matplotlib_inline.backend_inline import configure_inline_support
    path = repo / "src/tyrex_pm/research/strategy_lab/04_staged_settlement_research.ipynb"
    nb = nbformat.read(path, as_version=4)
    for cell in nb.cells:
        if cell.cell_type == "code":
            for stage in ("A", "B", "C"):
                if f"RUN_STAGE_{stage} = True" in cell.source:
                    raise RuntimeError("A stage is armed; refusing notebook execution.")
    print("Starting in-process IPython notebook validation.", flush=True)
    shell = InteractiveShell.instance()
    configure_inline_support(shell, "module://matplotlib_inline.backend_inline")
    count = 0
    for index, cell in enumerate(nb.cells):
        if cell.cell_type != "code":
            continue
        count += 1
        print(f"Executing notebook cell {index}", flush=True)
        with capture_output(stdout=True, stderr=True, display=True) as captured:
            result = shell.run_cell(cell.source, store_history=False)
        error = result.error_before_exec or result.error_in_exec
        if error is not None:
            print(captured.stdout, flush=True)
            print(captured.stderr, flush=True)
            raise RuntimeError(f"Notebook cell {index} failed") from error
        outputs = []
        for stream, value in (("stdout", captured.stdout), ("stderr", captured.stderr)):
            if value:
                outputs.append(nbformat.v4.new_output("stream", name=stream, text=value))
        outputs.extend(
            nbformat.v4.new_output("display_data", data=item.data, metadata=item.metadata)
            for item in captured.outputs
        )
        cell.outputs = outputs
        cell.execution_count = count
    nbformat.write(nb, path)
    errors = [
        output
        for cell in nb.cells
        if cell.cell_type == "code"
        for output in cell.get("outputs", [])
        if output.output_type == "error"
    ]
    print(
        json.dumps(
            {
                "executed_code_cells": sum(cell.cell_type == "code" for cell in nb.cells),
                "errors": len(errors),
                "notebook": str(path),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
