import { Fragment, useEffect, useMemo, useState, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { BrandMark } from "./BrandMark";
import "./style.css";
import "./workbenchMockup.css";

type Scene =
  | "data"
  | "replay-setup"
  | "replay-plan"
  | "replay-running"
  | "replay-stopped"
  | "replay-results"
  | "replay-timeline"
  | "replay-compare"
  | "replay-candidate"
  | "replay-export"
  | "tune-setup"
  | "tune-refused"
  | "tune-plan"
  | "tune-running"
  | "tune-ranking"
  | "tune-generalisation"
  | "tune-stability"
  | "tune-candidate"
  | "evidence"
  | "runs";

type Policy = "exploratory95_v1" | "strict_v1";
type Objective = "pnl" | "fill";
type Page = "Data" | "Research" | "Runs" | "Evidence";

const SCENES: { id: Scene; group: string; label: string }[] = [
  { id: "data", group: "Phase 1 · Data", label: "Policy, funnel, plain set" },
  { id: "replay-setup", group: "Phase 2 · Replay", label: "Configure" },
  { id: "replay-plan", group: "Phase 2 · Replay", label: "Plan, then confirm" },
  { id: "replay-running", group: "Phase 2 · Replay", label: "Run in progress" },
  { id: "replay-stopped", group: "Phase 2 · Replay", label: "Stopped, can resume" },
  { id: "replay-results", group: "Phase 2 · Replay", label: "Summaries and ledger" },
  { id: "replay-timeline", group: "Phase 2 · Replay", label: "Decision timeline" },
  { id: "replay-compare", group: "Phase 2 · Replay", label: "Compare two results" },
  { id: "replay-candidate", group: "Phase 2 · Replay", label: "Save candidate" },
  { id: "replay-export", group: "Phase 2 · Replay", label: "Export paper YAML" },
  { id: "tune-setup", group: "Phase 3 · Tune", label: "Grid and holdout" },
  { id: "tune-refused", group: "Phase 3 · Tune", label: "0% holdout refused" },
  { id: "tune-plan", group: "Phase 3 · Tune", label: "Plan before run" },
  { id: "tune-running", group: "Phase 3 · Tune", label: "Two-stage run" },
  { id: "tune-ranking", group: "Phase 3 · Tune", label: "Ranking" },
  { id: "tune-generalisation", group: "Phase 3 · Tune", label: "Generalisation" },
  { id: "tune-stability", group: "Phase 3 · Tune", label: "Stability" },
  { id: "tune-candidate", group: "Phase 3 · Tune", label: "Candidate from top-k" },
  { id: "evidence", group: "Evidence", label: "Replay and Tune origins" },
  { id: "runs", group: "Unchanged", label: "Runs stay gated" },
];

const RECORDINGS = [
  { id: "1oct", date: "1 Oct", folder: "warehouse_phase_b_validation", detail: "Finished session" },
  { id: "3oct", date: "3 Oct", folder: "warehouse_phase_b_validation", detail: "Finished session" },
  { id: "6oct", date: "6 Oct", folder: "warehouse_btc5m", detail: "14:36–19:36 UTC · 60 windows" },
  { id: "15oct", date: "15 Oct", folder: "warehouse_btc5m_s3", detail: "Finished session · counts not in the 21 Oct record" },
  { id: "19oct", date: "19 Oct", folder: "warehouse", detail: "Finished session" },
];

const LEDGER = [
  { id: "1440", time: "14:40", decision: "No entry", fill: "—", pnl: "—", qual: "Qualified", limit: "Provider-reported outcome" },
  { id: "1505", time: "15:05", decision: "Enter Up", fill: "5.00 @ 0.62", pnl: "+0.41", qual: "Qualified", limit: "Provisional K accepted by policy" },
  { id: "1520", time: "15:20", decision: "Enter Down", fill: "Request, no fill", pnl: "—", qual: "Qualified", limit: "Provider-reported outcome" },
  { id: "1610", time: "16:10", decision: "Enter Up", fill: "5.00 @ 0.71", pnl: "−0.55", qual: "Qualified", limit: "Provider-reported outcome" },
  { id: "1715", time: "17:15", decision: "Enter Down", fill: "3.20 @ 0.58 partial", pnl: "+0.22", qual: "Unqualified", limit: "Book gap during hold · excluded from the summary" },
  { id: "1845", time: "18:45", decision: "Blocked", fill: "—", pnl: "—", qual: "Unqualified", limit: "Reference warm-up short of the policy" },
];

type GridRow = { id: string; edge: string; tau: number; dev: number; hold: number; fillDev: number; fillHold: number };
const GRID: GridRow[] = [
  { id: "e2t120", edge: "0.02", tau: 120, dev: 0.7, hold: 0.2, fillDev: 0.33, fillHold: 0.2 },
  { id: "e2t150", edge: "0.02", tau: 150, dev: 1.4, hold: 0.44, fillDev: 0.5, fillHold: 0.4 },
  { id: "e2t180", edge: "0.02", tau: 180, dev: 0.9, hold: 0.12, fillDev: 0.47, fillHold: 0.2 },
  { id: "e3t120", edge: "0.03", tau: 120, dev: 0.4, hold: 0.15, fillDev: 0.22, fillHold: 0.2 },
  { id: "e3t150", edge: "0.03", tau: 150, dev: 1.1, hold: 0.51, fillDev: 0.38, fillHold: 0.4 },
  { id: "e3t180", edge: "0.03", tau: 180, dev: 0.55, hold: 0.18, fillDev: 0.3, fillHold: 0.2 },
  { id: "e4t120", edge: "0.04", tau: 120, dev: 0.1, hold: 0.04, fillDev: 0.11, fillHold: 0 },
  { id: "e4t150", edge: "0.04", tau: 150, dev: 0.35, hold: 0.09, fillDev: 0.16, fillHold: 0.2 },
  { id: "e4t180", edge: "0.04", tau: 180, dev: 0.22, hold: 0.06, fillDev: 0.14, fillHold: 0 },
];

const SET_WINDOWS = 15;
const USABLE_PATTERN = new Set([0, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48, 52, 56]);

function pageOf(scene: Scene): Page {
  if (scene === "data") return "Data";
  if (scene === "runs") return "Runs";
  if (scene === "evidence") return "Evidence";
  return "Research";
}
function money(value: number) {
  return (value > 0 ? "+" : "") + value.toFixed(2);
}
function tailCount(percent: number) {
  if (!Number.isFinite(percent) || percent <= 0) return 0;
  return Math.min(SET_WINDOWS, Math.round((SET_WINDOWS * percent) / 100));
}
function rankGrid(objective: Objective, topK: number) {
  const score = (row: GridRow) => (objective === "pnl" ? row.dev : row.fillDev);
  const holdScore = (row: GridRow) => (objective === "pnl" ? row.hold : row.fillHold);
  const sorted = [...GRID].sort((a, b) => score(b) - score(a));
  const top = sorted.slice(0, topK);
  const holdOrder = [...top].sort((a, b) => holdScore(b) - holdScore(a));
  return sorted.map((row, index) => ({
    ...row,
    devRank: index + 1,
    inTop: index < topK,
    holdRank: holdOrder.findIndex((item) => item.id === row.id) + 1,
    score: score(row),
    holdScore: holdScore(row),
  }));
}

function Layer({ kind }: { kind: "exists" | "changes" | "new" | "open" }) {
  const label = { exists: "Exists today", changes: "Rule changes", new: "New", open: "Open decision" }[kind];
  return <span className={"wb-layer " + kind}>{label}</span>;
}
function Panel({ title, sub, layer, action, children }: { title: string; sub?: string; layer?: ReactNode; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h3>{title}</h3>
          {sub && <p>{sub}</p>}
        </div>
        <div className="inline">{layer}{action}</div>
      </div>
      {children}
    </section>
  );
}
function Next({ label, onClick }: { label: string; onClick: () => void }) {
  return <div className="wb-actions"><button className="button primary" onClick={onClick}>{label}</button></div>;
}

function Workbench() {
  const [theme, setTheme] = useState("dark");
  const [scene, setScene] = useState<Scene>("data");
  const [policy, setPolicy] = useState<Policy>("exploratory95_v1");
  const [extra, setExtra] = useState(false);
  const [showOld, setShowOld] = useState(false);
  const [holdout, setHoldout] = useState(30);
  const [topK, setTopK] = useState(3);
  const [objective, setObjective] = useState<Objective>("pnl");
  const [scenario, setScenario] = useState("friction");
  const [picked, setPicked] = useState("e2t150");
  const [frozenReplay, setFrozenReplay] = useState(false);
  const [frozenTune, setFrozenTune] = useState(false);
  const [windowId, setWindowId] = useState("1610");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [left, setLeft] = useState("replay-2c");
  const [right, setRight] = useState("replay-3c");
  const [stage, setStage] = useState(0);

  const go = (next: Scene) => { setScene(next); window.scrollTo(0, 0); };
  const page = pageOf(scene);
  const strict = policy === "strict_v1";
  const usable = strict ? 0 : SET_WINDOWS;
  const tail = tailCount(holdout);
  const development = SET_WINDOWS - tail;
  const ranked = useMemo(() => rankGrid(objective, topK), [objective, topK]);
  const chosen = ranked.find((row) => row.id === picked && row.inTop) ?? ranked.find((row) => row.inTop)!;
  const objectiveLabel = objective === "pnl" ? "Realised P&L on qualified rows" : "Fill rate on qualified rows";

  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);
  useEffect(() => {
    if (scene !== "tune-running" && scene !== "replay-running") return;
    const timer = window.setInterval(() => setStage((value) => Math.min(value + 1, 8)), 700);
    return () => window.clearInterval(timer);
  }, [scene]);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#data" onClick={(event) => { event.preventDefault(); go("data"); }}>
          <BrandMark className="brand-cat" />
          <span>tyrex<span className="brand-dot">.</span></span>
        </a>
        <div className="nav-label">WORKSPACE</div>
        <nav aria-label="Main navigation">
          {(["Data", "Research", "Runs", "Evidence"] as Page[]).map((item) => (
            <button key={item} className={"nav-item " + (page === item ? "active" : "")} onClick={() => go(item === "Data" ? "data" : item === "Research" ? (scene.startsWith("tune") ? scene : "replay-setup") : item === "Runs" ? "runs" : "evidence")}>
              <span>{item}</span>
              {item === "Runs" && <span className="nav-count">Gated</span>}
            </button>
          ))}
        </nav>
        <div className="sidebar-note"><div><strong>End-state review</strong><p>Mockup on port 4175. The live workspace stays on 4173.</p></div></div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="breadcrumb">Workspace <span>/</span> <b>{page}</b></div>
          <div className="inline">
            <label className="wb-tag" htmlFor="scene">Review scene</label>
            <select id="scene" className="wb-scene" value={scene} onChange={(event) => go(event.target.value as Scene)}>
              {["Phase 1 · Data", "Phase 2 · Replay", "Phase 3 · Tune", "Evidence", "Unchanged"].map((group) => (
                <optgroup key={group} label={group}>
                  {SCENES.filter((item) => item.group === group).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                </optgroup>
              ))}
            </select>
            <button className="icon-button" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>Theme</button>
          </div>
        </header>
        <div className="wb-banner">
          <p>Review mockup of the end state in <b>research_IHM_enhancment.md</b>. The 6 October counts — 60 windows, 15 usable under exploratory 95%, 0 strict-complete — are from the 21 October record. Every P&L figure is illustrative. This page does not read the warehouse or start a job.</p>
          <div className="wb-legend"><Layer kind="exists" /><Layer kind="changes" /><Layer kind="new" /><Layer kind="open" /></div>
        </div>
        <main>
          {scene === "data" && <DataPage policy={policy} setPolicy={setPolicy} extra={extra} setExtra={setExtra} showOld={showOld} setShowOld={setShowOld} usable={usable} go={go} />}
          {scene.startsWith("replay") && <ReplayPage scene={scene} go={go} scenario={scenario} setScenario={setScenario} windowId={windowId} setWindowId={setWindowId} left={left} right={right} setLeft={setLeft} setRight={setRight} frozen={frozenReplay} setFrozen={setFrozenReplay} stage={stage} setStage={setStage} />}
          {scene.startsWith("tune") && <TunePage scene={scene} go={go} holdout={holdout} setHoldout={setHoldout} topK={topK} setTopK={setTopK} objective={objective} setObjective={setObjective} objectiveLabel={objectiveLabel} scenario={scenario} setScenario={setScenario} ranked={ranked} chosen={chosen} setPicked={setPicked} expanded={expanded} setExpanded={setExpanded} frozen={frozenTune} setFrozen={setFrozenTune} tail={tail} development={development} stage={stage} />}
          {scene === "evidence" && <EvidencePage frozenReplay={frozenReplay} frozenTune={frozenTune} go={go} />}
          {scene === "runs" && <RunsPage />}
          <footer><span>Tyrex research workbench mockup</span><span>Fixture review · no order submit</span></footer>
        </main>
      </div>
    </div>
  );
}

