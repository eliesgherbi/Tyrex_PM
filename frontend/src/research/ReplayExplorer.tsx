// Ported from the approved mockup (prototypes/ihm-review/src/ReplayExplorer.tsx).
// Every line, marker and inspector value is read from the saved trace of one window.
import { useEffect, useMemo, useState } from "react";
import { Button, Field, Notice, Pair, Panel, Tag } from "../../../prototypes/ihm-review/src/WorkflowReviewUI";
import { Chart } from "./Chart";
import { paletteFor, timelineBounds, timelineOption } from "./chartOptions";
import { eventsFrom, executionText, explain, fmtMoney, fmtPrice, fmtShares, heldGapText, kCheckText, nsToMs, pnlText, policyLabel, show, ticksFrom, utcMs, windowDay, windowLabel, type EventRow, type ResultView, type Tick, type Trace } from "./model";

export type TraceState = { unitId: string; loading: boolean; trace: Trace | null; reason: string };

type Props = {
  result: ResultView;
  traceState: TraceState;
  theme: string;
  scenarioLabel: (name?: string) => string;
  onSelect: (unitId: string) => void;
  onBack: () => void;
  onCandidate: () => void;
};

const actionText: Record<string, string> = { WAIT: "Wait", ENTER: "Enter", HOLD: "Hold position", SKIP: "Skip", HOST_SKIPPED: "Skip · required input unavailable" };

// The saved row at or just before a time. Rows sharing one instant prefer the decision that acted.
function current(ticks: Tick[], ms: number): Tick | null {
  let found: Tick | null = null;
  for (const tick of ticks) {
    if (tick.ms > ms) break;
    if (!found || tick.ms !== found.ms || tick.action === "ENTER" || (tick.chosen && !found.chosen)) found = tick;
  }
  return found && ms - found.ms <= 1500 ? found : null;
}

