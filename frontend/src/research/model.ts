// Types and adapters between workflow operations and the approved research views.
// The views keep the mockup's shapes; every value here comes from a saved backend record.

export type Flat = Record<string, unknown>;
export type Nested = Record<string, Record<string, unknown>>;

export type FieldMeta = { key: string; section: string; type: string; integer?: boolean; default: unknown; choices?: string[] | null; label: string; unit?: string; policy_owned?: boolean };
export type ExecutionField = { key: string; label: string; unit: string; type: string; integer: boolean };
export type Scenario = { name: string; label: string; source: string; values: Flat; locked: string[]; fields: ExecutionField[] };
export type Base = { id: string; group: "source" | "saved" | "candidate"; label: string; source: string; parameters: Nested; scenario?: string | null; execution?: Flat; fingerprint?: string };
export type Capabilities = {
  strategy_kind?: string;
  // Protection block the strategy requires (null when it needs none).
  protection?: Record<string, unknown> | null;
  strategies: { kind: string; label: string; available: boolean; reason: string }[];
  essentials: string[];
  groups: { id: string; name: string; description: string; sections: string[] }[];
  fields: FieldMeta[];
  scenarios: Scenario[];
  bases: Base[];
  policy: Record<string, { name: string; fields: Flat; description: string }>;
  default_scenario?: string;
};

export type SetSummary = {
  selection_id: string;
  name?: string;
  included: number;
  research_policy?: string | null;
  research_limitation?: string | null;
  block_ids?: string[];
  start_ns?: number | null;
  end_ns?: number | null;
  digest?: string;
  recipe?: string;
  legacy_unpinned?: boolean;
  created_at?: string;
  compatibility?: string;
  status?: string;
};

export type ReplayConfig = { id: string; name: string; strategy: string; protection?: Record<string, unknown> | null; base: string; baseLabel: string; parameters: Nested; scenario: string; execution: Flat; fingerprint: string; validatedAt: string };

export type HistoryRun = {
  batch_id: string;
  name: string;
  experiment_id: string;
  experiments_in_batch?: number;
  job_id?: string | null;
  state: string;
  created_at?: string;
  selection_id?: string;
  selection_name?: string;
  window_count?: number;
  strategy_kind: string;
  scenario_name?: string;
  configurations: string[];
  completed?: number;
  expected?: number;
  error?: string | null;
  research_policy?: string | null;
  status?: string;
};

export type Position = { side?: string | null; filled_gross_shares?: string | null; filled_net_shares?: string | null; vwap?: string | null; debit?: string | null; sold_shares?: string | null; fee_usdc?: string | null; fee_shares?: string | null; payout?: string | null };
export type ResultRow = {
  unit_id: string;
  market_id: string;
  window_start_ns?: number | null;
  window_end_ns?: number | null;
  qualified?: boolean | null;
  entered?: boolean;
  filled?: boolean;
  realized_pnl?: number | string | null;
  label_winner?: string | null;
  configuration_name?: string;
  attempt_summary?: { role?: string; outcome?: string; outcome_reason?: string | null; side?: string; decided_at_ns?: number }[];
  execution_state?: string;
  position?: Position | null;
  window_clock?: string;
  limitations?: string[];
  label_reason?: string;
  label_method?: string;
  position_source?: string;
  qualification?: "qualified" | "qualified_with_notes" | "unqualified" | string;
  qualification_notes?: { held_through_gap?: { duration_ms: number; start_ns: number }[] };
  k_check?: string;
};

export type Summary = {
  windows: number;
  qualified: number;
  unqualified: number;
  unknown_qualification: number;
  attempts: number;
  qualified_attempts: number;
  qualified_requested_windows: number;
  fills: number;
  qualified_fills: number;
  no_trade: number;
  qualified_pnl: number | null;
  qualified_pnl_rows: number;
  execution_states: Record<string, number>;
  qualified_with_notes?: number;
  pnl_per_fill?: { n: number; mean: number | null; sd: number | null; ci95: [number, number] | null; verdict: string };
  k_checks?: Record<string, number>;
};

