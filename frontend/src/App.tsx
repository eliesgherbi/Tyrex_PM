import { useEffect, useState } from "react";
import { BrandMark } from "../../prototypes/ihm-review/src/BrandMark";
import "../../prototypes/ihm-review/src/style.css";
import "../../prototypes/ihm-review/src/dataLibrary.css";
import "../../prototypes/ihm-review/src/researchJourney.css";
import "../../prototypes/ihm-review/src/workflowReview.css";
import "./workspace.css";
import { bootstrap, type Bootstrap } from "./api";
import { DataLibrary } from "./DataLibrary";
import { EvidencePage, RunsPage } from "./Operations";
import { ResearchWorkspace, type Screen } from "./research/ResearchWorkspace";

type Page = "Data" | "Research" | "Runs" | "Evidence";
type Selection = { selection_id: string };

function remembered(key: string, fallback: string): string {
  try { return localStorage.getItem("tyrex:" + key) || fallback; } catch { return fallback; }
}

export function App() {
  const [page, setPage] = useState<Page>(() => remembered("page", "Data") as Page);
  const [screen, setScreen] = useState<Screen>(() => remembered("screen", "Replay") as Screen);
  const [theme, setTheme] = useState(() => remembered("theme", "light"));
  const [handoff, setHandoff] = useState<{ selectionId: string; token: number } | null>(null);
  const [openJob, setOpenJob] = useState<{ jobId: string; token: number } | null>(null);
  const [config, setConfig] = useState<Bootstrap | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("tyrex:theme", theme); localStorage.setItem("tyrex:page", page); localStorage.setItem("tyrex:screen", screen); } catch { /* storage unavailable */ }
  }, [theme, page, screen]);
  useEffect(() => {
    bootstrap().then(setConfig).catch(() => setError("The workspace API is not reachable."));
  }, []);
  const research = page === "Research";
  const go = (next: Screen) => { setPage("Research"); setScreen(next); window.scrollTo({ top: 0 }); };

  return (
    <div className="app-shell research-v2 workflow-v3">
      <aside className="sidebar">
        <a className="brand" href="#data" onClick={(event) => { event.preventDefault(); setPage("Data"); }}>
          <BrandMark className="brand-cat" />
          <span>tyrex<span className="brand-dot">.</span></span>
        </a>
        <div className="workspace-chip"><span className="workspace-icon"><BrandMark className="workspace-cat" /></span><div><strong>Research workspace</strong><span>Integrated · local warehouse</span></div></div>
        <div className="nav-label">WORKSPACE</div>
        <nav aria-label="Main navigation">
          {(["Data", "Research", "Runs", "Evidence"] as Page[]).map((item, i) => (
            <button key={item} className={"nav-item " + (page === item ? "active" : "")} aria-label={item} onClick={() => { setPage(item); window.scrollTo(0, 0); }}>
              <span className="r-nav-icon">{["▦", "⌁", "▷", "▤"][i]}</span>
              <span>{item}</span>
              {item === "Runs" && <small className="nav-count">Gated</small>}
            </button>
          ))}
        </nav>
        {research && <div className="r-side-flow"><div className="nav-label">RESEARCH</div>{(["Replay", "Tune", "Results", "Compare", "Candidates"] as Screen[]).map((item, i) => <button key={item} className={screen === item || (item === "Results" && screen === "Explorer") ? "active" : ""} onClick={() => go(item)}><span>{String(i + 1).padStart(2, "0")}</span>{item}{item === "Tune" && <small>Phase 3</small>}</button>)}</div>}
        <div className="sidebar-note"><span className="small-dot" /><div><strong>Integrated workspace</strong><p>Warehouse reads and saved research records.<br />No order submit.</p></div></div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="breadcrumb">Workspace <span>/</span>{research ? <>Research<span>/</span><b>{screen}</b></> : <b>{page}</b>}</div>
          <div className="inline"><button className="r-theme" aria-label="Toggle appearance" title="Toggle light / dark" onClick={() => setTheme(theme === "light" ? "dark" : "light")}>◐</button></div>
        </header>
        <main>
          {!research && <div className="page-heading">
            <div>
              <div className="eyebrow">TYREX WORKSPACE</div>
              <h1>{page === "Data" ? "Data library" : page}</h1>
              <p>{page === "Data" ? "Tick recordings, read coverage, and save the windows that pass." : "Operations come from the shared workflow catalog."}</p>
            </div>
          </div>}
          {error && <div className="inline-warning">{error}</div>}
          {config && (
            <div hidden={page !== "Data"}>
              <DataLibrary
                roots={config.roots || []}
                theme={theme}
                onOpenExperiment={(item: Selection) => { setHandoff({ selectionId: item.selection_id, token: Date.now() }); go("Replay"); }}
                onOpenRun={(jobId) => { setOpenJob({ jobId, token: Date.now() }); go("Results"); }}
              />
            </div>
          )}
          <div hidden={!research}>
            <ResearchWorkspace screen={screen} onScreen={go} theme={theme} handoff={handoff} openJob={openJob} onOpenData={() => setPage("Data")} />
          </div>
          {page === "Runs" && <RunsPage />}
          {page === "Evidence" && <EvidencePage />}
          <footer><span>Tyrex workspace · Replay over saved window sets</span><span>Local API · no collector, paper session or order from this page</span></footer>
        </main>
      </div>
    </div>
  );
}
