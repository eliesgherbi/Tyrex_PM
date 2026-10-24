import { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { Chart } from "./Chart";
import { BrandMark } from "./BrandMark";
import { DataLibrary, WindowSetExperimentDraft } from "./DataLibrary";
import type { WindowSet } from "./dataLibraryMock";
import { dark, light, timelineOption, outcomesOption, distributionOption } from "./options";
import { START, baseWindows, windowsFor, metrics, makeTrace, eventsFor, fmtMoney, utc, labels, type Strategy, type ConfigId, type Scenario, type WindowRow } from "./mock";
import "./style.css";

type Page="Data"|"Research"|"Runs"|"Evidence";
type ResearchTab="Explorer"|"Compare"|"Experiments"|"Candidates";
function useSaved<T,>(key:string,initial:T):[T,(next:T|((old:T)=>T))=>void] {
 const [value,setValue]=useState<T>(()=>{try{const v=sessionStorage.getItem("tyrex-review:"+key);return v?JSON.parse(v):initial;}catch{return initial;}});
 return [value,next=>setValue(old=>{const updated=typeof next==="function"?(next as (v:T)=>T)(old):next;try{sessionStorage.setItem("tyrex-review:"+key,JSON.stringify(updated));}catch{}return updated;})];
}
function Icon({name}:{name:string}) {
 const paths:Record<string,string>={Data:"M3 5h14v12H3z M3 9h14 M7 5v12",Research:"M3 16V5m0 11h14M6 12l3-5 3 3 4-6",Runs:"m6 3 11 7-11 7z",Evidence:"M5 2h8l3 3v13H5z M8 8h5m-5 4h5m-5 3h3",sun:"M10 6a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M10 1v2m0 14v2M1 10h2m14 0h2M3.6 3.6 5 5m10 10 1.4 1.4M3.6 16.4 5 15M15 5l1.4-1.4",arrow:"M3 10h13m-5-5 5 5-5 5",help:"M7 7a3 3 0 1 1 4 3c-1 .5-1 1-1 2m0 3h.01M10 1a9 9 0 1 0 0 18 9 9 0 0 0 0-18"};
 return <svg width="19" height="19" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.45" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]??paths.arrow}/></svg>;
}
function Badge({children,tone=""}:{children:React.ReactNode;tone?:string}){return <span className={"badge "+tone}>{children}</span>;}
function Button({children,onClick,primary=false,disabled=false,small=false}:{children:React.ReactNode;onClick?:()=>void;primary?:boolean;disabled?:boolean;small?:boolean}){return <button className={"button "+(primary?"primary ":"")+(small?"small":"")} onClick={onClick} disabled={disabled}>{children}</button>;}
function Field({label,value,onChange,options}:{label:string;value:string;onChange:(v:string)=>void;options:[string,string][]}){return <label className="field"><span>{label}</span><select value={value} onChange={e=>onChange(e.target.value)}>{options.map(([id,text])=><option key={id} value={id}>{text}</option>)}</select></label>;}
function Panel({title,subtitle,children,actions,className=""}:{title:string;subtitle?:string;children:React.ReactNode;actions?:React.ReactNode;className?:string}){return <section className={"panel "+className}><div className="panel-head"><div><h3>{title}</h3>{subtitle&&<p>{subtitle}</p>}</div>{actions&&<div className="inline">{actions}</div>}</div>{children}</section>;}
function EmptyHint({title,children}:{title:string;children:React.ReactNode}){return <div className="notice"><strong>{title}</strong><p>{children}</p></div>;}
const statusTone=(row:WindowRow)=>row.status==="won"?"good":row.status==="lost"?"bad":row.status==="unresolved"?"warn":"";
const configName=(id:ConfigId)=>id==="reference"?"A · Reference hold":"B · Tighter entry";
const scenarioName=(id:Scenario)=>id==="friction"?"Friction scenario":"Delayed fill scenario";
function ActionStrip({trace,model,onSelect}:{trace:ReturnType<typeof makeTrace>;model:boolean;onSelect:(t:number)=>void}) {
 const segments:{start:number;end:number;label:string;className:string}[]=[];
 for(const tick of trace.filter(t=>t.t>=0&&t.t<300)) {
  const name=tick.action==="HOST_SKIPPED"?"Inputs gap":tick.action==="WARMUP"?(model?"Warm-up":"Book check"):tick.action==="HOLD"?"Hold":tick.action==="SKIP"?"Skip":tick.action==="CLOSED_NO_FILL"?"No fill":tick.action==="FLAT"?"Flat":tick.action==="EXIT_PENDING"?"Exit pending":tick.action==="WAIT"?"Wait":"Enter";
  const className=name==="Inputs gap"?"gap":name==="Hold"?"hold":name==="Enter"?"enter":name==="Wait"?"wait":"warmup";
  const last=segments.at(-1);if(last?.label===name)last.end=tick.t+1;else segments.push({start:tick.t,end:tick.t+1,label:name,className});
 }
 return <div className="action-strip" aria-label="Lifecycle and host availability across the full window">{segments.map(s=><button key={s.start} title={s.label+" · "+s.start+"–"+s.end+" s"} className={s.className} style={{flex:s.end-s.start}} onClick={()=>onSelect(Math.floor((s.start+s.end)/2))}>{s.label}</button>)}</div>;
}

function App() {
 const [page,setPage]=useSaved<Page>("page","Research"),[tab,setTab]=useSaved<ResearchTab>("tab","Explorer");
 const [theme,setTheme]=useSaved("theme","light"),[strategy,setStrategy]=useSaved<Strategy>("strategy","settlement_value");
 const [config,setConfig]=useSaved<ConfigId>("config","reference"),[scenario,setScenario]=useSaved<Scenario>("scenario","friction");
 const [selected,setSelected]=useSaved("window",0),[instant,setInstant]=useSaved("instant",58);
 const [provenance,setProvenance]=useSaved("provenance","saved"),[warmup,setWarmup]=useSaved("warmup",false);
 const [strict,setStrict]=useSaved("strict",true),[showAsk,setShowAsk]=useSaved("ask",false),[point,setPoint]=useSaved("point",true),[conservative,setConservative]=useSaved("conservative",true);
 const [range,setRange]=useState("all"),[drawer,setDrawer]=useState<string|null>(null),[metricDrawer,setMetricDrawer]=useState("");
 const [dataPinned,setDataPinned]=useSaved("pinned",true);
 const [librarySet,setLibrarySet]=useSaved<WindowSet|null>("library-experiment-set",null);
 const [note,setNote]=useSaved("note","");
 const [candidate,setCandidate]=useSaved<{strategy:Strategy;config:ConfigId;scenario:Scenario}|null>("candidate",null);
 const [design,setDesign]=useSaved("design","compare"),[job,setJob]=useSaved<{status:string;done:number;total:number}>("job",{status:"idle",done:0,total:16});
 const palette=theme==="dark"?dark:light, model=strategy==="settlement_value";
 const rows=useMemo(()=>windowsFor(config,scenario,strategy),[config,scenario,strategy]),m=useMemo(()=>metrics(rows),[rows]);
 const row=rows[Math.max(0,Math.min(rows.length-1,selected))];
 const trace=useMemo(()=>makeTrace(selected,strategy,row),[selected,strategy,row]);
 const events=useMemo(()=>eventsFor(row,trace,strategy),[row,trace,strategy]);
 const current=trace.find(t=>t.t===instant)??trace[118];
 const visibleEvents=events.filter(e=>provenance==="saved"||e.kind==="request");
 const known=rows.filter(r=>r.qty>0&&r.pnl!==null);
 const entryEstimates=useMemo(()=>{
  if(!model)return null;
  const values=rows.map((r,i)=>{if(!r.requested)return null;const tick=makeTrace(i,strategy,r).find(t=>t.t===52)!;
   if(tick.upPoint===null||tick.upLow===null||tick.upHigh===null)return null;
   const point=r.side==="DOWN"?1-tick.upPoint:tick.upPoint,conservative=r.side==="DOWN"?1-tick.upHigh:tick.upLow;
   return {point:point*10-r.price*10-.03,conservative:conservative*10-r.price*10-.03,p:conservative};
  }).filter((v):v is {point:number;conservative:number;p:number}=>v!==null);
  return {count:values.length,point:values.reduce((s,v)=>s+v.point,0)/values.length,conservative:values.reduce((s,v)=>s+v.conservative,0)/values.length,p:values.reduce((s,v)=>s+v.p,0)/values.length};
 },[rows,model,strategy]);
 const budgetOverruns=rows.filter(r=>r.qty>0&&r.qty*r.price+r.fee>5).length;
 const dataTime=START+selected*300000;
 const opt=useMemo(()=>timelineOption({trace,events,row,strategy,windowIndex:selected,warmup,strict,showAsk,point,conservative,provenance,palette,range}),[trace,events,row,strategy,selected,warmup,strict,showAsk,point,conservative,provenance,palette,range]);
 const changeStrategy=(v:Strategy)=>{setStrategy(v);setSelected(0);setInstant(58);setRange("all");};
 const selectWindow=(i:number)=>{setSelected(i);setInstant(58);setRange("all");};
 const chooseTime=(ms:number)=>setInstant(Math.max(-60,Math.min(315,Math.round((ms-dataTime)/1000))));
 const saveCandidate=()=>{setCandidate({strategy,config,scenario});setTab("Candidates");};
 useEffect(()=>{document.documentElement.dataset.theme=theme;},[theme]);
 useEffect(()=>{
  const close=(e:KeyboardEvent)=>{if(e.key==="Escape"){setDrawer(null);setMetricDrawer("");}};
  window.addEventListener("keydown",close);return()=>window.removeEventListener("keydown",close);
 },[]);
 useEffect(()=>{
  if(job.status!=="running")return;
  const timer=setInterval(()=>setJob(old=>{const done=Math.min(old.total,old.done+2);return {...old,done,status:done===old.total?"completed":"running"};}),550);
  return()=>clearInterval(timer);
 },[job.status,setJob]);
 const goResearch=(next:ResearchTab)=>{setPage("Research");setTab(next);};
 const metric=(title:string,value:string,sub:string,tone="",explanation="")=><button className={"metric "+tone} onClick={()=>setMetricDrawer(explanation||title)}><span>{title}</span><strong>{value}</strong><small>{sub}</small></button>;
 const sharedFilters=<div className="context-filters">
   <Field label="Saved bundle" value="demo-dev" onChange={()=>{}} options={[["demo-dev","DEMO · development research"]]}/>
   <Field label="Strategy adapter" value={strategy} onChange={v=>changeStrategy(v as Strategy)} options={[["settlement_value","Settlement value"],["ask70","ask70 · book / threshold sketch"]]}/>
   <Field label="Execution scenario" value={scenario} onChange={v=>setScenario(v as Scenario)} options={[["friction","Friction · saved sample"],["delayed","Delayed fill · saved sample"]]}/>
   <Field label="Configuration" value={config} onChange={v=>setConfig(v as ConfigId)} options={[["reference",model?"A · Reference hold":"A · Ask threshold 0.70"],["tighter",model?"B · Tighter entry":"B · Alternative fixture"]]}/>
  </div>;

 return <div className="app-shell">
  <aside className="sidebar"><a className="brand" href="#" aria-label="Tyrex research workspace" onClick={e=>{e.preventDefault();goResearch("Explorer");}}><BrandMark className="brand-cat"/><span>tyrex<span className="brand-dot">.</span></span></a>
   <div className="workspace-chip"><span className="workspace-icon"><BrandMark className="workspace-cat"/></span><div><strong>Tyrex workspace</strong><span>Research & operations</span></div><span className="subtle">⌄</span></div>
   <div className="nav-label">WORKSPACE</div>
   <nav aria-label="Main navigation">{(["Data","Research","Runs","Evidence"]as Page[]).map(p=><button key={p} className={"nav-item "+(page===p?"active":"")} aria-label={p} title={p} aria-current={page===p?"page":undefined} onClick={()=>setPage(p)}><Icon name={p}/><span>{p}</span>{p==="Runs"&&<span className="nav-count">Gated</span>}</button>)}</nav>
   <div className="sidebar-note"><span className="small-dot"/><div><strong>Design review</strong><p>Standalone prototype.<br/>Synthetic fixtures only.</p></div></div>
   <div className="sidebar-bottom"><button onClick={()=>setDrawer("adapter")}><Icon name="help"/> Explorer → dashboard</button><small>Local UI · no backend connection</small></div>
  </aside>
  <div className="workspace">
   <header className="topbar"><div className="breadcrumb">Workspace <span>/</span> {page}{page==="Research"&&<><span>/</span><b>{tab}</b></>}</div><div className="inline"><span className="prototype-label">REVIEW PROTOTYPE</span><button className="icon-button" title="Toggle light or dark appearance" onClick={()=>setTheme(theme==="light"?"dark":"light")}><Icon name="sun"/></button><button className="avatar" title="Design review notes" onClick={()=>setDrawer("review")}>R</button></div></header>
   <div className="prototype-banner"><span className="small-dot"/> All prices, probabilities, results and events are synthetic. Actions change this prototype only; no jobs or orders are sent.</div>
   <main>
    <div className="page-heading"><div><div className="eyebrow">{page==="Research"?"FROM EXPLORER TO WORKSPACE":"TYREX WORKSPACE"}</div><h1>{page==="Research"?"Research":page==="Data"?"Data library":page==="Runs"?"Runs & readiness":"Evidence & qualification"}</h1><p>{page==="Research"?"Explore a configuration, understand its windows, and trace each decision.":page==="Data"?"Inspect the warehouse, build a scenario, and save exact inputs for research.":page==="Runs"?"Review paper profiles, host evidence and the blockers that remain.":"Keep implementation, test results and accepted evidence distinct."}</p></div><div className="inline"><Button onClick={()=>setDrawer("review")}>Leave review notes</Button>{page==="Research"&&<Button primary onClick={()=>goResearch("Experiments")}>New experiment <Icon name="arrow"/></Button>}</div></div>
    {page==="Research"&&<>
     <div className="section-tabs" role="tablist" aria-label="Research areas">{(["Explorer","Compare","Experiments","Candidates"]as ResearchTab[]).map(t=><button role="tab" aria-selected={tab===t} key={t} className={tab===t?"selected":""} onClick={()=>setTab(t)}>{t}{t==="Candidates"&&candidate&&<span className="tab-dot"/>}</button>)}</div>
     {tab!=="Experiments"&&tab!=="Candidates"&&sharedFilters}
     {tab==="Explorer"&&<>
      {!model&&<EmptyHint title="ask70 adapter preview · no probability model">This interface sketch reuses the window, book and execution layout. ask70 emits asks and an entry threshold, not a settlement probability model. Model and uncertainty curves are omitted. This is not evidence of ask70 research-lab support.</EmptyHint>}
      <div className="metric-grid">
       {metric("Known simulated P&L",fmtMoney(m.pnl)+" USDC",m.resolved+" resolved fills · "+m.unresolved+" unresolved",m.pnl!==null&&m.pnl>=0?"positive":"negative","Known P&L sums only resolved filled windows. Unresolved fills stay unknown; skipped and unfilled windows are not losses. This is simulated research P&L under the sample assumptions, not account equity or paper qualification.")}
       {metric("Filled windows",String(m.fills)+" / "+m.windows,m.requests+" entry requests · "+m.unfilled+" no fill","","Filled windows ÷ tested windows. Requests, POSTs and fills are separate counts. A partial fill counts as one filled window. These are illustrative fixture counts, not execution evidence.")}
       {metric("Win rate",m.resolved?Math.round(100*m.wins/m.resolved)+"%":"Unknown",m.wins+" profitable / "+m.resolved+" resolved fills","","Profitable filled windows ÷ fills with known P&L. No-entry windows, no-fill requests and unresolved fills are excluded from this denominator.")}
       {metric("Acquisition spending",m.spend.toFixed(2)+" USDC",m.skipped+" windows without an entry","","Sum of synthetic credited shares × fill price plus the explicit illustrative collateral fee. This mock convention is not provider fee qualification. Return on spending would use resolved spending, not total account collateral.")}
      </div>
      {entryEstimates&&<div className="estimate-strip"><span>MODEL ESTIMATES AT ENTRY</span><button onClick={()=>setMetricDrawer("Mean expected profit uses the recorded intended purchase at each entry request, including requests that did not fill. In this synthetic fixture: 10 intended shares, a 0.03 USDC projected collateral fee, and the chosen-side point or conservative estimate. It is not realized P&L. The integrated dashboard must read the saved EV fields, not recompute a strategy.")}>Point EV <b>{fmtMoney(entryEstimates.point)} USDC</b></button><button onClick={()=>setMetricDrawer("Conservative expected profit at request uses the cautious model probability for the selected leg. Its denominator is entry requests with a recorded estimate; it is not an average of successful fills.")}>Conservative EV <b>{fmtMoney(entryEstimates.conservative)} USDC</b></button><span>Mean conservative P <b>{entryEstimates.p.toFixed(3)}</b></span><small>{entryEstimates.count} requests · synthetic intended purchase</small></div>}
      {budgetOverruns>0&&<div className="inline-warning">{budgetOverruns} synthetic filled window exceeds the displayed 5 USDC spending cap after the delayed-fill assumptions. The dashboard exposes this discrepancy; it does not repair or qualify the accounting.</div>}
      <div className="dashboard-top">
       <Panel title="Windows in this configuration" subtitle="Click a bar to inspect that window." actions={<Badge>8 development windows</Badge>}>
        <Chart option={outcomesOption(rows,palette,selected)} height={160} label="Simulated P&L per market window, USDC by UTC start" onCategory={i=>selectWindow(i)}/>
        <div className="chart-foot"><span>P&L · USDC / window</span><span>Start time · UTC</span></div>
        <div className="legend-notes"><span><i className="legend-swatch green"/>Resolved positive</span><span><i className="legend-swatch red"/>Resolved negative</span><span>◆ unresolved · zero-height bars may be no-entry or no-fill</span></div>
       </Panel>
       <Panel title="Configuration at a glance" subtitle={model?configName(config):"ask70 · sample adapter"}>
        <dl className="details compact"><div><dt>Lifecycle</dt><dd>{model?"Hold to resolution":"Flatten before close"}</dd></div><div><dt>Selection</dt><dd>demo-dev-v1 · pinned</dd></div><div><dt>Scenario</dt><dd>{scenarioName(scenario)}</dd></div><div><dt>Source quality</dt><dd>Illustrative · not attested</dd></div><div><dt>Trace level</dt><dd>Decisions + explanations</dd></div></dl>
        <button className="text-button" onClick={()=>setDrawer("config")}>Inspect parameters & assumptions <Icon name="arrow"/></button>
       </Panel>
      </div>
      <div className="explorer-layout">
       <div className="explorer-main">
        <Panel title={model?"Model probability vs each leg’s book":"Each leg’s book vs entry threshold"} subtitle={"BTC Up / Down · "+row.time+"–"+utc(dataTime+300000).slice(0,5)+" UTC · "+row.id}
         actions={<Field label="Selected window" value={String(selected)} onChange={v=>selectWindow(Number(v))} options={rows.map((r,i)=>[String(i),r.time+" · "+labels[r.status]])}/>}>
         <div className="chart-toolbar">
          <div className="segmented" aria-label="Visible interval">{[["all","Full window"],["entry","Entry"],["hold","After entry"]].map(([id,label])=><button key={id} className={range===id?"selected":""} onClick={()=>setRange(id)}>{label}</button>)}</div>
          <label className="check"><input type="checkbox" checked={warmup} onChange={e=>setWarmup(e.target.checked)}/>Warm-up</label>
          <label className="check"><input type="checkbox" checked={strict} onChange={e=>setStrict(e.target.checked)}/>Strict gaps</label>
          <label className="check"><input type="checkbox" checked={showAsk} onChange={e=>setShowAsk(e.target.checked)}/>Ask</label>
          {model&&<><label className="check"><input type="checkbox" checked={point} onChange={e=>setPoint(e.target.checked)}/>Point P̂</label><label className="check"><input type="checkbox" checked={conservative} onChange={e=>setConservative(e.target.checked)}/>Conservative</label></>}
         </div>
         <div className="trace-meta"><Badge>{provenance==="saved"?"Original saved replay":"Regenerated diagnostic"} · synthetic</Badge><button className="text-button" onClick={()=>setDrawer("guide")}>How to read this chart</button></div>
         {provenance==="diagnostic"&&<div className="inline-warning">Diagnostic trace differs from the saved run. Original fill / arrival / sale markers are omitted.</div>}
         <Chart option={opt} height={548} label={model?"Synchronized Up and Down probability and own-book midpoint panels":"Up and Down own-book prices and entry ask threshold"} onTime={chooseTime}/>
         <div className="action-strip-label"><strong>Lifecycle overview</strong><span>Full five-minute window · click a segment</span></div>
         <ActionStrip trace={trace} model={model} onSelect={setInstant}/>
         <p className="plot-caption">Illustrative 1-second samples, 21 Oct 2026 · {row.time} UTC. Missing evaluations at +106–111 s and +215–230 s remain visible as gaps. {strict?"Strict: break every missing evaluation.":"Display joins at most 10 s; longer outages stay broken."} Price is USDC/share; model curves are probabilities, both displayed on a 0–1 scale.</p>
         <div className="chart-actions"><label className="field inline-field"><span>Trace source</span><select value={provenance} onChange={e=>setProvenance(e.target.value)}><option value="saved">Original saved replay · synthetic</option><option value="diagnostic">Different diagnostic replay · synthetic</option></select></label><Button small onClick={()=>setDrawer("mapping")}>Explorer parity & adapter contract</Button></div>
        </Panel>
        <Panel title="Execution & decision events" subtitle={provenance==="saved"?"Saved synthetic timestamps; a request is not an accepted order.":"Only diagnostic decision requests are shown; original execution is not overlaid."} actions={<Badge>{visibleEvents.length} events</Badge>}>
         {visibleEvents.length?<div className="table-wrap"><table><thead><tr><th>Time · UTC</th><th>Event</th><th>Leg</th><th>Value & meaning</th><th>Origin</th></tr></thead><tbody>{visibleEvents.map((e,i)=><tr key={i} className={instant===e.t?"highlight":""} onClick={()=>setInstant(e.t)} tabIndex={0} onKeyDown={k=>{if(k.key==="Enter")setInstant(e.t);}}><td className="mono">{utc(dataTime+e.t*1000)}</td><td><span className={"event-dot "+e.kind}/>{e.label}</td><td>{e.side}</td><td>{e.y===null?"Timestamp only":e.y.toFixed(3)}<small>{e.meaning}</small></td><td>{e.origin}</td></tr>)}</tbody></table></div>:<div className="table-note">No entry was requested in this selected fixture. The timeline and decision reasons remain inspectable.</div>}
        </Panel>
       </div>
       <aside className="inspector">
        <Panel title="Selected instant" subtitle={"Click either chart or an event."} actions={<Badge>UTC</Badge>}>
         <div className="inspector-time">{utc(current.ms)}<small>+{current.t} s from open</small></div>
         <label className="scrubber"><span className="sr-only">Inspect time offset in seconds</span><input aria-label="Inspect time offset in seconds" type="range" min={warmup?-60:0} max={315} value={Math.max(warmup?-60:0,instant)} onChange={e=>setInstant(Number(e.target.value))}/></label>
         <div className="decision-box"><span>DECISION STATE</span><strong>{current.action.replaceAll("_"," ")}</strong><p>{current.reason.replaceAll("_"," ").toLowerCase()}</p></div>
         <dl className="details">
          {model?<><div><dt>Up point P̂</dt><dd>{current.upPoint===null?"Not recorded":current.upPoint.toFixed(3)}</dd></div><div><dt>Up conservative</dt><dd>{current.upLow===null?"Not recorded":current.upLow.toFixed(3)}</dd></div><div><dt>Down point P̂</dt><dd>{current.upPoint===null?"Not recorded":(1-current.upPoint).toFixed(3)}</dd></div><div><dt>Down conservative</dt><dd>{current.upHigh===null?"Not recorded":(1-current.upHigh).toFixed(3)}</dd></div></>:<div><dt>Entry ask threshold</dt><dd>0.700 USDC/share</dd></div>}
          <div><dt>Up book midpoint</dt><dd>{current.upMid.toFixed(3)}<small>USDC/share</small></dd></div><div><dt>Down book midpoint</dt><dd>{current.downMid.toFixed(3)}<small>USDC/share</small></dd></div>
         </dl>
        </Panel>
        <Panel title="Window outcome" subtitle="Saved run · separate from model estimates">
         <div className="outcome-title"><Badge tone={statusTone(row)}>{labels[row.status]}</Badge><strong className={row.pnl!==null&&row.pnl<0?"negative":"positive"}>{row.qty?fmtMoney(row.pnl):"No trade"}{row.qty&&row.pnl!==null?<small> USDC</small>:null}</strong></div>
         <dl className="details compact"><div><dt>Selected leg</dt><dd>{row.side??"None"}</dd></div><div><dt>Credited filled shares</dt><dd>{row.qty||"None"}</dd></div><div><dt>Average fill</dt><dd>{row.qty?row.price.toFixed(3)+" USDC/share":"Not filled"}</dd></div><div><dt>Acquisition + fee</dt><dd>{row.qty?(row.qty*row.price+row.fee).toFixed(2)+" USDC":"0.00 USDC"}</dd></div><div><dt>{model?"Proxy winner":"Exit source"}</dt><dd>{model?row.winner??"Unknown":"Host flatten"}</dd></div></dl>
         <button className="text-button" onClick={()=>setDrawer("window")}>All window details <Icon name="arrow"/></button>
        </Panel>
        <Panel title="Indicator explanations" subtitle="At the selected decision instant">
         <dl className="details compact">{model?<><div><dt>Volatility σ</dt><dd>{current.eligible?current.sigma.toFixed(3):"Unavailable"}<small>annualized, demo convention</small></dd></div><div><dt>Reference BTC price</dt><dd>{current.reference.toFixed(2)} USD</dd></div><div><dt>Effective samples</dt><dd>{Math.max(0,current.t+60)}</dd></div><div><dt>Reference age</dt><dd>120 ms · synthetic</dd></div></>:<><div><dt>Up ask</dt><dd>{current.upAsk.toFixed(3)} USDC/share</dd></div><div><dt>Down ask</dt><dd>{current.downAsk.toFixed(3)} USDC/share</dd></div><div><dt>Leg preference</dt><dd>First eligible</dd></div><div><dt>Model series</dt><dd>Not emitted</dd></div></>}</dl>
         <button className="text-button" onClick={()=>setDrawer("indicators")}>Inspect recorded inputs <Icon name="arrow"/></button>
        </Panel>
        <Panel title="Decision reasons" subtitle="Whole selected window · sample evaluation counts">
         <div className="reason-list">{Object.entries(trace.filter(t=>t.t>=0&&t.t<=300).reduce<Record<string,number>>((a,t)=>{a[t.reason]=(a[t.reason]??0)+1;return a;},{})).sort((a,b)=>b[1]-a[1]).slice(0,4).map(([reason,count])=><div key={reason}><span>{reason.replaceAll("_"," ").toLowerCase()}</span><b>{count}</b></div>)}</div>
        </Panel>
       </aside>
      </div>
      <div className="bottom-grid">
       <Panel title="Resolved P&L distribution" subtitle={known.length+" resolved filled windows · "+m.unresolved+" unresolved excluded"}>
        <Chart option={distributionOption(rows,palette)} height={180} label="Histogram of resolved simulated P&L, USDC per filled window"/>
        <div className="chart-foot"><span>Y: filled-window count</span><span>X: P&L bin · USDC</span></div><p className="plot-caption">One configuration and scenario. Skipped, no-fill and unresolved windows are excluded. Synthetic fixture values.</p>
       </Panel>
       <Panel title="Research choice → paper profile" subtitle="A deliberate handoff, not automatic promotion">
        <p className="body-copy">Save the configuration with its data identity, assumptions and limitations. Review the full operational translation before a paper session becomes available.</p>
        <div className="steps-inline"><span>Candidate</span><i/> <span>Profile</span><i/> <span>Readiness</span></div>
        <div className="inline"><Button primary onClick={saveCandidate}>Save review candidate</Button><Button onClick={()=>setPage("Evidence")}>View evidence</Button></div>
        <p className="plot-caption">This saves a browser-only design choice. It does not freeze evaluation, generate YAML or qualify a strategy.</p>
       </Panel>
      </div>
     </>}
     {tab==="Compare"&&<Compare rows={rows} strategy={strategy} scenario={scenario} config={config} setConfig={setConfig} palette={palette} onInspect={()=>setTab("Explorer")} onGuide={()=>setDrawer("metrics")}/>}
     {tab==="Experiments"&&librarySet&&<WindowSetExperimentDraft key={librarySet.id} selection={librarySet} onBack={()=>setPage("Data")}/>}
     {tab==="Experiments"&&!librarySet&&<><div className="workflow-heading"><h2>From saved data to repeatable research</h2><Badge>UI simulation only</Badge></div><div className="wizard-grid"><Panel title="1. Define the experiment" subtitle="Explicit data, settings and execution assumptions">
       <div className="form-stack"><Field label="Pinned input selection" value={dataPinned?"pinned":"none"} onChange={v=>setDataPinned(v==="pinned")} options={[["pinned","demo-dev-v1 · 8 development windows"],["none","No saved selection"]]}/>
        <Field label="Strategy" value={strategy} onChange={v=>changeStrategy(v as Strategy)} options={[["settlement_value","Settlement value"],["ask70","ask70 · fixture-only adapter"]]} />
        <Field label="Experiment design" value={design} onChange={setDesign} options={[["single","One fixed configuration"],["compare","Reference versus comparison"]]} />
        <Field label="Execution assumptions" value={scenario} onChange={v=>setScenario(v as Scenario)} options={[["friction","Friction fixture"],["delayed","Delayed-fill fixture"]]} />
       </div><p className="plot-caption">Model series and research adapters are explicit capabilities. ask70 here is a presentation fixture, not a newly integrated research engine.</p>
      </Panel><Panel title="2. Preview & follow the job" subtitle="No estimated duration without measured samples">
       <div className="preview-total">8 × {design==="compare"?"2":"1"} × 1 <span>= {design==="compare"?16:8} replay units</span></div>
       <dl className="details"><div><dt>Input identity</dt><dd>{dataPinned?"Pinned demo-dev-v1":"Selection required"}</dd></div><div><dt>Reserved evaluation</dt><dd>Excluded</dd></div><div><dt>Estimated duration</dt><dd>Not measured</dd></div><div><dt>Output scope</dt><dd>Browser UI simulation</dd></div></dl>
       <div className="inline"><Button primary disabled={!dataPinned||job.status==="running"} onClick={()=>setJob({status:"running",done:0,total:design==="compare"?16:8})}>Play job UI demo</Button>{job.status==="running"&&<Button onClick={()=>setJob(o=>({...o,status:"stopped"}))}>Stop UI demo</Button>}{job.status==="stopped"&&<Button onClick={()=>setJob(o=>({...o,status:"running"}))}>Resume UI demo</Button>}</div>
       <div className="job-progress"><div className="inline spread"><b>{job.status}</b><span>{job.done} / {job.total} sample units</span></div><progress max={job.total} value={job.done}/></div>
       {job.status==="completed"&&<Button onClick={()=>setTab("Explorer")}>Open fixed sample results</Button>}
       <p className="plot-caption">The saved explorer fixture does not change when this animation completes. Real jobs will run in Python independently of browser lifetime.</p>
      </Panel></div></>}
     {tab==="Candidates"&&<div className="wizard-grid"><Panel title="Candidate review" subtitle="Saved locally for design discussion">
       {candidate?<><Badge>Browser-only candidate</Badge><h2 className="candidate-name">{candidate.strategy==="settlement_value"?configName(candidate.config):"ask70 · threshold fixture"}</h2><dl className="details"><div><dt>Source bundle</dt><dd>demo-dev-v1</dd></div><div><dt>Scenario</dt><dd>{scenarioName(candidate.scenario)}</dd></div><div><dt>Evaluation freeze</dt><dd>Not requested</dd></div><div><dt>Qualification</dt><dd>Not conferred</dd></div></dl><Button onClick={()=>setTab("Explorer")}>Return to supporting results</Button></>:<><EmptyHint title="Choose a configuration explicitly">Inspect the sample results, then use “Save review candidate”. No winner is chosen automatically.</EmptyHint><Button primary onClick={()=>setTab("Explorer")}>Explore saved results</Button></>}
      </Panel><Panel title="Paper-profile translation" subtitle="Separate strategy intent from operational additions">
       <dl className="details"><div><dt>Preserve</dt><dd>Strategy, settings, identities</dd></div><div><dt>Add explicitly</dt><dd>Paper account, state, ledger, limits</dd></div><div><dt>Record separately</dt><dd>Assumptions and tolerances</dd></div><div><dt>Validate</dt><dd>Complete host configuration</dd></div></dl>
       <EmptyHint title="No runnable profile generated in this prototype">The integrated screen will call the existing profile operations. It must not silently inherit research-only settings.</EmptyHint>
       <Button primary disabled={!candidate} onClick={()=>setPage("Runs")}>Review paper readiness</Button>
      </Panel></div>}
    </>}
    {page==="Data"&&<DataLibrary theme={theme} onOpenExperiment={selection=>{setLibrarySet(selection);goResearch("Experiments");}}/>}
    {page==="Runs"&&<><EmptyHint title="Paper launch is gated · step2_follow_up_open">The design follows the retained implementation record. Required Step 2 accounting, restart and reconstruction evidence remains open. This prototype does not run preflight or inspect real exposure.</EmptyHint>
     <div className="wizard-grid"><Panel title="Profile & readiness" subtitle="A validated YAML is not trading qualification">
      <dl className="details"><div><dt>Review candidate</dt><dd>{candidate?candidate.strategy+" · "+candidate.config:"Not selected"}</dd></div><div><dt>Actual venue</dt><dd>Not connected</dd></div><div><dt>Current exposure</dt><dd>Unknown · host not queried</dd></div><div><dt>Accepted paper evidence</dt><dd>Pending follow-up</dd></div><div><dt>ask70 public capture</dt><dd>Pending applicable gate</dd></div></dl>
      <div className="inline"><Button primary disabled>Launch paper · gated</Button><Button disabled>Restart · gated</Button><Button onClick={()=>setPage("Evidence")}>Why is it blocked?</Button></div>
     </Panel><Panel title="What the operating screen will show" subtitle="Host-derived observations with timestamps">
      <div className="feature-list"><div><b>Positions & obligations</b><p>Credited shares, original lifecycle, pending settlement and inventory discrepancies.</p></div><div><b>Orders & feedback</b><p>Requests, POSTs, fills, residuals and uncertainty as separate events.</p></div><div><b>Explain / verify this run</b><p>Trace completeness, raw context and supported reconstruction contract.</p></div></div><p className="plot-caption">These are proposed views, not fabricated host readings. Real-order controls are outside the first interface.</p>
     </Panel></div>
    </>}
    {page==="Evidence"&&<><div className="evidence-heading"><Badge>Static design snapshot</Badge><p>The following statuses follow the supplied Step 3 feedback; they are not live queries or newly verified evidence.</p></div>
      <Panel title="Capability evidence" subtitle="Keep historical observations and accepted proof distinct">
       <div className="table-wrap"><table><thead><tr><th>Capability</th><th>Reported state</th><th>Remaining evidence</th></tr></thead><tbody>
        <tr><td>Offline workflow P0–P3</td><td><Badge tone="good">Implemented & locally tested</Badge></td><td>Bind interface to the shared operations; validate UI parity.</td></tr>
        <tr><td>Collection controls P4</td><td><Badge tone="warn">Incomplete in supplied record</Badge></td><td>Isolated real start, status, stop and resume proof.</td></tr>
        <tr><td>Settlement paper hold</td><td><Badge>Observed, partially qualified</Badge></td><td>S0 exact reconstruction and accounting-consistent recovery.</td></tr>
        <tr><td>Paper launch / restart P5</td><td><Badge tone="warn">Gated</Badge></td><td>Accepted relevant Step 2 follow-up.</td></tr>
        <tr><td>ask70 P6</td><td><Badge>Profile checked; capture gated</Badge></td><td>Host recording and supported reconstruction.</td></tr>
        <tr><td>This IHM</td><td><Badge>Design prototype</Badge></td><td>User layout review, then API integration and end-to-end checks.</td></tr>
       </tbody></table></div>
      </Panel><div className="bottom-grid"><Panel title="Explorer parity" subtitle="Preserve meaning while changing presentation">
       <div className="feature-list"><div><b>Same scope & denominators</b><p>Dataset, scenario, configuration, unresolved fills and trace provenance remain visible.</p></div><div><b>Same Up / Down model semantics</b><p>Conservative Down = 1 − high Up. Each leg keeps its own book.</p></div><div><b>Same execution distinctions</b><p>Requested, arrived, partial, filled, cancelled and sold retain origin and timestamps.</p></div></div><Button onClick={()=>setDrawer("mapping")}>Inspect mapping</Button>
      </Panel><Panel title="Before integration" subtitle="Review this UI without changing the engine">
       <ol className="checklist"><li>Validate dashboard layout, chart meaning and navigation.</li><li>Define versioned result/view adapters for recorded fields.</li><li>Bind the existing catalog through a thin Python API.</li><li>Keep all paper evidence gates authoritative.</li></ol><Button onClick={()=>setDrawer("review")}>Record design feedback</Button>
      </Panel></div>
    </>}
    <footer><span>TYREX · IHM design review · 21 October 2026</span><span>React + ECharts · isolated from the Python runtime</span></footer>
   </main>
  </div>
  {(drawer||metricDrawer)&&<div className="drawer-backdrop" onClick={()=>{setDrawer(null);setMetricDrawer("");}}><section className="drawer" role="dialog" aria-modal="true" aria-label={metricDrawer?"Metric definition":"Design details"} onClick={e=>e.stopPropagation()}><header><span className="eyebrow">{metricDrawer?"METRIC DEFINITION":"DESIGN DETAILS"}</span><button className="close-button" aria-label="Close details" onClick={()=>{setDrawer(null);setMetricDrawer("");}}>×</button></header>
   {metricDrawer?<><h2>Meaning & denominator</h2><p>{metricDrawer}</p><EmptyHint title="Synthetic research view">This number comes from browser fixture arithmetic. It is not the result of a newly executed strategy or accepted paper session.</EmptyHint></>:
    drawer==="review"?<><h2>Review the IHM</h2><p>Notes stay in this browser tab. They do not send a message or change project documents.</p><label className="field"><span>Layout, chart or workflow feedback</span><textarea value={note} onChange={e=>setNote(e.target.value)} rows={10} placeholder="For example: show the selected decision beside both legs, or move the distribution above the event table."/></label><Button primary onClick={()=>setDrawer(null)}>Keep review notes</Button><hr/><Button onClick={()=>{for(const k of Object.keys(sessionStorage))if(k.startsWith("tyrex-review:"))sessionStorage.removeItem(k);location.reload();}}>Reset prototype choices</Button></>:
    drawer==="guide"?<><h2>Read model and market together</h2><div className="feature-list"><div><b>Three series, two meanings</b><p>Point and conservative estimates are model probabilities. The book midpoint is a market token price in USDC/share, not a verified probability.</p></div><div><b>Up and Down are separate panels</b><p>For settlement, Down point = 1 − Up point; Down conservative = 1 − Up high. The two conservative estimates need not sum to one. Neither leg’s book price is inferred from the other.</p></div><div><b>A request is not a fill</b><p>The triangle marks the decision’s conservative probability. A filled circle marks the execution price. Arrival is a timestamp, not an invented price coordinate.</p></div><div><b>Gaps and diagnostics stay visible</b><p>Missing evaluations break lines. A different diagnostic trace does not inherit the original run’s fills.</p></div></div></>:
    drawer==="adapter"||drawer==="mapping"?<><h2>Explorer → reusable dashboard</h2><p>Keep the notebook for bespoke exploration; reuse its recorded information through versioned read-only view adapters.</p><ol className="checklist"><li>Scope and configuration comparison → filters, overview metrics and Compare.</li><li>Window outcomes and distribution → selectable bars and resolved-fill histogram.</li><li>Probability-vs-book → synchronized Up / Down panels and action strip.</li><li>Window detail and reason counts → selected-window inspector.</li><li>Recorded explanations → indicator panel at the selected instant.</li><li>Candidate and paper profile → explicit handoff.</li></ol><h3>Per-strategy chart contract</h3><p>The adapter declares available model series, their units and complements, own-book quotes, decision reasons, trace identity and execution origins. Settlement supplies point and conservative probability. ask70 supplies book asks and its threshold, so no model curve is drawn. Other plugins require their own declared mapping; registration alone is not dashboard or lab support.</p><EmptyHint title="Read-only by default">Missing traces report availability. Creating diagnostics remains an explicit guarded operation; opening the dashboard must not create indexes, caches or new runs.</EmptyHint></>:
    drawer==="config"?<><h2>Effective settings & scope</h2><dl className="details"><div><dt>Strategy adapter</dt><dd>{strategy}</dd></div><div><dt>Configuration</dt><dd>{config}</dd></div><div><dt>Scenario</dt><dd>{scenarioName(scenario)}</dd></div><div><dt>Development selection</dt><dd>demo-dev-v1 · 8 windows</dd></div><div><dt>Window debit cap</dt><dd>5 USDC · fixture limit</dd></div><div><dt>Model tolerances</dt><dd>Explicit mapping required</dd></div><div><dt>Provider semantics</dt><dd>Not attested</dd></div></dl><p>Every displayed setting must come from the saved definition after integration. This prototype does not load or change a YAML file.</p></>:
    drawer==="indicators"?<><h2>Recorded decision inputs</h2><p>Decision time: {utc(current.ms)} UTC. Sample reference and variance are illustrative, not recomputed indicators.</p><pre>{JSON.stringify({trace:"synthetic saved fixture",decision_time_utc:utc(current.ms),strategy,action:current.action,reason:current.reason,p_up_point:model?current.upPoint:null,p_up_low:model?current.upLow:null,p_up_high:model?current.upHigh:null,up_book_mid:current.upMid,down_book_mid:current.downMid,recorded_sigma:model?current.sigma:null,inputs_eligible:current.eligible},null,2)}</pre><p>The integrated adapter must preserve recorded variance units, sample counts, reference age, jump guard and causal timing where present; it must not invent missing fields.</p></>:
    drawer==="metrics"?<><h2>Comparison rules</h2><p>Pair by underlying window and declared data/build/scenario identities. Show common coverage and exclusions. A fill with unknown P&L is never coerced to zero. Return on spending uses resolved acquisition spending, not account collateral.</p><p>The prototype comparison uses fixed sample fixtures; it performs no strategy optimization or automatic candidate promotion.</p></>:
    <><h2>Selected window · {row.id}</h2><dl className="details"><div><dt>Start · UTC</dt><dd>{new Date(dataTime).toISOString()}</dd></div><div><dt>Outcome</dt><dd>{labels[row.status]}</dd></div><div><dt>Leg</dt><dd>{row.side??"None"}</dd></div><div><dt>Credited shares</dt><dd>{row.qty}</dd></div><div><dt>Illustrative fee</dt><dd>{row.fee.toFixed(2)} USDC collateral</dd></div><div><dt>Simulated P&L</dt><dd>{fmtMoney(row.pnl)} USDC</dd></div></dl><p>Outcome labels, execution assumptions, prices and fees in this prototype are synthetic. Historical paper account/ledger disagreements are not hidden or repaired here.</p></>}
  </section></div>}
 </div>;
}

function Compare({rows,strategy,scenario,config,setConfig,palette,onInspect,onGuide}:{rows:WindowRow[];strategy:Strategy;scenario:Scenario;config:ConfigId;setConfig:(id:ConfigId)=>void;palette:typeof light;onInspect:()=>void;onGuide:()=>void}) {
 const alternatives=(["reference","tighter"]as ConfigId[]).map(id=>({id,rows:windowsFor(id,scenario,strategy)}));
 const paired=baseWindows.map((_,i)=>i).filter(i=>alternatives.every(a=>a.rows[i].pnl!==null));
 const option=useMemo(()=>({animation:false,legend:{top:2,textStyle:{color:palette.muted}},grid:{left:44,right:15,top:46,bottom:30},
 tooltip:{trigger:"axis",backgroundColor:palette.surface,borderColor:palette.grid,textStyle:{color:palette.text}},
 xAxis:{type:"category",data:paired.map(i=>baseWindows[i].time),axisLabel:{color:palette.muted},axisLine:{show:false},axisTick:{show:false}},
 yAxis:{type:"value",axisLabel:{color:palette.muted},splitLine:{lineStyle:{color:palette.grid}}},
 series:alternatives.map((a,i)=>({name:i?"B · alternative":"A · reference",type:"bar",barMaxWidth:20,itemStyle:{color:i?palette.teal:palette.blue},data:paired.map(j=>a.rows[j].pnl)}))}),[strategy,scenario,palette]);
 return <><div className="compare-heading"><div><h2>Compare configurations on common windows</h2><p>{paired.length} of 8 underlying windows have known outcomes in both configurations. No-entry windows are included as no-trade zero; unresolved fills are excluded.</p></div><Button onClick={onGuide}>Pairing & metric definitions</Button></div>
  <Panel title="Paired per-window simulated P&L" subtitle="Same sample input selection, build and execution scenario."><Chart option={option} height={320} label="Paired A and B simulated P&L by market window"/><div className="chart-foot"><span>Y: simulated P&L · USDC/window</span><span>X: window start · UTC</span></div><p className="plot-caption">Synthetic development fixtures · 21 October 2026, 10:00–10:40 UTC. This is not account equity or accepted trading evidence.</p></Panel>
  <Panel title="Configuration metrics" subtitle="Inspect the underlying window population before choosing a candidate."><div className="table-wrap"><table><thead><tr><th>Configuration</th><th>Known P&L</th><th>Filled windows</th><th>Resolved fills</th><th>Unknown fills</th><th>Win rate</th><th/></tr></thead><tbody>{alternatives.map(a=>{const v=metrics(a.rows);return <tr key={a.id} className={config===a.id?"highlight":""}><td><strong>{strategy==="settlement_value"?configName(a.id):a.id==="reference"?"A · threshold fixture":"B · alternative fixture"}</strong></td><td>{fmtMoney(v.pnl)} USDC</td><td>{v.fills} / {v.windows}</td><td>{v.resolved}</td><td>{v.unresolved}</td><td>{v.resolved?Math.round(100*v.wins/v.resolved)+"%":"Unknown"}</td><td><Button small onClick={()=>{setConfig(a.id);onInspect();}}>Inspect</Button></td></tr>;})}</tbody></table></div></Panel>
  <div className="notice"><strong>Inspection is separate from selection</strong><p>A stronger-looking sample metric does not choose a winner or qualify a strategy. Review concentration, assumed labels, fees and coverage first.</p></div>
 </>;
}
createRoot(document.getElementById("root")!).render(<App/>);