export type ResultView = {
  job_id: string;
  experiment_id: string;
  batch_id?: string;
  batch_name?: string;
  selection_id?: string;
  selection_name?: string;
  selection_window_count?: number | null;
  recordings?: number | null;
  research_policy?: string | null;
  scenario_name?: string;
  engine?: string;
  engine_current?: boolean;
  strategy_kind?: string;
  created_at?: string;
  window_count?: number;
  windows: ResultRow[];
  series: ResultRow[];
  window_total: number;
  ledger_total: number;
  job_window_total: number;
  next_cursor: number | null;
  configurations: { name: string; fingerprint: string; parameters: Nested }[];
  configuration_names: string[];
  configuration_name: string | null;
  execution: Flat;
  summary: Summary;
  limitation?: string | null;
  job_state?: string;
  expected?: number;
  completed?: number;
  error?: string | null;
};

export const policyNames: Record<string, string> = { exploratory: "exploratory95_v1", replay_research_v2: "replay_research_v2", replay_research_v2_1: "replay_research_v2_1" };
export const replayablePolicy = (policy?: string | null) => policy === "exploratory" || policy === "replay_research_v2" || policy === "replay_research_v2_1";
export const policyLabel = (policy?: string | null) => (policy ? policyNames[policy] || policy : "No named policy");
export const strategyName = (kind?: string) => (kind === "settlement_value" ? "Settlement value" : String(kind || "").replaceAll("_", " "));

// Parameter trees are sections of fields (settlement_value, z_gap) or flat fields
// (ask70, feature_probe). Form keys are "section.field" or just "field".
const isSection = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
export function flatten(parameters: Nested): Flat {
  const flat: Flat = {};
  for (const [section, values] of Object.entries(parameters || {})) {
    if (isSection(values)) for (const [name, value] of Object.entries(values)) flat[`${section}.${name}`] = value;
    else flat[section] = values;
  }
  return flat;
}
export function nest(flat: Flat): Nested {
  const nested: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(flat)) {
    const dot = key.indexOf(".");
    if (dot < 0) { nested[key] = value; continue; }
    const section = key.slice(0, dot), name = key.slice(dot + 1);
    const part = isSection(nested[section]) ? (nested[section] as Record<string, unknown>) : {};
    part[name] = value;
    nested[section] = part;
  }
  return nested as Nested;
}
export const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
export function show(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "boolean") return value ? "On" : "Off";
  if (value === null || value === undefined) return "—";
  return String(value);
}

export function asNumber(value: unknown): number | null {
  if (value == null || value === "" || typeof value === "boolean") return null;
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}
export function nsToMs(value: unknown): number | null {
  const ns = asNumber(value);
  if (ns == null || ns <= 0) return null;
  return ns > 1e15 ? ns / 1e6 : ns;
}
export const utc = (ms: number) => new Date(ms).toISOString().slice(11, 19);
export const utcMs = (ms: number) => new Date(ms).toISOString().slice(11, 23);
export const hm = (ms: number) => new Date(ms).toISOString().slice(11, 16);
export const day = (ms: number) => new Date(ms).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
export function dateRange(startNs?: number | null, endNs?: number | null): string {
  const start = nsToMs(startNs), end = nsToMs(endNs);
  if (start == null) return "Dates unavailable";
  if (end == null) return `${day(start)} · ${hm(start)} UTC`;
  return day(start) === day(end) ? `${day(start)} · ${hm(start)}–${hm(end)} UTC` : `${day(start)} ${hm(start)} – ${day(end)} ${hm(end)} UTC`;
}
export function created(iso?: string): { date: string; time: string } {
  const ms = iso ? Date.parse(iso) : NaN;
  if (!Number.isFinite(ms)) return { date: "Unknown", time: "" };
  return { date: new Date(ms).toISOString().slice(0, 10), time: hm(ms) + " UTC" };
}
export function fmtMoney(value: unknown): string {
  const v = asNumber(value);
  if (v === null) return "Unknown";
  return (v < 0 ? "−" : v > 0 ? "+" : "") + Math.abs(v).toFixed(2);
}
export function fmtShares(value: unknown): string {
  const v = asNumber(value);
  return v === null ? "—" : v.toFixed(2).replace(/\.?0+$/, "");
}
export function fmtPrice(value: unknown, digits = 3): string {
  const v = asNumber(value);
  return v === null ? "—" : v.toFixed(digits);
}

export function windowLabel(row: Pick<ResultRow, "window_start_ns" | "window_end_ns">): string {
  const start = nsToMs(row.window_start_ns);
  if (start == null) return "Time unavailable";
  const end = nsToMs(row.window_end_ns);
  return end == null ? hm(start) : `${hm(start)}–${hm(end)}`;
}
export function windowDay(row: Pick<ResultRow, "window_start_ns">): string {
  const start = nsToMs(row.window_start_ns);
  return start == null ? "" : day(start);
}

