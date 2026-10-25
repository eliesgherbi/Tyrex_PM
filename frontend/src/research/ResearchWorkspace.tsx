// The Research workbench: ported from the approved mockup (ResearchWorkbench.tsx) without its shell.
// Saved sets, configurations, previews, jobs, results and traces come from workflow operations.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, Field, Notice, Pair, Panel, Tag } from "../design/WorkflowReviewUI";
import { call, type Envelope } from "../api";
import { ReplayBuilder } from "./ReplayBuilder";
import { ReplayExplorer, type TraceState } from "./ReplayExplorer";
import { ReplayResults, type HistoryPage, type HistoryQuery } from "./ReplayResults";
import { SavedSetChoices } from "./SavedSetChoices";
import { TunePreview } from "./TunePreview";
import { created, flatten, fmtMoney, policyLabel, show, strategyName, type Capabilities, type Flat, type ReplayConfig, type ResultView, type SetSummary } from "./model";

export type Screen = "Replay" | "Tune" | "Results" | "Explorer" | "Compare" | "Candidates";
// A failed job resumes from its saved windows; the backend refuses when its inputs changed.
const resumable = (state: string) => state === "interrupted" || state === "failed";
type Job = { experiment_id: string; job_id: string; state: string; completed: number; expected: number; error: string; configurations: string[] };
type PreviewExperiment = { experiment_id: string; configurations: string[]; resolved: { name: string; fingerprint: string; parameters: ReplayConfig["parameters"] }[]; execution: Flat; scenario_name: string; units: number; windows: number; execution_fingerprint: string };
type Estimate = { experiment_id: string; basis_windows: number; entries: number | null; fills: number | null; verdict?: string };
type Preview = { estimate?: Estimate[]; reserved_overlap?: { reservation_id: string; name: string; windows: number }[]; batch_id: string; name: string; selection_id: string; selection_digest?: string; research_policy?: string; windows: number; configurations: number; units: number; experiments: PreviewExperiment[]; limitation?: string | null };
type Candidate = { candidate_id: string; name?: string; rationale: string; result_ids: string[]; configuration_name?: string; configuration_fingerprint?: string; research_policy?: string; experiment_id?: string; selection_id?: string; frozen: boolean; created_at?: string; origin?: string; scenario?: string };

const SESSION = "tyrex-research:";
const store = {
  get<T>(key: string, fallback: T): T { try { return (JSON.parse(sessionStorage.getItem(SESSION + key) || "null") as T) ?? fallback; } catch { return fallback; } },
  set(key: string, value: unknown) { try { sessionStorage.setItem(SESSION + key, JSON.stringify(value)); } catch { /* storage unavailable */ } },
  drop(key: string) { try { sessionStorage.removeItem(SESSION + key); } catch { /* storage unavailable */ } },
};
const reason = (envelope: Envelope | null | undefined, fallback = "The request failed.") => envelope?.error?.reason || envelope?.error?.code || fallback;
const live = (state: string) => state === "preparing" || state === "running";
async function safeCall(operation: string, request: Record<string, unknown> = {}): Promise<Envelope> {
  try { return await call(operation, request); } catch { return { ok: false, result: null, error: { code: "unreachable", reason: "The workspace API is not reachable." } }; }
}

