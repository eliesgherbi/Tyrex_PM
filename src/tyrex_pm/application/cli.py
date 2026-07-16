"""Minimal CLI for the clean-reset skeleton (R1)."""

from __future__ import annotations

import argparse
import sys

from tyrex_pm import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tyrex-pm",
        description="Tyrex_PM - Polymarket-focused event-driven trading framework",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"tyrex-pm {__version__}",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("version", help="Print package version")
    sub.add_parser("help", help="Show help (same as -h)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in (None, "help"):
        parser.print_help()
        return 0
    if args.command == "version":
        print(__version__)
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