function DataPage(props: { policy: Policy; setPolicy: (value: Policy) => void; extra: boolean; setExtra: (value: boolean) => void; showOld: boolean; setShowOld: (value: boolean) => void; usable: number; go: (scene: Scene) => void }) {
  const strict = props.policy === "strict_v1";
  return (
    <>
      <div className="wb-head"><div><div className="wb-phase">PHASE 1</div><h1>Data library</h1><p>One policy drives the chart, the funnel, the preview, and the save. A window set stores data. It has no development or holdout split.</p></div></div>
      <div className="wb-grid main">
        <div className="wb-stack">
          <Panel title="Policy" sub="Named, versioned readiness profile. Stored on the set and on every configuration that runs on it." layer={<Layer kind="new" />}>
            <div className="wb-policy">
              <button className={props.policy === "exploratory95_v1" ? "selected" : ""} onClick={() => props.setPolicy("exploratory95_v1")}><strong>exploratory95_v1</strong><span>Books at least 95%. Provider-reported outcome. Provisional K accepted. $1 / $0 payout.</span></button>
              <button className={props.policy === "strict_v1" ? "selected" : ""} onClick={() => props.setPolicy("strict_v1")}><strong>strict_v1</strong><span>Every input complete. Confirmed K only. The 6 October recording has 0 windows under this policy.</span></button>
            </div>
            <p className="wb-note">Outcome wording follows the policy: provider-reported outcome for a provider payout. The word proxy appears only when the outcome is derived.</p>
            <div className="wb-callout" style={{ marginTop: 12 }}><Layer kind="open" /> <strong>Whether strict_v1 is offered in Phase 1 is still open.</strong> It is drawn here so the empty result is visible. No live recording currently has a strict-complete window.</div>
          </Panel>
          <Panel title="Recordings" sub="Time order. A window belongs to a recording when its start falls inside that capture." layer={<Layer kind="exists" />}>
            <div className="wb-cards">
              {RECORDINGS.map((item) => {
                const on = item.id === "6oct" || (item.id === "15oct" && props.extra);
                return (
                  <button key={item.id} className={"wb-card " + (on ? "on" : "")} onClick={() => item.id === "15oct" && props.setExtra(!props.extra)} disabled={item.id !== "15oct" && item.id !== "6oct"}>
                    <b>{item.date}</b>
                    <span>{item.folder}</span>
                    <span>{item.detail}</span>
                    <span>{item.id === "6oct" ? "Selected for the set" : item.id === "15oct" ? (props.extra ? "Ticked · counts unmeasured" : "Tick to review a second funnel row") : "Listed, not in this set"}</span>
                  </button>
                );
              })}
            </div>
          </Panel>
          <Panel title="Funnel" sub="Recorded windows, then filters, then usable under this policy, then excluded." layer={<Layer kind="changes" />}>
            <div className="wb-funnel">
              <div><b>60</b><span>Recorded · 6 Oct</span></div>
              <div><b>60</b><span>Match the filters</span></div>
              <div><b>{props.usable}</b><span>Usable under {props.policy}</span></div>
              <div><b>{60 - props.usable}</b><span>Excluded</span></div>
            </div>
            {props.extra && <p className="wb-note">15 Oct is ticked. Its window counts are not in the 21 October record, so this row stays blank: recorded —, usable —. The layout is what Phase 1 has to show per recording.</p>}
            <p className="wb-note">{strict ? "Strict-complete on this recording is 0. That matches the 21 October record." : "15 of 60 is the exploratory 95% count from the 21 October record. One known exclusion is book_below_floor on the 17:55Z window (about 281s / 300s on each book). The reason split of the other 44 is not invented here."}</p>
            <div className="wb-cells" aria-label="60 windows, usable ones marked">
              {Array.from({ length: 60 }, (_, index) => <i key={index} className={!strict && USABLE_PATTERN.has(index) ? "on" : ""} title={USABLE_PATTERN.has(index) ? "Usable position is illustrative" : "Excluded"} />)}
            </div>
            <p className="wb-note">Cell positions are illustrative. The totals are not. The chart, the funnel, and the save use the same policy, so they can no longer disagree.</p>
          </Panel>
        </div>
        <aside className="wb-sticky wb-stack">
          <Panel title="Set preview" sub="What Save will freeze." layer={<Layer kind="changes" />}>
            <dl className="wb-kv">
              <div><dt>Recordings</dt><dd>6 Oct · warehouse_btc5m{props.extra ? " + 15 Oct" : ""}</dd></div>
              <div><dt>Policy</dt><dd>{props.policy}</dd></div>
              <div><dt>Filters</dt><dd>None</dd></div>
              <div><dt>Windows</dt><dd>{props.extra ? "15 + unmeasured" : props.usable}</dd></div>
              <div><dt>Date range</dt><dd>6 Oct 2026</dd></div>
              <div><dt>Partition</dt><dd>None. A set is data.</dd></div>
            </dl>
            <div className="wb-actions">
              <button className="button primary" disabled={props.usable === 0} onClick={() => props.go("replay-setup")}>{props.usable === 0 ? "Nothing to save" : "Save set and open Research"}</button>
            </div>
            <p className="wb-note">Preparation plan stays available. Stop is unsupported and says so. The Research screens in this mockup stay on the 15-window 6 October set, which is the set named in the Phase 2 and Phase 3 done lines.</p>
          </Panel>
          <Panel title="Saved sets" layer={<Layer kind="changes" />}>
            <p className="wb-note">The card shows recordings, policy, filters, count, and date range.</p>
            <label className="check" style={{ marginTop: 10 }}><input type="checkbox" checked={props.showOld} onChange={(event) => props.setShowOld(event.target.checked)} /> Show superseded</label>
            {props.showOld && (
              <ul className="wb-note">
                <li>7470720940814968bac80839d60e1636 · 6 Oct exploratory 95%</li>
                <li>a623f7b6d3cc4525b5310d3613072020 · partial book coverage</li>
                <li>fe35312b19df44a8bcfa2eebfb0580a2 · missing-input diagnostic</li>
                <li>Two 82-window sets · files kept, ids read at implementation</li>
              </ul>
            )}
            <p className="wb-note">Files are kept. They are not rewritten. split.json stays as notebook history and is not a gate.</p>
          </Panel>
        </aside>
      </div>
    </>
  );
}

