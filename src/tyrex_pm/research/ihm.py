"""Local HTTP bridge for the integrated workspace. It adds no workflow rules."""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from tyrex_pm.research.workflows.api import call
from tyrex_pm.research.workflows.artifacts import REPO
from tyrex_pm.research.workflows.contracts import fail

STATIC = REPO / "frontend" / "dist"
WORKFLOW = REPO / "var" / "research" / "workflows" / "ihm"


def allowed_roots() -> list[Path]:
    # Every var/warehouse* folder with collections, including ones recorded from the page.
    from tyrex_pm.research.workflows.recording import warehouse_roots

    return warehouse_roots()


def _allowed(path: Path) -> bool:
    resolved = path.resolve()
    return resolved in allowed_roots()


class Handler(BaseHTTPRequestHandler):
    server_version = "tyrex-ihm/1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[ihm] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        path = urlparse(self.path).path
        if path == "/api/bootstrap":
            self._json(
                200,
                {
                    "roots": [str(item) for item in allowed_roots()],
                    "workflow_root": str(WORKFLOW),
                    "time_zones": ["UTC", "Europe/Paris", "America/New_York"],
                    "launch_allowed": False,
                    "recording_allowed": False,
                    "reference_prototype": "prototypes/ihm-review",
                },
            )
            return
        if path.startswith("/api/"):
            self._json(404, {"ok": False, "error": {"code": "not_found", "reason": path}})
            return
        self._static(path)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        path = urlparse(self.path).path
        if path != "/api/workflow":
            self._json(404, fail("unknown", "not_found", "only /api/workflow accepts operations"))
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, fail("unknown", "invalid_json", "request body is not JSON"))
            return
        operation = str(payload.get("operation") or "")
        request = dict(payload.get("request") or {})
        refusal = _guard(operation, request)
        if refusal is not None:
            self._json(200, refusal)
            return
        envelope = call(operation, request, root=WORKFLOW)
        self._json(200, envelope)

    def _static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (STATIC / relative).resolve()
        if STATIC.resolve() not in target.parents and target != STATIC.resolve():
            self._json(404, {"ok": False})
            return
        if not target.is_file():
            target = STATIC / "index.html"
        if not target.is_file():
            self._send(
                503,
                b"Integrated frontend is not built. Run frontend npm run build.",
                "text/plain; charset=utf-8",
            )
            return
        kind = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".json": "application/json",
        }.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), kind)


def _guard(operation: str, request: dict) -> dict | None:
    roots = request.get("roots")
    if not roots and operation.startswith(("library.", "data_recipe.")):
        request["roots"] = [str(item) for item in allowed_roots()]
    for item in request.get("roots") or []:
        if not _allowed(Path(item)):
            return fail(
                operation,
                "root_not_allowed",
                "warehouse root is not configured for this workspace",
                {"root": item},
            )
    warehouse = request.get("warehouse")
    if warehouse and operation.startswith("preparation.") and not _allowed(Path(warehouse)):
        return fail(
            operation,
            "root_not_allowed",
            "preparation is limited to configured warehouse roots",
            {"warehouse": warehouse},
        )
    if operation in {"collection.start", "paper.preflight"} and request.get("launch"):
        return fail(operation, "launch_gated", "paper launch and recording start stay gated", {})
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tyrex-ihm")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4173)
    args = parser.parse_args(argv)

    class Server(ThreadingHTTPServer):
        # On Windows SO_REUSEADDR lets a second workspace bind the same port silently, and
        # requests then reach whichever process the OS picks. One port, one workspace.
        allow_reuse_address = os.name != "nt"

    try:
        server = Server((args.host, args.port), Handler)
    except OSError as exc:
        print(f"Port {args.port} is already in use; another workspace is running? ({exc})")
        return 1
    print(f"Tyrex workspace http://{args.host}:{args.port}/", flush=True)
    print(f"Workflow records: {WORKFLOW}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
