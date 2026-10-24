// Data Library views: policy coverage, window map, recording health and controls, set
// versions and market-condition filters. Every number comes from a workflow operation.
import { useEffect, useMemo, useRef, useState } from "react";
import { call } from "../api";
import { Chart } from "../research/Chart";
import { paletteFor } from "../research/chartOptions";

export type Profile = { market_id: string; window_start_ns?: number | null; block_id?: string | null; kept: boolean; reason?: string | null; outcome?: string | null; holes: [number, number][]; valid_ratio?: number | null; longest_hole_s?: number | null };
export type RecordingSummary = { block_id: string; warehouse?: string; collection?: string; started_at_ns?: number; stopped_at_ns?: number; windows_until_ns?: number | null; winners_missing?: number; outcome_wait?: { windows: number; known: number; missing: string[] } | null; windows: number; kept: number; policy: string; book_time_lost?: number | null; degraded_from_ns?: number | null; series: Profile[] };
export type FeatureSummary = Record<string, { min: number; p33: number; p50: number; p67: number; max: number }>;

const hm = (ns?: number | null) => (ns ? new Date(ns / 1e6).toISOString().slice(11, 16) : "—");
const hms = (ns?: number | null) => (ns ? new Date(ns / 1e6).toISOString().slice(11, 19) : "—");
const day = (ns?: number | null) => (ns ? new Date(ns / 1e6).toISOString().slice(0, 10) : "—");
const pct = (value?: number | null) => (value == null ? "—" : `${Math.round(value * 100)}%`);
const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export const reasonGroup = (reason?: string | null) => {
  if (!reason) return "Kept";
  if (reason === "recording_ended_before_close") return "Recording ended";
  if (reason.startsWith("book_")) return "Book holes";
  if (reason.startsWith("reference")) return "Reference";
  if (reason.includes("winner") || reason.includes("outcome")) return "Outcome";
  if (reason.includes("outside") || reason.includes("condition")) return "Filters";
  return "Other";
};
const GROUPS = ["Kept", "Book holes", "Reference", "Outcome", "Recording ended", "Filters", "Other"];

export function useRecordings(roots: string[], version = 0) {
  const [recordings, setRecordings] = useState<RecordingSummary[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!roots.length) return;
    let alive = true;
    void call("library.recordings", { roots }).then((envelope) => {
      if (!alive) return;
      if (envelope.ok) setRecordings((envelope.result?.recordings as RecordingSummary[]) || []);
      else setError(envelope.error?.reason || "Recording summary unavailable.");
    }).catch(() => alive && setError("The workspace API is not reachable."));
    return () => { alive = false; };
  }, [roots.join("|"), version]);
  return { recordings, error };
}

export function RecordingBadge({ summary }: { summary?: RecordingSummary }) {
  if (!summary) return null;
  return <span className="dv-badge">
    <b>{summary.kept}</b> of {summary.windows} windows kept · {pct(summary.book_time_lost)} book time lost
    {summary.degraded_from_ns ? <em> · gaps every window from {hm(summary.degraded_from_ns)}</em> : null}
  </span>;
}

export function PolicyCoverage({ profiles, theme, policy }: { profiles: Profile[]; theme: string; policy: string }) {
  const option = useMemo(() => {
    const p = paletteFor(theme);
    const colors: Record<string, string> = { Kept: p.green, "Book holes": p.red, Reference: p.teal, Outcome: p.blue, "Recording ended": p.muted, Filters: p.market, Other: p.muted };
    const hours: string[] = [];
    const counts: Record<string, Record<string, number>> = {};
    for (const row of profiles) {
      if (!row.window_start_ns) continue;
      const key = new Date(row.window_start_ns / 1e6).toISOString().slice(0, 13).replace("T", " ") + ":00";
      if (!counts[key]) { counts[key] = {}; hours.push(key); }
      const group = reasonGroup(row.kept ? null : row.reason);
      counts[key][group] = (counts[key][group] || 0) + 1;
    }
    hours.sort();
    const present = GROUPS.filter((group) => hours.some((hour) => counts[hour][group]));
    return {
      animation: false,
      grid: { left: 36, right: 12, top: 30, bottom: 40 },
      legend: { top: 0, textStyle: { color: p.muted, fontSize: 10 }, itemWidth: 10, itemHeight: 8 },
      tooltip: { trigger: "axis", backgroundColor: p.surface, borderColor: p.grid, textStyle: { color: p.text, fontSize: 11 } },
      xAxis: { type: "category", data: hours.map((hour) => hour.slice(5)), axisLabel: { color: p.muted, fontSize: 10, hideOverlap: true }, axisLine: { lineStyle: { color: p.grid } } },
      yAxis: { type: "value", minInterval: 1, name: "windows", nameTextStyle: { color: p.muted, fontSize: 10 }, axisLabel: { color: p.muted, fontSize: 10 }, splitLine: { lineStyle: { color: p.grid } } },
      series: present.map((group) => ({ name: group, type: "bar", stack: "w", barMaxWidth: 26, itemStyle: { color: colors[group] }, data: hours.map((hour) => counts[hour][group] || 0) })),
    };
  }, [profiles, theme]);
  const kept = profiles.filter((row) => row.kept).length;
  return <div className="dv-coverage">
    <div className="dv-summary"><b>{kept}</b> of {profiles.length} windows kept under <code>{policy}</code>. The preview and the save use this same rule.</div>
    <Chart option={option} height={240} label="Windows per hour, kept or excluded by reason" />
  </div>;
}

