"""Execute selected offline lessons using this Python interpreter; no provider calls."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

import nbformat
from jupyter_client import KernelManager
from jupyter_client.kernelspec import KernelSpecManager
from nbclient import NotebookClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("var/research/tuto_btc5m/validation"))
    parser.add_argument("--lesson", type=int, choices=range(1, 13))
    parser.add_argument("--start-lesson", type=int, choices=range(1, 13), default=1)
    parser.add_argument("--end-lesson", type=int, choices=range(1, 13), default=12)
    args = parser.parse_args()
    if args.start_lesson > args.end_lesson:
        parser.error("--start-lesson must be at or before --end-lesson")
    root = Path(__file__).resolve().parents[1]
    folder = root / "src/tyrex_pm/research/tuto_btc5m"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="tyrex-tutorial-kernel-") as temp:
        spec_folder = Path(temp) / "tyrex-tutorial"
        spec_folder.mkdir()
        (spec_folder / "kernel.json").write_text(
            json.dumps(
                {
                    "argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
                    "display_name": "Tyrex tutorial validation",
                    "language": "python",
                    "env": {
                        "MPLBACKEND": "module://matplotlib_inline.backend_inline",
                        "MPLCONFIGDIR": str(Path(temp) / "matplotlib"),
                        "IPYTHONDIR": str(Path(temp) / "ipython"),
                        "JUPYTER_RUNTIME_DIR": str(Path(temp) / "runtime"),
                    },
                }
            ),
            encoding="utf-8",
        )
        notebooks = sorted(folder.glob("[0-1][0-9]_*.ipynb"))
        if len(notebooks) != 12 or {int(p.name[:2]) for p in notebooks} != set(range(1, 13)):
            raise ValueError("Expected lessons 01–12 exactly once.")
        if args.lesson:
            notebooks = [p for p in notebooks if p.name.startswith(f"{args.lesson:02d}_")]
        else:
            notebooks = [
                p for p in notebooks if args.start_lesson <= int(p.name[:2]) <= args.end_lesson
            ]
        for path in notebooks:
            print(f"Executing {path.name}", flush=True)
            started = time.perf_counter()
            nb = nbformat.read(path, as_version=4)
            nbformat.validate(nb)
            for cell in nb.cells:
                if cell.cell_type == "code":
                    compile(cell.source, path.name, "exec")
            manager = KernelManager(
                kernel_name="tyrex-tutorial",
                kernel_spec_manager=KernelSpecManager(kernel_dirs=[temp]),
            )
            try:
                NotebookClient(
                    nb,
                    km=manager,
                    timeout=180,
                    allow_errors=False,
                    resources={"metadata": {"path": str(folder)}},
                ).execute()
            finally:
                if manager.has_kernel:
                    manager.shutdown_kernel(now=True)
            nbformat.write(nb, output / path.name)
            images = sum(
                "image/png" in result.get("data", {})
                for cell in nb.cells
                for result in cell.get("outputs", [])
            )
            result = {
                "notebook": path.name,
                "status": "passed",
                "figures": images,
                "seconds": round(time.perf_counter() - started, 2),
            }
            results.append(result)
            print(result, flush=True)
    receipt = {
        "python": sys.version.split()[0],
        "results": results,
        "scope": "all_notebook_cells_on_local_pinned_sample_no_live_validation",
    }
    receipt_name = (
        f"receipt_{args.lesson:02d}.json"
        if args.lesson
        else "receipt.json"
        if (args.start_lesson, args.end_lesson) == (1, 12)
        else f"receipt_{args.start_lesson:02d}_{args.end_lesson:02d}.json"
    )
    (output / receipt_name).write_text(json.dumps(receipt, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
