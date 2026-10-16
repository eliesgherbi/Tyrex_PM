"""S0 diagnostics: candidate settlement rules versus proxy outcomes.

Agreement with recorded winners is a *diagnostic*, never proof of the rule or of tie
behavior. It is computed from the replayed TWAP series (PolyBolt
``price.crypto.twap``, ``chainlink:btcusd:twap:60``) and proxy outcome labels.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from tyrex_pm.research.strategy_replay.labels import proxy_outcome
from tyrex_pm.research.strategy_replay.loader import NS, WindowCatalog, WindowData

# Candidate close boundaries: the TWAP tick whose source time is the window end, and
# its neighbours one second either side (alignment uncertainty).
CLOSE_OFFSETS_S = (-1, 0, 1)


def twap_by_source_second(catalog: WindowCatalog) -> dict[int, dict]:
    """Latest-received TWAP tick per provider source second (one collection scan)."""
    ticks: dict[int, dict] = {}
    for row in catalog.reader.iter_rows("reference_observations"):
        if row.get("measurement_type") != "TWAP" or row.get("source_time") is None:
            continue
        second = int(row["source_time"]) // NS
        ticks[second] = row
    return ticks


def _label_window(catalog: WindowCatalog, ref) -> WindowData:
    """A WindowData carrying only identity + resolution rows (no timeline load)."""
    from tyrex_pm.data_warehouse.contracts import decode

    rows = catalog.gamma_rows(ref.market_id)
    attrs = decode(rows[-1].get("family_attributes") or "{}") or {}
    inst = {i["normalized_label"]: i for i in attrs.get("instruments", [])}
    return WindowData(
        ref=ref, condition_id="", question="", rules_text=None, resolution_source=None,
        crypto_market_config=None,
        up_instrument_id=inst["UP"]["instrument_id"],
        down_instrument_id=inst["DOWN"]["instrument_id"],
        up_token_id=str(inst["UP"]["token_id"]), down_token_id=str(inst["DOWN"]["token_id"]),
        up_outcome_index=int(inst["UP"]["outcome_index"]),
        down_outcome_index=int(inst["DOWN"]["outcome_index"]),
        events=[], resolution_rows=list(catalog.resolutions.get(ref.market_id, [])),
        metadata_rows=[], range_start_ns=ref.window_start_ns, range_end_ns=ref.window_end_ns,
        source_fingerprint="", load_seconds=0.0,
    )


def settlement_rule_table(catalog: WindowCatalog) -> pd.DataFrame:
    ticks = twap_by_source_second(catalog)
    records = []
    for ref in catalog.windows():
        start_s, end_s = ref.window_start_ns // NS, ref.window_end_ns // NS
        label = proxy_outcome(_label_window(catalog, ref))
        k_row = ticks.get(start_s)
        rec: dict = {
            "slug": ref.slug,
            "market_id": ref.market_id,
            "window_start_s": start_s,
            "proxy_winner": label.winner,
            "label_reason": label.reason,
            "k_exact": None if k_row is None else Decimal(str(k_row["value"])),
            "k_receive_lag_ms": None
            if k_row is None
            else (k_row["received_at"] - start_s * NS) / 1e6,
        }
        for offset in CLOSE_OFFSETS_S:
            row = ticks.get(end_s + offset)
            rec[f"close_{offset:+d}s"] = None if row is None else Decimal(str(row["value"]))
        records.append(rec)
    frame = pd.DataFrame(records)
    if frame.empty:
        return frame
    for offset in CLOSE_OFFSETS_S:
        column = f"close_{offset:+d}s"
        predicted = []
        for k, close in zip(frame["k_exact"], frame[column]):
            if k is None or close is None or pd.isna(k) or pd.isna(close):
                predicted.append(None)
            else:
                predicted.append("UP" if close >= k else "DOWN")  # rules text: ">= start"
        frame[f"rule_{offset:+d}s"] = predicted
        frame[f"agrees_{offset:+d}s"] = [
            None if p is None or w is None else p == w
            for p, w in zip(predicted, frame["proxy_winner"])
        ]
    frame["margin_usd_0s"] = [
        None if k is None or c is None else float(c - k)
        for k, c in zip(frame["k_exact"], frame["close_+0s"])
    ]
    frame["tie_0s"] = [m == 0 if m is not None else None for m in frame["margin_usd_0s"]]
    return frame


def agreement_summary(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for offset in CLOSE_OFFSETS_S:
        column = f"agrees_{offset:+d}s"
        known = frame[column].dropna()
        rows.append(
            {
                "candidate_rule": f"TWAP(close{offset:+d}s) >= TWAP(start)",
                "windows_evaluable": int(len(known)),
                "agree": int(known.sum()),
                "disagree": int((~known.astype(bool)).sum()),
                "agreement_rate": None if known.empty else float(known.mean()),
            }
        )
    rows.append(
        {
            "candidate_rule": "(windows without proxy label or ticks)",
            "windows_evaluable": int(frame["agrees_+0s"].isna().sum()),
        }
    )
    return pd.DataFrame(rows)
