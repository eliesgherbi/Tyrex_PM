import { useEffect, useState } from "react";
import { call, type Envelope } from "./api";

type Selection = {
  selection_id: string;
  name?: string;
  recipe?: string;
  outcome_conditioned?: boolean;
  legacy_unpinned?: boolean;
  limitation?: string | null;
  intended_use?: string;
  research_policy?: string | null;
  research_limitation?: string | null;
};

const metric = {
  name: "realized_pnl",
  unit: "USDC",
  denominator: "resolved_fills",
  eligible_population: "included_windows",
  missing_value_rule: "unknown_is_not_zero",
};

function message(envelope: Envelope | null) {
  if (!envelope) return "";
  if (envelope.ok) return "";
  return envelope.error?.reason || envelope.error?.code || "Request failed";
}

export function ExperimentPage({ selection, onBack }: { selection: Selection | null; onBack: () => void }) {
  const [scenario, setScenario] = useState("friction");
  const [criteria, setCriteria] = useState("Account for every included window. Unknown P&L stays unknown.");
  const [experiment, setExperiment] = useState<Record<string, unknown> | null>(null);
  const [job, setJob] = useState<Record<string, unknown> | null>(null);
  const [results, setResults] = useState<Record<string, unknown> | null>(null);
  const [trace, setTrace] = useState<Record<string, unknown> | null>(null);
  const [notice, setNotice] = useState("");
  const [binding, setBinding] = useState(false);
  const [running, setRunning] = useState(false);

  async function bind() {
    if (!selection || binding) return;
    setBinding(true);
    setNotice("Binding the saved windows into replay units.");
    try {
      const envelope = await call("experiment.bind", {
        selection_id: selection.selection_id,
        scenarios: [scenario],
        baseline: "reference",
        acceptance_criteria: criteria,
        metrics: [metric],
        parameters: {},
        max_units: selection.research_policy === "exploratory" ? 256 : 64,
      });
      if (!envelope.ok) {
        setNotice(message(envelope));
        setExperiment(null);
        return;
      }
      setNotice("Experiment bound. Run replay when you are ready.");
      setExperiment(envelope.result);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "The experiment did not bind.");
    } finally {
      setBinding(false);
    }
  }

  async function run(blockId?: string) {
    if (!experiment?.experiment_id || running) return;
    setRunning(true);
    setNotice("Starting the replay for this recording.");
    try {
      const envelope = await call("job.start", {
        experiment_id: String(experiment.experiment_id),
        ...(blockId ? { block_id: blockId } : {}),
      });
      if (!envelope.ok) {
        setNotice(message(envelope));
        return;
      }
      setNotice("");
      setJob(envelope.result);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "The replay did not start.");
    } finally {
      setRunning(false);
    }
  }

  useEffect(() => {
    const jobId = job?.job_id ? String(job.job_id) : "";
    if (!jobId || job?.state === "completed" || job?.state === "failed") return;
    const timer = setInterval(async () => {
      const status = await call("job.status", { job_id: jobId });
      if (status.ok) setJob(status.result);
    }, 1000);
    return () => clearInterval(timer);
  }, [job?.job_id, job?.state]);

  async function openResults() {
    if (!job?.job_id) return;
    const envelope = await call("results.open", { job_id: job.job_id });
    if (!envelope.ok) setNotice(message(envelope));
    else setResults(envelope.result);
  }

  async function openTrace(unitId: string) {
    if (!job?.job_id) return;
    const envelope = await call("results.trace", { job_id: job.job_id, unit_id: unitId });
    setTrace(envelope.ok ? envelope.result : { available: false, reason: envelope.error?.reason });
  }

  return (
    <div className="dl-experiment-draft">
      <div className="workflow-heading">
        <div>
          <div className="eyebrow">DATA LIBRARY → EXPERIMENT</div>
          <h2>Research on a saved window set</h2>
        </div>
        <button className="button" onClick={onBack}>Back to data</button>
      </div>
      {!selection && <div className="notice"><strong>No selection</strong><p>Save a window set, then open an experiment. This page does not use the synthetic explorer bundle.</p></div>}
      {selection && (
        <div className="notice">
          <strong>{selection.name || selection.selection_id}</strong>
          <p>{selection.selection_id} · {selection.intended_use || "development"} {selection.outcome_conditioned ? "· outcome-conditioned diagnostic" : ""}</p>
        </div>
      )}
      {selection?.outcome_conditioned && <div className="dl-diagnostic-note"><strong>Outcome-conditioned diagnostic</strong><p>Results describe this selected population. They are not a representative sample.</p></div>}
      {notice && <div className="dl-toast" role="status">{notice}</div>}
      <div className="wizard-grid">
        <section className="panel">
          <div className="panel-head"><div><h3>Define the comparison</h3><p>Python builds the replay units. The browser sends the selection id and the metric definition.</p></div></div>
          <label className="field"><span>Strategy</span><select value="settlement_value"><option>settlement_value</option></select></label>
          <label className="field"><span>Execution scenario</span>
            <select value={scenario} onChange={(event) => { setScenario(event.target.value); setExperiment(null); }}>
              <option value="friction">friction</option>
              <option value="favorable">favorable</option>
            </select>
          </label>
          <label className="field"><span>Acceptance criteria</span><textarea rows={4} value={criteria} onChange={(event) => setCriteria(event.target.value)} /></label>
          <button className="button primary" disabled={!selection || binding || selection.recipe === "missing_inputs" || selection.legacy_unpinned} onClick={() => void bind()}>{binding ? "Binding the windows…" : "Bind experiment"}</button>
        </section>
        <section className="panel">
          <div className="panel-head"><div><h3>Planned work</h3><p>Duration stays unavailable until a run is measured.</p></div></div>
          {experiment ? (
            <>
              <div className="preview-total">{String(experiment.planned_units)}<span>server-built units</span></div>
              <dl className="details">
                <div><dt>Experiment</dt><dd className="mono">{String(experiment.experiment_id)}</dd></div>
                <div><dt>Population</dt><dd>{String(experiment.population || "—")}</dd></div>
                <div><dt>Limitation</dt><dd>{String(experiment.limitation || "none")}</dd></div>
                <div><dt>K acceptance</dt><dd>{Array.isArray(experiment.k_acceptance) ? (experiment.k_acceptance as string[]).join(", ") : "—"}</dd></div>
                <div><dt>Estimate</dt><dd>Unavailable · no measured sample</dd></div>
              </dl>
              <div className="inline">
                {Array.isArray(experiment.block_jobs) && (experiment.block_jobs as { block_id: string; units: number }[]).length > 1
                  ? (experiment.block_jobs as { block_id: string; units: number }[]).map((block, index) => (
                    <button className="button primary" key={block.block_id} disabled={running} title={block.block_id} onClick={() => void run(block.block_id)}>{running ? "Starting…" : `Run ${index + 1} · ${block.units} windows`}</button>
                  ))
                  : <button className="button primary" disabled={running} onClick={() => void run((experiment.block_jobs as { block_id: string }[] | undefined)?.[0]?.block_id)}>{running ? "Starting…" : "Run replay"}</button>}
                {job?.job_id != null && <button className="button" onClick={() => void call("job.stop", { job_id: String(job.job_id) })}>Stop</button>}
              </div>
            </>
          ) : <p className="plot-caption">Bind the saved set before any unit exists.</p>}
          {job && (
            <div className="ux-share">
              <div className="ux-meter-track"><i style={{ width: `${Math.min(100, (Number(job.expected) > 0 ? (Number(job.completed) || 0) / Number(job.expected) : job.state === "completed" ? 1 : 0) * 100)}%` }} /></div>
              <span>Job {String(job.state)} · {String(job.completed ?? "—")} / {String(job.expected ?? "—")} windows</span>
            </div>
          )}
          {job?.job_id != null && <button className="button" onClick={() => void openResults()}>Open saved results</button>}
          {results && (
            <>
              {results.limitation && <div className="dl-diagnostic-note"><strong>Result limitation</strong><p>{String(results.limitation)}</p></div>}
              <p className="plot-caption">A finished job is not a qualified result. {String(results.qualified_windows ?? 0)} windows are qualified and {String(results.unqualified_windows ?? 0)} are unqualified. Unqualified P&L stays on its own row and is not added into a qualified total.</p>
              <table>
                <thead><tr><th>Unit</th><th>Market</th><th>Qualification</th><th>Limitations</th><th>P&L</th><th>Trace</th></tr></thead>
                <tbody>
                  {((results.windows as Record<string, unknown>[]) || []).map((row) => (
                    <tr key={String(row.unit_id)}>
                      <td className="mono">{String(row.unit_id)}</td>
                      <td className="mono">{String(row.market_id || "—")}</td>
                      <td>{row.qualified === true ? "Qualified" : "Unqualified"}</td>
                      <td>{Array.isArray(row.limitations) ? (row.limitations as unknown[]).map(String).join("; ") || "—" : "—"}</td>
                      <td>{row.realized_pnl == null ? "Unknown" : String(row.realized_pnl)}</td>
                      <td><button className="text-button" onClick={() => void openTrace(String(row.unit_id))}>Availability</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!((results.windows as unknown[]) || []).length && <p>No saved result parts yet.</p>}
            </>
          )}
          {trace && <p>Trace available: {String(trace.available)}. {String(trace.reason || "")}</p>}
        </section>
      </div>
    </div>
  );
}

export function RunsPage() {
  const [profile, setProfile] = useState("");
  const [report, setReport] = useState<Record<string, unknown> | null>(null);
  const [notice, setNotice] = useState("");
  async function preflight() {
    const envelope = await call("paper.preflight", { profile });
    if (!envelope.ok) setNotice(message(envelope));
    else {
      setNotice("");
      setReport(envelope.result);
    }
  }
  return (
    <>
      <div className="notice"><strong>Paper launch stays gated</strong><p>Preflight is observational. This screen cannot submit an order or start a paper session.</p></div>
      <section className="panel">
        <label className="field"><span>Profile path</span><input value={profile} onChange={(event) => setProfile(event.target.value)} placeholder="Path to a paper profile YAML" /></label>
        <button className="button" disabled={!profile} onClick={() => void preflight()}>Read preflight</button>
        <button className="button primary" disabled>Launch paper · gated</button>
        {notice && <p className="inline-warning">{notice}</p>}
        {report && <pre>{JSON.stringify({ launch_allowed: report.launch_allowed, blockers: report.blockers }, null, 2)}</pre>}
      </section>
    </>
  );
}

export function EvidencePage() {
  const [directory, setDirectory] = useState("");
  const [status, setStatus] = useState<Record<string, unknown> | null>(null);
  const [notice, setNotice] = useState("");
  async function readStatus() {
    const envelope = await call("paper.status", { report_directory: directory });
    if (!envelope.ok) setNotice(message(envelope));
    else {
      setNotice("");
      setStatus(envelope.result);
    }
  }
  return (
    <>
      <div className="notice"><strong>Evidence is what the operations return</strong><p>A missing trace stays unavailable. Research P&L is not paper qualification.</p></div>
      {notice && <p className="inline-warning">{notice}</p>}
      <section className="panel">
        <label className="field"><span>Report directory</span><input value={directory} onChange={(event) => setDirectory(event.target.value)} /></label>
        <button className="button" disabled={!directory} onClick={() => void readStatus()}>Read paper status</button>
        {status && <pre>{JSON.stringify(status, null, 2)}</pre>}
      </section>
      <section className="panel">
        <h3>Gates that remain closed</h3>
        <ul>
          <li>Step 2 follow-up is still deferred.</li>
          <li>Paper launch and restart are not available.</li>
          <li>ask70 capture is not available.</li>
          <li>A new recording session is not started from this workspace.</li>
        </ul>
      </section>
    </>
  );
}

export function CandidatesPage() {
  const [saved, setSaved] = useState<Record<string, unknown> | null>(null);
  const [notice, setNotice] = useState("");
  async function save() {
    const envelope = await call("candidate.save", { strategy_kind: "settlement_value", parameters: {} });
    if (!envelope.ok) setNotice(message(envelope));
    else setSaved(envelope.result);
  }
  async function freeze() {
    if (!saved?.candidate_id) return;
    const envelope = await call("candidate.freeze", { candidate_id: saved.candidate_id });
    if (!envelope.ok) setNotice(message(envelope));
    else setSaved({ ...saved, ...(envelope.result || {}) });
  }
  return (
    <section className="panel">
      <h3>Candidate</h3>
      <p>Saving a candidate does not choose a winner or open evaluation.</p>
      {notice && <p className="inline-warning">{notice}</p>}
      <div className="inline">
        <button className="button primary" onClick={() => void save()}>Save candidate</button>
        <button className="button" disabled={!saved?.candidate_id} onClick={() => void freeze()}>Freeze candidate</button>
      </div>
      {saved && <pre>{JSON.stringify(saved, null, 2)}</pre>}
    </section>
  );
}