const reasonText: Record<string, string> = {
  unfilled_cancelled: "No displayed depth at the limit · remainder cancelled",
  order_constraints_unqualified: "Tick / minimum size not known at submission",
  gap_with_exposure: "Input gap while a position was open",
  EDGE_BELOW_COST: "Edge below cost",
  EDGE_CLEARS_COST: "Edge clears cost",
  OUTSIDE_ENTRY_BAND: "Outside the entry time band",
  EPISODE_CONSUMED: "Entry episode already used",
  DEPTH_INSUFFICIENT: "Insufficient displayed depth",
  ENTRY_WINDOW_CLOSED: "Entry window closed",
  HOLDING_TO_SETTLEMENT: "Holding to settlement",
  HOLD_TO_SETTLEMENT: "Holding to settlement",
  MODEL_NOT_READY: "Model not ready",
  BOOK_NOT_READY: "Book not ready",
  exact_candidate_absent: "No exact market candidate",
  incomplete_fallback_candidates: "Fallback candidates incomplete",
};
export function explain(code?: string | null): string {
  if (!code) return "Not stored";
  return code.split(",").map((part) => reasonText[part.trim()] || part.trim().replaceAll("_", " ").toLowerCase().replace(/^./, (c) => c.toUpperCase())).join(" · ");
}

export const executionStates: Record<string, string> = {
  no_entry: "No eligible entry",
  not_sent: "Request · not sent",
  no_fill: "Request · no fill",
  partial: "Partial fill",
  filled: "Filled · hold",
  exited: "Filled · exited",
};
export function executionText(row: ResultRow): { title: string; detail: string } {
  const state = row.execution_state || (row.filled ? "filled" : row.entered ? "no_fill" : "no_entry");
  const title = executionStates[state] || state;
  const position = row.position;
  if (row.filled && position?.filled_gross_shares) return { title, detail: `${fmtShares(position.filled_gross_shares)} shares · ${position.side || "leg not stored"} @ ${fmtPrice(position.vwap)}` };
  if (row.filled) return { title, detail: "Fill recorded · size not stored on this earlier result" };
  const attempt = (row.attempt_summary || [])[0];
  if (attempt) return { title, detail: explain(attempt.outcome_reason) };
  return { title, detail: "No acquired position" };
}

export const kCheckText: Record<string, string> = {
  confirmed_by_provider: "K confirmed by the provider",
  differs_from_provider: "K differs from the provider",
  provider_not_published: "Provider K not published",
  not_stored: "K not stored on this earlier result",
  not_comparable: "K not comparable",
};
export function qualificationTag(row: ResultRow): { text: string; tone: string } {
  if (row.qualification === "qualified_with_notes") return { text: "Qualified · with notes", tone: "good" };
  if (row.qualified === true) return { text: "Qualified under policy", tone: "good" };
  if (row.qualified === false) return { text: "Unqualified", tone: "warn" };
  return { text: "Unknown", tone: "" };
}
export function heldGapText(row: ResultRow): string {
  const held = row.qualification_notes?.held_through_gap || [];
  if (!held.length) return "";
  const seconds = held.reduce((sum, item) => sum + item.duration_ms, 0) / 1000;
  return `Held through ${held.length} book gap${held.length > 1 ? "s" : ""} (${seconds.toFixed(1)} s) with no order inside`;
}

export function pnlText(row: ResultRow): string {
  if (row.entered !== true) return "No trade";
  if (row.filled !== true) return "No fill";
  return fmtMoney(row.realized_pnl);
}

export type Tick = { ms: number; action: string; reason: string; chosen: string | null; upPoint: number | null; upLow: number | null; upHigh: number | null; upBid: number | null; upAsk: number | null; downBid: number | null; downAsk: number | null; upMid: number | null; downMid: number | null; reference: number | null; k: number | null; kQuality: string | null; sigma: number | null };
export type EventKind = "request" | "arrival" | "fill" | "cancel" | "exit" | "payout";
export type EventRow = { ms: number | null; label: string; kind: EventKind; side: "UP" | "DOWN" | null; y: number | null; axis: "probability" | "price"; origin: string; detail: string };
export type Trace = {
  schema?: string;
  window_start_ns?: number | null;
  window_end_ns?: number | null;
  range_start_ns?: number | null;
  range_end_ns?: number | null;
  window_clock_contract?: string;
  truncated?: boolean;
  decisions?: Flat[];
  orders?: Flat[];
  attempts?: Flat[];
  ledger?: Flat[];
};

