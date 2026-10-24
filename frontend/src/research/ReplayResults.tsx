// Ported from the approved mockup (prototypes/ihm-review/src/ReplayResults.tsx).
// Totals come from the server's full-population summary; history is paged on the server.
import { useEffect, useMemo, useRef } from "react";
import { Button, Field, Notice, Panel, Tag } from "../design/WorkflowReviewUI";
import { Chart } from "./Chart";
import { outcomesOption, paletteFor } from "./chartOptions";
import { DeleteControl, runStatusText } from "./Trash";
import { created, executionText, explain, fmtMoney, heldGapText, kCheckText, pnlText, policyLabel, qualificationTag, strategyName, windowDay, windowLabel, type HistoryRun, type ResultView } from "./model";

export type HistoryQuery = { query: string; status: string; order: string; page: number };
export type HistoryPage = { runs: HistoryRun[]; total: number; strategies: { kind: string; runs: number; latest_name?: string | null; latest_job_id?: string | null; latest_state?: string | null }[]; previews: number; loading: boolean; error: string };

type Props = {
  history: HistoryPage;
  historyQuery: HistoryQuery;
  historyOpen: boolean;
  onHistoryOpen: (open: boolean) => void;
  onHistoryQuery: (next: HistoryQuery) => void;
  selectedJob: string | null;
  result: ResultView | null;
  resultLoading: boolean;
  resultError: string;
  configIndex: number;
  ledgerFilter: string;
  ledgerCursor: number;
  theme: string;
  scenarioLabel: (name?: string) => string;
  onSelect: (jobId: string) => void;
  onConfig: (index: number) => void;
  onLedger: (filter: string, cursor: number) => void;
  onInspect: (unitId: string) => void;
  onCandidate: () => void;
  onCompare: () => void;
  onResume: (jobId: string) => void;
  onNewReplay: () => void;
  onDeleteRun: (run: HistoryRun) => Promise<string | null>;
};

const PAGE = 5;
const statusTone = (state: string) => (state === "completed" ? "good" : state === "failed" ? "bad" : "warn");
const statusText = (state: string) => (state === "interrupted" ? "stopped" : state);
const pct = (num: number, den: number) => (den ? Math.round((num / den) * 100) : null);