export function WindowMap({ profiles, theme, entryBand = [60, 270], onPick }: { profiles: Profile[]; theme: string; entryBand?: [number, number]; onPick?: (row: Profile) => void }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const [hover, setHover] = useState<Profile | null>(null);
  const rows = useMemo(() => [...profiles].sort((a, b) => (a.window_start_ns || 0) - (b.window_start_ns || 0)), [profiles]);
  const rowH = rows.length > 150 ? 3 : rows.length > 60 ? 5 : 8;
  const labelW = 74;
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1, width = canvas.clientWidth, height = rows.length * rowH + 22;
    canvas.width = width * dpr; canvas.height = height * dpr; canvas.style.height = `${height}px`;
    const g = canvas.getContext("2d");
    if (!g) return;
    g.scale(dpr, dpr);
    const x = (t: number) => labelW + ((width - labelW - 6) * t) / 300;
    g.fillStyle = css("--accent-soft") || "rgba(80,120,220,.12)";
    g.fillRect(x(entryBand[0]), 0, x(entryBand[1]) - x(entryBand[0]), rows.length * rowH);
    g.font = "10px ui-monospace, Consolas, monospace";
    g.textBaseline = "middle";
    let lastDay = "";
    rows.forEach((row, index) => {
      const y = index * rowH;
      const label = day(row.window_start_ns);
      if (label !== lastDay && rowH >= 3) { g.fillStyle = css("--muted"); g.fillText(label.slice(5), 0, y + Math.max(rowH, 8) / 2); lastDay = label; }
      g.globalAlpha = row.kept ? 1 : 0.32;
      g.fillStyle = css("--line");
      g.fillRect(x(0), y, x(300) - x(0), rowH - 1);
      g.fillStyle = css("--negative");
      for (const [start, length] of row.holes) g.fillRect(x(start), y, Math.max(1, x(start + length) - x(start)), rowH - 1);
      g.globalAlpha = 1;
    });
    g.fillStyle = css("--muted");
    [0, 60, 120, 180, 240, 300].forEach((t) => g.fillText(t === 300 ? "close" : `${t}s`, Math.min(x(t) - 6, width - 30), rows.length * rowH + 12));
  }, [rows, rowH, theme, entryBand[0], entryBand[1]]);
  const pick = (event: React.MouseEvent<HTMLCanvasElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    return rows[Math.floor((event.clientY - box.top) / rowH)] || null;
  };
  if (!rows.length) return <p className="plot-caption">No window to draw.</p>;
  return <div className="dv-map">
    <canvas ref={ref} className="dv-canvas" onMouseMove={(event) => setHover(pick(event))} onMouseLeave={() => setHover(null)} onClick={(event) => { const row = pick(event); if (row && onPick) onPick(row); }} aria-label="Book holes in each window" role="img" />
    <div className="dv-map-foot">
      {hover ? <span><b>{day(hover.window_start_ns)} {hm(hover.window_start_ns)} UTC</b> · {hover.kept ? "kept" : `excluded: ${(hover.reason || "").replaceAll("_", " ")}`} · valid {pct(hover.valid_ratio)} · longest hole {hover.longest_hole_s ?? "—"} s{hover.outcome ? ` · ${hover.outcome}` : ""}</span>
        : <span>One row per window, time left to right over its 5 minutes. Red = book hole (blips under 250 ms removed). Faded = excluded. Shaded = default entry period.</span>}
    </div>
  </div>;
}