export function ResearchWorkspace({ screen, onScreen, theme, handoff, onOpenData, openJob }: { screen: Screen; onScreen: (screen: Screen) => void; theme: string; handoff: { selectionId: string; token: number } | null; onOpenData: () => void; openJob?: { jobId: string; token: number } | null }) {
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [capsError, setCapsError] = useState("");
  const [sets, setSets] = useState<SetSummary[]>([]);
  const [setsState, setSetsState] = useState({ loading: false, error: "" });
  const [setId, setSetId] = useState<string | null>(() => store.get("set", null));
  const [generation, setGeneration] = useState(() => store.get("generation", 0));
  const [step, setStep] = useState<number>(() => store.get("step", 0));
  const [preview, setPreview] = useState<Preview | null>(() => store.get("preview", null));
  const [previewError, setPreviewError] = useState("");
  const [busy, setBusy] = useState("");
  const [jobs, setJobs] = useState<Job[]>(() => store.get("jobs", []));
  const [jobNote, setJobNote] = useState("");
  const [drawer, setDrawer] = useState<"data" | "candidate" | null>(null);
  // Results
  const [historyQuery, setHistoryQuery] = useState<HistoryQuery>({ query: "", status: "all", order: "newest", page: 0 });
  const [history, setHistory] = useState<HistoryPage>({ runs: [], total: 0, strategies: [], previews: 0, loading: true, error: "" });
  const [historyOpen, setHistoryOpen] = useState(false);
  const [selectedJob, setSelectedJob] = useState<string | null>(() => store.get("job", null));
  const [configIndex, setConfigIndex] = useState<number>(() => store.get("config", 0));
  const [ledger, setLedger] = useState({ filter: "all", cursor: 0 });
  const [result, setResult] = useState<ResultView | null>(null);
  const [resultState, setResultState] = useState({ loading: false, error: "" });
  const [traceState, setTraceState] = useState<TraceState>({ unitId: store.get("unit", ""), loading: false, trace: null, reason: "" });
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const resultToken = useRef(0), traceToken = useRef(0), historyToken = useRef(0);

  useEffect(() => { store.set("set", setId); store.set("generation", generation); store.set("step", step); store.set("preview", preview); store.set("jobs", jobs); store.set("job", selectedJob); store.set("config", configIndex); store.set("unit", traceState.unitId); }, [setId, generation, step, preview, jobs, selectedJob, configIndex, traceState.unitId]);

  const [capsKind, setCapsKind] = useState("settlement_value");
  const loadCaps = useCallback((kind?: string) => safeCall("strategy.capabilities", { strategy_kind: kind || capsKind }).then((envelope) => { if (envelope.ok && envelope.result) { setCaps(envelope.result as unknown as Capabilities); setCapsError(""); } else setCapsError(reason(envelope)); }), [capsKind]);
  const loadSets = useCallback(() => { setSetsState({ loading: true, error: "" }); return safeCall("selection.list", { limit: 200 }).then((envelope) => { if (envelope.ok) setSets(((envelope.result?.selections as SetSummary[]) || []).slice().sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")))); setSetsState({ loading: false, error: envelope.ok ? "" : reason(envelope) }); }); }, []);
  useEffect(() => { void loadCaps(); void loadSets(); }, [loadCaps, loadSets]);
  const set = sets.find((item) => item.selection_id === setId) || null;
  const scenarioLabel = useCallback((name?: string) => caps?.scenarios.find((item) => item.name === name)?.label || name || "Scenario not stored", [caps]);

  const draftKey = (id: string | null) => `${SESSION}draft:${id}:${generation}`;
  // A different set or an explicit new replay starts a clean draft. History and saved items stay.
  const freshDraft = useCallback((id: string | null) => {
    store.drop(`draft:${id}:${generation}`);
    setGeneration((value) => value + 1);
    setPreview(null); setPreviewError(""); setStep(0); setJobs([]); setJobNote(""); setDrawer(null);
    onScreen("Replay");
    window.scrollTo({ top: 0 });
  }, [generation, onScreen]);
  useEffect(() => { if (drawer === "data") void loadSets(); }, [drawer, loadSets]);
  const useSet = (item: SetSummary) => { setSetId(item.selection_id); freshDraft(item.selection_id); };
  useEffect(() => {
    if (!handoff) return;
    setSetId(handoff.selectionId);
    freshDraft(handoff.selectionId);
    void loadSets();
  }, [handoff?.token]);

  // Preview: the server resolves every configuration and builds the units from the saved set.
  const runPreview = async (configs: ReplayConfig[], name: string) => {
    if (!set) return;
    setPreviewError(""); setBusy("preview");
    const envelope = await safeCall("replay.preview", { selection_id: set.selection_id, name, mode: "replay", configurations: configs.map((c) => ({ name: c.name, strategy_kind: c.strategy, parameters: c.parameters, scenario: c.scenario, execution: c.execution, ...(c.protection ? { protection: c.protection } : {}) })) });
    setBusy("");
    if (!envelope.ok || !envelope.result?.valid) { setPreview(null); setPreviewError(!envelope.ok ? reason(envelope) : ((envelope.result?.errors as string[]) || ["The preview was refused."]).join(" ")); return; }
    setPreview(envelope.result as unknown as Preview); setStep(1); window.scrollTo(0, 0);
  };

  // Start uses the reviewed snapshot only. Every started job is kept, even if a later start fails.
  const start = async () => {
    if (!preview || busy) return;
    setBusy("start"); setJobNote("");
    const started: Job[] = [];
    for (const experiment of preview.experiments) {
      const envelope = await safeCall("job.start", { experiment_id: experiment.experiment_id });
      if (!envelope.ok) {
        const existing = envelope.error?.fields?.job_id as string | undefined;
        setJobNote(`${reason(envelope)}${existing ? ` (job ${existing})` : ""}. ${started.length} of ${preview.experiments.length} experiment(s) started.`);
        if (existing) started.push({ experiment_id: experiment.experiment_id, job_id: existing, state: String(envelope.error?.fields?.state || "unknown"), completed: 0, expected: experiment.units, error: "", configurations: experiment.configurations });
        break;
      }
      started.push({ experiment_id: experiment.experiment_id, job_id: String(envelope.result?.job_id), state: String(envelope.result?.state || "preparing"), completed: 0, expected: experiment.units, error: "", configurations: experiment.configurations });
      setJobs([...started]);
    }
    setJobs(started); setStep(2); setBusy("");
  };

  const activeLive = jobs.some((job) => live(job.state));
  useEffect(() => {
    if (!jobs.length || jobs.every((job) => ["completed", "failed", "interrupted"].includes(job.state))) return;
    let cancelled = false;
    const tick = async () => {
      const next = await Promise.all(jobs.map(async (job) => {
        const envelope = await safeCall("job.status", { job_id: job.job_id });
        if (!envelope.ok || !envelope.result) return { ...job, error: reason(envelope, "Status unavailable") };
        const status = envelope.result;
        const failure = status.error as { reason?: string } | null;
        return { ...job, state: String(status.state || job.state), completed: Number(status.completed ?? job.completed), expected: Number(status.expected ?? job.expected), error: failure?.reason || "" };
      }));
      if (!cancelled) setJobs(next);
    };
    const timer = window.setInterval(() => void tick(), 2000);
    void tick();
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [jobs.map((job) => job.job_id + job.state).join("|")]);
  const control = async (operation: "job.stop" | "job.resume", ids: string[]) => {
    setJobNote("");
    const replies = await Promise.all(ids.map((job_id) => safeCall(operation, { job_id })));
    const refused = replies.filter((envelope) => !envelope.ok);
    if (refused.length) setJobNote(refused.map((envelope) => reason(envelope)).join(" · "));
    setJobs((old) => old.map((job) => {
      const reply = replies[ids.indexOf(job.job_id)];
      if (!reply?.ok) return job;
      return operation === "job.resume" ? { ...job, state: String(reply.result?.state || "preparing") } : { ...job, state: job.state === "running" ? "running" : job.state };
    }));
  };

  // History: paged on the server; filtering never changes the open dashboard.
  const loadHistory = useCallback(async () => {
    const token = ++historyToken.current;
    setHistory((old) => ({ ...old, loading: true, error: "" }));
    const envelope = await safeCall("replay.history", { query: historyQuery.query, status: historyQuery.status, order: historyQuery.order, limit: 5, cursor: historyQuery.page * 5 });
    if (token !== historyToken.current) return;
    if (!envelope.ok) { setHistory((old) => ({ ...old, loading: false, error: reason(envelope) })); return; }
    const body = envelope.result || {};
    setHistory({ runs: (body.runs as HistoryPage["runs"]) || [], total: Number(body.total || 0), strategies: (body.strategies as HistoryPage["strategies"]) || [], previews: Number(body.previews_without_job || 0), loading: false, error: "" });
  }, [historyQuery]);
  useEffect(() => { if (screen === "Results" || screen === "Compare" || screen === "Explorer") void loadHistory(); }, [screen, loadHistory]);
  useEffect(() => { if (screen === "Results" && !selectedJob && !history.loading) { const latest = history.strategies.find((item) => item.latest_job_id)?.latest_job_id; if (latest) openResult(latest); } }, [screen, history.loading, history.strategies, selectedJob]);

  useEffect(() => { if (openJob) { openResult(openJob.jobId); onScreen("Results"); } }, [openJob?.token]);
  const openResult = (jobId: string, index = 0) => { setSelectedJob(jobId); setConfigIndex(index); setLedger({ filter: "all", cursor: 0 }); };
  // Configuration names are learned from the first response for a job; later requests name one.
  const [names, setNames] = useState<{ job: string; names: string[] } | null>(null);
  const knownNames = names?.job === selectedJob ? names.names : null;
  useEffect(() => {
    if (!selectedJob) { setResult(null); return; }
    const token = ++resultToken.current;
    const name = knownNames ? knownNames[configIndex] ?? knownNames[0] : undefined;
    setResultState({ loading: true, error: "" });
    void safeCall("results.open", { job_id: selectedJob, configuration_name: name, ledger: ledger.filter, limit: 50, cursor: ledger.cursor }).then((envelope) => {
      if (token !== resultToken.current) return;
      if (!envelope.ok) { setResultState({ loading: false, error: reason(envelope) }); setResult(null); return; }
      const view = envelope.result as unknown as ResultView;
      if (!knownNames) {
        setNames({ job: selectedJob, names: view.configuration_names });
        if (configIndex > 0 && view.configuration_names[configIndex]) return;
      }
      setResult(view); setResultState({ loading: false, error: "" });
    });
  }, [selectedJob, configIndex, ledger.filter, ledger.cursor, knownNames?.join("|")]);

  const inspect = async (unitId: string) => {
    if (!selectedJob) return;
    const token = ++traceToken.current;
    setTraceState({ unitId, loading: true, trace: null, reason: "" });
    onScreen("Explorer");
    const envelope = await safeCall("results.trace", { job_id: selectedJob, unit_id: unitId });
    if (token !== traceToken.current) return;
    if (!envelope.ok) { setTraceState({ unitId, loading: false, trace: null, reason: reason(envelope) }); return; }
    if (!envelope.result?.available) { setTraceState({ unitId, loading: false, trace: null, reason: String(envelope.result?.reason || "trace_not_saved") }); return; }
    setTraceState({ unitId, loading: false, trace: envelope.result.trace as TraceState["trace"], reason: "" });
  };
  useEffect(() => { if (screen === "Explorer" && result && traceState.unitId && !traceState.trace && !traceState.loading && !traceState.reason) void inspect(traceState.unitId); }, [screen, result?.job_id]);

  const loadCandidates = useCallback(() => safeCall("candidate.list", { limit: 100 }).then((envelope) => { if (envelope.ok) setCandidates((envelope.result?.candidates as Candidate[]) || []); }), []);
  useEffect(() => { if (screen === "Candidates") void loadCandidates(); }, [screen, loadCandidates]);

  const steps = ["Configure", "Preview", "Run"];
  const title = screen === "Replay" ? "Replay" : screen === "Results" ? "Open a run. Understand its result." : screen === "Explorer" ? "From the result to the reason." : screen === "Compare" ? "Compare saved results." : screen === "Candidates" ? "Keep the configuration and the evidence." : "Tune is a different question.";
  const subtitle = screen === "Replay" ? "Choose a strategy, make one configuration yours, validate it—then add another only if you want to." : screen === "Results" ? "Each dashboard belongs to one selected run and one named configuration." : screen === "Explorer" ? "Keep the useful notebook Explorer information, with clear ownership and an inspectable timeline." : screen === "Candidates" ? "Preserve a selected result. Freeze it once. Review export separately from paper launch." : "Same engine, explicit research intent and preserved evidence.";
  const go = (next: Screen) => { onScreen(next); window.scrollTo({ top: 0 }); };
  const newReplay = () => freshDraft(setId);

  return <>
    <nav className="w-research-tabs" aria-label="Research views">{(["Replay", "Tune", "Results", "Compare", "Candidates"] as Screen[]).map((tab) => <button key={tab} className={screen === tab || (tab === "Results" && screen === "Explorer") ? "active" : ""} aria-current={screen === tab ? "page" : undefined} onClick={() => go(tab)}>{tab}{tab === "Tune" && <small>Phase 3</small>}</button>)}</nav>
    <div className="r-page-heading"><div><div className="eyebrow">{screen === "Tune" ? "PHASE 3 · DESIGN PREVIEW" : "RESEARCH WORKBENCH · " + screen.toUpperCase()}</div><h1>{title}</h1><p>{subtitle}</p></div>{(screen !== "Replay" || step > 0) && <Button onClick={newReplay}>＋ New replay</Button>}</div>
    {capsError && <Notice tone="warning" title="Strategy capabilities could not be loaded">{capsError}</Notice>}
    {screen === "Replay" && <>
      <div className="r-journey w-mode-nav"><div className="w-mode-choice"><Tag>Replay</Tag><span>Whole set · explicit configurations</span></div><button onClick={() => go("Tune")}>Tune <small>Phase 3 · preview</small></button></div>
      <div className="w-setup-steps">{steps.map((label, i) => <button key={label} className={step === i ? "active" : ""} disabled={i > step || (i === 1 && !preview)} onClick={() => i <= step && setStep(i)}><span>{i + 1}</span>{label}</button>)}</div>
      {step === 0 && (!set ? <Panel title="1. Use a saved window set" sub="Choose data here, or start from Data Library → Use in experiment." action={<Button onClick={() => setDrawer("data")}>Choose / change window set</Button>}>{setsState.loading ? <p className="r-note">Loading saved window sets…</p> : setId ? <Notice tone="warning" title="The selected set is no longer listed">{setId}. Choose another saved set.</Notice> : <Notice title="No window set is selected">Choose a saved set, or save one in Data first.</Notice>}</Panel>
        : !caps ? <p className="r-note">Loading strategy capabilities…</p>
        : <ReplayBuilder key={set.selection_id + ":" + generation} set={set} caps={caps} draftKey={draftKey(set.selection_id)} busy={busy === "preview"} onPreview={(configs, name) => void runPreview(configs, name)} onChooseData={() => setDrawer("data")} onSaved={() => void loadCaps()} onStrategy={(kind) => { setCapsKind(kind); void loadCaps(kind); }} />)}
      {previewError && step === 0 && <Notice tone="warning" title="Preview was refused">{previewError}</Notice>}
      {step === 1 && preview && <div className="r-stack"><Panel title="Preview the exact replay batch" sub="No objective, no partition, no hidden parameter overlay." action={<Tag>{preview.configurations} configuration(s)</Tag>}>
        <div className="w-preview-summary"><div><strong>{preview.units}</strong><span>replay units</span></div><div><strong>{preview.experiments.length}</strong><span>experiment(s)</span></div><div><strong>{preview.windows}</strong><span>windows · whole set</span></div></div>
        <dl className="details compact"><Pair label="Batch">{preview.name}</Pair><Pair label="Input set / policy">{set?.name || preview.selection_id} · {policyLabel(preview.research_policy)}<small>{preview.selection_id} · digest {String(preview.selection_digest || "").slice(0, 16)}</small></Pair><Pair label="Holdout">None · Replay mode</Pair><Pair label="Configurations">Fully resolved on the server · no later merges</Pair><Pair label="Pinned files">Verified against the saved set</Pair></dl>
        {preview.limitation && <p className="r-note">{preview.limitation}</p>}
        {(preview.reserved_overlap || []).map((item) => <Notice key={item.reservation_id} tone="warning" title={`${item.windows} windows are reserved for the Tune holdout “${item.name}”`}>Replaying them now means a later Tune cannot treat them as unseen data. Use a set without them, or release the reservation in Data.</Notice>)}</Panel>
        {preview.experiments.map((experiment, i) => <Panel key={experiment.experiment_id} title={`Experiment ${i + 1} · ${strategyName("settlement_value")}`} sub={scenarioLabel(experiment.scenario_name)} action={<Tag>{experiment.units} units</Tag>}>
          <div className="w-preview-configs">{experiment.resolved.map((cfg) => <details key={cfg.name}><summary><strong>{cfg.name}</strong><code>{cfg.fingerprint.slice(0, 16)}</code><span>Inspect effective parameters ⌄</span></summary><div className="w-effective-values">{Object.entries(flatten(cfg.parameters)).map(([key, value]) => <div key={key}><code>{key}</code><span>{show(value)}</span></div>)}</div></details>)}</div>
          <details className="w-execution"><summary>Resolved execution assumptions <Tag>{Object.keys(experiment.execution).length} fields</Tag></summary><div className="w-effective-values">{Object.entries(experiment.execution).map(([key, value]) => <div key={key}><code>{key}</code><span>{show(value)}</span></div>)}</div></details>
          {(() => { const est = preview.estimate?.find((item) => item.experiment_id === experiment.experiment_id); if (!est) return null; return <p className="r-note">{est.fills == null ? `No sample-size estimate: fewer than 20 earlier windows ran under ${experiment.scenario_name}.` : `Expected from ${est.basis_windows} earlier windows under ${experiment.scenario_name}: about ${est.entries} entries and ${est.fills} fills. ${est.verdict === "comparable" ? "Enough fills to compare configurations." : est.verdict === "small" ? "A small sample: differences are likely noise." : "Too few fills to compare configurations; use a larger set."} Rates change with the parameters.`}</p>; })()}
          <p className="r-note">{experiment.resolved.length} configuration(s) × {experiment.windows} windows. Experiment {experiment.experiment_id} · execution identity {experiment.execution_fingerprint.slice(0, 16)}.</p></Panel>)}
        <Notice title="Real replay engine">Starting runs every unit through the strategy replay engine on the pinned recordings. Progress is the job’s own; no ETA is invented.</Notice>
        {jobNote && <Notice tone="warning" title="Start was refused">{jobNote}</Notice>}
        <div className="inline"><Button onClick={() => { setPreview(null); setStep(0); }}>← Edit configurations</Button><Button primary disabled={Boolean(busy) || activeLive} onClick={() => void start()}>{busy === "start" ? "Starting…" : "Start this replay →"}</Button></div></div>}
      {step === 2 && <div className="r-stack"><Panel title={activeLive ? "Replay batch in progress" : jobs.some((j) => j.state === "interrupted") ? "Stopped · progress retained" : jobs.some((j) => j.state === "failed") ? "Replay batch failed" : jobs.length && jobs.every((j) => j.state === "completed") ? "Replay batch completed" : "Replay batch"} sub="One job per experiment. Stop and resume retain this job identity.">
        {jobs.map((job) => <div key={job.job_id} className="w-job-row"><div><strong>{preview?.name || "Replay"} · {job.configurations.join(" · ")}</strong><code>{job.job_id} · {job.configurations.length} configuration(s)</code></div><Tag tone={job.state === "completed" ? "good" : job.state === "failed" ? "bad" : "warn"}>{job.state === "interrupted" ? "stopped" : job.state}</Tag><span>{job.completed} / {job.expected} units</span><progress max={job.expected || 1} value={job.completed} />{job.error && <p className="r-note">{job.error}</p>}</div>)}
        {!jobs.length && <p className="r-note">No job was started.</p>}
        {jobNote && <Notice tone="warning" title="The workspace reported a refusal">{jobNote}</Notice>}
        <div className="inline">{activeLive && <Button onClick={() => void control("job.stop", jobs.filter((j) => live(j.state)).map((j) => j.job_id))}>Stop replay batch</Button>}{jobs.some((j) => resumable(j.state)) && <Button primary onClick={() => void control("job.resume", jobs.filter((j) => resumable(j.state)).map((j) => j.job_id))}>Resume same batch</Button>}<Button primary={!activeLive} disabled={!jobs.some((j) => j.completed > 0 || j.state === "completed")} onClick={() => { const done = jobs.find((j) => j.state === "completed") || jobs[0]; openResult(done.job_id); go("Results"); }}>Open this batch’s results →</Button>{!activeLive && <Button onClick={newReplay}>＋ Start a new replay</Button>}</div></Panel></div>}
    </>}
    {screen === "Results" && <ReplayResults history={history} historyQuery={historyQuery} historyOpen={historyOpen} onHistoryOpen={setHistoryOpen} onHistoryQuery={setHistoryQuery} selectedJob={selectedJob} result={result?.job_id === selectedJob ? result : null} resultLoading={resultState.loading} resultError={resultState.error} configIndex={configIndex} ledgerFilter={ledger.filter} ledgerCursor={ledger.cursor} theme={theme} scenarioLabel={scenarioLabel} onSelect={(id) => openResult(id)} onConfig={(i) => { setConfigIndex(i); setLedger({ filter: "all", cursor: 0 }); }} onLedger={(filter, cursor) => setLedger({ filter, cursor })} onInspect={(unit) => void inspect(unit)} onCandidate={() => setDrawer("candidate")} onCompare={() => go("Compare")} onDeleteRun={async (run) => {
      const envelope = await safeCall("replay.delete", { experiment_id: run.experiment_id });
      if (!envelope.ok) return reason(envelope);
      if (run.job_id && run.job_id === selectedJob) { setSelectedJob(null); setResult(null); }
      setJobs((old) => old.filter((job) => job.experiment_id !== run.experiment_id));
      await loadHistory();
      return null;
    }} onResume={(id) => void safeCall("job.resume", { job_id: id }).then((envelope) => { if (!envelope.ok) setResultState({ loading: false, error: reason(envelope) }); else { setJobs([{ experiment_id: result?.experiment_id || "", job_id: id, state: String(envelope.result?.state || "preparing"), completed: result?.completed || 0, expected: result?.expected || 0, error: "", configurations: result?.configuration_names || [] }]); setStep(2); go("Replay"); } })} onNewReplay={newReplay} />}
    {screen === "Explorer" && (result && result.job_id === selectedJob ? <ReplayExplorer key={result.job_id + result.configuration_name} result={result} traceState={traceState} theme={theme} scenarioLabel={scenarioLabel} onSelect={(unit) => void inspect(unit)} onBack={() => go("Results")} onCandidate={() => setDrawer("candidate")} /> : <Notice title={resultState.loading ? "Opening the run…" : "Open a run first"}>{resultState.loading ? "Loading the selected run’s windows." : "Choose a run in Results, then a window."}</Notice>)}
    {screen === "Compare" && <CompareResults runs={history.runs.filter((run) => run.job_id && run.state === "completed")} scenarioLabel={scenarioLabel} onOpen={(job, index) => { openResult(job, index); go("Results"); }} />}
    {screen === "Tune" && <TunePreview />}
    {screen === "Candidates" && <CandidateList candidates={candidates} onFreeze={(id) => void safeCall("candidate.freeze", { candidate_id: id }).then(() => loadCandidates())} onOpen={(c) => { const job = c.result_ids[0]; if (job) { openResult(job); go("Results"); } }} onResults={() => go("Results")} />}
    {drawer && <div className="drawer-backdrop" onClick={() => setDrawer(null)}><section className="drawer r-drawer" role="dialog" aria-modal="true" aria-label={drawer === "data" ? "Choose a window set" : "Save candidate"} onClick={(e) => e.stopPropagation()}><header><span className="eyebrow">{drawer === "data" ? "CHOOSE REPLAY DATA" : "FROM THIS SELECTED RESULT"}</span><button autoFocus className="close-button" aria-label="Close panel" onClick={() => setDrawer(null)}>×</button></header>
      {drawer === "data" ? <><h2>Choose a saved window set</h2><p>This selects the exact inputs for a new experiment. Previous replay results remain in history.</p><SavedSetChoices sets={sets} selected={setId} loading={setsState.loading} error={setsState.error} onUse={useSet} /><p className="r-note">To save a new set, use the Data Library. <button className="text-button" onClick={() => { setDrawer(null); onOpenData(); }}>Open Data →</button></p></>
        : result ? <CandidateForm result={result} configIndex={configIndex} onSaved={() => { setDrawer(null); void loadCandidates(); go("Candidates"); }} /> : <p className="r-note">Open a completed result first.</p>}
    </section></div>}
  </>;
}

function CandidateForm({ result, configIndex, onSaved }: { result: ResultView; configIndex: number; onSaved: () => void }) {
  const name = result.configuration_name || result.configuration_names[configIndex] || "";
  const config = result.configurations.find((c) => c.name === name);
  const [candidateName, setCandidateName] = useState(`${name} · candidate`);
  const [rationale, setRationale] = useState("");
  const [error, setError] = useState("");
  const save = async () => {
    const envelope = await safeCall("candidate.save", { strategy_kind: result.strategy_kind || "settlement_value", parameters: config?.parameters || {}, rationale: rationale.trim(), result_ids: [result.job_id], name: candidateName.trim(), configuration_name: name, configuration_fingerprint: config?.fingerprint, scenario: result.scenario_name, execution: result.execution, research_policy: result.research_policy, selection_id: result.selection_id, experiment_id: result.experiment_id, origin: "replay" });
    if (!envelope.ok) { setError(reason(envelope)); return; }
    onSaved();
  };
  return <><h2>Keep this configuration.</h2><p>{result.batch_name} / {name}. The exact result, parameters and policy come with it.</p><Field label="Candidate name"><input value={candidateName} onChange={(e) => setCandidateName(e.target.value)} /></Field><Field label="Why keep it?"><textarea value={rationale} onChange={(e) => setRationale(e.target.value)} rows={4} placeholder="What did you learn, and what should be checked next?" /></Field><dl className="details"><Pair label="Configuration">{config?.fingerprint || "Unavailable"}</Pair><Pair label="Result">{result.job_id}</Pair><Pair label="Policy">{policyLabel(result.research_policy)}</Pair><Pair label="Holdout">No holdout evaluation · Replay</Pair></dl><Notice title="Save, then freeze once">A candidate is a research checkpoint. Paper / live qualification is unchanged.</Notice>{error && <Notice tone="warning" title="Candidate was refused">{error}</Notice>}<Button primary disabled={!candidateName.trim() || !rationale.trim() || !config} onClick={() => void save()}>Save candidate</Button></>;
}

function CandidateList({ candidates, onFreeze, onOpen, onResults }: { candidates: Candidate[]; onFreeze: (id: string) => void; onOpen: (c: Candidate) => void; onResults: () => void }) {
  if (!candidates.length) return <div className="r-stack"><div className="r-candidate-empty"><span className="r-empty-icon">◇</span><h2>Start from a result you inspected.</h2><p>A candidate is a chosen configuration with the result IDs that justify it—not an empty form.</p><Button primary onClick={onResults}>Open run history →</Button></div></div>;
  return <div className="r-stack">{candidates.map((c) => <Panel key={c.candidate_id} title={c.name || c.configuration_name || c.candidate_id} sub={`${c.result_ids.join(", ") || "No result linked"} · ${created(c.created_at).date}`} action={<Tag tone={c.frozen ? "good" : ""}>{c.frozen ? "Frozen once" : "Saved draft"}</Tag>}><div className="r-candidate-grid"><div><p className="r-rationale">“{c.rationale}”</p><dl className="details compact"><Pair label="Origin">{c.origin === "replay" ? `Replay · ${c.experiment_id || "experiment not stored"}` : "Not stored"}</Pair><Pair label="Configuration">{c.configuration_name || "—"} · {(c.configuration_fingerprint || "").slice(0, 16)}</Pair><Pair label="Holdout">No holdout evaluation</Pair><Pair label="Data policy">{policyLabel(c.research_policy)}</Pair></dl></div><div className="r-candidate-state"><span>PRESERVED WITH THIS CANDIDATE</span><p>Resolved strategy values, execution assumptions, result identity and your rationale.</p><Tag tone="warn">Research evidence · paper launch gated</Tag><div className="inline">{c.result_ids[0] && <Button onClick={() => onOpen(c)}>Reopen source result</Button>}{!c.frozen ? <Button primary onClick={() => onFreeze(c.candidate_id)}>Freeze this candidate</Button> : <Button disabled>Paper export · needs a verified research-tolerance guard</Button>}</div></div></div></Panel>)}</div>;
}

type Pick = { job: string; config: string };
function CompareResults({ runs, scenarioLabel, onOpen }: { runs: { job_id?: string | null; name: string; configurations: string[]; created_at?: string }[]; scenarioLabel: (name?: string) => string; onOpen: (job: string, index: number) => void }) {
  const choices = useMemo(() => runs.flatMap((run) => run.configurations.map((config, index) => ({ id: `${run.job_id}::${config}`, job: run.job_id as string, config, index, label: `${run.name} / ${config} · ${created(run.created_at).date}` }))), [runs]);
  const [left, setLeft] = useState(choices[0]?.id || ""), [right, setRight] = useState(choices[1]?.id || "");
  const [views, setViews] = useState<Record<string, ResultView | string>>({});
  useEffect(() => { if (!left && choices[0]) setLeft(choices[0].id); if (!right && choices[1]) setRight(choices[1].id); }, [choices]);
  useEffect(() => {
    for (const id of [left, right]) {
      if (!id || views[id]) continue;
      const [job, config] = id.split("::");
      void safeCall("results.open", { job_id: job, configuration_name: config, limit: 1 }).then((envelope) => setViews((old) => ({ ...old, [id]: envelope.ok ? (envelope.result as unknown as ResultView) : reason(envelope) })));
    }
  }, [left, right]);
  const a = typeof views[left] === "object" ? (views[left] as ResultView) : null, b = typeof views[right] === "object" ? (views[right] as ResultView) : null;
  const params = (v: ResultView | null) => flatten(v?.configurations.find((c) => c.name === v.configuration_name)?.parameters || {});
  const compatible = Boolean(a && b && a.selection_id === b.selection_id && JSON.stringify(a.execution) === JSON.stringify(b.execution) && a.research_policy === b.research_policy);
  const pa = params(a), pb = params(b);
  const differences = a && b ? Object.keys({ ...pa, ...pb }).filter((k) => JSON.stringify(pa[k]) !== JSON.stringify(pb[k])) : [];
  const pick = (id: string): Pick => { const [job, config] = id.split("::"); return { job, config }; };
  if (choices.length < 2) return <Notice title="Two completed results are needed">Compare is a view across saved results. Complete at least two configurations or runs first.</Notice>;
  return <div className="r-stack"><div className="r-comparison-pair">{([["Left result", left, setLeft], ["Right result", right, setRight]] as const).map(([label, value, change]) => <Panel key={label} title={label}><Field label={"Choose " + label.toLowerCase()}><select value={value} onChange={(e) => change(e.target.value)}>{choices.map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}</select></Field></Panel>)}</div>
    {left === right ? <Notice title="Choose two different saved results">Compare is a view, not a new experiment mode.</Notice> : !a || !b ? <p className="r-note">{[views[left], views[right]].find((v) => typeof v === "string") || "Loading both results…"}</p> : <>
      <Notice tone={compatible ? "success" : "warning"} title={compatible ? "Same pinned windows and execution assumptions" : "Descriptive comparison · assumptions differ"}>{compatible ? `${a.summary.windows} windows on the same saved set. Unknown and unqualified windows stay out of any qualified difference.` : "Both results can be read side by side. Do not call the difference a paired configuration improvement when population, policy or execution differs."}</Notice>
      <div className="r-comparison-pair">{[a, b].map((v, i) => { const s = v.summary; const p = pick(i === 0 ? left : right); return <Panel key={i} title={v.configuration_name || ""} sub={v.batch_name}><div className="w-compare-total">{fmtMoney(s.qualified_pnl)}<small> USDC · qualified P&L</small></div><dl className="details compact"><Pair label="Mode">Replay</Pair><Pair label="Window set">{v.selection_name || v.selection_id}</Pair><Pair label="Policy">{policyLabel(v.research_policy)}</Pair><Pair label="Qualification">{s.qualified} / {s.windows} qualified</Pair><Pair label="Requests → fills (qualified)">{s.qualified_requested_windows} → {s.qualified_fills}</Pair><Pair label="Execution">{scenarioLabel(v.scenario_name)}</Pair><Pair label="Holdout">No holdout evaluation</Pair></dl><Button onClick={() => onOpen(p.job, Math.max(0, v.configuration_names.indexOf(p.config)))}>Open this result →</Button></Panel>; })}</div>
      <Panel title="What actually differs" sub="Configuration differences come from the preserved resolved values.">{differences.length ? <div className="w-diff-list">{differences.map((key) => <div key={key}><span>{key.split(".").at(-1)?.replaceAll("_", " ")}<code>{key}</code></span><b>{show(pa[key])}</b><i>→</i><b>{show(pb[key])}</b></div>)}</div> : <p className="r-note">No strategy parameter differs. Check data, execution, source and mode before interpreting any numerical difference.</p>}</Panel>
    </>}
  </div>;
}