function ModeSwitch({ scene, go }: { scene: Scene; go: (scene: Scene) => void }) {
  const tune = scene.startsWith("tune");
  return (
    <div className="segmented" style={{ marginBottom: 14 }}>
      <button className={tune ? "" : "selected"} onClick={() => tune && go("replay-setup")}>Replay</button>
      <button className={tune ? "selected" : ""} onClick={() => !tune && go("tune-setup")}>Tune</button>
    </div>
  );
}

function ReplayPage(props: {
  scene: Scene; go: (scene: Scene) => void; scenario: string; setScenario: (value: string) => void;
  windowId: string; setWindowId: (value: string) => void; left: string; right: string;
  setLeft: (value: string) => void; setRight: (value: string) => void; frozen: boolean; setFrozen: (value: boolean) => void;
  stage: number; setStage: (value: number) => void;
}) {
  const steps: { id: Scene; label: string }[] = [
    { id: "replay-setup", label: "Configure" },
    { id: "replay-plan", label: "Plan" },
    { id: "replay-running", label: "Run" },
    { id: "replay-results", label: "Results" },
    { id: "replay-timeline", label: "Timeline" },
    { id: "replay-compare", label: "Compare" },
    { id: "replay-candidate", label: "Candidate" },
    { id: "replay-export", label: "Export" },
  ];
  const active = props.scene === "replay-stopped" ? "replay-running" : props.scene;
  const selected = LEDGER.find((row) => row.id === props.windowId) ?? LEDGER[3];
  return (
    <>
      <div className="wb-head"><div><div className="wb-phase">PHASE 2</div><h1>Replay</h1><p>What does this strategy do on these windows? The whole set is used. A candidate from Replay carries “no holdout evaluation”.</p></div></div>
      <ModeSwitch scene={props.scene} go={props.go} />
      <div className="wb-steps">{steps.map((step, index) => <button key={step.id} className={active === step.id ? "active" : ""} onClick={() => props.go(step.id)}><small>0{index + 1}</small>{step.label}</button>)}</div>
      {props.scene === "replay-setup" && (
        <div className="wb-grid main">
          <div className="wb-stack">
            <Panel title="Window set" layer={<Layer kind="exists" />}><dl className="wb-kv"><div><dt>Set</dt><dd>6 Oct exploratory95 · 15 windows</dd></div><div><dt>Policy</dt><dd>exploratory95_v1</dd></div><div><dt>Range</dt><dd>6 Oct · 14:36–19:36 UTC</dd></div></dl></Panel>
            <Panel title="Base configuration" sub="Schema defaults, a YAML under config/runs, or a saved candidate." layer={<Layer kind="new" />}>
              <label className="wb-field">Source<select defaultValue="yaml"><option value="schema">Strategy schema defaults</option><option value="yaml">config/runs/settlement_value_baseline_2c_150s_live.template.yaml</option><option value="candidate">Saved candidate</option></select></label>
            </Panel>
            <Panel title="Configurations" sub="Each value is the value that will run. Nothing is merged afterwards." layer={<Layer kind="new" />}>
              <div className="wb-grid two">
                <ConfigCard name="A · 2¢ / 150s" edge="0.02" changed={false} />
                <ConfigCard name="B · 3¢ / 150s" edge="0.03" changed />
              </div>
              <p className="wb-note">Accepted K moves into the policy and is copied into the stored configuration: CONFIRMED_CANONICAL and PROVISIONAL. The hidden RESEARCH_BASE_PARAMETERS overlay is gone.</p>
            </Panel>
            <Panel title="Execution scenario" layer={<Layer kind="new" />}><label className="wb-field">Scenario<select value={props.scenario} onChange={(event) => props.setScenario(event.target.value)}><option value="friction">friction</option><option value="favorable">favorable</option></select></label></Panel>
          </div>
          <aside className="wb-sticky"><Panel title="Replay, at a glance" layer={<Layer kind="new" />}><dl className="wb-kv"><div><dt>Windows</dt><dd>All 15</dd></div><div><dt>Configurations</dt><dd>2 explicit</dd></div><div><dt>Units</dt><dd>30</dd></div><div><dt>Split</dt><dd>None in Replay</dd></div><div><dt>Objective</dt><dd>None in Replay</dd></div></dl><Next label="Preview the plan" onClick={() => props.go("replay-plan")} /></Panel></aside>
        </div>
      )}
      {props.scene === "replay-plan" && (
        <Panel title="Plan" sub="Nothing runs until you confirm." layer={<Layer kind="new" />}>
          <div className="wb-metrics"><div className="wb-metric"><span>Configurations</span><strong>2</strong><p>2¢ / 150s and 3¢ / 150s</p></div><div className="wb-metric"><span>Windows</span><strong>15</strong><p>The whole set</p></div><div className="wb-metric"><span>Units</span><strong>30</strong><p>15 × 2 × 1 scenario</p></div><div className="wb-metric"><span>Claim</span><strong>Exploratory</strong><p>No holdout evaluation</p></div></div>
          <Next label="Confirm and run" onClick={() => { props.setStage(4); props.go("replay-running"); }} />
        </Panel>
      )}
      {(props.scene === "replay-running" || props.scene === "replay-stopped") && (
        <Panel title={props.scene === "replay-stopped" ? "Stopped · progress kept" : "Replay running"} sub="One resumable job. This animation is local to the mockup." layer={<Layer kind="changes" />}>
          <div className="wb-funnel"><div><b>{props.scene === "replay-stopped" ? 11 : Math.min(30, 4 + props.stage * 3)}</b><span>of 30 units</span></div></div>
          <progress max={30} value={props.scene === "replay-stopped" ? 11 : Math.min(30, 4 + props.stage * 3)} />
          <div className="wb-actions">
            {props.scene === "replay-running" ? <button className="button" onClick={() => props.go("replay-stopped")}>Stop</button> : <button className="button primary" onClick={() => props.go("replay-results")}>Resume</button>}
            <button className="button" onClick={() => props.go("replay-results")}>Open the finished result</button>
          </div>
          <p className="wb-note">Resume keeps the finished units. In this mockup, Resume opens the finished illustrative result.</p>
        </Panel>
      )}
      {props.scene === "replay-results" && (
        <div className="wb-stack">
          <div className="wb-callout accent"><strong>Exploratory result.</strong> A candidate saved from here carries “no holdout evaluation”. Qualified, unqualified, and a later verified or failed check are different facts.</div>
          <div className="wb-metrics">
            <Summary name="A · 2¢ / 150s" pnl="+1.84" entries="8 / 15" fills="6 / 8" />
            <Summary name="B · 3¢ / 150s" pnl="+0.62" entries="5 / 15" fills="4 / 5" />
          </div>
          <Panel title="Window ledger" sub="Six of 15 rows. Illustrative. P&L in the summary uses qualified rows only." layer={<Layer kind="new" />} action={<button className="button" onClick={() => props.go("replay-compare")}>Compare</button>}>
            <div className="table-wrap"><table><thead><tr><th>Window · UTC</th><th>Decision</th><th>Fill</th><th>P&L</th><th>Qualification</th><th>Limitations</th><th></th></tr></thead><tbody>
              {LEDGER.map((row) => <tr key={row.id} className={row.qual === "Unqualified" ? "wb-dim" : ""}><td>{row.time}</td><td>{row.decision}</td><td>{row.fill}</td><td className={row.pnl.startsWith("+") ? "positive" : row.pnl.startsWith("−") ? "negative" : ""}>{row.pnl}</td><td>{row.qual}</td><td>{row.limit}</td><td><button className="wb-rowbtn" onClick={() => { props.setWindowId(row.id); props.go("replay-timeline"); }}>Timeline</button></td></tr>)}
            </tbody></table></div>
          </Panel>
          <Next label="Save configuration A as a candidate" onClick={() => props.go("replay-candidate")} />
        </div>
      )}
      {props.scene === "replay-timeline" && (
        <Panel title={"Decision timeline · " + selected.time + " UTC"} sub="One window. Book prices sit on this same timeline when the result is wired. This mockup shows the decisions the screen has to carry." layer={<Layer kind="new" />}>
          <dl className="wb-kv"><div><dt>Decision</dt><dd>{selected.decision}</dd></div><div><dt>Fill</dt><dd>{selected.fill}</dd></div><div><dt>Qualification</dt><dd>{selected.qual}</dd></div><div><dt>Limitation</dt><dd>{selected.limit}</dd></div></dl>
          <div className="wb-timeline" style={{ marginTop: 14 }}>{["Warm-up", "Wait", "Entry request", "Arrival", "Fill", "Hold", "Resolution"].map((item, index) => <button key={item} className={index === 4 ? "on" : ""}>{item}</button>)}</div>
          <p className="wb-note">{selected.id === "1610" ? "Illustrative 16:10 window: entry Up at about 132s remaining, fill 5.00 at 0.71, provider-reported winner Down, qualified P&L −0.55." : "This row uses the same timeline shape. Only the 16:10 row has a written example."}</p>
          <Next label="Compare the two configurations" onClick={() => props.go("replay-compare")} />
        </Panel>
      )}
      {props.scene === "replay-compare" && (
        <Panel title="Compare" sub="A view, not a mode. Any two results, including a Replay beside a Tune." layer={<Layer kind="new" />}>
          <div className="wb-form">
            <label>Result A<select value={props.left} onChange={(event) => props.setLeft(event.target.value)}>{COMPARE.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
            <label>Result B<select value={props.right} onChange={(event) => props.setRight(event.target.value)}>{COMPARE.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
          </div>
          <div className="wb-grid two" style={{ marginTop: 14 }}>
            <CompareCard item={COMPARE.find((item) => item.id === props.left)!} />
            <CompareCard item={COMPARE.find((item) => item.id === props.right)!} />
          </div>
          <p className="wb-note">Same-set example: at 15:05 both configurations request Up; only 2¢ / 150s fills. At 16:10 both fill and both lose. A candidate replayed later on a new recording is the same view with a different result id.</p>
          <Next label="Save a candidate" onClick={() => props.go("replay-candidate")} />
        </Panel>
      )}
      {props.scene === "replay-candidate" && (
        <CandidateForm origin="Replay" holdoutText="No holdout evaluation" frozen={props.frozen} setFrozen={props.setFrozen} onExport={() => props.go("replay-export")} />
      )}
      {props.scene === "replay-export" && <ExportPanel go={props.go} />}
    </>
  );
}

const COMPARE = [
  { id: "replay-2c", label: "Replay · 2¢ / 150s", claim: "Exploratory · no holdout", pnl: "+1.84", qualified: "9 qualified · 6 unqualified" },
  { id: "replay-3c", label: "Replay · 3¢ / 150s", claim: "Exploratory · no holdout", pnl: "+0.62", qualified: "9 qualified · 6 unqualified" },
  { id: "tune-dev", label: "Tune · 2¢ / 150s · development", claim: "Development stage", pnl: "+1.40", qualified: "7 qualified · 3 unqualified" },
  { id: "tune-hold", label: "Tune · 2¢ / 150s · holdout", claim: "Holdout check", pnl: "+0.44", qualified: "3 qualified · 2 unqualified" },
];
function CompareCard({ item }: { item: (typeof COMPARE)[number] }) {
  return <div className="wb-metric"><span>{item.label}</span><strong className="positive">{item.pnl}</strong><p>{item.qualified}</p><p>{item.claim}</p></div>;
}
function Summary({ name, pnl, entries, fills }: { name: string; pnl: string; entries: string; fills: string }) {
  return <div className="wb-metric"><span>{name}</span><strong className="positive">{pnl}</strong><p>Qualified P&L, illustrative</p><p>Qualified 9 · unqualified 6</p><p>Entries {entries} · fills {fills}</p></div>;
}
function ConfigCard({ name, edge, changed }: { name: string; edge: string; changed: boolean }) {
  return (
    <div className="wb-choice selected">
      <strong>{name}</strong>
      <div className="wb-form" style={{ marginTop: 8 }}>
        <label>min_edge_per_share<input defaultValue={edge} /></label>
        <label>tau_max_s<input defaultValue="150" /></label>
        <label>tau_min_s<input defaultValue="30" /></label>
        <label>max_debit_usdc<input defaultValue="5" /></label>
      </div>
      <span>{changed ? "Edited from the base. The resolved configuration is stored on every unit." : "Unchanged from the YAML base."} Exit policy hold_to_resolution.</span>
    </div>
  );
}

function TunePage(props: {
  scene: Scene; go: (scene: Scene) => void; holdout: number; setHoldout: (value: number) => void; topK: number; setTopK: (value: number) => void;
  objective: Objective; setObjective: (value: Objective) => void; objectiveLabel: string; scenario: string; setScenario: (value: string) => void;
  ranked: ReturnType<typeof rankGrid>; chosen: ReturnType<typeof rankGrid>[number]; setPicked: (id: string) => void;
  expanded: string | null; setExpanded: (id: string | null) => void; frozen: boolean; setFrozen: (value: boolean) => void;
  tail: number; development: number; stage: number;
}) {
  const steps: { id: Scene; label: string }[] = [
    { id: "tune-setup", label: "Grid" },
    { id: "tune-plan", label: "Plan" },
    { id: "tune-running", label: "Run" },
    { id: "tune-ranking", label: "Ranking" },
    { id: "tune-generalisation", label: "Generalisation" },
    { id: "tune-stability", label: "Stability" },
    { id: "tune-candidate", label: "Candidate" },
  ];
  const configs = 9;
  const stage1 = configs * props.development;
  const stage2 = props.topK * props.tail;
  const preview = () => props.go(props.tail === 0 ? "tune-refused" : "tune-plan");
  return (
    <>
      <div className="wb-head"><div><div className="wb-phase">PHASE 3</div><h1>Tune</h1><p>Which parameters are best for this objective, and do they generalise? Selection stays on development. Holdout only checks the top-k.</p></div></div>
      <ModeSwitch scene={props.scene} go={props.go} />
      <div className="wb-steps">{steps.map((step, index) => <button key={step.id} className={(props.scene === "tune-refused" ? "tune-setup" : props.scene) === step.id ? "active" : ""} onClick={() => props.go(step.id)}><small>0{index + 1}</small>{step.label}</button>)}</div>
      {props.scene === "tune-setup" && (
        <div className="wb-grid main">
          <div className="wb-stack">
            <Panel title="Window set and base" layer={<Layer kind="exists" />}><p className="wb-note">6 Oct exploratory95_v1 · 15 windows. Base: the 2¢ / 150s YAML. Other parameters keep that base.</p></Panel>
            <Panel title="Grid" sub="Phase 3 done example: 3 × 3." layer={<Layer kind="new" />}>
              <div className="wb-form">
                <label>entry.min_edge_per_share<input readOnly value="0.02, 0.03, 0.04" /></label>
                <label>entry.tau_max_s<input readOnly value="120, 150, 180" /></label>
              </div>
              <p className="wb-note">9 configurations. Every other parameter stays at the base value.</p>
            </Panel>
            <Panel title="Chosen before the run" layer={<Layer kind="new" />}>
              <div className="wb-form">
                <label>Objective<select value={props.objective} onChange={(event) => props.setObjective(event.target.value as Objective)}><option value="pnl">Realised P&L · maximise · qualified rows</option><option value="fill">Fill rate · maximise · qualified rows</option></select></label>
                <label>Scenario<select value={props.scenario} onChange={(event) => props.setScenario(event.target.value)}><option value="friction">friction</option><option value="favorable">favorable</option></select></label>
                <label>Holdout % · chronological tail<input type="number" min={0} max={100} value={props.holdout} onChange={(event) => props.setHoldout(Number(event.target.value))} /></label>
                <label>Top-k<input type="number" min={1} max={9} value={props.topK} onChange={(event) => props.setTopK(Math.min(9, Math.max(1, Number(event.target.value) || 1)))} /></label>
              </div>
              <div className="wb-callout" style={{ marginTop: 12 }}><Layer kind="open" /> Metrics beyond realised P&L and fill rate are undecided. A drawdown-penalised variant is likely. Default top-k is 3 and is editable here, which is the proposal in the document.</div>
            </Panel>
          </div>
          <aside className="wb-sticky"><Panel title="Tune inputs" layer={<Layer kind="new" />}><p className="wb-note">Method stored as chronological_tail. A 0% holdout is refused. Try 0 in the holdout field, then preview.</p><Next label="Preview the plan" onClick={preview} /></Panel></aside>
        </div>
      )}
      {props.scene === "tune-refused" && (
        <Panel title="Tune refused" layer={<Layer kind="new" />}>
          <div className="wb-callout"><strong>A Tune with no holdout window is refused.</strong> {props.tail === 0 ? <>Typed holdout is {props.holdout}%. On this 15-window set the chronological tail is 0 windows. {props.holdout <= 0 ? "0% is refused because holdout is part of Tune." : "The percentage rounds to an empty tail, so there is nothing to check."} Use Replay to run the whole set with no split.</> : <>The rule: 0% holdout is refused, because holdout is part of Tune. Use Replay to run the whole set with no split. The field is still at {props.holdout}%. Set it to 0 on the grid step and preview to reach this screen from the form.</>}</div>
          <div className="wb-actions"><button className="button" onClick={() => { props.setHoldout(30); props.go("tune-setup"); }}>Restore 30% and edit</button></div>
        </Panel>
      )}
      {props.scene === "tune-plan" && (
        <Panel title="Plan" sub="The server expands the grid, cuts each recording, stores both lists, and waits." layer={<Layer kind="new" />}>
          <div className="wb-metrics">
            <div className="wb-metric"><span>Configurations</span><strong>{configs}</strong><p>3 × 3 grid</p></div>
            <div className="wb-metric"><span>Development</span><strong>{props.development}</strong><p>Earlier windows of 6 Oct</p></div>
            <div className="wb-metric"><span>Holdout</span><strong>{props.tail}</strong><p>{props.holdout}% chronological tail · {SET_WINDOWS} × {props.holdout}% = {((SET_WINDOWS * props.holdout) / 100).toFixed(1)}, rounded half up</p></div>
            <div className="wb-metric"><span>Units</span><strong>{stage1 + stage2}</strong><p>Stage 1: {stage1}. Stage 2: top {props.topK} × {props.tail} = {stage2}</p></div>
          </div>
          <p className="wb-note">Objective: {props.objectiveLabel}. The other {configs - props.topK} configurations never touch holdout. Scoring every configuration on holdout and then picking the best holdout score would be tuning on the check.</p>
          {props.development === 0 && <div className="wb-callout"><strong>This percentage leaves no development window.</strong> The document refuses 0% holdout. An empty development stage is shown here so we can decide whether it is the same refusal. Confirm stays off.</div>}
          {props.development > 0 && <Next label="Confirm and run" onClick={() => props.go("tune-running")} />}
        </Panel>
      )}
      {props.scene === "tune-running" && (
        <Panel title="Two-stage job" sub="Resumable at either stage." layer={<Layer kind="new" />}>
          <div className="wb-metrics">
            <div className="wb-metric"><span>Stage 1 · development</span><strong>{stage1} / {stage1}</strong><p>Complete · ranking available</p></div>
            <div className="wb-metric"><span>Stage 2 · holdout</span><strong>{Math.min(stage2, props.stage)} / {stage2}</strong><p>Top {props.topK} only</p></div>
          </div>
          <progress max={stage2 || 1} value={Math.min(stage2, props.stage)} />
          <div className="wb-actions"><button className="button" onClick={() => props.go("tune-ranking")}>Stop and keep stage 1</button><button className="button primary" onClick={() => props.go("tune-ranking")}>Open ranking</button></div>
        </Panel>
      )}
      {props.scene === "tune-ranking" && (
        <Panel title="Ranking" sub={"Development, ordered by " + props.objectiveLabel + ". Illustrative. One recording, so the per-recording breakdown is a single column."} layer={<Layer kind="new" />}>
          <div className="table-wrap"><table><thead><tr><th>Rank</th><th>Configuration</th><th>Development</th><th>Qualified</th><th>Holdout</th><th></th></tr></thead><tbody>
            {props.ranked.map((row) => (
              <Fragment key={row.id}>
                <tr className={row.inTop ? "" : "wb-dim"}>
                  <td>{row.devRank}</td><td>{row.edge} / {row.tau}s</td>
                  <td>{props.objective === "pnl" ? money(row.dev) + " USDC" : Math.round(row.fillDev * 100) + "%"}</td>
                  <td>7 qualified · 3 unqualified</td>
                  <td>{row.inTop ? "Carried into stage 2" : "Not run"}</td>
                  <td><button className="wb-rowbtn" onClick={() => props.setExpanded(props.expanded === row.id ? null : row.id)}>{props.expanded === row.id ? "Hide" : "Windows"}</button></td>
                </tr>
                {props.expanded === row.id && <tr><td colSpan={6}>Illustrative development windows for this configuration: 14:40 no entry, 15:05 filled, 16:10 filled and lost. Qualification and limitations stay on each row.</td></tr>}
              </Fragment>
            ))}
          </tbody></table></div>
          <Next label="Open generalisation" onClick={() => props.go("tune-generalisation")} />
        </Panel>
      )}
      {props.scene === "tune-generalisation" && (
        <Panel title="Generalisation" sub="Top-k only. The gap is holdout minus development. Order survives when the development rank and the holdout rank match." layer={<Layer kind="new" />}>
          <div className="table-wrap"><table><thead><tr><th>Development rank</th><th>Configuration</th><th>Development</th><th>Holdout</th><th>Gap</th><th>Order</th></tr></thead><tbody>
            {props.ranked.filter((row) => row.inTop).map((row) => {
              const gap = row.holdScore - row.score;
              return <tr key={row.id}><td>{row.devRank}</td><td>{row.edge} / {row.tau}s</td><td>{props.objective === "pnl" ? money(row.score) : Math.round(row.score * 100) + "%"}</td><td>{props.objective === "pnl" ? money(row.holdScore) : Math.round(row.holdScore * 100) + "%"}</td><td className={gap < 0 ? "negative" : "positive"}>{props.objective === "pnl" ? money(gap) : Math.round(gap * 100) + " pp"}</td><td>{row.holdRank === row.devRank ? "Survives" : "Changes · holdout rank " + row.holdRank}</td></tr>;
            })}
          </tbody></table></div>
          <p className="wb-note">The tool proposes nothing. You pick one configuration from the top-k.</p>
          <div className="wb-actions">{props.ranked.filter((row) => row.inTop).map((row) => <button key={row.id} className={"button " + (props.chosen.id === row.id ? "primary" : "")} onClick={() => props.setPicked(row.id)}>{row.edge} / {row.tau}s</button>)}</div>
          <Next label="Open stability" onClick={() => props.go("tune-stability")} />
        </Panel>
      )}
      {props.scene === "tune-stability" && (
        <Panel title={"Stability · " + props.chosen.edge + " / " + props.chosen.tau + "s"} sub="Neighbouring grid points on development, and the spread of per-window results." layer={<Layer kind="new" />}>
          <div className="wb-heat">
            <b />
            {[120, 150, 180].map((tau) => <b key={tau}>{tau}s</b>)}
            {["0.02", "0.03", "0.04"].map((edge) => (
              <Fragment key={edge}>
                <b>{edge}</b>
                {[120, 150, 180].map((tau) => {
                  const cell = GRID.find((row) => row.edge === edge && row.tau === tau)!;
                  const pickedCell = cell.id === props.chosen.id;
                  const near = !pickedCell && (cell.edge === props.chosen.edge || cell.tau === props.chosen.tau);
                  const value = props.objective === "pnl" ? money(cell.dev) : Math.round(cell.fillDev * 100) + "%";
                  return <span key={cell.id} className={pickedCell ? "pick" : near ? "near" : ""}>{value}</span>;
                })}
              </Fragment>
            ))}
          </div>
          <p className="wb-note">Highlighted neighbours share the edge or the entry-window end. This set has one recording, so dispersion across recordings is empty. Per-window spread on development, illustrative: worst −0.55, best +0.41.</p>
          <Next label="Save the picked configuration" onClick={() => props.go("tune-candidate")} />
        </Panel>
      )}
      {props.scene === "tune-candidate" && (
        <CandidateForm origin="Tune" holdoutText={props.objective === "pnl" ? money(props.chosen.holdScore) + " USDC on holdout" : Math.round(props.chosen.holdScore * 100) + "% fill rate on holdout"} frozen={props.frozen} setFrozen={props.setFrozen} detail={props.chosen.edge + " / " + props.chosen.tau + "s · development " + (props.objective === "pnl" ? money(props.chosen.score) + " USDC" : Math.round(props.chosen.score * 100) + "%")} onExport={() => props.go("evidence")} />
      )}
    </>
  );
}

function CandidateForm({ origin, holdoutText, frozen, setFrozen, onExport, detail }: { origin: string; holdoutText: string; frozen: boolean; setFrozen: (value: boolean) => void; onExport: () => void; detail?: string }) {
  return (
    <Panel title="Candidate" sub="One configuration, the result ids that justify it, and a written rationale. Frozen once." layer={<Layer kind="new" />}>
      <dl className="wb-kv">
        <div><dt>Origin</dt><dd>{origin}</dd></div>
        <div><dt>Configuration</dt><dd>{detail ?? "2¢ / 150s · resolved from the YAML base"}</dd></div>
        <div><dt>Result ids</dt><dd>Illustrative ids, linked on save</dd></div>
        <div><dt>Holdout</dt><dd>{holdoutText}</dd></div>
      </dl>
      <label className="wb-field" style={{ marginTop: 12 }}>Rationale<textarea defaultValue={origin === "Replay" ? "Exploratory look at the 2¢ configuration on the 6 October set. No holdout check." : "Picked from the top-k after reading the holdout gap and the neighbours."} /></label>
      <div className="wb-actions">
        <button className="button primary" disabled={frozen} onClick={() => setFrozen(true)}>{frozen ? "Frozen" : "Save and freeze"}</button>
        <button className="button" disabled={!frozen} onClick={onExport}>{origin === "Replay" ? "Export" : "Open evidence"}</button>
      </div>
    </Panel>
  );
}

function ExportPanel({ go }: { go: (scene: Scene) => void }) {
  return (
    <Panel title="Export" sub="profile_generate writes a paper YAML and lists preserved and added fields. Launch stays gated." layer={<Layer kind="changes" />}>
      <div className="wb-grid two">
        <div>
          <h3>Preserved</h3>
          <p className="wb-note">strategy.kind, strategy.parameters. The resolved configuration, with research-only tolerances refused.</p>
        </div>
        <div>
          <h3>Added, explicit</h3>
          <p className="wb-note">venue (paper), paper, state_directory, report_directory, market, account, risk, lifecycle, settlement. Schema version and run name are written with them.</p>
        </div>
      </div>
      <div className="wb-callout" style={{ marginTop: 12 }}><strong>Launch allowed: false.</strong> Unresolved: the Step 2 follow-up, and paper launch is not qualified. Validation uses the real run-config loader once this is wired. This mockup does not write a file.</div>
      <Next label="Open evidence" onClick={() => go("evidence")} />
    </Panel>
  );
}

function EvidencePage({ frozenReplay, frozenTune, go }: { frozenReplay: boolean; frozenTune: boolean; go: (scene: Scene) => void }) {
  return (
    <>
      <div className="wb-head"><div><div className="wb-phase">EVIDENCE</div><h1>Evidence</h1><p>Verified, failed, and unqualified are different things. This fixture has unqualified windows. It has no verified or failed engine check, because no replay has been run from the screen.</p></div></div>
      <Panel title="Candidates" layer={<Layer kind="new" />}>
        <div className="table-wrap"><table><thead><tr><th>Candidate</th><th>Origin</th><th>Development</th><th>Holdout</th><th>Export</th><th>Paper</th></tr></thead><tbody>
          <tr><td>2¢ / 150s</td><td>Replay</td><td>Illustrative result</td><td>No holdout evaluation</td><td>{frozenReplay ? "Ready to export" : "Not frozen in this session"}</td><td>Gated</td></tr>
          <tr><td>Picked Tune row</td><td>Tune</td><td>Attached when frozen</td><td>{frozenTune ? "Attached when frozen" : "Not frozen in this session"}</td><td>Not exported</td><td>Gated</td></tr>
        </tbody></table></div>
        <p className="wb-note">A Replay origin and a Tune origin sit side by side with different facts. Unqualified windows stay visible on the result. They are not a failed verification, and they are not a paper qualification.</p>
        <div className="wb-actions"><button className="button" onClick={() => go("replay-results")}>Back to Replay results</button><button className="button" onClick={() => go("tune-ranking")}>Back to Tune ranking</button></div>
      </Panel>
    </>
  );
}

function RunsPage() {
  return (
    <>
      <div className="wb-head"><div><div className="wb-phase">UNCHANGED</div><h1>Runs</h1><p>Paper and live launch stay gated. Phases 0 to 4 do not touch them. Export produces the YAML. Launch stays off until the Step 2 follow-up has its evidence.</p></div></div>
      <Panel title="Launch" layer={<Layer kind="exists" />}><div className="wb-callout"><strong>Gated.</strong> Gross/net share reconciliation, the two decision mismatches, and host-originated protection orders in reconstruction are still open. No collector start, paper session, or order from this screen.</div></Panel>
    </>
  );
}

createRoot(document.getElementById("root")!).render(<Workbench />);
