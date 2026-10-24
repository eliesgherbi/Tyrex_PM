"""Market conditions per window, for stratified window sets.

Computed once per recording from the reference ticks and the CLOB top-of-book quotes, and
cached next to the warehouses under the recording's build stamp:

- ``hour_utc``: hour of the window start.
- ``realised_vol_bp``: standard deviation of 1-second log returns of the reference over the
  window, scaled to the window (× √n), in basis points. The reference is Chainlink's 60-second
  TWAP, so this reads lower than raw BTC volatility; use it to rank windows, not as BTC vol.
- ``move_bp``: |log(close / K)| in basis points, K being the reference at the window start.
- ``spread``: mean best-ask minus best-bid over the window, averaged over the Up and Down
  legs [USDC/share].
- ``wide_spread_share``: share of quotes wider than one 0.01 tick, averaged over the legs.
  Recorded quotes carry no sizes, so top-of-book depth is not offered.
"""

from __future__ import annotations

import bisect
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tyrex_pm.data_warehouse.capture.files import read_json
from tyrex_pm.data_warehouse.query.reader import Reader

from .artifacts import plan_digest, write_json
from .inventory import _live_stamp, _root_stamp

FEATURES_SCHEMA = "workflow_window_features_v2"
FEATURE_KEYS = ("hour_utc", "realised_vol_bp", "move_bp", "spread", "wide_spread_share")
_MEMORY: dict[str, dict] = {}


def _cache_path(warehouse: Path, collection: str) -> Path:
    stamp = _live_stamp(warehouse) or _root_stamp([warehouse])
    key = plan_digest([FEATURES_SCHEMA, str(warehouse.resolve()), collection, list(stamp)])
    return warehouse.resolve().parent / "research" / "cache" / "features" / f"{key}.json"


def _time(row: dict) -> int:
    return int(row.get("source_time") or row["received_at"])


_COLLECTIONS: dict[tuple, tuple] = {}


def collection_data(warehouse: Path, collection: str) -> tuple:
    """Reference ticks and per-leg quotes of one recording, read once per process."""
    key = (str(warehouse.resolve()), collection, _live_stamp(warehouse) or _root_stamp([warehouse]))
    cached = _COLLECTIONS.get(key)
    if cached is not None:
        return cached
    reader = Reader(warehouse, collection)
    ticks = sorted(
        (_time(row), float(row["value"]))
        for row in reader.iter_rows("reference_observations")
        if row.get("value") is not None
    )
    leg_of: dict[str, tuple[str, str]] = {}
    for row in reader.iter_rows("instruments"):
        label = str(row.get("normalized_label") or "")
        if row.get("market_id") and label in ("UP", "DOWN"):
            leg_of[str(row["instrument_id"])] = (str(row["market_id"]), label)
    quotes: dict[tuple[str, str], list[tuple[int, float, float]]] = {}
    for row in reader.iter_rows("quote_observations"):
        owner = leg_of.get(str(row.get("instrument_id")))
        bid, ask = row.get("best_bid"), row.get("best_ask")
        if owner is None or bid is None or ask is None:
            continue
        quotes.setdefault(owner, []).append((_time(row), float(bid), float(ask)))
    for rows in quotes.values():
        rows.sort()
    _COLLECTIONS.clear()
    _COLLECTIONS[key] = (ticks, quotes)
    return _COLLECTIONS[key]


def _compute(warehouse: Path, collection: str, windows: list[dict]) -> dict[str, dict]:
    ticks, quotes = collection_data(warehouse, collection)
    tick_times = [item[0] for item in ticks]
    out: dict[str, dict] = {}
    for window in windows:
        market = window["market_id"]
        start, end = int(window["window_start_ns"]), int(window["window_end_ns"])
        lo, hi = bisect.bisect_left(tick_times, start), bisect.bisect_right(tick_times, end)
        values = [value for _, value in ticks[lo:hi] if value > 0]
        returns = [math.log(b / a) for a, b in zip(values, values[1:])]
        vol = (
            statistics.pstdev(returns) * math.sqrt(len(returns)) * 1e4 if len(returns) > 2 else None
        )
        move = abs(math.log(values[-1] / values[0])) * 1e4 if len(values) > 1 else None
        spreads, wide = [], []
        for leg in ("UP", "DOWN"):
            rows = [ask - bid for t, bid, ask in quotes.get((market, leg), []) if start <= t <= end]
            if rows:
                spreads.append(statistics.fmean(rows))
                wide.append(sum(1 for value in rows if value > 0.015) / len(rows))
        out[market] = {
            "hour_utc": datetime.fromtimestamp(start / 1e9, UTC).hour,
            "realised_vol_bp": None if vol is None else round(vol, 2),
            "move_bp": None if move is None else round(move, 2),
            "spread": round(sum(spreads) / len(spreads), 4) if spreads else None,
            "wide_spread_share": round(sum(wide) / len(wide), 3) if wide else None,
        }
    return out


