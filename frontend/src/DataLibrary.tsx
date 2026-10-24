import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Chart } from "../../prototypes/ihm-review/src/Chart";
import { dark, light, type Palette } from "../../prototypes/ihm-review/src/options";
import { call, type Envelope } from "./api";
import { DeleteControl, TrashPanel, setStatusText } from "./research/Trash";
import { CookbookCards, RecipeParameters, ResolvedThresholds, SaveAsRecipe, useCookbook, type CookbookRecipe, type CookbookRecord, type CookbookSpec } from "./data/Cookbook";
import { ConditionFilters, DeriveControl, PolicyCoverage, RecordingBadge, RecordingHealth, RecordingPanel, Reservations, ReserveControl, SetDetails, WindowData, WindowMap, useRecordings, type Profile } from "./data/DataViews";

type Track = {
  state: string;
  reason?: string;
  partial?: boolean;
  usable_duration_ns?: number;
  required_duration_ns?: number;
};
export type WarehouseWindow = {
  market_id: string | null;
  collection?: string;
  block_id?: string | null;
  warehouse?: string;
  window_start_ns: number;
  tracks: Record<string, Track>;
  warmup: Track;
  outcome: string | null;
  label_source: string | null;
  partition?: string;
  availability?: string;
  duplicate?: boolean;
  reference_series?: string;
  publication_id?: string | null;
  assessed_at?: number | null;
  inferred?: boolean;
  sessions?: string[];
};
type Filters = {
  outcome: string;
  label_source: string;
  time_zone: string;
  daily_time: { enabled: boolean; start: string; end: string; time_zone: string };
  weekdays: number[];
  selection_mode: "all_matches" | "picked";
  picked: string[];
  conditions?: Record<string, [number | null, number | null]>;
};
type ExclusionExample = {
  market_id: string | null;
  reason: string;
  window_start_ns?: number;
  preparation_can_help?: boolean;
  up_book_usable_ns?: number;
  up_book_required_ns?: number;
  down_book_usable_ns?: number;
  down_book_required_ns?: number;
  reference_usable_ns?: number;
  reference_required_ns?: number;
};
type ExclusionGroup = {
  id: string;
  title: string;
  count: number;
  preparation_can_help: boolean;
  examples: ExclusionExample[];
};
type Preview = {
  preview_id: string;
  cookbook?: CookbookRecord | null;
  candidates: { market_id: string; outcome: string; label_source: string; window_start_ns: number }[];
  included: { market_id: string; collection: string; warehouse: string; coverage?: string }[];
  excluded: ExclusionExample[];
  exclusion_groups?: ExclusionGroup[];
  outcome_mix: Record<string, number>;
  population: string;
  intended_use: string;
  matched: number;
  research_policy?: string | null;
  research_limitation?: string | null;
  clock?: { weekday_basis: string };
  requires_choice?: string[];
  window_profiles?: Profile[];
  reserved_overlap?: { reservation_id: string; name: string; windows: number }[];
};
type Requirements = {
  books: boolean;
  reference: boolean;
  warmup: boolean;
  scoring_outcome: boolean;
  provider_confirmed: boolean;
  coverage: "complete" | "partial" | "exploratory" | "research_v2" | "research_v2_1";
  coverage_floor?: 0.9 | 0.95 | 0.99;
  settlement_confidence?: "strict" | "provider_reported";
};
type CoverageBucket = { start_ns: number; ready: number; preparation: number; gap: number; windows: number };
type CaptureBlock = {
  block_id: string;
  warehouse: string;
  collection: string;
  started_at_ns: number;
  stopped_at_ns: number;
  capture_clean?: boolean;
};
type SavedSet = {
  selection_id: string;
  name: string;
  recipe?: string;
  cookbook?: CookbookRecord | null;
  included: number;
  intended_use?: string;
  population?: string;
  outcome_conditioned?: boolean;
  legacy_unpinned?: boolean;
  research_policy?: string | null;
  research_limitation?: string | null;
  block_ids?: string[];
  coverage_floor?: number | null;
  settlement_confidence?: string | null;
  holdout_overlap?: number;
  compatibility?: string;
  status?: string;
  version?: number;
  parent_selection_id?: string | null;
  requirements?: Requirements;
  start_utc?: string;
  end_utc?: string;
};
type OpenedSet = SavedSet & {
  filters?: Filters;
  included_rows?: { market_id: string }[];
  limitation?: string | null;
  preview_id?: string;
};
type PrepPlan = {
  kind: string;
  preparation_id: string;
  plan_id: string;
  reason: string;
  safe_stop: string;
  collection: string;
  warehouse: string;
};

const emptyFilters = (): Filters => ({
  outcome: "any",
  label_source: "any",
  time_zone: "UTC",
  daily_time: { enabled: false, start: "22:00", end: "02:00", time_zone: "UTC" },
  weekdays: [1, 2, 3, 4, 5, 6, 7],
  selection_mode: "all_matches",
  picked: [],
});

const recipeName: Record<string, string> = {
  eligible_windows: "All windows that pass the policy",
  custom_window_set: "Custom window set",
  cookbook: "Cookbook recipe",
  // Older diagnostic sets; the builder no longer offers this (see Recording health).
  missing_inputs: "Missing inputs",
};
const BUILD_YOUR_OWN = ["eligible_windows", "custom_window_set"];
const recipeDetail: Record<string, string> = {
  eligible_windows: "Every window inside the recordings you ticked that passes the coverage rule on the next step.",
  custom_window_set: "The same recordings, narrowed by hour, weekday, time zone or market conditions. You can save the filters as a cookbook recipe.",
};
const setRecipeLabel = (item: { recipe?: string; cookbook?: CookbookRecord | null }) => (item.recipe === "cookbook" && item.cookbook ? `Cookbook · ${item.cookbook.title} v${item.cookbook.version}` : recipeName[item.recipe || ""] || item.recipe || "Legacy");
const dayName = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function formatSpan(seconds: number) {
  const safe = Math.max(0, seconds);
  if (safe < 5400) return `${Math.max(1, Math.round(safe / 60))} min`;
  return `${(safe / 3600).toFixed(1)} h`;
}

function durationLabel(block: CaptureBlock) {
  return formatSpan((block.stopped_at_ns - block.started_at_ns) / 1e9);
}

const REASON_COLOR: Record<string, string> = {
  book_below_floor: "var(--warn)",
  reference_below_floor: "var(--warn)",
  warmup_below_floor: "var(--warn)",
  book_partial: "var(--warn)",
  reserved_evaluation: "var(--muted)",
  withheld: "var(--muted)",
};

function collectionLabel(collection: string, blocks: CaptureBlock[]) {
  const matches = blocks.filter((block) => block.collection === collection);
  if (!matches.length) return "Capture folder";
  return matches.map((block) => stamp(block.started_at_ns)).join(", ");
}

function stamp(ns: number) {
  return new Date(ns / 1e6).toISOString().replace(".000Z", "Z").replace("T", " ").slice(0, 16);
}

function sessionTime(value: unknown) {
  if (typeof value === "number" && Number.isFinite(value)) return `${stamp(value)} UTC`;
  if (typeof value === "string" && /^\d+$/.test(value)) return `${stamp(Number(value))} UTC`;
  if (typeof value === "string" && value) return value.replace("T", " ").replace("+00:00", " UTC").replace("Z", " UTC");
  return "—";
}
function utcDay(ns: number) {
  return new Date(ns / 1e6).toISOString().slice(0, 10);
}
function exclusiveDay(ns: number) {
  const day = new Date(utcDay(ns) + "T00:00:00Z");
  day.setUTCDate(day.getUTCDate() + 1);
  return day.toISOString().slice(0, 10);
}
function visual(track?: Track) {
  if (!track) return "unknown";
  if (track.partial) return "partial";
  if (track.state === "present") return "complete";
  if (track.state === "missing") return "missing";
  if (track.state === "not_expected") return "missing";
  return "unknown";
}
function readiness(row: WarehouseWindow) {
  if (row.partition === "reserved" || row.outcome === "withheld") return "Reserved evaluation";
  if (row.availability === "ready") return "Inputs ready";
  if (row.availability === "preparation") return "Preparation needed";
  if (row.inferred) return "Not recorded";
  return "Coverage gap";
}
function outcomeText(row: WarehouseWindow) {
  if (row.outcome === "withheld" || row.partition === "reserved") return "Protected";
  if (row.outcome === "up") return "Up";
  if (row.outcome === "down") return "Down";
  if (row.outcome === "unresolved") return "Unresolved";
  if (row.outcome === "unknown") return "Unknown";
  return "Not recorded";
}
function labelText(source: string | null) {
  if (source === "provider_confirmed") return "Provider confirmed";
  if (source === "proxy") return "Reference-derived proxy";
  if (source === "withheld") return "Protected";
  return "Not available";
}
function err(envelope: Envelope | null) {
  return envelope && !envelope.ok ? envelope.error?.reason || envelope.error?.code || "Request failed" : "";
}

