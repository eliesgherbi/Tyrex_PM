from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..contracts import decode, identity

BINANCE = "binance:spot:BTCUSDT"
OUTCOME_PREFIX = "polymarket:137:outcome:"


@dataclass
class Book:
    instrument_id: str
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    valid: bool = False
    connection_id: str | None = None
    session_id: str | None = None
    source_id: str | None = None
    epoch_id: str | None = None
    seed_id: str | None = None
    usable_from: int | None = None
    last_update: int | None = None
    reason: str = "no_seed"
    update_id: int | None = None
    assurance: str = "UNUSABLE"

    def view(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "valid": self.valid,
            "reason": self.reason,
            "epoch_id": self.epoch_id,
            "seed_id": self.seed_id,
            "usable_from": self.usable_from,
            "last_update": self.last_update,
            "assurance": self.assurance,
            "connection_id": self.connection_id,
            "session_id": self.session_id,
            "bids": [
                {"price": p, "quantity": q} for p, q in sorted(self.bids.items(), reverse=True)
            ],
            "asks": [{"price": p, "quantity": q} for p, q in sorted(self.asks.items())],
        }


class Books:
    """Causal book reconstruction.

    CLOB books are kept per (instrument, session, connection) stream. A socket's
    updates apply only to that socket's own snapshot: there is no shared sequence that
    would permit splicing streams. When collector shards overlap during a token move,
    each stream stays independently usable. ``books`` is the per-instrument view: the
    Binance book, and for CLOB the selected valid stream (else the latest one seen).
    """

    def __init__(self, *, retain_epochs: bool = True) -> None:
        self.books: dict[str, Book] = {}
        self.streams: dict[tuple, Book] = {}
        self.epochs: list[dict] = []
        self.binance_buffer: list[dict] = []
        self.confirmed: dict[tuple, int] = {}
        self.retain_epochs = retain_epochs

    def physical(self) -> list[Book]:
        """Every reconstructed book once: CLOB streams plus non-stream (Binance) books."""
        streams = list(self.streams.values())
        members = {id(book) for book in streams}
        return streams + [b for b in self.books.values() if id(b) not in members]

    def forget(self, instrument: str) -> None:
        self.books.pop(instrument, None)
        for key in [k for k in self.streams if k[0] == instrument]:
            del self.streams[key]

    def _select(self, instrument: str) -> None:
        current = self.books.get(instrument)
        if current is not None and current.valid:
            return  # Keep a valid selection stable.
        candidates = [b for k, b in self.streams.items() if k[0] == instrument]
        valid = [b for b in candidates if b.valid]
        if valid:
            self.books[instrument] = max(valid, key=lambda b: b.usable_from or 0)
        elif current is None and candidates:
            self.books[instrument] = candidates[-1]

    def _reselect(self, books: list[Book]) -> None:
        for instrument in {b.instrument_id for b in books if b.source_id != "binance"}:
            self._select(instrument)

    def invalidate(self, book: Book, at: int, reason: str) -> None:
        if book.valid and self.retain_epochs:
            self.epochs.append(
                {
                    **book.view(),
                    "invalidated_at": at,
                    "reason": reason,
                    "reconstruction_version": "3",
                }
            )
        book.valid, book.reason = False, reason

    def apply(self, dataset: str, row: dict) -> None:
        if dataset == "operational_events":
            self._operational(row)
            return
        if dataset == "book_snapshots":
            self._snapshot(row)
            return
        if dataset == "book_updates":
            self._update(row)

    def _operational(self, row: dict) -> None:
        event = decode(row["details"])
        kind = event.get("type")
        if kind == "heartbeat":
            for feed in event.get("status", {}).get("feeds", {}).values():
                if feed.get("connected"):
                    self.confirmed[(row.get("session_id"), feed.get("connection_id"))] = row[
                        "received_at"
                    ]
        affected = []
        if kind in ("session_interrupted", "session_started"):
            previous = event.get("previous_session")
            for book in self.physical():
                if (
                    book.session_id == previous
                    if previous
                    else book.session_id != row.get("session_id")
                ):
                    bound = event.get("last_confirmed_at")
                    if bound is None:
                        bound = max(
                            book.last_update or 0,
                            self.confirmed.get((book.session_id, book.connection_id), 0),
                        )
                    self.invalidate(book, min(row["received_at"], bound), "session_interrupted")
                    book.update_id = None
                    affected.append(book)
            self.binance_buffer.clear()
        elif kind in ("disconnected", "sequence_gap"):
            for book in self.physical():
                if event.get("source") in (None, book.source_id) and (
                    not event.get("connection_id") or event["connection_id"] == book.connection_id
                ):
                    self.invalidate(book, row["received_at"], kind)
                    affected.append(book)
            if event.get("source") == "binance":
                self.binance_buffer.clear()
        elif kind == "subscription" and event.get("operation") == "unsubscribe":
            # The socket stops carrying these tokens; its copy must not stay usable.
            for token in event.get("tokens", []):
                key = (
                    f"{OUTCOME_PREFIX}{token}",
                    row.get("session_id"),
                    event.get("connection_id"),
                )
                book = self.streams.get(key)
                if book is not None:
                    self.invalidate(book, row["received_at"], "unsubscribed")
                    affected.append(book)
        self._reselect(affected)

    def _snapshot(self, row: dict) -> None:
        if row["snapshot_origin"] == "rest_audit":
            return  # No shared sequence permits splicing this into a CLOB WS epoch.
        iid = row["instrument_id"]
        if row["source_id"] == "binance":
            book = self.books.setdefault(iid, Book(iid))
        else:
            book = self.streams.setdefault(
                (iid, row["session_id"], row["connection_id"]), Book(iid)
            )
        self.invalidate(book, row["received_at"], "new_snapshot")
        book.bids = {v["price"]: v["quantity"] for v in row["bids"] if v["quantity"] > 0}
        book.asks = {v["price"]: v["quantity"] for v in row["asks"] if v["quantity"] > 0}
        book.connection_id, book.session_id = row["connection_id"], row["session_id"]
        book.source_id = row["source_id"]
        book.seed_id = row["record_id"]
        book.epoch_id = identity(row["record_id"], "epoch")
        book.usable_from = book.last_update = row["received_at"]
        book.update_id = int(row["provider_update_id"]) if row.get("provider_update_id") else None
        book.valid, book.reason = True, "seeded"
        book.assurance = "SEQUENCED" if book.update_id is not None else "UNKNOWN_CONTINUITY"
        if book.update_id is not None:
            book.valid, book.reason = False, "awaiting_sequence_bridge"
            buffered, self.binance_buffer = self.binance_buffer, []
            for change in buffered:
                if (change["connection_id"], change["session_id"]) == (
                    book.connection_id,
                    book.session_id,
                ):
                    self.apply("book_updates", change)
        self._crossed(book, row["received_at"])
        self._reselect([book])

    def _update(self, row: dict) -> None:
        if row["source_id"] == "binance":
            book = self.books.setdefault(BINANCE, Book(BINANCE))
            if book.update_id is None or (book.connection_id, book.session_id) != (
                row["connection_id"],
                row["session_id"],
            ):
                self.invalidate(book, row["received_at"], "awaiting_bootstrap")
                book.update_id = None
                self.binance_buffer.append(row)
                if len(self.binance_buffer) > 20000:
                    self.binance_buffer.clear()
                    book.reason = "bootstrap_buffer_overflow"
                return
            first, last = int(row["first_update_id"]), int(row["last_update_id"])
            if last <= book.update_id:
                return
            if first > book.update_id + 1:
                self.invalidate(book, row["received_at"], "sequence_gap")
                book.update_id = None
                self.binance_buffer = [row]
                return
            if not book.valid:
                book.usable_from = max(book.usable_from or 0, row["received_at"])
            book.update_id, book.valid, book.reason = last, True, "sequence_bridged"
        touched = {}
        for change in row["changes"]:
            iid = change["instrument_id"]
            if row["source_id"] == "binance":
                book = self.books.setdefault(iid, Book(iid))
                if (book.connection_id, book.session_id) != (
                    row["connection_id"],
                    row["session_id"],
                ):
                    self.invalidate(book, row["received_at"], "connection_without_seed")
                    continue
            else:
                key = (iid, row["session_id"], row["connection_id"])
                book = self.streams.get(key)
                if book is None:
                    # This socket never sent this token a snapshot; other streams are untouched.
                    self.streams[key] = Book(
                        iid,
                        connection_id=row["connection_id"],
                        session_id=row["session_id"],
                        source_id=row["source_id"],
                        reason="connection_without_seed",
                    )
                    self._select(iid)
                    continue
            if not book.valid:
                continue
            side = book.bids if change["side"] == "BUY" else book.asks
            if change["new_quantity"] == 0:
                side.pop(change["price"], None)
            else:
                side[change["price"]] = change["new_quantity"]
            book.last_update = row["received_at"]
            touched[id(book)] = book
        for book in touched.values():  # Validate each book once after the entire batch.
            self._crossed(book, row["received_at"])
        self._reselect(list(touched.values()))

    def _crossed(self, book: Book, at: int) -> None:
        if book.bids and book.asks and max(book.bids) >= min(book.asks):
            self.invalidate(book, at, "crossed_book")