function FetchWinners({ rec, onStarted }: { rec: RecordingSummary; onStarted?: () => void }) {
  const [state, setState] = useState<"" | "busy" | "started">("");
  const [error, setError] = useState("");
  const missing = rec.winners_missing || 0;
  const waited = rec.outcome_wait;
  const fetch = async () => {
    setState("busy"); setError("");
    const envelope = await call("recording.fetch_outcomes", { block_id: rec.block_id }).catch(() => null);
    if (!envelope?.ok) { setState(""); setError([envelope?.error?.reason || "The workspace API is not reachable.", ...((envelope?.error?.fields?.unreachable as string[]) || [])].join(" · ")); return; }
    setState("started"); onStarted?.();
  };
  if (!missing && !waited) return null;
  return <p className="plot-caption">
    {waited ? `After the capture the recorder waited for winners: ${waited.known} of ${waited.windows} observed. ` : ""}
    {missing > 0 ? <>{missing} complete window{missing > 1 ? "s have" : " has"} no observed winner, so sets that need the provider's winner exclude {missing > 1 ? "them" : "it"}. {state === "started" ? <b>Fetching: progress shows under Record market data; the windows update when it ends.</b> : <button className="text-button" disabled={state === "busy"} onClick={() => void fetch()}>{state === "busy" ? "Starting…" : `Fetch the missing winner${missing > 1 ? "s" : ""}`}</button>}</> : "Every complete window has its winner."}
    {error && <span className="dv-error"> {error}</span>}
  </p>;
}

export function RecordingHealth({ recordings, theme, onFetchStarted }: { recordings: RecordingSummary[]; theme: string; onFetchStarted?: () => void }) {
  const p = paletteFor(theme);
  if (!recordings.length) return <p className="plot-caption">No finished recording in these warehouses.</p>;
  return <div className="dv-health">{recordings.map((rec) => {
    const series = rec.series;
    const option = {
      animation: false,
      grid: { left: 40, right: 12, top: 14, bottom: 28 },
      tooltip: { trigger: "axis", backgroundColor: p.surface, borderColor: p.grid, textStyle: { color: p.text, fontSize: 11 } },
      xAxis: { type: "category", data: series.map((row) => hm(row.window_start_ns)), axisLabel: { color: p.muted, fontSize: 10, hideOverlap: true }, axisLine: { lineStyle: { color: p.grid } } },
      yAxis: { type: "value", max: 300, name: "s lost", nameTextStyle: { color: p.muted, fontSize: 10 }, axisLabel: { color: p.muted, fontSize: 10 }, splitLine: { lineStyle: { color: p.grid } } },
      series: [{
        type: "bar", barCategoryGap: "15%",
        data: series.map((row) => ({ value: row.valid_ratio == null ? 0 : Math.round((1 - row.valid_ratio) * 300), itemStyle: { color: row.kept ? p.green : p.red } })),
        markLine: rec.degraded_from_ns ? { symbol: "none", silent: true, lineStyle: { color: p.text, type: "dashed" }, label: { formatter: "continuous gaps", color: p.muted, fontSize: 10 }, data: [{ xAxis: hm(rec.degraded_from_ns) }] } : undefined,
      }],
    };
    return <section className="panel dv-health-card" key={rec.block_id}>
      <div className="panel-head"><div><h3>{day(rec.started_at_ns)} · {hm(rec.started_at_ns)}–{hm(rec.stopped_at_ns)} UTC</h3><p>{rec.warehouse?.split(/[/\\]/).pop()} · {rec.windows} windows · {rec.kept} kept under {rec.policy} · {pct(rec.book_time_lost)} book time lost{rec.degraded_from_ns ? ` · gaps in almost every window from ${hm(rec.degraded_from_ns)}` : " · no continuous degradation"}</p></div></div>
      <Chart option={option} height={150} label={`Book seconds lost per window, recording of ${day(rec.started_at_ns)}`} />
      <FetchWinners rec={rec} onStarted={onFetchStarted} />
    </section>;
  })}<p className="plot-caption">Bars: seconds of book missing in each 5-minute window (green kept, red excluded). Recordings before 23 Oct subscribed every market a day ahead; the subscription grew to ~700 tokens and the venue then dropped the socket every few minutes.</p></div>;
}

type Plan = { warehouses: { path: string; name: string; collections: number; busy: boolean }[]; new_warehouse_name: string; duration: { min_s: number; max_s: number; default_s: number }; bytes_per_hour_estimate: number; bytes_per_hour_basis: string; free_disk_bytes: number; credentials_present: boolean; network: { name: string; ok: boolean; reason: string }[]; subscription: string; running: Recording[]; closing: { window_s: number; close_margin_s: number; finish_windows_default: boolean; outcome_wait_default_s: number; outcome_wait_max_s: number; basis: string } };
type Recording = { recording_id: string; kind?: string; name: string; warehouse: string; duration_s: number; started_at: string; state: string; phase?: string | null; elapsed_s: number; remaining_s: number; finish_windows?: boolean; outcome_wait_s?: number; collection?: string | null; host_status?: { markets?: number; book_markets?: number; subscribed_tokens?: number; feeds?: Record<string, { connected: boolean; source?: string }>; raw_bytes?: number; writer_healthy?: boolean; capture_until?: number | null; outcomes?: { windows: number; known: number; wait_until?: number | null } | null } | null; log_tail?: string[] };
const gb = (bytes?: number | null) => (bytes == null ? "—" : `${(bytes / 1e9).toFixed(1)} GB`);
const span = (s: number) => (s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min` : `${Math.round(s / 60)} min`);

export function RecordingPanel({ onFinished, version = 0 }: { onFinished: () => void; version?: number }) {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [hours, setHours] = useState(6);
  const [target, setTarget] = useState("new");
  const [name, setName] = useState("");
  const [normalize, setNormalize] = useState(true);
  const [finish, setFinish] = useState(true);
  const [waitMin, setWaitMin] = useState(15);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = async () => {
    try {
      const [planned, listed] = await Promise.all([call("recording.plan", {}), call("recording.list", {})]);
      if (planned.ok) setPlan(planned.result as unknown as Plan); else setError(planned.error?.reason || "Recording plan unavailable.");
      if (listed.ok) setRecordings((listed.result?.recordings as Recording[]) || []);
    } catch { setError("The workspace API is not reachable."); }
  };
  useEffect(() => { void load(); }, [version]);
  const live = recordings.some((item) => item.state === "recording" || item.state === "starting");
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(async () => {
      const listed = await call("recording.list", {}).catch(() => null);
      if (listed?.ok) {
        const next = (listed.result?.recordings as Recording[]) || [];
        if (recordings.some((item) => item.state === "recording") && !next.some((item) => item.state === "recording" || item.state === "starting")) onFinished();
        setRecordings(next);
      }
    }, 5000);
    return () => window.clearInterval(timer);
  }, [live, recordings]);
  const start = async () => {
    setBusy(true); setError("");
    const envelope = await call("recording.start", { duration_s: Math.round(hours * 3600), name, normalize_live: normalize, finish_windows: finish, outcome_wait_s: waitMin * 60, ...(target === "new" ? { new_warehouse: true } : { warehouse: target }) }).catch(() => null);
    setBusy(false);
    if (!envelope?.ok) { setError([envelope?.error?.reason || "The workspace API is not reachable.", ...((envelope?.error?.fields?.unreachable as string[]) || [])].join(" · ")); return; }
    await load();
  };
  const stop = async (id: string) => {
    const envelope = await call("recording.stop", { recording_id: id }).catch(() => null);
    if (!envelope?.ok) setError(envelope?.error?.reason || "Stop was refused."); else await load();
  };
  const blocked = plan?.network.filter((row) => !row.ok && row.name.startsWith("Polymarket") && !row.name.includes("winners")) || [];
  // What the recording will contain, from now: windows are aligned to the clock.
  const windowMs = (plan?.closing.window_s || 300) * 1000;
  const now = Date.now();
  const deadline = now + hours * 3600e3;
  const firstWindow = Math.ceil(now / windowMs) * windowMs;
  const captureEnd = finish ? Math.ceil(deadline / windowMs) * windowMs : deadline;
  const complete = Math.max(0, Math.floor((captureEnd - firstWindow) / windowMs));
  const cutWindow = !finish && deadline % windowMs !== 0;
  const estimate = plan ? ((captureEnd - now) / 3600e3) * plan.bytes_per_hour_estimate : 0;
  return <section className="panel dv-record">
    <div className="panel-head"><div><h2>Record market data</h2><p>Runs the collector for BTC 5-minute markets: order books of both legs, the Chainlink reference, Binance, metadata and resolutions. No order is placed and no paper session starts.</p></div></div>
    {!plan ? <p className="plot-caption">Checking the network, the credentials and the disk…</p> : <>
      <div className="dv-checks">
        {plan.network.map((row) => <span key={row.name} className={"ux-chip " + (row.ok ? "ok" : "warn")}>{row.ok ? "✓" : "✕"} {row.name}{row.ok ? "" : ` · ${row.reason}`}</span>)}
        <span className={"ux-chip " + (plan.credentials_present ? "ok" : "warn")}>{plan.credentials_present ? "✓" : "✕"} Reference feed credentials</span>
        <span className="ux-chip">Free disk {gb(plan.free_disk_bytes)}</span>
      </div>
      {blocked.length > 0 && <div className="notice"><strong>Polymarket is not reachable from this network.</strong><p>The TLS certificate presented for Polymarket's domains is rejected, which usually means the network blocks Polymarket. Recordings made now would contain Binance only. Connect through a network where Polymarket is reachable, then check again.</p></div>}
      <div className="dv-form">
        <label className="field"><span>Duration · {span(hours * 3600)}</span><input type="range" min={plan.duration.min_s / 3600} max={plan.duration.max_s / 3600} step={0.25} value={hours} onChange={(event) => setHours(Number(event.target.value))} /></label>
        <label className="field"><span>Warehouse</span><select value={target} onChange={(event) => setTarget(event.target.value)}><option value="new">New folder · {plan.new_warehouse_name}</option>{plan.warehouses.map((item) => <option key={item.path} value={item.path} disabled={item.busy}>{item.name}{item.busy ? " · recording" : ""}</option>)}</select></label>
        <label className="field"><span>Name</span><input value={name} placeholder={`Recording ${new Date().toISOString().slice(0, 10)}`} onChange={(event) => setName(event.target.value)} /></label>
        <label className="check"><input type="checkbox" checked={normalize} onChange={(event) => setNormalize(event.target.checked)} />Normalize while recording (needed to build sets right after the stop)</label>
        <label className="check"><input type="checkbox" checked={finish} onChange={(event) => setFinish(event.target.checked)} />Finish the last window (record until it closes, plus {plan.closing.close_margin_s} s)</label>
        <label className="field"><span>Then wait for winners</span><select value={waitMin} onChange={(event) => setWaitMin(Number(event.target.value))}>{[0, 5, 10, 15, 30, 60].filter((m) => m * 60 <= plan.closing.outcome_wait_max_s).map((m) => <option key={m} value={m}>{m ? `up to ${m} min` : "do not wait"}</option>)}</select></label>
      </div>
      <div className="dv-plan">
        <b>{complete} complete window{complete === 1 ? "" : "s"}</b>
        <span>Books and reference from {hm(now * 1e6)} to {hm(captureEnd * 1e6)} UTC{finish ? ` + ${plan.closing.close_margin_s} s` : ""} · first complete window {hm(firstWindow * 1e6)}{cutWindow ? ` · the window open at ${hm(deadline * 1e6)} is cut and will be excluded` : ""}.</span>
        <span>{waitMin ? `Then only the winners and market metadata are polled, until every window has its winner or ${waitMin} min have passed. Usually a few minutes.` : "Winners that appear after the stop are not observed; fetch them later from the recording."}</span>
      </div>
      <p className="plot-caption">Estimated {gb(estimate)} on disk ({plan.bytes_per_hour_basis}). {plan.subscription}</p>
      <button className="button primary" disabled={busy || blocked.length > 0 || !plan.credentials_present} onClick={() => void start()}>{busy ? "Starting…" : `Start recording · ${span(hours * 3600)}`}</button>
      <button className="button" onClick={() => void load()}>Check again</button>
    </>}
    {error && <p className="dv-error">{error}</p>}
    {recordings.length > 0 && <div className="dv-recordings">{recordings.slice(0, 6).map((item) => {
      const host = item.host_status;
      const outcomes = host?.outcomes;
      const phase = item.kind === "outcome_fetch" ? "fetching winners" : item.phase === "finishing_window" ? `finishing the last window · until ${hms(host?.capture_until)} UTC` : item.phase === "awaiting_outcomes" ? "waiting for winners" : "";
      const done = outcomes?.windows ? outcomes.known / outcomes.windows : Math.min(1, item.elapsed_s / item.duration_s);
      return <div key={item.recording_id} className="dv-recording-row">
        <div><strong>{item.name}</strong><small>{item.warehouse.split(/[/\\]/).pop()} · started {item.started_at.slice(0, 16).replace("T", " ")} UTC · {span(item.duration_s)}</small></div>
        <span className={"ux-chip " + (item.state === "recording" ? "ok" : item.state === "not_responding" ? "warn" : "")}>{item.state.replace("_", " ")}</span>
        {item.state === "recording" || item.state === "starting" ? <>
          <progress max={1} value={done} />
          <small>{phase ? `${phase} · ` : ""}{outcomes?.windows ? `${outcomes.known} of ${outcomes.windows} winners known · waiting until ${hm(outcomes.wait_until)} UTC at most` : `${span(item.elapsed_s)} done · ${item.phase ? "planned time reached" : `${span(item.remaining_s)} left`} · ${host?.markets ?? 0} markets known · ${host?.subscribed_tokens ?? 0} tokens subscribed · ${Object.entries(host?.feeds || {}).map(([feed, info]) => `${feed} ${info.connected ? "up" : "down"}`).join(" · ")}`}</small>
          <button className="text-button trash-delete" onClick={() => void stop(item.recording_id)}>Stop cleanly</button>
        </> : <small>{host?.markets != null ? `${host.markets} markets` : ""}{item.log_tail?.length ? ` · ${item.log_tail[item.log_tail.length - 1]}` : ""}</small>}
      </div>;
    })}</div>}
  </section>;
}

type Derived = { difference: { kept: number; added: string[]; removed: string[]; before: number; after: number }; preview_id: string; version: number; policy: string; block_ids: string[] };
export function DeriveControl({ selectionId, status, blocks, currentBlocks, onSaved }: { selectionId: string; status?: string; blocks: { block_id: string; started_at_ns: number; stopped_at_ns: number }[]; currentBlocks: string[]; onSaved: () => void }) {
  const [mode, setMode] = useState<"" | "resave" | "extend">("");
  const [add, setAdd] = useState<string[]>([]);
  const [dry, setDry] = useState<Derived | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async (save: boolean, extra = add) => {
    setBusy(true); setError("");
    const envelope = await call("selection.derive", { selection_id: selectionId, coverage: "research_v2_1", add_block_ids: extra, save }).catch(() => null);
    setBusy(false);
    if (!envelope?.ok) { setError(envelope?.error?.reason || "The workspace API is not reachable."); return; }
    if (save) { setMode(""); setDry(null); onSaved(); } else setDry(envelope.result as unknown as Derived);
  };
  const others = blocks.filter((block) => !currentBlocks.includes(block.block_id));
  if (!mode) return <span className="trash-actions">
    {status !== "current" && <button className="text-button" onClick={() => { setMode("resave"); void run(false, []); }}>Re-save under current rules</button>}
    {others.length > 0 && <button className="text-button" onClick={() => { setMode("extend"); setDry(null); }}>New version with more recordings</button>}
  </span>;
  return <div className="dv-derive">
    {mode === "extend" && <div className="dv-derive-pick">{others.map((block) => <label key={block.block_id} className="check"><input type="checkbox" checked={add.includes(block.block_id)} onChange={(event) => { const next = event.target.checked ? [...add, block.block_id] : add.filter((id) => id !== block.block_id); setAdd(next); setDry(null); }} />{day(block.started_at_ns)} {hm(block.started_at_ns)}–{hm(block.stopped_at_ns)}</label>)}<button className="button" disabled={!add.length || busy} onClick={() => void run(false)}>Show the difference</button></div>}
    {busy && <span className="plot-caption">Checking…</span>}
    {dry && <p className="plot-caption">Version {dry.version} under <code>{dry.policy}</code>: {dry.difference.before} → {dry.difference.after} windows · {dry.difference.added.length} added · {dry.difference.removed.length} removed · {dry.difference.kept} unchanged. The current set is not modified.</p>}
    {error && <span className="dv-error">{error}</span>}
    <span className="trash-actions">{dry && <button className="button primary" disabled={busy || !dry.difference.after} onClick={() => void run(true)}>Save as version {dry.version}</button>}<button className="text-button" onClick={() => { setMode(""); setDry(null); setAdd([]); setError(""); }}>Cancel</button></span>
  </div>;
}

const CONDITIONS: { key: string; label: string; unit: string }[] = [
  { key: "realised_vol_bp", label: "Reference volatility over the window", unit: "bp" },
  { key: "move_bp", label: "Move from K to close", unit: "bp" },
  { key: "spread", label: "Mean bid-ask spread", unit: "USDC/share" },
  { key: "wide_spread_share", label: "Time with a spread wider than one tick", unit: "share" },
];
export function ConditionFilters({ value, onChange, roots }: { value: Record<string, [number | null, number | null]>; onChange: (next: Record<string, [number | null, number | null]>) => void; roots: string[] }) {
  const [summary, setSummary] = useState<FeatureSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const load = async () => {
    setLoading(true);
    const envelope = await call("library.features", { roots }).catch(() => null);
    setLoading(false);
    if (envelope?.ok) setSummary(envelope.result?.summary as FeatureSummary);
  };
  const set = (key: string, low: number | null, high: number | null) => {
    const next = { ...value };
    if (low == null && high == null) delete next[key]; else next[key] = [low, high];
    onChange(next);
  };
  if (!summary) return <div className="dv-conditions"><button className="button" disabled={loading} onClick={() => void load()}>{loading ? "Measuring market conditions… (once per recording)" : "Filter by market conditions"}</button></div>;
  return <div className="dv-conditions">{CONDITIONS.map(({ key, label, unit }) => {
    const s = summary[key];
    if (!s) return null;
    const current = value[key];
    const preset = (name: string, low: number | null, high: number | null) => <button key={name} type="button" className={current && current[0] === low && current[1] === high ? "selected" : ""} onClick={() => set(key, low, high)}>{name}</button>;
    return <div key={key} className="dv-condition">
      <span>{label} <small>({unit}; range {s.min}–{s.max}, median {s.p50})</small></span>
      <div className="segmented">{preset("any", null, null)}{preset("low third", null, s.p33)}{preset("middle", s.p33, s.p67)}{preset("high third", s.p67, null)}</div>
    </div>;
  })}<p className="plot-caption">Thirds are measured on the windows of these warehouses. A window without the measurement is excluded with that reason.</p></div>;
}

type Lineage = { selection_id: string; name?: string; description: string; version: number; ancestors: { selection_id: string; name?: string; version: number; deleted: boolean }[]; children: { selection_id: string; name?: string; version: number }[]; runs: { job_id: string; name: string; state: string; created_at: string; scenario_name?: string; configurations: string[] }[]; candidates: { candidate_id: string; name?: string; frozen: boolean }[]; last_used_at?: string | null; profiles: Profile[]; has_profiles: boolean };

function download(filename: string, content: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url; link.download = filename; document.body.appendChild(link); link.click(); link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function SetDetails({ selectionId, theme, onChanged, onOpenRun }: { selectionId: string; theme: string; onChanged: () => void; onOpenRun?: (jobId: string) => void }) {
  const [open, setOpen] = useState(false);
  const [lineage, setLineage] = useState<Lineage | null>(null);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [note, setNote] = useState("");
  const load = async () => {
    const envelope = await call("selection.lineage", { selection_id: selectionId }).catch(() => null);
    if (envelope?.ok) { const body = envelope.result as unknown as Lineage; setLineage(body); setName(body.name || ""); setDescription(body.description || ""); }
    else setNote(envelope?.error?.reason || "Details unavailable.");
  };
  useEffect(() => { if (open && !lineage) void load(); }, [open]);
  const save = async () => {
    const envelope = await call("selection.annotate", { selection_id: selectionId, name, description }).catch(() => null);
    if (!envelope?.ok) { setNote(envelope?.error?.reason || "Not saved."); return; }
    setEditing(false); setNote("Saved. The windows and pins of the set did not change."); await load(); onChanged();
  };
  const exportAs = async (format: "csv" | "json") => {
    const envelope = await call("selection.export", { selection_id: selectionId, format }).catch(() => null);
    if (!envelope?.ok || !envelope.result) { setNote(envelope?.error?.reason || "Export failed."); return; }
    download(String(envelope.result.filename), String(envelope.result.content), String(envelope.result.content_type));
    setNote(`Exported ${envelope.result.windows} windows. A copy is kept at ${envelope.result.path}`);
  };
  if (!open) return <button className="text-button" onClick={() => setOpen(true)}>Details, history and export</button>;
  return <div className="dv-details">
    {!lineage ? <p className="plot-caption">{note || "Loading…"}</p> : <>
      <div className="dv-details-grid">
        <div>
          <h4>Name and description</h4>
          {editing ? <>
            <label className="field"><span>Name</span><input value={name} onChange={(event) => setName(event.target.value)} /></label>
            <label className="field"><span>Description</span><textarea rows={3} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="What this set is for, what it excludes" /></label>
            <span className="trash-actions"><button className="button primary" onClick={() => void save()}>Save</button><button className="text-button" onClick={() => setEditing(false)}>Cancel</button></span>
          </> : <>
            <p>{lineage.description || <span className="plot-caption">No description.</span>}</p>
            <button className="text-button" onClick={() => setEditing(true)}>Rename or describe</button>
          </>}
          <h4>Versions</h4>
          <p className="plot-caption">{lineage.ancestors.length ? `Derived from ${lineage.ancestors.map((item) => `${item.name || item.selection_id.slice(0, 8)} (v${item.version}${item.deleted ? ", deleted" : ""})`).join(" ← ")}. ` : "First version. "}{lineage.children.length ? `Newer: ${lineage.children.map((item) => `${item.name || item.selection_id.slice(0, 8)} (v${item.version})`).join(", ")}.` : "No newer version."}</p>
          <h4>Export membership</h4>
          <span className="trash-actions"><button className="button" onClick={() => void exportAs("csv")}>CSV</button><button className="button" onClick={() => void exportAs("json")}>JSON</button></span>
        </div>
        <div>
          <h4>Used by {lineage.runs.length} run(s){lineage.last_used_at ? ` · last ${lineage.last_used_at.slice(0, 16).replace("T", " ")} UTC` : ""}</h4>
          {lineage.runs.length ? <div className="dv-runs">{lineage.runs.slice(0, 8).map((run) => <div key={run.job_id}><span><b>{run.name}</b> · {run.configurations.join(", ")}<small>{run.scenario_name} · {run.state} · {run.created_at?.slice(0, 16).replace("T", " ")} UTC</small></span>{onOpenRun && <button className="text-button" onClick={() => onOpenRun(run.job_id)}>Open result →</button>}</div>)}</div> : <p className="plot-caption">No replay has used this set yet.</p>}
          <h4>Candidates from those runs</h4>
          <p className="plot-caption">{lineage.candidates.length ? lineage.candidates.map((item) => `${item.name || item.candidate_id.slice(0, 8)}${item.frozen ? " (frozen)" : ""}`).join(", ") : "None."}</p>
        </div>
      </div>
      {lineage.has_profiles ? <WindowMap profiles={lineage.profiles} theme={theme} /> : <p className="plot-caption">This set was saved before window gap profiles were stored. Re-save it under current rules to get its map.</p>}
      {note && <p className="plot-caption">{note}</p>}
      <button className="text-button" onClick={() => setOpen(false)}>Hide details</button>
    </>}
  </div>;
}

type Series = { t_ns: number[]; reference: (number | null)[]; k_reconstructed: number | null; up: { bid: (number | null)[]; ask: (number | null)[] }; down: { bid: (number | null)[]; ask: (number | null)[] }; window_start_ns: number; window_end_ns: number; source: string };
export function WindowData({ row, holes, priceToBeat, outcome, theme }: { row: { warehouse?: string; collection?: string; market_id: string; window_start_ns?: number; window_end_ns?: number }; holes?: [number, number][]; priceToBeat?: string | null; outcome?: string | null; theme: string }) {
  const [series, setSeries] = useState<Series | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setSeries(null); setError("");
    if (!row.warehouse || !row.collection || !row.window_start_ns) { setError("This window has no recorded data."); return; }
    void call("library.window_series", { warehouse: row.warehouse, collection: row.collection, market_id: row.market_id, window_start_ns: row.window_start_ns, window_end_ns: row.window_end_ns })
      .then((envelope) => { if (envelope.ok) setSeries(envelope.result as unknown as Series); else setError(envelope.error?.reason || "Series unavailable."); })
      .catch(() => setError("The workspace API is not reachable."));
  }, [row.market_id]);
  const option = useMemo(() => {
    if (!series) return null;
    const p = paletteFor(theme);
    const t = series.t_ns.map((ns) => ns / 1e6);
    const pair = (values: (number | null)[]) => t.map((ms, i) => [ms, values[i]]);
    const start = series.window_start_ns / 1e6, end = series.window_end_ns / 1e6;
    const holeAreas = (holes || []).map(([offset, length]) => [{ xAxis: start + offset * 1000 }, { xAxis: start + (offset + length) * 1000 }]);
    const axis = (grid: number) => ({ type: "time", gridIndex: grid, min: t[0], max: t[t.length - 1], axisLabel: { color: p.muted, fontSize: 10, formatter: (v: number) => new Date(v).toISOString().slice(11, 16), hideOverlap: true }, splitLine: { show: false } });
    const yAxis = (grid: number, name: string, scale = false) => ({ type: "value", gridIndex: grid, name, nameGap: 8, scale, min: scale ? undefined : 0, max: scale ? undefined : 1, nameTextStyle: { color: p.muted, fontSize: 10 }, axisLabel: { color: p.muted, fontSize: 10 }, splitLine: { lineStyle: { color: p.grid } } });
    const line = (name: string, data: unknown[], color: string, grid: number, dashed = false) => ({ name, type: "line", xAxisIndex: grid, yAxisIndex: grid, data, showSymbol: false, connectNulls: false, lineStyle: { color, width: 1.3, type: dashed ? "dashed" : "solid" }, itemStyle: { color } });
    const marks = (grid: number) => ({ name: "Window", type: "line", xAxisIndex: grid, yAxisIndex: grid, data: [], silent: true, markLine: { symbol: "none", silent: true, label: { show: false }, lineStyle: { color: p.muted }, data: [{ xAxis: start }, { xAxis: end }] }, markArea: { silent: true, itemStyle: { color: p.red, opacity: 0.12 }, data: holeAreas } });
    const k = series.k_reconstructed;
    return {
      animation: false, legend: { top: 0, left: 60, textStyle: { color: p.muted, fontSize: 10 }, itemWidth: 14, itemHeight: 3, data: ["Up bid", "Up ask", "Down bid", "Down ask", "Reference"] },
      tooltip: { trigger: "axis", backgroundColor: p.surface, borderColor: p.grid, textStyle: { color: p.text, fontSize: 11 } },
      axisPointer: { link: [{ xAxisIndex: [0, 1, 2] }] },
      grid: [{ left: 64, right: 14, top: 56, height: 118 }, { left: 64, right: 14, top: 214, height: 118 }, { left: 64, right: 14, top: 372, height: 118 }],
      graphic: ([["UP", 40], ["DOWN", 198], ["REFERENCE", 356]] as [string, number][]).map(([text, top]) => ({ type: "text", left: 6, top, style: { text, fill: p.text, fontSize: 10, fontWeight: 700 } })),
      xAxis: [axis(0), axis(1), axis(2)], yAxis: [yAxis(0, "USDC/share"), yAxis(1, "USDC/share"), yAxis(2, "USD", true)],
      series: [
        line("Up bid", pair(series.up.bid), p.blue, 0), line("Up ask", pair(series.up.ask), p.blue, 0, true), marks(0),
        line("Down bid", pair(series.down.bid), p.teal, 1), line("Down ask", pair(series.down.ask), p.teal, 1, true), marks(1),
        { ...line("Reference", pair(series.reference), p.text, 2), markLine: k == null ? undefined : { symbol: "none", silent: true, label: { formatter: `K ${k.toFixed(2)}`, color: p.muted, fontSize: 10 }, lineStyle: { color: p.market, type: "dashed" }, data: [{ yAxis: k }] } },
        marks(2),
      ],
    };
  }, [series, theme, holes]);
  if (error) return <p className="plot-caption">{error}</p>;
  if (!series || !option) return <p className="plot-caption">Reading the quotes and the reference of this window… (the first window of a recording takes a few seconds)</p>;
  return <div className="dv-window-data">
    <p className="plot-caption">Recorded data only, before any replay: best bid and ask of each leg, and the Chainlink reference, from 5 minutes before the open to 30 s after the close. Red bands are book holes. Reconstructed K {series.k_reconstructed?.toFixed(2) ?? "—"}{priceToBeat ? ` · provider price-to-beat ${Number(priceToBeat).toFixed(2)}` : ""}{outcome ? ` · outcome ${outcome}` : ""}.</p>
    <Chart option={option} height={520} label="Bid and ask of both legs and the reference over the window" />
  </div>;
}

type Reservation = { reservation_id: string; name: string; created_at: string; windows: number; block_ids: string[]; start_utc?: string | null; end_utc?: string | null };
export function Reservations({ version, onChanged }: { version: number; onChanged: () => void }) {
  const [rows, setRows] = useState<Reservation[]>([]);
  const load = async () => { const envelope = await call("reservation.list", {}).catch(() => null); if (envelope?.ok) setRows((envelope.result?.reservations as Reservation[]) || []); };
  useEffect(() => { void load(); }, [version]);
  return <section className="panel">
    <div className="panel-head"><div><h3>Reserved for a Tune holdout · {rows.length}</h3><p>Windows set aside so Tune can test on data no Replay has seen. Replay and the set builder warn when a set uses them; nothing is removed silently.</p></div></div>
    {!rows.length ? <p className="plot-caption">No reservation. Tick recordings on the Warehouse tab and choose Reserve for Tune.</p> : <div className="trash-list">{rows.map((item) => <div className="trash-row" key={item.reservation_id}><div><strong>{item.name}</strong><small>{item.windows} windows · {item.block_ids.length} recording(s) · created {item.created_at.slice(0, 16).replace("T", " ")} UTC</small></div><DeleteReservation id={item.reservation_id} onDone={() => { void load(); onChanged(); }} /></div>)}</div>}
  </section>;
}
function DeleteReservation({ id, onDone }: { id: string; onDone: () => void }) {
  const [asking, setAsking] = useState(false);
  if (!asking) return <button className="text-button trash-delete" onClick={() => setAsking(true)}>Release</button>;
  return <span className="trash-confirm"><span>Release these windows?</span><button className="text-button trash-delete" onClick={async () => { await call("reservation.delete", { reservation_id: id }).catch(() => null); onDone(); }}>Release</button><button className="text-button" onClick={() => setAsking(false)}>Cancel</button></span>;
}
export function ReserveControl({ blockIds, onSaved }: { blockIds: string[]; onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("Tune holdout");
  const [note, setNote] = useState("");
  if (!blockIds.length) return null;
  if (!open) return <button className="button" onClick={() => setOpen(true)}>Reserve for Tune · {blockIds.length}</button>;
  return <span className="trash-confirm"><input className="dv-inline-input" value={name} onChange={(event) => setName(event.target.value)} aria-label="Reservation name" /><button className="button primary" onClick={async () => {
    const envelope = await call("reservation.save", { name, block_ids: blockIds }).catch(() => null);
    if (!envelope?.ok) { setNote(envelope?.error?.reason || "Not saved."); return; }
    setNote(`Reserved ${envelope.result?.windows} windows.`); setOpen(false); onSaved();
  }}>Reserve</button><button className="text-button" onClick={() => setOpen(false)}>Cancel</button>{note && <span className="plot-caption">{note}</span>}</span>;
}
