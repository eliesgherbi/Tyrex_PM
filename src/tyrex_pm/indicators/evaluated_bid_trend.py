"""Bounded, causal trend over evaluated best bids, independent of any strategy."""
from collections import deque
from decimal import Decimal


class EvaluatedBidTrend:
    def __init__(self, *, lookback_s, min_drop, confirmation_s, max_gap_s):
        self.lookback = float(lookback_s)
        self.min_drop = Decimal(str(min_drop))
        self.confirmation = float(confirmation_s)
        self.max_gap = float(max_gap_s)
        self.samples = deque(maxlen=4096)
        self.since = None
        self.epoch = None

    def observe(self, at_s, bid, *, ready=True, epoch=None):
        if (not ready or bid is None or epoch != self.epoch or
                (self.samples and (at_s <= self.samples[-1][0] or
                                  at_s - self.samples[-1][0] > self.max_gap))):
            self.samples.clear()
            self.since = None
        self.epoch = epoch
        out = {"ready": False, "confirmed": False, "drop": None,
               "anchor_at_s": None, "anchor_bid": None, "confirmation_age_s": 0.0}
        if not ready or bid is None:
            return out
        self.samples.append((at_s, bid))
        boundary = at_s - self.lookback
        while len(self.samples) > 1 and self.samples[1][0] <= boundary:
            self.samples.popleft()
        anchor_at, anchor_bid = self.samples[0]
        if anchor_at > boundary or boundary - anchor_at > self.max_gap:
            self.since = None
            return out
        drop = anchor_bid - bid
        out.update(ready=True, drop=str(drop), anchor_at_s=anchor_at, anchor_bid=str(anchor_bid))
        if drop < self.min_drop:
            self.since = None
            return out
        if self.since is None:
            self.since = at_s
        age = at_s - self.since
        out.update(confirmation_age_s=age, confirmed=age >= self.confirmation)
        return out
