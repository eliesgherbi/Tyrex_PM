"""Version-owned level maps with lazy immutable publication.

Maps are copied once at a delta boundary so a failed validation cannot corrupt the
last valid state. Sorting and BookLevel construction happen only on a depth read.
"""
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot


class DepthSnapshot:
    def __init__(self, instrument_id, ts_event, bids, asks):
        self.instrument_id = instrument_id
        self.ts_event = ts_event
        self.bid_map, self.ask_map = bids, asks
        self._snapshot = None
        self._best_bid = (None if not bids else BookLevel(
            price=max(bids), quantity=bids[max(bids)]))
        self._best_ask = (None if not asks else BookLevel(
            price=min(asks), quantity=asks[min(asks)]))

    def snapshot(self):
        if self._snapshot is None:
            self._snapshot = BookSnapshot(
                instrument_id=self.instrument_id, ts_event=self.ts_event,
                bids=tuple(BookLevel(price=p, quantity=q) for p, q in self.bid_map.items()),
                asks=tuple(BookLevel(price=p, quantity=q) for p, q in self.ask_map.items()))
        return self._snapshot

    @property
    def bids(self):
        return self.snapshot().bids

    @property
    def asks(self):
        return self.snapshot().asks

    @property
    def best_bid(self):
        return self._best_bid

    @property
    def best_ask(self):
        return self._best_ask


def materialize(book):
    return book.snapshot() if isinstance(book, DepthSnapshot) else book
