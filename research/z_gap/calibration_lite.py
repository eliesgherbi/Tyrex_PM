"""Calibration-lite: EWMA sigma + fair-value sanity on recordings (A0.3).

Does NOT unlock enforce. Produces operator-review artifacts only.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from tyrex_pm.quant.binary_fair_value import FairValueInput, compute_fair_value
from tyrex_pm.quant.volatility import EwmaVolatilityEstimator, SigmaConfig

MIN_EVAL_ROWS = 5
DECILES = 10


@dataclass
class MarketRecordingContext:
    market_id: str
    event_start_ts: float
    event_end_ts: float
    price_to_beat: Decimal | None = None
    resolved_up: bool | None = None
    pm_mid: Decimal | None = None


@dataclass
class EvalRow:
    market_id: str
    eval_ts: float
    tau_s: float
    p_up: float
    outcome_up: int
    pm_mid: float | None


@dataclass
class CalibrationLiteReport:
    input_dir: str
    generated_at_utc: str
    calibration_lite_status: str
    recommendation: str
    eval_row_count: int
    model_brier: float | None
    pm_mid_brier: str | float | None
    reliability_table: list[dict[str, Any]] = field(default_factory=list)
    markets_seen: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_iso_ts(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.isdigit():
        return float(text)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _decimal_price(raw: object) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except Exception:
        return None


def _btc_price_from_payload(payload: dict[str, Any]) -> Decimal | None:
    mid = _decimal_price(payload.get("mid"))
    if mid is not None:
        return mid
    bid = _decimal_price(payload.get("bid"))
    ask = _decimal_price(payload.get("ask"))
    if bid is not None and ask is not None:
        return (bid + ask) / Decimal("2")
    return _decimal_price(payload.get("price"))


def load_btc_ticks(recording_root: Path) -> list[tuple[float, Decimal]]:
    ticks: list[tuple[float, Decimal]] = []
    for path in sorted(recording_root.rglob("events*.jsonl")):
        if "btc_binance" not in path.as_posix() and "external" not in path.as_posix():
            continue
        for event in _iter_jsonl(path):
            if event.get("event_type") != "external_btc_tick":
                continue
            payload = event.get("payload") or {}
            px = _btc_price_from_payload(payload)
            ts = _parse_iso_ts(event.get("source_ts")) or _parse_iso_ts(event.get("recv_ts"))
            if px is None or ts is None:
                continue
            ticks.append((ts, px))
    ticks.sort(key=lambda x: x[0])
    return ticks


def load_market_contexts(recording_root: Path) -> dict[str, MarketRecordingContext]:
    markets: dict[str, MarketRecordingContext] = {}
    for path in sorted(recording_root.rglob("events*.jsonl")):
        for event in _iter_jsonl(path):
            et = event.get("event_type")
            mid = str(event.get("market_id") or "").strip()
            if not mid.startswith("btc_5m_"):
                continue
            payload = event.get("payload") or {}
            if et == "market_discovered":
                markets[mid] = MarketRecordingContext(
                    market_id=mid,
                    event_start_ts=float(payload.get("event_start_ts") or 0),
                    event_end_ts=float(payload.get("event_end_ts") or 0),
                )
            elif et == "price_to_beat_observed":
                ctx = markets.setdefault(
                    mid,
                    MarketRecordingContext(
                        market_id=mid,
                        event_start_ts=float(payload.get("event_start_ts") or 0),
                        event_end_ts=float(payload.get("event_end_ts") or 0),
                    ),
                )
                ctx.price_to_beat = _decimal_price(payload.get("price_to_beat"))
            elif et == "market_resolved":
                ctx = markets.setdefault(
                    mid,
                    MarketRecordingContext(
                        market_id=mid,
                        event_start_ts=0.0,
                        event_end_ts=0.0,
                    ),
                )
                raw = payload.get("raw") or {}
                winning = str(raw.get("winning_outcome") or payload.get("winning_outcome") or "").lower()
                if winning in {"up", "yes"}:
                    ctx.resolved_up = True
                elif winning in {"down", "no"}:
                    ctx.resolved_up = False
            elif et == "best_bid_ask":
                ctx = markets.setdefault(
                    mid,
                    MarketRecordingContext(market_id=mid, event_start_ts=0.0, event_end_ts=0.0),
                )
                raw = payload.get("raw") or {}
                bid = _decimal_price(raw.get("best_bid"))
                ask = _decimal_price(raw.get("best_ask"))
                if bid is not None and ask is not None:
                    ctx.pm_mid = (bid + ask) / Decimal("2")
    return markets


def build_eval_rows(
    *,
    ticks: list[tuple[float, Decimal]],
    markets: dict[str, MarketRecordingContext],
    sigma_cfg: SigmaConfig | None = None,
) -> list[EvalRow]:
    cfg = sigma_cfg or SigmaConfig(min_samples_s=5, sample_interval_s=1)
    rows: list[EvalRow] = []
    if not ticks:
        return rows

    for ctx in markets.values():
        if ctx.price_to_beat is None or ctx.resolved_up is None:
            continue
        if ctx.event_end_ts <= ctx.event_start_ts:
            continue
        est = EwmaVolatilityEstimator(cfg)
        for ts, px in ticks:
            if ts < ctx.event_start_ts or ts > ctx.event_end_ts:
                continue
            vol = est.update(px, datetime.fromtimestamp(ts, tz=timezone.utc))
            tau_s = ctx.event_end_ts - ts
            fair = compute_fair_value(
                FairValueInput(
                    S=px,
                    K=ctx.price_to_beat,
                    sigma=vol.sigma,
                    tau_s=tau_s,
                    tau_floor_s=cfg.tau_floor_s,
                    snapshot_ts=datetime.fromtimestamp(ts, tz=timezone.utc),
                ),
                vol=vol,
            )
            if fair.model_status != "ready" or fair.p_up is None:
                continue
            pm_mid = float(ctx.pm_mid) if ctx.pm_mid is not None else None
            rows.append(
                EvalRow(
                    market_id=ctx.market_id,
                    eval_ts=ts,
                    tau_s=tau_s,
                    p_up=fair.p_up,
                    outcome_up=1 if ctx.resolved_up else 0,
                    pm_mid=pm_mid,
                )
            )
    return rows


def brier_score(predictions: list[float], outcomes: list[int]) -> float:
    if not predictions:
        raise ValueError("empty predictions")
    return sum((p - float(y)) ** 2 for p, y in zip(predictions, outcomes, strict=True)) / len(predictions)


def reliability_table(rows: list[EvalRow], *, buckets: int = DECILES) -> list[dict[str, Any]]:
    if not rows:
        return []
    sorted_rows = sorted(rows, key=lambda r: r.p_up)
    n = len(sorted_rows)
    table: list[dict[str, Any]] = []
    for i in range(buckets):
        start = (i * n) // buckets
        end = ((i + 1) * n) // buckets
        chunk = sorted_rows[start:end]
        if not chunk:
            continue
        mean_pred = sum(r.p_up for r in chunk) / len(chunk)
        realized = sum(r.outcome_up for r in chunk) / len(chunk)
        table.append(
            {
                "bucket": i + 1,
                "predicted_mean": round(mean_pred, 6),
                "realized_frequency": round(realized, 6),
                "count": len(chunk),
            }
        )
    return table


def classify_status(
    *,
    row_count: int,
    model_brier: float | None,
    reliability: list[dict[str, Any]],
) -> tuple[str, str]:
    if row_count < MIN_EVAL_ROWS:
        return (
            "insufficient_data",
            "Insufficient evaluation rows; collect more live/recorded windows before enforce review.",
        )
    if model_brier is None:
        return "fail", "Model Brier score unavailable."
    if model_brier > 0.35:
        return "fail", f"Model Brier {model_brier:.4f} exceeds fail threshold 0.35."
    max_gap = 0.0
    for row in reliability:
        gap = abs(row["predicted_mean"] - row["realized_frequency"])
        max_gap = max(max_gap, gap)
    if max_gap > 0.35:
        return "warn", f"Reliability gap {max_gap:.3f} is large; operator review required."
    if model_brier > 0.25:
        return "warn", f"Model Brier {model_brier:.4f} is elevated; operator review required."
    return "pass", "Calibration-lite sanity checks passed; operator sign-off still required for enforce."


def run_calibration_lite(
    *,
    input_dir: Path,
    output_dir: Path,
    sigma_cfg: SigmaConfig | None = None,
) -> CalibrationLiteReport:
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    ticks = load_btc_ticks(input_dir)
    markets = load_market_contexts(input_dir)
    rows = build_eval_rows(ticks=ticks, markets=markets, sigma_cfg=sigma_cfg)

    model_brier: float | None = None
    pm_mid_brier: str | float | None = "unavailable"
    rel_table: list[dict[str, Any]] = []
    notes: list[str] = []

    if rows:
        model_brier = brier_score([r.p_up for r in rows], [r.outcome_up for r in rows])
        rel_table = reliability_table(rows)
        pm_rows = [r for r in rows if r.pm_mid is not None]
        if pm_rows:
            pm_mid_brier = brier_score([r.pm_mid for r in pm_rows], [r.outcome_up for r in pm_rows])
        else:
            pm_mid_brier = "unavailable"
            notes.append("PM mid not available in fixture; pm_mid_brier set to unavailable.")
    else:
        notes.append("No ready model evaluation rows produced from recordings.")

    if len(ticks) < MIN_EVAL_ROWS:
        notes.append(f"Only {len(ticks)} BTC ticks found under input_dir.")

    status, recommendation = classify_status(
        row_count=len(rows),
        model_brier=model_brier,
        reliability=rel_table,
    )

    report = CalibrationLiteReport(
        input_dir=str(input_dir),
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        calibration_lite_status=status,
        recommendation=recommendation,
        eval_row_count=len(rows),
        model_brier=round(model_brier, 6) if model_brier is not None else None,
        pm_mid_brier=pm_mid_brier if isinstance(pm_mid_brier, str) else round(pm_mid_brier, 6),
        reliability_table=rel_table,
        markets_seen=sorted(markets.keys()),
        notes=notes,
    )

    json_path = output_dir / "calibration_lite_report.json"
    md_path = output_dir / "calibration_lite_report.md"
    json_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return report


def render_markdown(report: CalibrationLiteReport) -> str:
    lines = [
        "# Z-Gap Calibration-Lite Report",
        "",
        f"**Status:** `{report.calibration_lite_status}`",
        f"**Generated:** {report.generated_at_utc}",
        f"**Input:** `{report.input_dir}`",
        "",
        f"**Recommendation:** {report.recommendation}",
        "",
        "## Summary",
        "",
        f"- Eval rows: {report.eval_row_count}",
        f"- Model Brier: {report.model_brier}",
        f"- PM mid Brier: {report.pm_mid_brier}",
        f"- Markets seen: {', '.join(report.markets_seen) or '(none)'}",
        "",
        "## Reliability table",
        "",
        "| Bucket | Predicted mean | Realized freq | Count |",
        "|--------|----------------|---------------|-------|",
    ]
    for row in report.reliability_table:
        lines.append(
            f"| {row['bucket']} | {row['predicted_mean']:.4f} | {row['realized_frequency']:.4f} | {row['count']} |"
        )
    if not report.reliability_table:
        lines.append("| — | — | — | 0 |")
    if report.notes:
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {n}" for n in report.notes)
    lines.append("")
    lines.append(
        "> This report does **not** unlock `entry_mode: enforce`. "
        "Operator must set `calibration_lite_review.json` after review."
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Z-Gap calibration-lite offline report")
    parser.add_argument(
        "--input",
        dest="input_dir",
        default="tests/fixtures/recordings/golden_day",
        help="Recording root (golden_day or var/recordings)",
    )
    parser.add_argument(
        "--output-dir",
        default="research/output/z_gap",
        help="Output directory for JSON/Markdown artifacts",
    )
    args = parser.parse_args(argv)
    report = run_calibration_lite(
        input_dir=Path(args.input_dir),
        output_dir=Path(args.output_dir),
    )
    print(json.dumps(report.to_dict(), indent=2))
    print(f"\nWrote {Path(args.output_dir) / 'calibration_lite_report.json'}")
    print(f"STATUS: {report.calibration_lite_status}")
    return 0 if report.calibration_lite_status in {"pass", "warn", "insufficient_data"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