function mid(bid: number | null, ask: number | null): number | null {
  return bid == null || ask == null || bid <= 0 || ask <= 0 || bid > ask ? null : (bid + ask) / 2;
}

export function ticksFrom(trace: Trace | null): Tick[] {
  const ticks: Tick[] = [];
  for (const row of trace?.decisions || []) {
    const ms = nsToMs(row.at_ns) ?? (typeof row.observed_at === "string" ? Date.parse(row.observed_at) : nsToMs(row.observed_at));
    if (ms == null || !Number.isFinite(ms)) continue;
    const upBid = asNumber(row.up_bid), upAsk = asNumber(row.up_ask), downBid = asNumber(row.down_bid), downAsk = asNumber(row.down_ask);
    ticks.push({
      ms, action: String(row.action || ""), reason: String(row.reason || ""), chosen: row.chosen_side ? String(row.chosen_side).toUpperCase() : null,
      upPoint: asNumber(row.p_up_point), upLow: asNumber(row.p_up_low), upHigh: asNumber(row.p_up_high),
      upBid, upAsk, downBid, downAsk, upMid: mid(upBid, upAsk), downMid: mid(downBid, downAsk),
      reference: asNumber(row.reference_price), k: asNumber(row.k), kQuality: row.k_quality ? String(row.k_quality) : null, sigma: asNumber(row.sigma),
    });
  }
  return ticks.sort((a, b) => a.ms - b.ms);
}

const leg = (value: unknown): "UP" | "DOWN" | null => {
  const text = String(value || "").toUpperCase();
  return text === "UP" || text === "DOWN" ? text : null;
};

export function eventsFrom(trace: Trace | null): EventRow[] {
  const orders = new Map<string, Flat>();
  for (const order of trace?.orders || []) orders.set(String(order.order_id), order);
  const events: EventRow[] = [];
  for (const attempt of trace?.attempts || []) {
    const side = leg(attempt.side);
    const role = String(attempt.role || "ENTRY").toLowerCase();
    const p = asNumber(attempt.p_point);
    events.push({ ms: nsToMs(attempt.decided_at_ns), label: `${role === "exit" ? "Exit" : "Entry"} request · ${side || "leg not stored"} ≤ ${fmtPrice(attempt.limit_price)}`, kind: "request", side, y: p, axis: "probability", origin: "strategy decision", detail: `${String(attempt.outcome || "outcome not stored")}${attempt.outcome_reason ? ` · ${explain(attempt.outcome_reason as string)}` : ""}` });
  }
  for (const event of trace?.ledger || []) {
    const order = orders.get(String(event.order_id || ""));
    const side = leg(event.leg) || leg(order?.side);
    const ms = nsToMs(event.at_ns);
    const kind = String(event.event || "");
    if (kind === "arrived") {
      events.push({ ms, label: "Order arrived", kind: "arrival", side, y: null, axis: "price", origin: "simulated execution", detail: `Spend ${fmtMoney(((event.intent || {}) as Flat).spend_amount)} USDC · limit ${fmtPrice(((event.intent || {}) as Flat).limit_price)}` });
    } else if (kind === "fill") {
      const sell = String(event.side || "BUY") === "SELL";
      events.push({ ms, label: `${sell ? "Sale" : "Fill"} · ${fmtShares(event.quantity)} shares @ ${fmtPrice(event.price)}`, kind: sell ? "exit" : "fill", side, y: asNumber(event.price), axis: "price", origin: "simulated execution", detail: `Fee ${String(event.fee ?? "not stored")} · net ${fmtShares(event.net_quantity)} shares` });
    } else if (kind === "unfilled_cancelled" || kind.includes("cancel")) {
      events.push({ ms, label: "Unfilled remainder cancelled", kind: "cancel", side, y: asNumber(order?.limit_price), axis: "price", origin: "simulated execution", detail: explain(kind) });
    } else if (kind === "payout_receivable") {
      events.push({ ms, label: `Payout receivable · ${fmtMoney(event.amount)} USDC`, kind: "payout", side: null, y: null, axis: "price", origin: "settlement", detail: String(event.label_status || "") });
    }
  }
  return events.sort((a, b) => (a.ms ?? Infinity) - (b.ms ?? Infinity));
}
