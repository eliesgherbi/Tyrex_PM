#!/usr/bin/env python3
"""
Resolve config/v1_markets.yaml slugs via Nautilus PolymarketDataLoader + public CLOB book.

Usage (repo root, after pip install -e .):
  python scripts/resolve_markets.py
  python scripts/resolve_markets.py --config config/v1_markets.yaml --tsv out.tsv
  python scripts/resolve_markets.py --write-notes Docs/validation/v1_01_resolution_notes.md

Requires network access to Polymarket Gamma + CLOB.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from collections.abc import Iterable
from pathlib import Path

from tyrex_pm.core.market_types import AllowlistEntry, ResolvedMarket
from tyrex_pm.data.allowlist import load_market_allowlist
from tyrex_pm.data.resolution import (
    DEFAULT_CHAIN_ID,
    DEFAULT_CLOB_HOST,
    resolve_market_slug,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _print_table(rows: Iterable[ResolvedMarket]) -> None:
    w = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    w.writerow(
        [
            "slug",
            "instrument_id",
            "token_id",
            "price_increment",
            "book_status",
            "book_detail",
            "clob_tick",
            "neg_risk",
            "resolved_at_utc",
        ]
    )
    for r in rows:
        w.writerow(
            [
                r.slug,
                r.instrument_id,
                r.token_id,
                r.price_increment,
                r.book_status,
                r.book_detail,
                r.clob_tick_size or "",
                "" if r.neg_risk is None else str(r.neg_risk).lower(),
                r.resolved_at_utc,
            ]
        )


def _markdown_notes(
    entries: list[AllowlistEntry],
    rows: list[ResolvedMarket],
    *,
    instrument_doc_url: str,
) -> str:
    lines = [
        "# v1.01 market resolution notes (auto-generated skeleton)",
        "",
        f"**Nautilus BinaryOption / instrument provider:** {instrument_doc_url}",
        "",
        "Re-run `python scripts/resolve_markets.py --write-notes "
        "Docs/validation/v1_01_resolution_notes.md` after changing `config/v1_markets.yaml`.",
        "",
        "**Instrument refresh:** `PolymarketDataClient` refreshes the catalogue on a "
        "configurable interval (~60 min in Nautilus Polymarket docs). "
        "Live nodes should tolerate tick metadata updates.",
        "",
    ]
    by_slug = {r.slug: r for r in rows}
    for e in entries:
        r = by_slug[e.slug]
        active = "see book_status / Gamma metadata at resolution time"
        lines.extend(
            [
                f"## {e.slug}",
                "",
                f"- **Allowlist note:** {e.note or '—'}",
                f"- **instrument_id:** `{r.instrument_id}`",
                f"- **token_id (loader / YES outcome default):** `{r.token_id}`",
                f"- **price_increment (Nautilus):** `{r.price_increment}`",
                f"- **CLOB tick_size:** `{r.clob_tick_size}`",
                f"- **neg_risk (Gamma metadata):** `{r.neg_risk}`",
                f"- **Book:** `{r.book_status}` — {r.book_detail}",
                f"- **resolved_at_utc:** `{r.resolved_at_utc}`",
                f"- **Active vs excluded (manual):** reviewer must confirm `{active}` in UI/API.",
                "",
            ]
        )
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    cfg = Path(args.config)
    if not cfg.is_file():
        print(f"ERROR: config not found: {cfg}", file=sys.stderr)
        return 1
    entries = load_market_allowlist(cfg)
    rows: list[ResolvedMarket] = []
    for e in entries:
        rows.append(
            await resolve_market_slug(
                e.slug,
                clob_host=args.clob_host,
                chain_id=args.chain_id,
            )
        )

    if args.tsv:
        outp = Path(args.tsv)
        outp.parent.mkdir(parents=True, exist_ok=True)
        with outp.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, delimiter="\t")
            w.writerow(
                [
                    "slug",
                    "instrument_id",
                    "token_id",
                    "price_increment",
                    "book_status",
                    "book_detail",
                    "clob_tick",
                    "neg_risk",
                    "resolved_at_utc",
                ]
            )
            for r in rows:
                w.writerow(
                    [
                        r.slug,
                        r.instrument_id,
                        r.token_id,
                        r.price_increment,
                        r.book_status,
                        r.book_detail,
                        r.clob_tick_size or "",
                        "" if r.neg_risk is None else str(r.neg_risk).lower(),
                        r.resolved_at_utc,
                    ]
                )
        print(f"Wrote {outp}", file=sys.stderr)
    else:
        _print_table(rows)

    if args.write_notes:
        outp = Path(args.write_notes)
        outp.parent.mkdir(parents=True, exist_ok=True)
        text = _markdown_notes(
            entries,
            rows,
            instrument_doc_url=args.doc_url,
        )
        outp.write_text(text, encoding="utf-8")
        print(f"Wrote {outp}", file=sys.stderr)

    if any(r.book_status == "book_error" for r in rows):
        return 2
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Resolve v1 market allowlist (v1.01).")
    p.add_argument(
        "--config",
        default=str(REPO_ROOT / "config" / "v1_markets.yaml"),
        help="Path to v1_markets.yaml",
    )
    p.add_argument("--clob-host", default=DEFAULT_CLOB_HOST)
    p.add_argument("--chain-id", type=int, default=DEFAULT_CHAIN_ID)
    p.add_argument("--tsv", help="Optional output TSV path (also stdout unless tsv set)")
    p.add_argument(
        "--write-notes",
        help="Write markdown validation notes to this path",
    )
    p.add_argument(
        "--doc-url",
        default="https://nautilustrader.io/docs/nightly/integrations/polymarket/",
        help="Link embedded in generated notes",
    )
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