export function ReplayExplorer({ result, traceState, theme, scenarioLabel, onSelect, onBack, onCandidate }: Props) {
  const windows = result.series;
  const position = windows.findIndex((r) => r.unit_id === traceState.unitId);
  const row = windows[position] || windows[0];
  const configName = result.configuration_name || "";
  const configMeta = result.configurations.find((c) => c.name === configName);
  const trace = traceState.trace;
  const ticks = useMemo(() => ticksFrom(trace), [trace]);
  const events = useMemo(() => eventsFrom(trace), [trace]);
  const start = nsToMs(row?.window_start_ns) ?? nsToMs(trace?.window_start_ns) ?? (ticks[0]?.ms ?? 0);
  const end = nsToMs(row?.window_end_ns) ?? nsToMs(trace?.window_end_ns) ?? start + 300000;
  // Warm-up exists only when the saved trace states its loaded range under the market clock contract.
  const rangeStart = trace?.window_clock_contract === "market_interval_v1" ? nsToMs(trace.range_start_ns) : ticks.length && ticks[0].ms < start - 1000 ? ticks[0].ms : null;
  const warmupAvailable = rangeStart != null && rangeStart < start;
  const request = events.find((e) => e.kind === "request" && e.ms != null) || null;
  const fill = events.find((e) => e.kind === "fill" && e.ms != null) || null;
  const exit = events.find((e) => e.kind === "exit" && e.ms != null) || null;
  const firstGap = ticks.find((t) => t.ms >= start && t.ms <= end && t.action === "HOST_SKIPPED") || null;
  const initial = request?.ms ?? ticks.find((t) => t.ms >= start && t.action !== "HOST_SKIPPED" && t.upPoint !== null)?.ms ?? start;
  const [selected, setSelected] = useState<number>(initial);
  const [detail, setDetail] = useState("Decision");
  const [range, setRange] = useState("all");
  const [warmup, setWarmup] = useState(false);
  useEffect(() => { setSelected(initial); setDetail("Decision"); setRange("all"); }, [traceState.unitId, trace]);
  const bounds = timelineBounds({ ticks, events, start, end, rangeStart, warmup: warmup && warmupAvailable, range });
  const tick = current(ticks, selected);
  const unavailable = !tick || tick.action === "HOST_SKIPPED";
  const palette = paletteFor(theme);
  const plot = useMemo(() => timelineOption({ ticks, events, start, end, rangeStart, warmup: warmup && warmupAvailable, palette, range, selected }), [ticks, events, start, end, rangeStart, warmup, warmupAvailable, palette, range, selected]);
  const heldSide = row?.position?.side ? String(row.position.side).toUpperCase() : null;
  const chosen = tick?.chosen || (fill && selected >= (fill.ms ?? Infinity) ? heldSide || fill.side : null) || (request && selected >= (request.ms ?? Infinity) ? request.side : null);
  const point = tick?.upPoint == null || !chosen ? null : chosen === "DOWN" ? 1 - tick.upPoint : tick.upPoint;
  const conservative = !tick || !chosen ? null : chosen === "DOWN" ? (tick.upHigh == null ? null : 1 - tick.upHigh) : tick.upLow;
  const ownAsk = !tick || !chosen ? null : chosen === "DOWN" ? tick.downAsk : tick.upAsk;
  const filledAt = (e: EventRow | null) => e && e.ms != null && selected >= e.ms;
  const stages: [string, number | null, string][] = [
    ...(warmupAvailable ? [["Warm-up", rangeStart, ""] as [string, number | null, string]] : []),
    ["Window opens", start, ""],
    [request ? "Entry request" : "No entry", request?.ms ?? null, ""],
    [fill ? "Fill" : row?.filled ? "Fill · time not stored" : request ? "No fill" : "No position", fill?.ms ?? (request && !row?.filled ? events.find((e) => e.kind === "cancel")?.ms ?? null : null), ""],
    ...(exit ? [["Exit", exit.ms, ""] as [string, number | null, string]] : []),
    ...(firstGap ? [["Input gap", firstGap.ms, "gap"] as [string, number | null, string]] : []),
    ["Window closes", end, ""],
  ];
  const ex = row ? executionText(row) : { title: "", detail: "" };
  const go = (ms: number) => { setSelected(ms); if ((warmup === false) && ms < start) setWarmup(true); };
  const selectWindow = (unit: string) => onSelect(unit);

  if (!row) return <Notice title="No window in this result">Open a run with saved windows first.</Notice>;
  return <>
    <div className="w-selected-result"><div><span>EXPLORER · {result.job_id}</span><h2>{configName}</h2><p>{result.batch_name || "Replay"} · {policyLabel(result.research_policy)} · {scenarioLabel(result.scenario_name)}</p></div><div className="inline"><Button onClick={onBack}>← This run’s dashboard</Button><Button primary disabled={result.job_state !== "completed"} onClick={onCandidate}>Save this configuration ↗</Button></div></div>
    <div className="r-window-nav"><Tag>{result.job_id}/{(configMeta?.fingerprint || "").slice(0, 16)}</Tag><div className="inline"><Button disabled={position <= 0} onClick={() => selectWindow(windows[position - 1].unit_id)}>←</Button><Field label="Selected window"><select aria-label="Selected window" value={row.unit_id} onChange={(e) => selectWindow(e.target.value)}>{windows.map((r) => <option key={r.unit_id} value={r.unit_id}>{windowLabel(r)} UTC · {r.qualified === true ? "Qualified" : r.qualified === false ? "Unqualified" : "Unknown"}</option>)}</select></Field><Button disabled={position < 0 || position >= windows.length - 1} onClick={() => selectWindow(windows[position + 1].unit_id)}>→</Button></div></div>
    <div className="r-window-summary"><div><span>Market</span><strong>BTC · {windowLabel(row)} UTC</strong><small>{windowDay(row)}</small></div><div><span>Acquired position</span><strong>{row.filled ? (row.position?.filled_gross_shares ? `${fmtShares(row.position.filled_gross_shares)} shares · ${row.position.side || "leg not stored"}` : "Filled · size not stored") : "No position"}</strong><small>{ex.title}</small></div><div><span>Provider-reported winner</span><strong>{row.label_winner ?? "Unresolved"}</strong></div><div><span>Simulated window P&L</span><strong>{pnlText(row)}{row.filled ? " USDC" : ""}</strong>{row.qualified !== true && <small>Excluded from qualified summary</small>}</div></div>
    {row.qualification === "qualified_with_notes" && <Notice title="Qualified with a note">{heldGapText(row)}. No computed number depended on the missing book.</Notice>}
    {row.qualified !== true && <Notice tone="warning" title="This window is unqualified">{(row.limitations || []).map((l) => explain(l)).join(" · ") || "No limitation was stored"}. It remains inspectable and is excluded from the qualified summary.</Notice>}
    {traceState.loading && <Notice title="Loading this window’s saved trace">Only the selected window is read.</Notice>}
    {!traceState.loading && traceState.reason && <Notice tone="warning" title="This window has no saved trace">{traceState.reason === "trace_not_saved" ? "The job did not publish a decision trace for this unit." : traceState.reason} Nothing is drawn in its place.</Notice>}
    {trace && trace.window_clock_contract !== "market_interval_v1" && <Notice title="Earlier trace format">This trace was saved before market and load-range clocks were stored separately{(trace.ledger || []).length ? "" : " and before the engine ledger was kept, so fills have no markers"}. Market times come from the saved window set; rows without a clock are omitted.</Notice>}
    <div className="r-explorer-grid"><div className="r-stack"><Panel title="Up and Down · model against each leg’s book" sub="Point and conservative model probabilities against each leg’s own book. P(Down) = 1 − P(Up) under the binary model." action={<Tag>Saved trace</Tag>}>
      <div className="chart-toolbar"><div className="segmented">{[["all", "Full window"], ["entry", "Entry"], ["hold", "After entry"]].map(([key, label]) => <button key={key} className={range === key ? "selected" : ""} disabled={key !== "all" && !request} title={key !== "all" && !request ? "No entry request in this window" : undefined} onClick={() => setRange(key)}>{label}</button>)}</div><label className="check"><input type="checkbox" checked={warmup && warmupAvailable} disabled={!warmupAvailable} onChange={(e) => setWarmup(e.target.checked)} />Warm-up context{!warmupAvailable ? " · not stored" : ""}</label><span className="r-chart-unit">P [0–1] · USDC/share · UTC</span></div>
      {ticks.length ? <Chart label="Up and Down model probabilities and own-book prices; click a timestamp" height={548} option={plot} onTime={(ms) => setSelected(Math.max(bounds.min, Math.min(bounds.max, ms)))} /> : <p className="r-note">{traceState.loading ? "Loading…" : "No timed decision rows are stored for this window."}</p>}
      <div className="r-chart-key"><span>▲ Entry request</span><span>● Simulated fill</span><span>■ Cancelled remainder</span><span>⋮ Arrival</span><span>Gaps remain unavailable</span></div>
      <div className="r-lifecycle">{stages.map(([label, ms, kind]) => <button key={label} disabled={ms == null} className={ms != null && Math.abs(ms - selected) < 500 ? "active" : kind} onClick={() => ms != null && go(ms)}>{label}</button>)}</div>
      <p className="r-note">The inspector follows a chart click, an event or a stage. {ticks.length} timed decision rows{trace?.truncated ? " · trace truncated at 4,000 rows" : ""}. Host-skipped rows and gaps longer than 1.5 s stay empty.</p></Panel>
      <Panel title="Decision → order → fill" sub="A request is not a fill; every event keeps its origin."><div className="table-wrap"><table><thead><tr><th>UTC timestamp</th><th>Event</th><th>Origin</th><th></th></tr></thead><tbody>{events.length ? events.map((event, i) => <tr key={i} className={event.ms != null && Math.abs(event.ms - selected) < 1 ? "highlight" : ""}><td className="mono">{event.ms == null ? "Time not stored" : utcMs(event.ms)}</td><td>{event.label}<small>{event.detail}</small></td><td>{event.origin}</td><td>{event.ms != null && <button className="text-button" onClick={() => { go(event.ms as number); setDetail(event.kind === "request" ? "Decision" : "Execution"); }}>Inspect event</button>}</td></tr>) : <tr><td colSpan={4}>{trace ? "No order event in this window." : "No saved events."}</td></tr>}</tbody></table></div></Panel></div>
      <aside className="r-inspector"><Panel title="Moment inspector" sub={`${windowLabel(row)} UTC · saved trace`}>
        <div className="r-inspector-time">{utcMs(selected)}<small>UTC · {selected < start ? `${((start - selected) / 1000).toFixed(1)} seconds before the window opens` : `${Math.max(0, (end - selected) / 1000).toFixed(1)} seconds remaining`}</small></div>
        <label className="scrubber"><span className="sr-only">Inspect time</span><input type="range" min={bounds.min} max={bounds.max} step={100} value={selected} onChange={(e) => setSelected(Number(e.target.value))} /></label>
        <div className="r-decision"><span>{unavailable ? "HOST AVAILABILITY" : "STRATEGY / LIFECYCLE"}</span><strong>{!tick ? "No decision at this time" : actionText[tick.action] || tick.action}</strong><p>{!tick ? "No saved decision row within 1.5 s. Nothing is interpolated." : unavailable ? `${explain(tick.reason)}. No strategy order can execute here.` : explain(tick.reason)}</p></div>
        <div className="r-inspector-tabs" role="tablist" aria-label="Moment details">{["Decision", "Execution", "Inputs", "Identity"].map((t) => <button role="tab" aria-selected={detail === t} key={t} className={detail === t ? "active" : ""} onClick={() => setDetail(t)}>{t}</button>)}</div>
        <dl className="details compact">{detail === "Decision" ? <><Pair label="Chosen leg">{chosen ?? "None"}</Pair><Pair label="Point P">{point == null ? "Unavailable / no chosen leg" : point.toFixed(3)}</Pair><Pair label="Conservative P">{conservative == null ? "Unavailable / no chosen leg" : conservative.toFixed(3)}</Pair><Pair label="P(Up) point">{tick?.upPoint == null ? "Unavailable" : tick.upPoint.toFixed(3)}</Pair><Pair label="Own-book best ask">{ownAsk == null ? "Unavailable / no chosen leg" : `${ownAsk.toFixed(3)} USDC/share`}</Pair><Pair label="Reason code"><code>{tick?.reason || "—"}</code></Pair></>
          : detail === "Execution" ? <><Pair label="Window execution">{ex.title}</Pair><Pair label="Request at this moment">{request && filledAt(request) ? request.label : "None yet"}</Pair><Pair label="Confirmed fill">{fill && filledAt(fill) ? fill.label : "None at this moment"}</Pair><Pair label="Fee">{fill && filledAt(fill) ? fill.detail : "Not charged yet"}</Pair><Pair label="Order arrival assumption">{show(result.execution.order_delay_ms)} ms</Pair><Pair label="Depth participation">{show(result.execution.participation)}</Pair></>
          : detail === "Inputs" ? <><Pair label="Up bid / ask">{tick && (tick.upBid != null || tick.upAsk != null) ? `${fmtPrice(tick.upBid)} / ${fmtPrice(tick.upAsk)}` : "Invalid / unavailable"}</Pair><Pair label="Down bid / ask">{tick && (tick.downBid != null || tick.downAsk != null) ? `${fmtPrice(tick.downBid)} / ${fmtPrice(tick.downAsk)}` : "Invalid / unavailable"}</Pair><Pair label="Reference S">{tick?.reference == null ? "Unavailable" : `${tick.reference.toFixed(2)} USD`}</Pair><Pair label="Sealed threshold K">{tick?.k == null ? "Unavailable" : `${tick.k.toFixed(2)} USD · from the exact start tick`}<small>{row.k_check ? kCheckText[row.k_check] || row.k_check : "Provider check not stored"}</small></Pair><Pair label="Sigma">{tick?.sigma == null ? "Unavailable" : tick.sigma.toFixed(4)}</Pair><Pair label="Outcome source">Provider-reported · for later scoring</Pair></>
          : <><Pair label="Run">{result.job_id}</Pair><Pair label="Experiment">{result.experiment_id}</Pair><Pair label="Configuration">{configName}</Pair><Pair label="Configuration identity">{configMeta?.fingerprint || "Unavailable"}</Pair><Pair label="Window">{row.market_id}</Pair><Pair label="Research policy">{policyLabel(result.research_policy)}</Pair><Pair label="Trace format">{trace?.schema || "Unavailable"}</Pair><Pair label="Window P&L">{row.filled ? `${fmtMoney(row.realized_pnl)} USDC` : pnlText(row)}</Pair></>}</dl>
      </Panel></aside></div>
  </>;
}