def window_features(rows: list[dict]) -> dict[str, dict]:
    """Features for inventory rows, computed per recording once and then cached."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if row.get("window_start_ns") and row.get("window_end_ns") and row.get("collection"):
            groups.setdefault((str(row["warehouse"]), str(row["collection"])), []).append(row)
    features: dict[str, dict] = {}
    for (warehouse, collection), members in groups.items():
        path = _cache_path(Path(warehouse), collection)
        cached = _MEMORY.get(str(path))
        if cached is None and path.is_file():
            try:
                body = read_json(path)
                cached = body["features"] if body.get("schema") == FEATURES_SCHEMA else None
            except (OSError, ValueError, KeyError):
                cached = None
        missing = [row for row in members if cached is None or row["market_id"] not in cached]
        if missing:
            computed = _compute(Path(warehouse), collection, missing)
            cached = {**(cached or {}), **computed}
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, {"schema": FEATURES_SCHEMA, "features": cached})
        _MEMORY[str(path)] = cached
        for row in members:
            features[row["market_id"]] = cached.get(row["market_id"]) or {}
    return features


def feature_reason(features: dict, ranges: dict) -> str | None:
    """Exclusion reason when a window falls outside a requested feature range."""
    for key, bounds in (ranges or {}).items():
        if key not in FEATURE_KEYS or not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            continue
        value = features.get(key)
        low, high = bounds
        if value is None:
            return f"{key}_unavailable"
        if (low is not None and value < float(low)) or (high is not None and value > float(high)):
            return "outside_condition_filters"
    return None


def library_features(request: dict, root: Path) -> dict[str, Any]:
    """Features and their quantiles for the windows in scope, for filter controls."""
    del root
    from .inventory import discover

    rows = [row for row in discover({**request, "limit": 100000})["windows"] if row.get("block_id")]
    features = window_features(rows)
    summary = {}
    for key in FEATURE_KEYS:
        values = sorted(v[key] for v in features.values() if v.get(key) is not None)
        if values:
            pick = lambda q: values[min(len(values) - 1, int(q * (len(values) - 1)))]  # noqa: E731
            summary[key] = {
                "min": values[0],
                "p33": pick(1 / 3),
                "p50": pick(0.5),
                "p67": pick(2 / 3),
                "max": values[-1],
            }
    return {"features": features, "summary": summary, "keys": list(FEATURE_KEYS)}


def window_series(request: dict, root: Path) -> dict[str, Any]:
    """One window's books (best bid and ask of both legs) and reference, per second.

    Data only, before any replay. Includes the 5 minutes before the market opens. The
    quotes are the venue's best_bid_ask messages; a second without a message keeps the last
    value, and a second before the first message is empty.
    """
    del root
    warehouse = Path(str(request["warehouse"]))
    collection = str(request["collection"])
    market = str(request["market_id"])
    start = int(request["window_start_ns"])
    end = int(request.get("window_end_ns") or start + 300 * 10**9)
    first, last = start - 300 * 10**9, end + 30 * 10**9
    ticks, quotes = collection_data(warehouse, collection)
    seconds = list(range(first, last + 1, 10**9))

    def stepped(rows: list[tuple], pick) -> list[float | None]:
        out, index, value = [], 0, None
        for second in seconds:
            while index < len(rows) and rows[index][0] <= second:
                value = pick(rows[index])
                index += 1
            out.append(value)
        return out

    reference = stepped(
        [row for row in ticks if first - 120 * 10**9 <= row[0] <= last], lambda r: r[1]
    )
    legs = {}
    for leg in ("UP", "DOWN"):
        rows = [row for row in quotes.get((market, leg), []) if row[0] <= last]
        legs[leg] = {
            "bid": stepped(rows, lambda r: r[1]),
            "ask": stepped(rows, lambda r: r[2]),
        }
    k_index = next((i for i, second in enumerate(seconds) if second >= start), None)
    return {
        "market_id": market,
        "window_start_ns": start,
        "window_end_ns": end,
        "t_ns": seconds,
        "reference": reference,
        "k_reconstructed": reference[k_index] if k_index is not None else None,
        "up": legs["UP"],
        "down": legs["DOWN"],
        "source": "best_bid_ask quotes and Chainlink 60 s TWAP reference",
    }