const OUTCOME_COLOR: Record<string, string> = {
  up: "var(--accent)",
  down: "var(--text)",
  unresolved: "var(--warn)",
  unknown: "var(--muted)",
  withheld: "var(--muted)",
};

function useElapsed(active: boolean) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!active) {
      setElapsed(0);
      return;
    }
    const started = Date.now();
    const timer = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [active]);
  return elapsed;
}

function ShareBar({ parts }: { parts: { label: string; value: number; color: string }[] }) {
  const total = parts.reduce((sum, part) => sum + part.value, 0);
  return (
    <div className="ux-share">
      <div className="ux-share-track">
        {total > 0 && parts.filter((part) => part.value > 0).map((part) => (
          <i key={part.label} style={{ width: `${(part.value / total) * 100}%`, background: part.color }} />
        ))}
      </div>
      <div className="ux-share-legend">
        {parts.map((part) => (
          <span key={part.label}><i style={{ background: part.color }} />{part.label} <b>{part.value}</b></span>
        ))}
      </div>
    </div>
  );
}

function Meter({ label, usable, required, floor = 0.95 }: { label: string; usable?: number; required?: number; floor?: number }) {
  if (!required) return null;
  const ratio = Math.max(0, Math.min(1, (usable || 0) / required));
  const below = ratio + 1e-9 < floor;
  return (
    <div className="ux-meter">
      <span>{label}</span>
      <div className="ux-meter-track">
        <i style={{ width: `${ratio * 100}%` }} data-short={below ? "" : undefined} />
        <b className="ux-floor" style={{ left: `${Math.min(100, floor * 100)}%` }} />
      </div>
      <small>{(ratio * 100).toFixed(1)}%</small>
    </div>
  );
}

function ChartSkeleton() {
  return (
    <div className="ux-skeleton" aria-hidden="true">
      {[46, 72, 58, 84, 40, 66, 52].map((height, index) => <i key={index} style={{ height: `${height}%` }} />)}
    </div>
  );
}