export function ReplayResults(props: Props) {
  const { history, historyQuery: hq, historyOpen, result, configIndex, theme } = props;
  const dashboard = useRef<HTMLDivElement>(null), scrollOnSelection = useRef(false);
  useEffect(() => { if (scrollOnSelection.current && result) { dashboard.current?.scrollIntoView({ block: "start", behavior: "smooth" }); scrollOnSelection.current = false; } }, [result?.job_id]);
  const openRun = (id: string) => { if (id === props.selectedJob) dashboard.current?.scrollIntoView({ block: "start", behavior: "smooth" }); else { scrollOnSelection.current = true; props.onSelect(id); } };
  const strategies = history.strategies.length ? history.strategies : [];
  const strategy = result?.strategy_kind || strategies[0]?.kind || "settlement_value";
  const strategyRuns = strategies.find((s) => s.kind === strategy)?.runs ?? 0;
  const pages = Math.max(1, Math.ceil(history.total / PAGE)), currentPage = Math.min(hq.page + 1, pages);
  const configName = result?.configuration_name || result?.configuration_names[configIndex] || "";
  const configMeta = result?.configurations.find((c) => c.name === configName);
  const s = result?.summary;
  const rows = result?.series || [];
  const plot = useMemo(() => outcomesOption(rows, paletteFor(theme), -1), [rows, theme]);
  const runChoices = useMemo(() => {
    const listed = history.runs.filter((r) => r.job_id);
    if (result && !listed.some((r) => r.job_id === result.job_id)) listed.unshift({ job_id: result.job_id, name: result.batch_name || "Replay", experiment_id: result.experiment_id, batch_id: "", state: result.job_state || "", strategy_kind: strategy, configurations: result.configuration_names });
    return listed;
  }, [history.runs, result]);
  const finished = result?.job_state === "completed";
  const entryRate = s ? pct(s.qualified_requested_windows, s.qualified) : null;
  const fillRate = s && s.qualified_requested_windows ? pct(s.qualified_fills, s.qualified_requested_windows) : null;
  const states = s?.execution_states || {};

  return <div className="r-stack">
    <div className="w-results-intro"><div><h2>Your strategies and replay history</h2><p>The latest run opens below. Choose a strategy to browse its runs.</p></div><Tag>{history.loading && !history.total ? "Loading history…" : `${history.total} started run(s)${history.previews ? ` · ${history.previews} unstarted preview(s) not listed` : ""}`}</Tag></div>
    {history.error && <Notice tone="warning" title="Run history could not be loaded">{history.error}</Notice>}
    <div className="w-strategy-cards">{strategies.map((item) => <button key={item.kind} className={"w-strategy-card " + (strategy === item.kind ? "selected" : "")} aria-pressed={strategy === item.kind} aria-label={`Browse ${strategyName(item.kind)} runs`} onClick={() => { if (item.latest_job_id) openRun(item.latest_job_id); props.onHistoryOpen(true); props.onHistoryQuery({ query: "", status: "all", order: "newest", page: 0 }); }}><div><strong>{strategyName(item.kind)}</strong><small>Latest: {item.latest_name || "—"}{item.latest_state ? ` · ${statusText(item.latest_state)}` : ""}</small></div><span>{item.runs} runs <b>↗</b></span></button>)}{!strategies.length && !history.loading && <div className="w-select-result"><span>↗</span><h3>No replay has been started yet.</h3><p>Configure a replay and start it from its preview.</p><Button primary onClick={props.onNewReplay}>Configure a replay</Button></div>}</div>
    {strategies.length > 0 && <div className="w-history-toggle"><div><strong>{strategyName(strategy)}</strong><span>{strategyRuns} saved runs · one dashboard at a time</span></div><Button onClick={() => props.onHistoryOpen(!historyOpen)}>{historyOpen ? "Hide run history ↑" : `Browse ${strategyRuns} runs ↓`}</Button></div>}
    {historyOpen && <section className="w-run-history" aria-label="Strategy run history">
      <div className="w-history-controls"><Field label="Find a replay"><input type="search" placeholder="Run, job, configuration or window set…" value={hq.query} onChange={(e) => props.onHistoryQuery({ ...hq, query: e.target.value, page: 0 })} /></Field><Field label="Run status"><select value={hq.status} onChange={(e) => props.onHistoryQuery({ ...hq, status: e.target.value, page: 0 })}><option value="all">All statuses</option><option value="completed">Completed</option><option value="interrupted">Stopped</option><option value="running">Running</option><option value="failed">Failed</option><option value="previewed">Previewed · not started</option></select></Field><Field label="Run order"><select value={hq.order} onChange={(e) => props.onHistoryQuery({ ...hq, order: e.target.value, page: 0 })}><option value="newest">Newest first</option><option value="oldest">Oldest first</option></select></Field></div>
      <div className="table-wrap"><table><thead><tr><th>Replay</th><th>Created · UTC</th><th>Inputs</th><th>Status</th><th></th></tr></thead><tbody>
        {history.runs.map((run) => { const c = created(run.created_at); return <tr key={run.experiment_id} className={run.job_id && run.job_id === props.selectedJob ? "highlight" : ""}><td><strong>{run.name}</strong><small>{run.configurations.join(" · ")}</small></td><td>{c.date}<small>{c.time}</small></td><td>{run.window_count} windows<small>{run.configurations.length} configuration(s){run.selection_name ? ` · ${run.selection_name}` : ""}</small></td><td><Tag tone={statusTone(run.state)}>{statusText(run.state)}</Tag>{run.state !== "completed" && run.job_id && <small>{run.completed} / {run.expected} units</small>}{run.status && runStatusText[run.status] && <small>{runStatusText[run.status].text}{run.scenario_name ? ` · ${run.scenario_name}` : ""}</small>}</td><td><div className="trash-actions">{run.job_id ? <button className="text-button" aria-label={`Open run ${run.name}`} onClick={() => openRun(run.job_id!)}>{props.selectedJob === run.job_id ? "Viewing · open ↓" : "Open result →"}</button> : <span className="r-note">Preview only · no job</span>}<DeleteControl what="this run" disabled={run.state === "running" || run.state === "preparing"} disabledReason="Stop it to delete" onDelete={() => props.onDeleteRun(run)} /></div></td></tr>; })}
        {!history.runs.length && <tr><td colSpan={5}>{history.loading ? "Loading runs…" : "No runs match these filters. Your open dashboard remains below."}</td></tr>}
      </tbody></table></div>
      <div className="w-history-pagination"><span>{history.total ? hq.page * PAGE + 1 : 0}–{Math.min((hq.page + 1) * PAGE, history.total)} of {history.total} runs</span><div className="inline"><Button disabled={hq.page <= 0} onClick={() => props.onHistoryQuery({ ...hq, page: hq.page - 1 })}>← Previous</Button><span>Page {currentPage} / {pages}</span><Button disabled={currentPage >= pages} onClick={() => props.onHistoryQuery({ ...hq, page: hq.page + 1 })}>Next →</Button></div></div>
    </section>}
    {props.resultError && <Notice tone="warning" title="This result could not be opened">{props.resultError}</Notice>}
    {!result ? <div className="w-select-result"><span>↗</span><h3>{props.resultLoading ? "Opening the run…" : "Choose a strategy and open a run."}</h3><p>Each dashboard belongs to one run and one configuration.</p></div> : <>
      <div ref={dashboard} className="w-selected-result"><div><span>OPEN RESULT · {result.job_id}</span><h2>{result.batch_name || "Replay"}</h2><p>{result.selection_name || result.selection_id} · {result.selection_window_count ?? result.window_count} windows · {policyLabel(result.research_policy)} · whole set, no split</p></div><div className="inline"><Field label="Selected run"><select value={result.job_id} onChange={(e) => openRun(e.target.value)}>{runChoices.map((r) => <option key={r.job_id!} value={r.job_id!}>{r.name} · {created(r.created_at).date} {created(r.created_at).time}</option>)}</select></Field><Button onClick={props.onCompare}>Compare results</Button><Button primary disabled={!finished} onClick={props.onCandidate}>Save this configuration as candidate ↗</Button></div></div>
      {!finished && <Panel title={`Run ${statusText(result.job_state || "unknown")}`} sub={result.batch_name}><p className="r-note">{result.completed ?? 0} / {result.expected ?? 0} units saved. {result.error ? `Refusal: ${result.error}. ` : ""}The figures below cover saved windows only. They are not a final result for this set.</p><div className="inline">{(result.job_state === "interrupted" || result.job_state === "failed") && <Button primary onClick={() => props.onResume(result.job_id)}>Resume this run</Button>}<Button onClick={props.onNewReplay}>＋ Start a new replay</Button></div></Panel>}
      <div className="w-configuration-tabs" aria-label="Configuration results">{result.configuration_names.map((name, i) => <button key={name} className={name === configName ? "selected" : ""} aria-pressed={name === configName} onClick={() => props.onConfig(i)}><span>{name}</span><small>{(result.configurations.find((c) => c.name === name)?.fingerprint || "").slice(0, 16)}</small></button>)}</div>
      <div className="w-result-owner"><strong>Dashboard for {configName}</strong><span>{props.scenarioLabel(result.scenario_name)} · {result.job_id}/{(configMeta?.fingerprint || "").slice(0, 16)}</span></div>
      {s && <div className="r-metrics">
        <div><span>Qualified simulated P&L</span><strong>{fmtMoney(s.qualified_pnl)}<small>{s.qualified_pnl === null ? "" : " USDC"}</small></strong><p>{s.qualified_pnl === null ? "No qualified filled window published a number" : `${s.qualified_pnl_rows} qualified filled windows`} · unqualified rows excluded</p></div>
        <div><span>Research qualification</span><strong>{s.qualified}<small> / {s.windows} windows</small></strong><p>{s.unqualified} unqualified{s.unknown_qualification ? ` · ${s.unknown_qualification} unknown` : ""} · still inspectable</p></div>
        <div><span>Entry rate</span><strong>{entryRate === null ? "Unknown" : entryRate}<small>{entryRate === null ? "" : "%"}</small></strong><p>{s.qualified_requested_windows} requested / {s.qualified} qualified windows</p></div>
        <div><span>Fill rate</span><strong>{fillRate === null ? "Unknown" : fillRate}<small>{fillRate === null ? "" : "%"}</small></strong><p>{s.qualified_fills} filled / {s.qualified_requested_windows} qualified requested windows</p></div>
      </div>}
      {s && <div className="r-results-grid"><Panel title={`Window outcomes · ${configName}`} sub="Click a bar or marker to inspect that window."><Chart label={`Qualified simulated P&L by window for ${configName}`} option={plot} height={210} onCategory={(i) => { const row = rows[i]; if (row) props.onInspect(row.unit_id); }} /><p className="r-note">Markers sit on the baseline for identification. Unqualified, unknown, no-trade and no-fill windows are not counted as zero or added into the qualified total.</p></Panel>
        <Panel title="Read the result honestly" sub="Research qualification is scoped to this policy."><div className="r-count-row"><span>All replay windows</span><b>{s.windows}</b></div><div className="r-count-row"><span>Qualified / unqualified</span><b>{s.qualified} / {s.unqualified}</b></div>{(s.qualified_with_notes || 0) > 0 && <div className="r-count-row"><span>Qualified with notes (held through a gap, no order inside)</span><b>{s.qualified_with_notes}</b></div>}{s.k_checks && <div className="r-count-row"><span>K confirmed by the provider</span><b>{s.k_checks.confirmed_by_provider || 0} / {s.windows}</b></div>}<div className="r-count-row"><span>Qualified entry requests</span><b>{s.qualified_requested_windows}</b></div><div className="r-count-row"><span>Qualified filled windows</span><b>{s.qualified_fills}</b></div><div className="r-count-row"><span>All windows · requests → fills</span><b>{s.attempts} → {s.fills}</b></div>{s.pnl_per_fill && s.pnl_per_fill.n > 0 && <div className="r-count-row"><span>Qualified P&L per fill · 95% interval</span><b>{fmtMoney(s.pnl_per_fill.mean)}{s.pnl_per_fill.ci95 ? ` [${fmtMoney(s.pnl_per_fill.ci95[0])}, ${fmtMoney(s.pnl_per_fill.ci95[1])}]` : ""}</b></div>}{s.pnl_per_fill && <p className="r-note">{({ none: "No qualified fill yet: nothing to compare.", too_small: `Only ${s.pnl_per_fill.n} qualified fills: too few to compare configurations. Aim for 30 or more.`, small: `${s.pnl_per_fill.n} qualified fills: a small sample; differences between configurations are likely noise.`, comparable: `${s.pnl_per_fill.n} qualified fills: enough to compare, within the interval shown.` } as Record<string, string>)[s.pnl_per_fill.verdict]}</p>}{Object.keys(states).length > 0 && <p className="r-note">{Object.entries(states).map(([k, v]) => `${v} ${({ no_entry: "no entry", not_sent: "not sent", no_fill: "no fill", partial: "partial", filled: "filled", exited: "exited" } as Record<string, string>)[k] || k}`).join(" · ")}</p>}<Notice title="Replay · no holdout evaluation">This result is exploratory. Nothing here qualifies paper or live trading.</Notice></Panel></div>}
      <Panel title={`Window ledger · ${configName}`} sub={`Only ${result.batch_name || "this run"} / ${configName}. Final outcome and trade P&L remain separate.`} action={<Field label="Filter result windows"><select value={props.ledgerFilter} onChange={(e) => props.onLedger(e.target.value, 0)}><option value="all">All windows</option><option value="qualified">Qualified only</option><option value="unqualified">Unqualified / incomplete</option><option value="nofill">No entry / no fill</option><option value="filled">Filled</option></select></Field>}>
        <div className="table-wrap"><table><thead><tr><th>Window · UTC</th><th>Decision / execution</th><th>Provider-reported outcome</th><th>P&L · USDC</th><th>Qualification</th><th></th></tr></thead><tbody>
          {result.windows.map((r) => { const ex = executionText(r); const value = Number(r.realized_pnl); return <tr key={r.unit_id}><td><strong>{windowLabel(r)}</strong><small>{windowDay(r)}{r.window_clock === "unavailable" ? " · market time not stored" : ""}</small></td><td>{ex.title}<small>{ex.detail}</small></td><td>{r.label_winner ?? "Unresolved"}<small>{r.label_method === "provider_payout" || !r.label_method ? "Provider-reported" : String(r.label_method).replaceAll("_", " ")}</small></td><td className={r.qualified === true && r.filled ? (value > 0 ? "positive" : value < 0 ? "negative" : "") : "subtle"}>{pnlText(r)}{r.qualified !== true && <small>Excluded from qualified summary</small>}</td><td><Tag tone={qualificationTag(r).tone}>{qualificationTag(r).text}</Tag><small>{[...(r.limitations || []).map((l) => explain(l)), heldGapText(r), r.k_check && r.k_check !== "provider_not_published" ? kCheckText[r.k_check] || r.k_check : ""].filter(Boolean).join(" · ")}</small></td><td><button className="text-button" onClick={() => props.onInspect(r.unit_id)} aria-label={`Inspect ${configName} window ${windowLabel(r)}`}>Open Explorer →</button></td></tr>; })}
          {!result.windows.length && <tr><td colSpan={6}>No window in this run matches this filter. The dashboard above is unchanged.</td></tr>}
        </tbody></table></div>
        <div className="w-history-pagination"><span>{result.ledger_total ? props.ledgerCursor + 1 : 0}–{props.ledgerCursor + result.windows.length} of {result.ledger_total} matching windows · totals use all {result.window_total}</span><div className="inline"><Button disabled={props.ledgerCursor === 0} onClick={() => props.onLedger(props.ledgerFilter, Math.max(0, props.ledgerCursor - 50))}>← Previous</Button><Button disabled={result.next_cursor == null} onClick={() => props.onLedger(props.ledgerFilter, Number(result.next_cursor))}>Next →</Button></div></div>
      </Panel>
    </>}
  </div>;
}
