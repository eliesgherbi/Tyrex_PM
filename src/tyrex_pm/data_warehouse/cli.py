"""Public command boundaries; optional data dependencies are imported on demand."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

from .capture.files import Ownership, atomic_json, read_json, within
from .config import load_credentials, load_profile
from .contracts import canonical


def duration(value: str) -> float:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(s|m|h)", value)
    if not match or float(match[1]) <= 0:
        raise argparse.ArgumentTypeError("duration must be positive, e.g. 45m")
    return float(match[1]) * {"s": 1, "m": 60, "h": 3600}[match[2]]


def live_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_data_collection_live")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--profile", choices=["btc-5m"])
    selection.add_argument("--config", type=Path)
    selection.add_argument("--resume")
    parser.add_argument("--root", type=Path, default=Path("var/warehouse"))
    parser.add_argument("--dotenv", type=Path, default=Path(".env"))
    parser.add_argument("--duration", type=duration)
    parser.add_argument("--validate-config", action="store_true")
    parser.add_argument("--no-normalize", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.resume:
            from .supervisor import resume_profile

            profile = resume_profile(args.root, args.resume)
        else:
            profile = load_profile(args.config)
        credentials = load_credentials(args.dotenv, required=profile.reference.required)
        if args.validate_config:
            print(
                canonical(
                    {
                        "config_ok": True,
                        "profile_hash": profile.hash,
                        "reference_credentials_present": credentials is not None,
                    }
                )
            )
            return 0
        from .supervisor import Collector

        collector = Collector(
            args.root,
            profile,
            credentials,
            collection=args.resume,
            normalize_live=not args.no_normalize,
        )
        print(
            canonical(
                {
                    "collection_id": collector.collection,
                    "session_id": collector.session,
                    "root": str(collector.root),
                }
            ),
            flush=True,
        )
        result = asyncio.run(collector.run(args.duration))
        print(canonical(result))
        # 3 stays reserved for raw durability; derived lag is recoverable offline.
        return 5 if result.get("derived_processing_error") else 0
    except (ValueError, FileNotFoundError, ImportError) as exc:
        print(canonical({"error": type(exc).__name__, "reason": str(exc)}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        from .capture.writer import DurabilityError

        print(canonical({"error": type(exc).__name__}), file=sys.stderr)
        return 3 if isinstance(exc, DurabilityError) else 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tyrex-data")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in (
        "normalize",
        "quality",
        "status",
        "verify",
        "export-market",
        "compact",
        "list-markets",
    ):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, default=Path("var/warehouse"))
        command.add_argument("--collection")
        if name in ("normalize", "quality"):
            command.add_argument("--follow", action="store_true")
            command.add_argument("--stop-file", type=Path, help=argparse.SUPPRESS)
        if name == "verify":
            command.add_argument("--require-ready", action="store_true")
            command.add_argument("--market")
        if name == "export-market":
            command.add_argument("--market", required=True)
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "status":
            if not args.collection:
                raise ValueError("status requires collection")
            result = read_json(within(root, f"collections/{args.collection}/status.json"))
            result["heartbeat_age_s"] = (time.time_ns() - result["heartbeat_at"]) / 10**9
            result["heartbeat_stale"] = result["heartbeat_age_s"] > 15
            collection_dir = within(root, f"collections/{args.collection}")
            session_path = collection_dir / "sessions" / f"{result.get('session_id')}.json"
            if session_path.exists():
                session = read_json(session_path)
                for key in (
                    "capture_clean",
                    "derived_processing_complete",
                    "derived_processing_error",
                ):
                    result[key] = session.get(key)
            for key, filename in (
                ("quality", "quality_status.json"),
                ("quality_progress", "quality_progress.json"),
            ):
                if (collection_dir / filename).exists():
                    publication = read_json(collection_dir / filename)
                    result[key] = {
                        k: v
                        for k, v in publication.items()
                        if k
                        not in (
                            "files",
                            "input_build_ids",
                            "sealed_raw_segment_ids",
                            "provider_policy",
                        )
                    }
        elif args.command == "quality":
            from .quality.publish import publish_quality

            if not args.collection:
                raise ValueError("quality requires collection")
            saved = read_json(within(root, f"collections/{args.collection}/collection.json"))
            interval = saved.get("profile", {}).get("operations", {}).get("quality_interval_s", 30)
            with Ownership(root, "quality-service"):
                while True:
                    result = publish_quality(root, args.collection)
                    if not args.follow or (args.stop_file and args.stop_file.exists()):
                        break
                    deadline = time.monotonic() + interval
                    while time.monotonic() < deadline:
                        if args.stop_file and args.stop_file.exists():
                            break
                        time.sleep(min(1, max(0, deadline - time.monotonic())))
        elif args.command == "normalize":
            from .normalize.worker import normalize_once

            with Ownership(root, "normalizer-service"):
                while True:
                    result = normalize_once(root, args.collection)
                    if not args.follow or (args.stop_file and args.stop_file.exists()):
                        break
                    time.sleep(2)
        elif args.command == "compact":
            from .normalize.worker import compact

            if not args.collection:
                raise ValueError("compaction requires collection")
            result = compact(root, args.collection)
        elif args.command == "verify":
            from .query.export import verify

            result = verify(
                root, args.collection, require_ready=args.require_ready, market=args.market
            )
            print(canonical(result))
            if args.collection:
                atomic_json(
                    within(
                        root, f"collections/{args.collection}/reports/verify-{time.time_ns()}.json"
                    ),
                    result,
                )
            return 0 if result["ok"] else 1
        else:
            from .query.reader import Reader

            reader = Reader(root, args.collection)
            if args.command == "export-market":
                from .query.export import export_market

                result = export_market(reader, args.market, args.output.resolve())
            else:
                result = reader.list_markets()
        print(canonical(result))
        return 0
    except (ValueError, OSError, ImportError, json.JSONDecodeError) as exc:
        print(canonical({"error": type(exc).__name__, "reason": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