export function DataLibrary({
  roots,
  theme,
  onOpenExperiment,
  onOpenRun,
}: {
  roots: string[];
  theme: string;
  onOpenExperiment: (selection: SavedSet) => void;
  onOpenRun?: (jobId: string) => void;
}) {
  const palette: Palette = theme === "dark" ? dark : light;
  const [area, setArea] = useState("Warehouse");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [collection, setCollection] = useState("");
  const [availability, setAvailability] = useState("all");
  const [rows, setRows] = useState<WarehouseWindow[]>([]);
  const [collections, setCollections] = useState<{ collection: string; sessions: { session_id: string }[] }[]>([]);
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [inspect, setInspect] = useState<WarehouseWindow | null>(null);
  const [detail, setDetail] = useState<WarehouseWindow | null>(null);
  const [builder, setBuilder] = useState(false);
  const [step, setStep] = useState(0);
  const [recipe, setRecipe] = useState("eligible_windows");
  const [cook, setCook] = useState<CookbookSpec | null>(null);
  const cookbook = useCookbook();
  const [filters, setFilters] = useState<Filters>(emptyFilters());
  const [requirements, setRequirements] = useState<Requirements>({ books: true, reference: true, warmup: false, scoring_outcome: false, provider_confirmed: false, coverage: "research_v2_1", settlement_confidence: "provider_reported" });
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [opening, setOpening] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [candidates, setCandidates] = useState<Preview["candidates"]>([]);
  const [previewStale, setPreviewStale] = useState(false);
  const [prepPlans, setPrepPlans] = useState<PrepPlan[] | null>(null);
  const [reviewingPrep, setReviewingPrep] = useState(false);
  const [coverageCapped, setCoverageCapped] = useState(false);
  const [pageSize, setPageSize] = useState(25);
  const [matched, setMatched] = useState(0);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [history, setHistory] = useState<number[]>([0]);
  const [buckets, setBuckets] = useState<CoverageBucket[]>([]);
  const [blocks, setBlocks] = useState<CaptureBlock[]>([]);
  const [selectedBlocks, setSelectedBlocks] = useState<string[]>([]);
  const [name, setName] = useState("BTC five-minute window set");
  const [sets, setSets] = useState<SavedSet[]>([]);
  const [trashVersion, setTrashVersion] = useState(0);
  const [recordingVersion, setRecordingVersion] = useState(0);
  const [reservationVersion, setReservationVersion] = useState(0);
  const { recordings: recordingSummaries } = useRecordings(roots, recordingVersion);
  const summaryByBlock = useMemo(() => Object.fromEntries(recordingSummaries.map((item) => [item.block_id, item])), [recordingSummaries]);
  const [policyProfiles, setPolicyProfiles] = useState<Profile[]>([]);
  const [policyLoading, setPolicyLoading] = useState(false);
  useEffect(() => {
    if (!selectedBlocks.length) { setPolicyProfiles([]); return; }
    let alive = true;
    setPolicyLoading(true);
    void call("data_recipe.resolve", { roots, block_ids: selectedBlocks, recipe: "eligible_windows", requirements: { coverage: "research_v2_1" } })
      .then((envelope) => { if (alive && envelope.ok) setPolicyProfiles((envelope.result?.window_profiles as Profile[]) || []); })
      .catch(() => undefined)
      .finally(() => { if (alive) setPolicyLoading(false); });
    return () => { alive = false; };
  }, [roots.join("|"), selectedBlocks.join("|"), recordingVersion]);
  const [opened, setOpened] = useState<OpenedSet | null>(null);
  const [prep, setPrep] = useState<{ id: string; state: string; safe_stop?: string; owner_result?: Record<string, unknown> } | null>(null);
  const [span, setSpan] = useState<{ start: string; end: string } | null>(null);
  const initialLoad = useRef(false);
  const loadGen = useRef(0);
  const resolveGen = useRef(0);

  const datesValid = !start || !end || start < end;

  function blockRequest(ids = selectedBlocks) {
    const chosen = blocks.filter((block) => ids.includes(block.block_id));
    const blockRoots = [...new Set(chosen.map((block) => block.warehouse))];
    return {
      roots: blockRoots.length ? blockRoots : roots,
      block_ids: ids,
      ...(collection ? { collection } : {}),
      ...(start ? { start_utc: start } : {}),
      ...(end ? { end_utc: end } : {}),
      asset: "BTC",
      duration: 300,
    };
  }

  async function loadWarehouse(pageCursor = 0, size = pageSize, ids = selectedBlocks) {
    if (!roots.length) {
      setNotice("No warehouse root is configured.");
      return;
    }
    const gen = ++loadGen.current;
    if (!ids.length) {
      setRows([]);
      setMatched(0);
      setBuckets([]);
      setCoverageCapped(false);
      setLoading(false);
      return;
    }
    if (!datesValid) return;
    setLoading(true);
    try {
      const listedRequest = { ...blockRequest(ids), availability, limit: size, cursor: pageCursor };
      const [listed, coverage] = await Promise.all([
        call("library.windows", listedRequest),
        call("library.coverage", { ...blockRequest(ids), availability: "all", bucket_s: 3600, limit: 500 }),
      ]);
      if (gen !== loadGen.current) return;
      const nextBuckets = coverage.ok ? ((coverage.result?.buckets as CoverageBucket[]) || []) : buckets;
      if (coverage.ok) {
        setBuckets(nextBuckets);
        setCoverageCapped(nextBuckets.length >= 500);
      }
      if (!listed.ok) {
        setNotice(err(listed));
        setRows([]);
        return;
      }
      setNotice("");
      const windows = ((listed.result?.windows as WarehouseWindow[]) || []).filter((row) => row.market_id);
      setRows(windows);
      setMatched(Number(listed.result?.matched || windows.length));
      setNextCursor(listed.result?.next_cursor == null ? null : Number(listed.result.next_cursor));
      const reportedStart = listed.result?.span_start_utc as string | undefined;
      const reportedEnd = listed.result?.span_end_utc as string | undefined;
      const starts = windows.map((row) => row.window_start_ns).filter((value) => Number.isFinite(value));
      const nextSpan = reportedStart && reportedEnd
        ? { start: reportedStart, end: reportedEnd }
        : starts.length
          ? { start: utcDay(Math.min(...starts)), end: exclusiveDay(Math.max(...starts)) }
          : null;
      if (nextSpan) setSpan(nextSpan);
    } catch (error) {
      if (gen !== loadGen.current) return;
      setNotice(error instanceof Error ? error.message : "The warehouse read did not finish.");
      setRows([]);
    } finally {
      if (gen === loadGen.current) setLoading(false);
    }
  }

  useEffect(() => {
    if (!roots.length || initialLoad.current) return;
    initialLoad.current = true;
    void (async () => {
      const [listed, found] = await Promise.all([
        call("library.blocks", { roots }),
        call("library.collections", { roots }),
      ]);
      if (!listed.ok) {
        setNotice(err(listed));
        return;
      }
      setBlocks((listed.result?.blocks as CaptureBlock[]) || []);
      const groups = (found.result?.collections as { collection: string; sessions: { session_id: string }[] }[]) || [];
      setCollections(groups);
    })();
  }, [roots]);

  async function loadSets() {
    const listed = await call("selection.list", {});
    if (listed.ok) setSets((listed.result?.selections as SavedSet[]) || []);
  }

  useEffect(() => {
    void loadSets();
  }, []);

  async function resolvePreview(next = filters, nextRecipe = recipe) {
    if (!selectedBlocks.length) {
      setNotice("Tick at least one recording on the warehouse page. The set uses those recordings.");
      return null;
    }
    const gen = ++resolveGen.current;
    setChecking(true);
    setNotice("Checking the ticked recordings…");
    const request = {
      ...blockRequest(),
      availability: "all",
      recipe: nextRecipe,
      filters: { ...next, time_zone: next.time_zone, daily_time: { ...next.daily_time, time_zone: next.time_zone } },
      requirements,
      ...(nextRecipe === "cookbook" && cook ? { cookbook: cook } : {}),
    };
    let envelope: Envelope;
    try {
      envelope = await call("data_recipe.resolve", request);
    } catch (error) {
      if (gen !== resolveGen.current) return null;
      setNotice(error instanceof Error ? error.message : "The preview did not finish.");
      setPreview(null);
      return null;
    } finally {
      if (gen === resolveGen.current) setChecking(false);
    }
    if (gen !== resolveGen.current) return null;
    if (!envelope.ok) {
      setPreview(null);
      setNotice(err(envelope));
      return null;
    }
    setNotice("");
    const body = envelope.result as unknown as Preview;
    setCandidates(body.candidates || []);
    setPreviewStale(false);
    setPreview(body);
    return body;
  }

  async function saveSet() {
    if (saving) return;
    if (!preview || previewStale || !name.trim()) {
      setNotice("Preview matching windows, then save the windows you reviewed.");
      return;
    }
    const body = preview;
    setSaving(true);
    setNotice("Saving these windows. The warehouse checksums are recorded with the set. Opening or binding it checks the file bytes.");
    try {
      const envelope = await call("selection.save", {
        ...blockRequest(),
        availability: "all",
        recipe,
        filters: { ...filters, daily_time: { ...filters.daily_time, time_zone: filters.time_zone } },
        requirements,
        ...(recipe === "cookbook" && cook ? { cookbook: cook } : {}),
        preview_id: body.preview_id,
        name: name.trim(),
      });
      if (!envelope.ok) {
        setNotice(err(envelope));
        return;
      }
      setNotice(`Saved ${String(envelope.result?.selection_id)}. Membership is fixed.`);
      setBuilder(false);
      setArea("Window sets");
      await loadSets();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "The save did not finish.");
    } finally {
      setSaving(false);
    }
  }

  async function openSet(selection: SavedSet) {
    setOpening(selection.selection_id);
    setNotice("Opening the saved set. Pinned files are checked, so a large set takes a minute.");
    let envelope: Envelope;
    try {
      envelope = await call("selection.open", { selection_id: selection.selection_id });
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "The set did not open.");
      setOpening(null);
      return;
    }
    setOpening(null);
    if (!envelope.ok) {
      setNotice(err(envelope));
      return;
    }
    setNotice("");
    setOpening(null);
    setOpened({
      ...selection,
      filters: envelope.result?.filters as Filters | undefined,
      included_rows: envelope.result?.included as { market_id: string }[],
      limitation: (envelope.result?.limitation as string | null) ?? null,
      preview_id: envelope.result?.preview_id as string | undefined,
      outcome_conditioned: Boolean(envelope.result?.outcome_conditioned),
      intended_use: envelope.result?.intended_use as string,
      population: envelope.result?.population as string,
      block_ids: (envelope.result?.block_ids as string[]) || selection.block_ids || [],
      requirements: envelope.result?.requirements as Requirements | undefined,
      coverage_floor: envelope.result?.coverage_floor as number | null | undefined,
      settlement_confidence: envelope.result?.settlement_confidence as string | null | undefined,
      research_policy: envelope.result?.research_policy as string | null | undefined,
      research_limitation: envelope.result?.research_limitation as string | null | undefined,
      start_utc: (envelope.result?.start_utc as string) || "",
      end_utc: (envelope.result?.end_utc as string) || "",
      holdout_overlap: Number(envelope.result?.holdout_overlap || selection.holdout_overlap || 0),
      compatibility: (envelope.result?.compatibility as string) || selection.compatibility,
    });
  }

  function sameFilterScope(left: Filters, right: Filters) {
    return left.outcome === right.outcome
      && left.label_source === right.label_source
      && left.time_zone === right.time_zone
      && left.selection_mode === right.selection_mode
      && JSON.stringify(left.daily_time) === JSON.stringify(right.daily_time)
      && JSON.stringify(left.weekdays) === JSON.stringify(right.weekdays);
  }

  function applyFilters(next: Filters) {
    const picksOnly = sameFilterScope(filters, next) && JSON.stringify(next.picked) !== JSON.stringify(filters.picked);
    setFilters(next);
    if (picksOnly) {
      setPreview(null);
      setPreviewStale(true);
      return;
    }
    resolveGen.current += 1;
    setChecking(false);
    setPreview(null);
    setCandidates([]);
    setPreviewStale(false);
  }

  function reuse(selection: OpenedSet) {
    if (selection.filters) setFilters(structuredClone(selection.filters));
    setRecipe(selection.recipe === "missing_inputs" ? "custom_window_set" : selection.recipe || "custom_window_set");
    setCook(selection.recipe === "cookbook" && selection.cookbook ? { id: selection.cookbook.id, version: selection.cookbook.version, parameters: selection.cookbook.parameters || {} } : null);
    setName(`${selection.name} copy`);
    if (selection.block_ids?.length) setSelectedBlocks(selection.block_ids);
    if (selection.requirements) setRequirements({ ...requirements, ...selection.requirements });
    setStart(selection.start_utc || "");
    setEnd(selection.end_utc || "");
    setCandidates([]);
    setPreviewStale(true);
    setOpened(null);
    setBuilder(true);
    setStep(1);
    setPreview(null);
    setNotice("The saved recipe is back in the draft. Preview matching windows again. The saved membership stays frozen.");
  }

  async function reviewPreparation() {
    const chosen = blocks.filter((block) => selectedBlocks.includes(block.block_id));
    const targets = [...new Map(chosen.map((block) => [`${block.warehouse}:${block.collection}`, block])).values()];
    if (!targets.length) {
      setNotice("Tick the recordings to review. Preparation follows those recordings, not the table page.");
      return;
    }
    setReviewingPrep(true);
    setNotice("Reading a preparation plan. Nothing starts until you confirm it.");
    try {
      const plans: PrepPlan[] = [];
      for (const target of targets) {
        for (const kind of ["normalize", "quality", "verify"]) {
          const plan = await call("preparation.plan", {
            kind,
            warehouse: target.warehouse,
            collection: target.collection,
          });
          if (!plan.ok) {
            setNotice(err(plan));
            return;
          }
          plans.push({
            kind,
            preparation_id: String(plan.result?.preparation_id),
            plan_id: String(plan.result?.plan_id),
            reason: String(plan.result?.reason || ""),
            safe_stop: String(plan.result?.safe_stop || "unsupported"),
            collection: target.collection,
            warehouse: target.warehouse,
          });
        }
      }
      setPrepPlans(plans);
      setNotice("");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "The preparation plan did not finish.");
    } finally {
      setReviewingPrep(false);
    }
  }

  async function startReviewedPreparation(plan: PrepPlan) {
    const started = await call("preparation.start", {
      preparation_id: plan.preparation_id,
      plan_id: plan.plan_id,
    });
    if (!started.ok) {
      setNotice(err(started));
      setPrep({ id: plan.preparation_id, state: started.error?.code || "failed", safe_stop: "unsupported" });
      return;
    }
    setPrep({ id: plan.preparation_id, state: String(started.result?.state), safe_stop: "unsupported" });
    setNotice("Preparation started for the whole collection. Stop is not supported.");
  }

  useEffect(() => {
    if (!prep || prep.state === "completed" || prep.state === "failed" || prep.state === "interrupted") return;
    const timer = setInterval(async () => {
      const status = await call("preparation.status", { preparation_id: prep.id });
      if (!status.ok) return;
      setPrep({
        id: prep.id,
        state: String(status.result?.state),
        safe_stop: String(status.result?.safe_stop || "unsupported"),
        owner_result: status.result?.owner_result as Record<string, unknown> | undefined,
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [prep?.id, prep?.state]);

  const option = useMemo(() => rangeOption(buckets, palette), [buckets, palette]);
  const pageStart = history[history.length - 1] || 0;
  const busy = saving ? "Saving the recorded files. Leave this page open."
    : checking ? "Checking the ticked recordings."
    : opening ? "Opening the saved set and checking its pinned files."
    : loading ? "Reading the warehouse. A long recording is slow the first time, then the same warehouse stays quick."
    : "";
  const elapsed = useElapsed(Boolean(busy));
  const coverageTotals = buckets.reduce(
    (totals, bucket) => ({
      ready: totals.ready + bucket.ready,
      preparation: totals.preparation + bucket.preparation,
      gap: totals.gap + bucket.gap,
    }),
    { ready: 0, preparation: 0, gap: 0 },
  );
  const longestCapture = Math.max(...blocks.map((block) => block.stopped_at_ns - block.started_at_ns), 1);

  return (
    <div className="dl-page">
      <div className="section-tabs" role="tablist">
        {["Warehouse", "Window sets", "Recording sessions"].map((tab) => (
          <button key={tab} className={area === tab && !builder ? "selected" : ""} onClick={() => { setArea(tab); setBuilder(false); }}>
            {tab}
            {tab === "Window sets" && <span className="dl-count">{sets.length}</span>}
          </button>
        ))}
      </div>
      {busy && (
        <div className="ux-busy" role="status">
          <div className="ux-busy-bar" aria-hidden="true"><i /></div>
          <span>{busy} {elapsed > 0 ? `· ${elapsed}s` : ""}</span>
        </div>
      )}
      {notice && (
        <div className="dl-toast" role="status">
          {notice}
          <button className="text-button" onClick={() => setNotice("")}>Dismiss</button>
        </div>
      )}
      {builder ? (
        <Builder
          step={step}
          setStep={setStep}
          recipe={recipe}
          setRecipe={(value) => { resolveGen.current += 1; setChecking(false); setRecipe(value); if (value !== "cookbook") setCook(null); setPreview(null); setCandidates([]); setPreviewStale(false); }}
          cookbookRecipes={cookbook.recipes}
          cookbookFolder={cookbook.folder}
          onCookbookSaved={() => void cookbook.reload()}
          cook={cook}
          setCook={(next, recipeItem) => { resolveGen.current += 1; setChecking(false); setRecipe("cookbook"); setCook(next); if (recipeItem) setRequirements((current) => ({ ...current, coverage: recipeItem.policy as Requirements["coverage"], coverage_floor: undefined, warmup: false, settlement_confidence: "provider_reported" })); setPreview(null); setCandidates([]); setPreviewStale(false); }}
          filters={filters}
          setFilters={applyFilters}
          requirements={requirements}
          setRequirements={(next) => { resolveGen.current += 1; setChecking(false); setRequirements(next); setPreview(null); setCandidates([]); setPreviewStale(false); }}
          checking={checking}
          saving={saving}
          selectedBlocks={[...blocks].filter((block) => selectedBlocks.includes(block.block_id)).sort((a, b) => a.started_at_ns - b.started_at_ns)}
          preview={preview}
          candidates={candidates}
          previewStale={previewStale}
          recordedWindows={coverageTotals.ready + coverageTotals.preparation + coverageTotals.gap}
          name={name}
          setName={setName}
          collections={collections}
          collection={collection}
          setCollection={(value) => { resolveGen.current += 1; setChecking(false); setCollection(value); setPreview(null); setCandidates([]); setPreviewStale(false); }}
          start={start}
          end={end}
          setStart={(value) => { resolveGen.current += 1; setChecking(false); setStart(value); setPreview(null); setCandidates([]); setPreviewStale(false); }}
          setEnd={(value) => { resolveGen.current += 1; setChecking(false); setEnd(value); setPreview(null); setCandidates([]); setPreviewStale(false); }}
          datesValid={datesValid}
          onResolve={resolvePreview}
          onSave={() => void saveSet()}
          onClose={() => setBuilder(false)}
          roots={roots}
          theme={theme}
          onPrepare={() => void reviewPreparation()}
        />
      ) : area === "Warehouse" ? (
        <>
          <section className="panel">
            <div className="panel-head"><div><h3>Recordings</h3><p>{selectedBlocks.length ? `${selectedBlocks.length} selected · ${formatSpan(blocks.filter((block) => selectedBlocks.includes(block.block_id)).reduce((sum, block) => sum + block.stopped_at_ns - block.started_at_ns, 0) / 1e9)}` : "Each card is one capture."} The gap between captures is not part of the set.</p></div></div>
            <div className="ux-recordings">
              {[...blocks].sort((a, b) => a.started_at_ns - b.started_at_ns).map((block) => {
                const on = selectedBlocks.includes(block.block_id);
                const spanNs = block.stopped_at_ns - block.started_at_ns;
                return (
                  <label className={"ux-recording" + (on ? " is-on" : "")} key={block.block_id}>
                    <span className="ux-recording-top">
                      <input
                        type="checkbox"
                        checked={on}
                        onChange={(event) => {
                          const next = event.target.checked
                            ? [...selectedBlocks, block.block_id]
                            : selectedBlocks.filter((id) => id !== block.block_id);
                          setSelectedBlocks(next);
                          setHistory([0]);
                          setPreview(null);
                          void loadWarehouse(0, pageSize, next);
                        }}
                      />
                      {durationLabel(block)}
                    </span>
                    <span className="ux-duration" aria-hidden="true"><i style={{ width: `${Math.max(8, (spanNs / longestCapture) * 100)}%` }} /></span>
                    <small>{stamp(block.started_at_ns)} – {stamp(block.stopped_at_ns)} UTC</small>
                    <RecordingBadge summary={summaryByBlock[block.block_id]} />
                    <small>{block.warehouse.split(/[/\\]/).pop()}</small>
                  </label>
                );
              })}
            </div>
            {!blocks.length && <div className="dl-empty">No finished capture sessions in the configured warehouses.</div>}
          </section>
          <div className="dl-toolbar">
            <div>
              <h2>What is in the warehouse?</h2>
              <p>Dates are an optional filter inside the ticked recordings. Leave them empty to see each recording in full. Reads do not prepare or record.</p>
            </div>
            <div className="inline">
              <button className="button" disabled={reviewingPrep} onClick={() => void reviewPreparation()}>{reviewingPrep ? "Reading the plan…" : "Review preparation"}</button>
              <ReserveControl blockIds={selectedBlocks} onSaved={() => setReservationVersion((value) => value + 1)} />
              <button className="button primary" onClick={() => { setBuilder(true); setStep(0); setPreview(null); }}>{selectedBlocks.length ? `Create window set · ${selectedBlocks.length}` : "Create window set"}</button>
            </div>
          </div>
          <div className="context-filters dl-filters">
            <label className="field"><span>Collection</span>
              <select value={collection} onChange={(event) => setCollection(event.target.value)}>
                <option value="">All ticked recordings</option>
                {collections.map((item) => <option key={item.collection} value={item.collection}>{collectionLabel(item.collection, blocks)}</option>)}
              </select>
            </label>
            <label className="field"><span>From · UTC</span><input type="date" value={start} onChange={(event) => setStart(event.target.value)} /></label>
            <label className="field"><span>Until · UTC, exclusive</span><input type="date" value={end} onChange={(event) => setEnd(event.target.value)} /></label>
            <label className="field"><span>Availability</span>
              <select value={availability} onChange={(event) => setAvailability(event.target.value)}>
                <option value="all">All states</option>
                <option value="ready">Inputs ready</option>
                <option value="preparation">Preparation needed</option>
                <option value="gap">Gaps / unknown</option>
              </select>
            </label>
            <label className="field"><span>Page size</span>
              <select value={pageSize} onChange={(event) => {
                const size = Number(event.target.value);
                setPageSize(size);
                setHistory([0]);
                void loadWarehouse(0, size);
              }}>
                <option value={25}>25</option>
                <option value={50}>50</option>
              </select>
            </label>
            <button className="button" disabled={!datesValid || loading} onClick={() => { setHistory([0]); void loadWarehouse(0); }}>{loading ? "Reading…" : "Load windows"}</button>
          </div>
          {!datesValid && <div className="inline-warning">The end date must be after the start date.</div>}
          {prepPlans && (
            <section className="panel">
              <div className="panel-head"><div><h3>Preparation plan</h3><p>Nothing has started. Each action covers the whole collection behind a ticked recording. It cannot fill time before the recorder connected, and stop is not supported.</p></div></div>
              {matched > 0 && matched === rows.length && rows.every((row) => row.tracks.normalization?.state === "present") && <p className="plot-caption">Normalization is already present for every window in these recordings. A new normalize run is not required.</p>}
              {rows.some((row) => row.tracks.quality?.state === "unassessed") && <p className="plot-caption">Some windows in view still have unassessed quality. A quality run can assess them. It cannot recreate a missing book.</p>}
              <div className="ux-candidates">
                {prepPlans.map((plan) => {
                  const normalized = plan.kind === "normalize" && matched > 0 && matched === rows.length && rows.every((row) => row.tracks.normalization?.state === "present");
                  return (
                    <div className="ux-candidate" key={`${plan.kind}-${plan.collection}`}>
                      <span>{plan.kind} · {plan.warehouse.split(/[/\\]/).pop()}</span>
                      <button className="button" disabled={normalized} onClick={() => void startReviewedPreparation(plan)}>{normalized ? "Already normalized" : `Start ${plan.kind}`}</button>
                    </div>
                  );
                })}
              </div>
            </section>
          )}
          <section className="panel">
            <div className="panel-head"><div><h3>Windows under the current policy</h3><p>replay_research_v2_1 on the ticked recordings: both books valid ≥ 90%, no hole over 20 s, none in the last 60 s. The set builder, the preview and the save use the same rule.</p></div></div>
            <div className={"ux-chart" + (policyLoading ? " is-busy" : "")}>
              {policyProfiles.length ? <PolicyCoverage profiles={policyProfiles} theme={theme} policy="replay_research_v2_1" /> : policyLoading ? <ChartSkeleton /> : <div className="dl-empty">{selectedBlocks.length ? "No windows start inside the ticked recordings." : "Tick one or more recordings above."}</div>}
            </div>
          </section>
          {policyProfiles.length > 0 && (
            <section className="panel">
              <div className="panel-head"><div><h3>Window map</h3><p>Where each window lost its order books. Click a row to inspect that window.</p></div></div>
              <WindowMap profiles={policyProfiles} theme={theme} onPick={(row) => { const found = rows.find((item) => item.market_id === row.market_id); if (found) setInspect(found); }} />
            </section>
          )}
          {rows.length > 0 && <WindowTable rows={rows} blocks={blocks} onInspect={setInspect} />}
          {rows.length > 0 && <p className="plot-caption">U up book · D down book · R reference price · W warm-up. Green is present, amber is partial, red is absent, and grey is unknown. The letters name the input. Color is the coverage.</p>}
          {matched > 0 && <div className="inline">
            <button className="button" disabled={history.length <= 1 || loading} onClick={() => {
              const copy = history.slice(0, -1);
              setHistory(copy);
              void loadWarehouse(copy[copy.length - 1] || 0);
            }}>Previous</button>
            <span>Showing {rows.length ? pageStart + 1 : 0}–{pageStart + rows.length} of {matched}</span>
            <button className="button" disabled={nextCursor == null || loading} onClick={() => {
              if (nextCursor == null) return;
              setHistory([...history, nextCursor]);
              void loadWarehouse(nextCursor);
            }}>Next</button>
          </div>}
        </>
      ) : area === "Window sets" ? (
        <div className="dl-set-list">
          <div className="dl-toolbar"><div><h2>Saved inputs</h2><p>Opening a set reads the pinned membership. Reusing filters does not change it.</p></div></div>
          {sets.map((item) => (
            <section className="panel ux-set" key={item.selection_id}>
              <div className="ux-set-main">
                <div className="ux-hero-number">{item.included}<span>windows</span></div>
                <div>
                  <h3>{item.name || item.selection_id}</h3>
                  <div className="ux-chips">
                    <span className="ux-chip">{setRecipeLabel(item)}</span>
                    {item.research_policy && <span className="ux-chip accent">{item.research_policy}{item.coverage_floor ? ` ${Math.round(item.coverage_floor * 100)}%` : ""}</span>}
                    <span className="ux-chip">{item.block_ids?.length || 0} recordings</span>
                    {item.recipe === "missing_inputs" && <span className="ux-chip warn">Not for replay</span>}
                    {item.compatibility === "saved_before_exploratory_policy" && <span className="ux-chip">Saved before the exploratory rule</span>}
                    {(item.holdout_overlap || 0) > 0 && <span className="ux-chip warn">Contains {item.holdout_overlap} reserved markets</span>}
                    {item.status && setStatusText[item.status] && <span className={"ux-chip " + setStatusText[item.status].tone} title={setStatusText[item.status].detail}>{setStatusText[item.status].text}</span>}
                    {(item.version || 1) > 1 && <span className="ux-chip" title={item.parent_selection_id || ""}>Version {item.version}</span>}
                  </div>
                </div>
              </div>
              <div className="inline">
                <button className="button" disabled={opening === item.selection_id} onClick={() => void openSet(item)}>{opening === item.selection_id ? "Opening…" : "Inspect set"}</button>
                <button className="button primary" disabled={item.recipe === "missing_inputs" || item.legacy_unpinned} onClick={() => onOpenExperiment(item)}>Use in experiment</button>
                <SetDetails selectionId={item.selection_id} theme={theme} onChanged={() => void loadSets()} onOpenRun={onOpenRun} />
                <DeriveControl selectionId={item.selection_id} status={item.status} blocks={blocks} currentBlocks={item.block_ids || []} onSaved={() => void loadSets()} />
                <DeleteControl what="this window set" onDelete={async () => { const envelope = await call("selection.delete", { selection_id: item.selection_id }).catch(() => null); if (!envelope?.ok) return envelope?.error?.reason || "The workspace API is not reachable."; await loadSets(); setTrashVersion((value) => value + 1); return null; }} />
              </div>
              {item.research_limitation && <details><summary>What this result assumes</summary><p>{item.research_limitation}</p></details>}
            </section>
          ))}
          {!sets.length && <div className="dl-empty">No saved window sets in this workspace yet.</div>}
          <Reservations version={reservationVersion} onChanged={() => setReservationVersion((value) => value + 1)} />
          <TrashPanel version={trashVersion} onChanged={() => void loadSets()} />
        </div>
      ) : (
        <section className="panel">
          <RecordingPanel version={recordingVersion} onFinished={() => setRecordingVersion((value) => value + 1)} />
          <div className="panel-head dv-space"><div><h2>Recording health</h2><p>Book seconds lost in every window of each finished recording, and windows kept under the current policy.</p></div></div>
          <RecordingHealth recordings={recordingSummaries} theme={theme} onFetchStarted={() => setRecordingVersion((value) => value + 1)} />
          <div className="panel-head dv-space"><div><h3>Sessions</h3></div></div>
          <table>
            <thead><tr><th>Started</th><th>Stopped</th><th>Warehouse</th><th>Stop</th></tr></thead>
            <tbody>
              {collections.flatMap((item) => (item.sessions || []).map((session) => {
                const started = sessionTime((session as { started_at?: unknown }).started_at);
                const stopped = sessionTime((session as { stopped_at?: unknown }).stopped_at);
                const clean = Boolean((session as { clean_stop?: boolean }).clean_stop);
                return (
                  <tr key={item.collection + session.session_id}>
                    <td>{started}</td>
                    <td>{stopped}</td>
                    <td>{(item as { warehouse?: string }).warehouse?.split(/[/\\]/).pop() || collectionLabel(item.collection, blocks)}</td>
                    <td><span className={"ux-chip " + (clean ? "ok" : "warn")}>{clean ? "Clean stop" : "Check stop"}</span></td>
                  </tr>
                );
              }))}
            </tbody>
          </table>
          {!collections.length && <div className="dl-empty">Load the warehouse to read collection sessions.</div>}
        </section>
      )}
      {inspect && (
        <Drawer title="Window evidence" onClose={() => { setInspect(null); setDetail(null); }}>
          {inspect.market_id && <WindowData row={{ ...(detail || inspect), market_id: inspect.market_id, window_end_ns: (inspect.window_start_ns || 0) + 300 * 1e9 }} holes={policyProfiles.find((item) => item.market_id === inspect.market_id)?.holes} outcome={inspect.outcome} theme={theme} />}
          <WindowInspector row={detail || inspect} onLoad={async () => {
            const envelope = await call("library.window", {
              roots: inspect.warehouse ? [inspect.warehouse] : roots,
              ...(inspect.collection ? { collection: inspect.collection } : {}),
              ...(inspect.block_id ? { block_ids: [inspect.block_id] } : {}),
              market_id: inspect.market_id,
              availability: "all",
            });
            if (envelope.ok) setDetail(envelope.result?.window as WarehouseWindow);
            else setNotice(err(envelope));
          }} />
        </Drawer>
      )}
      {opened && (
        <Drawer title="Saved window set" onClose={() => setOpened(null)}>
          <h2>{opened.name}</h2>
          <p className="mono">{opened.selection_id}</p>
          {opened.limitation && <div className="notice"><strong>{opened.limitation}</strong><p>This limitation travels with the set.</p></div>}
          <p>Filters: {opened.filters ? JSON.stringify(opened.filters) : "Legacy selection has no stored filters."}</p>
          <div className="dl-saved-members">{(opened.included_rows || []).map((row) => <div className="mono" key={row.market_id}>{row.market_id}</div>)}</div>
          <div className="inline">
            <button className="button primary" disabled={opened.recipe === "missing_inputs" || opened.legacy_unpinned} onClick={() => { onOpenExperiment(opened); setOpened(null); }}>Use in experiment</button>
            {opened.filters && <button className="button" onClick={() => reuse(opened)}>Use filters for a new set</button>}
          </div>
        </Drawer>
      )}
      {prep && (
        <div className="dl-toast" role="status">
          Preparation {prep.id} · {prep.state}. Stop is not supported.
          {prep.owner_result ? ` · ${JSON.stringify(prep.owner_result)}` : ""}
        </div>
      )}
    </div>
  );
}

function WindowTable({ rows, blocks, onInspect }: { rows: WarehouseWindow[]; blocks: CaptureBlock[]; onInspect: (row: WarehouseWindow) => void }) {
  return (
    <div className="table-wrap dl-table">
      <table>
        <thead><tr><th>Window start · UTC</th><th>Recording</th><th>Inputs</th><th>Outcome</th><th>Use</th><th /></tr></thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.collection}-${row.market_id}`}>
              <td><button className="dl-window-link" onClick={() => onInspect(row)}>{stamp(row.window_start_ns)}</button><small className="mono">{row.market_id}</small></td>
              <td>{row.tracks.recording?.state}<small>{blocks.find((block) => block.block_id === row.block_id) ? stamp(blocks.find((block) => block.block_id === row.block_id)!.started_at_ns) : "Outside the ticked recordings"}</small></td>
              <td>
                <div className="dl-mini-tracks">
                  {(["up_book", "down_book", "reference"] as const).map((key, index) => (
                    <span key={key} className={"dl-dot " + visual(row.tracks[key])} title={key}>{["U", "D", "R"][index]}</span>
                  ))}
                  <span className={"dl-dot " + visual(row.warmup)} title="Warm-up">W</span>
                </div>
                <small>{readiness(row)}</small>
              </td>
              <td><span className="badge">{outcomeText(row)}</span><small>{labelText(row.label_source)}</small></td>
              <td>{row.partition || "—"}</td>
              <td><button className="text-button" onClick={() => onInspect(row)}>Inspect</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function WindowInspector({ row, onLoad }: { row: WarehouseWindow; onLoad: () => void }) {
  useEffect(() => { void onLoad(); }, [row.market_id]);
  return (
    <>
      <h2>{stamp(row.window_start_ns)} UTC</h2>
      <p className="mono">{row.market_id}</p>
      <p>{readiness(row)} · publication {row.publication_id || "none"} · assessed {row.assessed_at ?? "unassessed"}</p>
      {(["recording", "normalization", "up_book", "down_book", "reference", "quality", "outcome"] as const).map((key) => (
        <div key={key}>
          <span className="ux-chip">{key.replaceAll("_", " ")} · {row.tracks[key]?.state}</span>
          <Meter label={key.replaceAll("_", " ")} usable={row.tracks[key]?.usable_duration_ns} required={row.tracks[key]?.required_duration_ns} />
        </div>
      ))}
      <Meter label="Warm-up" usable={row.warmup?.usable_duration_ns} required={row.warmup?.required_duration_ns} />
      <p>Outcome {outcomeText(row)} · {labelText(row.label_source)}. Book updates alone are not coverage.</p>
    </>
  );
}

function Builder(props: {
  step: number;
  setStep: (step: number) => void;
  recipe: string;
  setRecipe: (recipe: string) => void;
  cookbookRecipes: CookbookRecipe[];
  cookbookFolder: string;
  onCookbookSaved: () => void;
  cook: CookbookSpec | null;
  setCook: (spec: CookbookSpec, recipe?: CookbookRecipe) => void;
  filters: Filters;
  setFilters: (filters: Filters) => void;
  requirements: Requirements;
  setRequirements: (value: Requirements) => void;
  preview: Preview | null;
  candidates: Preview["candidates"];
  previewStale: boolean;
  recordedWindows: number;
  name: string;
  setName: (name: string) => void;
  collections: { collection: string }[];
  collection: string;
  setCollection: (value: string) => void;
  start: string;
  end: string;
  setStart: (value: string) => void;
  setEnd: (value: string) => void;
  datesValid: boolean;
  checking: boolean;
  saving: boolean;
  selectedBlocks: CaptureBlock[];
  onResolve: () => Promise<Preview | null>;
  onSave: () => void;
  onClose: () => void;
  onPrepare: () => void;
  roots: string[];
  theme: string;
}) {
  const { filters, setFilters, preview } = props;
  const card = props.recipe === "cookbook" && props.cook ? props.cookbookRecipes.find((item) => item.id === props.cook?.id) || null : null;
  const toggleDay = (day: number) => {
    const weekdays = filters.weekdays.includes(day) ? filters.weekdays.filter((item) => item !== day) : [...filters.weekdays, day].sort();
    setFilters({ ...filters, weekdays });
  };
  return (
    <>
      <div className="dl-builder-heading"><div><div className="eyebrow">CREATE A WINDOW SET</div><h2>{card ? card.title : recipeName[props.recipe]}</h2></div><button className="button" onClick={props.onClose}>Back</button></div>
      <ol className="dl-stepper">{["Goal", "Scope", "Review inputs", "Save"].map((label, index) => <li key={label}><button className={(props.step === index ? "active " : "") + (index < props.step ? "done" : "")} disabled={props.checking || props.saving} onClick={() => props.setStep(index)}><span>{index < props.step ? "✓" : index + 1}</span>{label}</button></li>)}</ol>
      {props.step === 0 && (
        <section className="panel">
          <h3>Cookbook</h3>
          <p className="plot-caption">Ready-made scenarios: pick a card, adjust its parameters on the next step if needed.</p>
          <CookbookCards recipes={props.cookbookRecipes} folder={props.cookbookFolder} selected={props.recipe === "cookbook" ? props.cook?.id || null : null} onPick={(item) => props.setCook({ id: item.id, version: item.version, parameters: {} }, item)} />
          <h3>Build your own</h3>
          <div className="dl-recipes">
            {BUILD_YOUR_OWN.map((id) => (
              <button key={id} className={"dl-recipe " + (props.recipe === id ? "active" : "")} onClick={() => props.setRecipe(id)}><strong>{recipeName[id]}</strong><p>{recipeDetail[id]}</p></button>
            ))}
          </div>
          <div className="dl-unavailable"><strong>Market conditions</strong><p>Custom window set can filter by reference volatility, the move from K to close, and the bid-ask spread. Reversal and depth are not defined (recorded quotes carry no sizes). Windows with missing inputs are shown per recording under Recording sessions, with a fetch for missing winners.</p></div>
        </section>
      )}
      {props.step === 1 && (
        <section className="panel">
          <p className="plot-caption">The set is the recordings you ticked. Leave the dates empty to use each recording in full. A date only narrows inside those recordings, and the time between recordings is not included.</p>
          {props.selectedBlocks.length ? (
            <div className="ux-chips">{props.selectedBlocks.map((block) => <span className="ux-chip" key={block.block_id}>{stamp(block.started_at_ns)} · {durationLabel(block)}</span>)}</div>
          ) : <div className="inline-warning">Tick at least one recording on the warehouse page before continuing.</div>}
          <div className="dl-scope-fields">
            <label className="field"><span>Optional from · UTC</span><input type="date" value={props.start} onChange={(event) => props.setStart(event.target.value)} /></label>
            <label className="field"><span>Optional until · UTC</span><input type="date" value={props.end} onChange={(event) => props.setEnd(event.target.value)} /></label>
          </div>
          {props.recipe === "custom_window_set" ? (
            <>
              <div className="dl-scope-fields">
                <label className="field"><span>Outcome</span>
                  <select value={filters.outcome} onChange={(event) => setFilters({ ...filters, outcome: event.target.value })}>
                    {["any", "up", "down", "unresolved", "unknown"].map((item) => <option key={item}>{item}</option>)}
                  </select>
                </label>
                <label className="field"><span>Label source</span>
                  <select value={filters.label_source} onChange={(event) => setFilters({ ...filters, label_source: event.target.value })}>
                    {["any", "provider_confirmed", "proxy", "unknown"].map((item) => <option key={item}>{item}</option>)}
                  </select>
                </label>
                <label className="field"><span>Time zone</span>
                  <select value={filters.time_zone} onChange={(event) => setFilters({ ...filters, time_zone: event.target.value })}>
                    <option>UTC</option><option>Europe/Paris</option><option>America/New_York</option>
                  </select>
                </label>
              </div>
              <label className="check"><input type="checkbox" checked={filters.daily_time.enabled} onChange={(event) => setFilters({ ...filters, daily_time: { ...filters.daily_time, enabled: event.target.checked } })} />Daily hours</label>
              {filters.daily_time.enabled && (
                <div className="dl-scope-fields">
                  <label className="field"><span>From</span><input value={filters.daily_time.start} onChange={(event) => setFilters({ ...filters, daily_time: { ...filters.daily_time, start: event.target.value } })} /></label>
                  <label className="field"><span>Until</span><input value={filters.daily_time.end} onChange={(event) => setFilters({ ...filters, daily_time: { ...filters.daily_time, end: event.target.value } })} /></label>
                </div>
              )}
              <div className="inline">{dayName.map((label, index) => <label key={label} className="check"><input type="checkbox" checked={filters.weekdays.includes(index + 1)} onChange={() => toggleDay(index + 1)} />{label}</label>)}</div>
              <ConditionFilters roots={props.roots} value={filters.conditions || {}} onChange={(conditions) => setFilters({ ...filters, conditions })} />
              <p className="plot-caption">Overnight ranges use the weekday of the zoned window start. {preview?.clock?.weekday_basis || "zoned_window_start"}.</p>
            </>
          ) : card && props.cook ? (
            <RecipeParameters recipe={card} spec={props.cook} onChange={(spec) => props.setCook(spec)} />
          ) : (
            <p className="plot-caption">This recipe ignores outcome, label source, daily hours, and weekdays. Choose Custom window set to use those filters.</p>
          )}
          <div className="dl-selection-mode">
            <button className={filters.selection_mode === "all_matches" ? "active" : ""} onClick={() => setFilters({ ...filters, selection_mode: "all_matches" })}>Use all matching windows</button>
            <button className={filters.selection_mode === "picked" ? "active" : ""} onClick={() => setFilters({ ...filters, selection_mode: "picked" })}>Pick individual windows</button>
          </div>
          {filters.selection_mode === "picked" && <button className="button" type="button" disabled={props.checking} onClick={() => void props.onResolve()}>{props.checking ? "Checking…" : "List the windows to pick from"}</button>}
          {preview && (
            <ShareBar parts={Object.entries(preview.outcome_mix).map(([label, value]) => ({ label, value, color: OUTCOME_COLOR[label] || "var(--accent)" }))} />
          )}
          {preview && <p className="plot-caption">{preview.matched} matches · {preview.included.length} kept under the current rule. Withheld markets stay out of the mix.</p>}
          {props.previewStale && <p className="plot-caption">The picks changed. Preview matching windows again before the kept count is final.</p>}
          {props.candidates.length > 0 && filters.selection_mode === "all_matches" && (
            <div className="ux-candidates">
              {props.candidates.slice(0, 8).map((row) => (
                <div className="ux-candidate" key={row.market_id} title={row.market_id}>
                  <span>{stamp(row.window_start_ns)} UTC</span>
                  <span className="ux-chip" style={{ color: OUTCOME_COLOR[row.outcome] || "var(--muted)" }}>{row.outcome}</span>
                </div>
              ))}
              {props.candidates.length > 8 && <p className="plot-caption">and {props.candidates.length - 8} more. The set uses every match, including windows past this sample.</p>}
            </div>
          )}
          {filters.selection_mode === "picked" && props.candidates.length > 0 && (
            <div className="ux-candidates">
              {props.candidates.map((row) => (
                <label key={row.market_id} className="ux-candidate">
                  <span><input type="checkbox" checked={filters.picked.includes(row.market_id)} onChange={(event) => setFilters({ ...filters, picked: event.target.checked ? [...filters.picked, row.market_id] : filters.picked.filter((id) => id !== row.market_id) })} /> {stamp(row.window_start_ns)} UTC</span>
                  <span className="ux-chip" style={{ color: OUTCOME_COLOR[row.outcome] || "var(--muted)" }}>{row.outcome}</span>
                </label>
              ))}
            </div>
          )}
        </section>
      )}
      {props.step === 2 && (
        <section className="panel">
          {!props.requirements.coverage.startsWith("research") && (["books", "reference", "warmup", "scoring_outcome", "provider_confirmed"] as const).map((key) => (
            <label key={key} className="check"><input type="checkbox" checked={props.requirements[key]} onChange={(event) => props.setRequirements({ ...props.requirements, [key]: event.target.checked })} />{key.replaceAll("_", " ")}</label>
          ))}
            <div className="dl-selection-mode">
              <button className={props.requirements.coverage === "research_v2_1" ? "active" : ""} onClick={() => props.setRequirements({ ...props.requirements, coverage: "research_v2_1", coverage_floor: undefined, warmup: false, settlement_confidence: "provider_reported" })}>Research v2.1 · recommended</button>
              <button className={props.requirements.coverage === "complete" ? "active" : ""} onClick={() => props.setRequirements({ ...props.requirements, coverage: "complete", settlement_confidence: "strict" })}>Complete coverage</button>
              <button className={props.requirements.coverage === "exploratory" && props.requirements.coverage_floor === 0.95 ? "active" : ""} onClick={() => props.setRequirements({ ...props.requirements, coverage: "exploratory", coverage_floor: 0.95, settlement_confidence: "provider_reported" })}>Exploratory 95%</button>
              <button className={props.requirements.coverage === "exploratory" && props.requirements.coverage_floor === 0.99 ? "active" : ""} onClick={() => props.setRequirements({ ...props.requirements, coverage: "exploratory", coverage_floor: 0.99, settlement_confidence: "provider_reported" })}>Exploratory 99%</button>
            </div>
            <p className="plot-caption">{props.requirements.coverage === "research_v2_1" || props.requirements.coverage === "research_v2" ? "Research v2.1 keeps a window when both books are valid for at least 90% of its 5 minutes, no hole lasts longer than 20 s, and no hole touches the last 60 s. Book blips under 250 ms are ignored. The reference must cover the window itself; the strategy decides when its model is warm. K comes from the exact start tick (not attested) and the winner from the provider. Each kept window stores its holes." : props.requirements.coverage === "exploratory" ? "Exploratory replay admits a window when each required book covers the chosen floor. It uses provisional K and the provider-reported winner, and it does not qualify the strategy for live trading. Execution still stops while a book is invalid." : "Complete coverage includes a window only when every required input is present and the quality verdict is ready."}</p>
            {preview?.research_limitation && <p className="dl-diagnostic-note">{preview.research_limitation}</p>}
          {!preview && <p className="plot-caption">{props.checking ? "Checking the ticked recordings…" : "Continue checks the ticked recordings under the rule highlighted above. A count from an earlier rule is discarded when you change the rule."}</p>}
          <button className="button" disabled={props.checking} onClick={() => void props.onResolve()}>{props.checking ? "Checking…" : "Preview matching windows"}</button>
          <button className="button" onClick={props.onPrepare}>Review preparation</button>
          {preview && (
            <>
              <div className="ux-metrics">
                <div className="ux-metric"><b>{props.recordedWindows}</b><span>Recorded in view</span></div>
                <div className="ux-metric"><b>{preview.matched}</b><span>Matched</span></div>
                <div className="ux-metric ready"><b>{preview.included.length}</b><span>Kept</span></div>
                <div className="ux-metric gap"><b>{preview.excluded.length}</b><span>Excluded</span></div>
              </div>
              <p className="plot-caption">Recorded counts windows in the strict coverage chart. Matched applies the outcome and time filters. Kept applies the coverage rule on this step.</p>
            </>
          )}
          {preview && (
            <ShareBar parts={[
              { label: "Kept", value: preview.included.length, color: "var(--positive)" },
              ...(preview.exclusion_groups || []).map((group) => ({
                label: group.title,
                value: group.count,
                color: REASON_COLOR[group.id] || "var(--negative)",
              })),
            ]} />
          )}
          {preview?.reserved_overlap?.map((item) => <div className="inline-warning" key={item.reservation_id}>{item.windows} kept windows are reserved for the Tune holdout “{item.name}”. Saving keeps them; a Replay on this set will use them.</div>)}
          {preview?.window_profiles && preview.window_profiles.length > 0 && <WindowMap profiles={preview.window_profiles} theme={props.theme} />}
          {(preview?.exclusion_groups || []).map((group) => (
            <div className="ux-group" key={group.id}>
              <h4>{group.title}</h4>
              <p>{group.count} windows. {group.preparation_can_help ? "Preparation can assess these windows. It cannot recreate data that was never recorded." : "Preparation cannot fill this gap."}</p>
              {group.examples.map((example) => (
                <div key={(example.market_id || "") + example.reason}>
                  <span className="ux-chip">{example.window_start_ns ? stamp(example.window_start_ns) : "—"} · {example.reason.replaceAll("_", " ")}</span>
                  <Meter label="Up book" usable={example.up_book_usable_ns} required={example.up_book_required_ns} floor={props.requirements.coverage === "research_v2" || props.requirements.coverage === "research_v2_1" ? 0.9 : props.requirements.coverage === "exploratory" ? props.requirements.coverage_floor || 0.95 : 1} />
                  <Meter label="Down book" usable={example.down_book_usable_ns} required={example.down_book_required_ns} floor={props.requirements.coverage === "research_v2" || props.requirements.coverage === "research_v2_1" ? 0.9 : props.requirements.coverage === "exploratory" ? props.requirements.coverage_floor || 0.95 : 1} />
                  <Meter label="Reference · 15 min before the window plus the 5-minute window" usable={example.reference_usable_ns} required={example.reference_required_ns} floor={props.requirements.coverage === "research_v2" || props.requirements.coverage === "research_v2_1" ? 0.9 : props.requirements.coverage === "exploratory" ? props.requirements.coverage_floor || 0.95 : 1} />
                </div>
              ))}
            </div>
          ))}
          {preview?.requires_choice?.length ? <div className="inline-warning">Choose a source before saving: {preview.requires_choice.join(", ")}</div> : null}
        </section>
      )}
      {props.step === 3 && (
        <section className="panel ux-save">
          <label className="field"><span>Name</span><input value={props.name} onChange={(event) => props.setName(event.target.value)} /></label>
          {preview ? (
            <>
              <div className="ux-hero-number">{preview.included.length}<span>windows kept · {preview.excluded.length} excluded</span></div>
              <ShareBar parts={[
                { label: "Kept", value: preview.included.length, color: "var(--positive)" },
                { label: "Excluded", value: preview.excluded.length, color: "var(--negative)" },
              ]} />
              <div className="ux-chips">
                <span className="ux-chip accent">{props.requirements.coverage === "research_v2_1" ? "replay_research_v2_1" : props.requirements.coverage === "research_v2" ? "replay_research_v2" : props.requirements.coverage}{props.requirements.coverage === "exploratory" ? ` ${Math.round((props.requirements.coverage_floor || 0) * 100)}%` : ""}</span>
                <span className="ux-chip">{preview.population}</span>
                {props.selectedBlocks.map((block) => <span className="ux-chip" key={block.block_id}>{stamp(block.started_at_ns)} · {durationLabel(block)}</span>)}
              </div>
            </>
          ) : <p className="plot-caption">Refresh the preview after any change.</p>}
          {preview?.cookbook && <p className="plot-caption">Recipe {preview.cookbook.title} · version {preview.cookbook.version}{Object.keys(preview.cookbook.parameters || {}).length ? ` · changed: ${Object.entries(preview.cookbook.parameters || {}).map(([key, value]) => `${key} = ${Array.isArray(value) ? value.join(",") : String(value)}`).join(", ")}` : " · recipe defaults"}.</p>}
          <ResolvedThresholds record={preview?.cookbook} />
          {props.recipe === "custom_window_set" && <SaveAsRecipe filters={filters} policy={props.requirements.coverage} onSaved={props.onCookbookSaved} />}
          <p className="plot-caption">Save records the warehouse checksums for these windows. Opening or binding the set checks the file bytes. Click it once.</p>
          {props.previewStale && <p className="plot-caption">Preview matching windows again before saving. The picks are newer than the last checked count.</p>}
          <button className="button primary" type="button" disabled={!preview || props.previewStale || !props.datesValid || !props.name.trim() || props.saving} onClick={props.onSave}>{props.saving ? "Saving the window set…" : "Save exact windows"}</button>
        </section>
      )}
      <div className="dl-wizard-actions">
        <button className="button" onClick={() => props.setStep(Math.max(0, props.step - 1))}>Back</button>
        {props.step < 3 && <button className="button primary" disabled={props.checking} onClick={() => void (async () => {
          if (props.step === 1 || props.step === 2) {
            const body = await props.onResolve();
            if (!body) return;
          }
          props.setStep(props.step + 1);
        })()}>{props.checking ? "Checking the recordings…" : "Continue"}</button>}
      </div>
    </>
  );
}

function Drawer({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => { closeRef.current?.focus(); }, []);
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <section className="drawer dl-drawer" role="dialog" aria-modal="true" aria-label={title} onClick={(event) => event.stopPropagation()}>
        <header><span className="eyebrow">{title}</span><button ref={closeRef} className="close-button" onClick={onClose}>×</button></header>
        {children}
      </section>
    </div>
  );
}

function rangeOption(buckets: CoverageBucket[], palette: Palette) {
  return {
    animation: false,
    backgroundColor: "transparent",
    grid: { left: 48, right: 16, top: 36, bottom: 48 },
    legend: { top: 0, textStyle: { color: palette.muted, fontSize: 11 } },
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", name: "Window start (UTC)", nameLocation: "middle", nameGap: 28, nameTextStyle: { color: palette.muted, fontSize: 11 }, data: buckets.map((bucket) => stamp(bucket.start_ns).slice(0, 16)), axisLabel: { color: palette.muted, fontSize: 11, hideOverlap: true } },
    yAxis: { type: "value", name: "Windows", nameTextStyle: { color: palette.muted, fontSize: 11 }, minInterval: 1, axisLabel: { color: palette.muted }, splitLine: { lineStyle: { color: palette.grid } } },
    series: [
      { name: "Inputs ready", type: "bar", stack: "coverage", barMaxWidth: 42, data: buckets.map((bucket) => bucket.ready), itemStyle: { color: palette.teal } },
      { name: "Preparation needed", type: "bar", stack: "coverage", barMaxWidth: 42, data: buckets.map((bucket) => bucket.preparation), itemStyle: { color: palette.blue } },
      { name: "Coverage gap", type: "bar", stack: "coverage", barMaxWidth: 42, data: buckets.map((bucket) => bucket.gap), itemStyle: { color: palette.red } },
    ],
  };
}
