// Ported from the approved mockup (prototypes/ihm-review/src/ReplayBuilder.tsx).
// Fields, defaults, bases and validation come from the workflow backend.
import { useEffect, useMemo, useState } from "react";
import { Button, Field, Notice, Panel, Tag } from "../../../prototypes/ihm-review/src/WorkflowReviewUI";
import { call } from "../api";
import { dateRange, flatten, nest, policyLabel, replayablePolicy, same, show, type Base, type Capabilities, type FieldMeta, type Flat, type ReplayConfig, type SetSummary } from "./model";

type Props = {
  set: SetSummary;
  caps: Capabilities;
  draftKey: string;
  onPreview: (configs: ReplayConfig[], name: string) => void;
  onChooseData: () => void;
  onSaved: () => void;
  busy: boolean;
};
type Draft = { name: string; experimentName: string; base: string; params: Flat; scenario: string; execution: Flat; configs: ReplayConfig[]; editor: boolean; editing: string | null };

function readDraft(key: string): Partial<Draft> {
  try { return JSON.parse(sessionStorage.getItem(key) || "{}") as Partial<Draft>; } catch { return {}; }
}

function ParamInput({ meta, value, baseline, locked, lockedText, onChange }: { meta: { key: string; label: string; unit?: string; type: string; integer?: boolean; choices?: string[] | null }; value: unknown; baseline: unknown; locked: boolean; lockedText: string; onChange: (v: unknown) => void }) {
  const changed = !same(value, baseline);
  const listed = Array.isArray(value) || meta.type === "list";
  const numeric = meta.type === "number";
  return <div className={"w-param " + (locked ? "policy" : "")}>
    <Field label={meta.label}>{meta.type === "boolean" ? <select disabled={locked} value={String(value)} onChange={(e) => onChange(e.target.value === "true")}><option value="false">Off</option><option value="true">On</option></select>
      : meta.choices?.length && !listed ? <select disabled={locked} value={String(value)} onChange={(e) => onChange(e.target.value)}>{meta.choices.map((v) => <option key={v} value={v}>{v.replaceAll("_", " ")}</option>)}</select>
      : <input disabled={locked} type={numeric ? "number" : "text"} step={meta.integer ? 1 : "any"} value={Array.isArray(value) ? value.join(", ") : String(value ?? "")} onChange={(e) => onChange(listed ? e.target.value.split(",").map((s) => s.trim()).filter(Boolean) : e.target.value)} />}</Field>
    <div className="w-param-meta"><code>{meta.key}</code><span>{locked ? lockedText : meta.unit || ""}{changed && !locked && <b>Changed</b>}</span></div>
  </div>;
}

export function ReplayBuilder({ set, caps, draftKey, onPreview, onChooseData, onSaved, busy }: Props) {
  const exploratory = replayablePolicy(set.research_policy);
  const policy = caps.policy[String(set.research_policy)] || Object.values(caps.policy)[0];
  const policyKeys = useMemo(() => new Set(Object.keys(policy.fields)), [policy]);
  const defaultScenario = caps.default_scenario || caps.scenarios[0].name;
  const scenarioOf = (name: string) => caps.scenarios.find((item) => item.name === name) || caps.scenarios[0];
  // Policy-owned fields are resolved into the configuration before validation and shown locked.
  const baseValues = (base: Base | undefined): Flat => ({ ...flatten(base?.parameters || {}), ...(exploratory ? policy.fields : {}) });
  const findBase = (id: string) => caps.bases.find((item) => item.id === id) || caps.bases[0];

  const cache = useMemo(() => readDraft(draftKey), [draftKey]);
  const [name, setName] = useState(cache.name ?? "My first replay");
  const [experimentName, setExperimentName] = useState(cache.experimentName ?? `${set.name || "Saved set"} · strategy check`);
  const [base, setBase] = useState(cache.base ?? "defaults");
  const [params, setParams] = useState<Flat>(() => cache.params ?? baseValues(findBase("defaults")));
  const [scenario, setScenario] = useState(cache.scenario ?? defaultScenario);
  const [execution, setExecution] = useState<Flat>(() => cache.execution ?? { ...scenarioOf(defaultScenario).values });
  const [configs, setConfigs] = useState<ReplayConfig[]>(cache.configs ?? []);
  const [editor, setEditor] = useState(cache.editor ?? true);
  const [editing, setEditing] = useState<string | null>(cache.editing ?? null);
  const [query, setQuery] = useState("");
  const [all, setAll] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [notice, setNotice] = useState("");
  const [checking, setChecking] = useState(false);
  useEffect(() => { try { sessionStorage.setItem(draftKey, JSON.stringify({ name, experimentName, base, params, scenario, execution, configs, editor, editing })); } catch { /* storage unavailable */ } }, [draftKey, name, experimentName, base, params, scenario, execution, configs, editor, editing]);

  const baseline = useMemo(() => baseValues(findBase(base)), [base, caps]);
  const scenarioMeta = scenarioOf(scenario);
  const executionBaseline = scenarioMeta.values;
  const changed = caps.fields.filter((f) => !policyKeys.has(f.key) && !same(params[f.key], baseline[f.key]));
  const executionChanged = Object.keys(execution).filter((k) => !same(String(execution[k]), String(executionBaseline[k])));
  const matches = (key: string, label = "") => !query.trim() || `${key} ${label}`.toLowerCase().includes(query.toLowerCase());
  const fieldOf = (key: string) => caps.fields.find((f) => f.key === key);
  const countMatching = caps.fields.filter((f) => matches(f.key, f.label)).length;
  const groupsInBatch = new Set(configs.map((c) => JSON.stringify([c.strategy, c.execution]))).size;
  const strategy = caps.strategies.find((item) => item.available)?.kind || "settlement_value";

  const loadBase = (id: string) => {
    const found = findBase(id);
    setBase(found.id); setParams(baseValues(found)); setErrors([]); setNotice("");
    if (found.scenario && found.execution && Object.keys(found.execution).length) { setScenario(found.scenario); setExecution({ ...found.execution }); }
  };
  const request = () => ({ strategy_kind: strategy, parameters: nest(params), scenario, execution, research_policy: set.research_policy, name: name.trim() });
  const validate = async () => {
    const local: string[] = [];
    if (!name.trim()) local.push("Give this configuration a name.");
    if (configs.some((c) => c.id !== editing && c.name.toLowerCase() === name.trim().toLowerCase())) local.push("Use a different name for each replay configuration.");
    if (local.length) { setErrors(local); return; }
    setChecking(true);
    try {
      const envelope = await call("replay.validate", request());
      if (!envelope.ok || !envelope.result) { setErrors([envelope.error?.reason || "The server could not validate this configuration."]); return; }
      const body = envelope.result;
      if (!body.valid) { setErrors((body.errors as string[]) || ["The configuration was refused."]); return; }
      const cfg: ReplayConfig = { id: editing ?? `config-${Date.now()}`, name: name.trim(), strategy, base, baseLabel: findBase(base).label, parameters: body.parameters as ReplayConfig["parameters"], scenario: String(body.scenario || scenario), execution: (body.execution as Flat) || execution, fingerprint: String(body.fingerprint || ""), validatedAt: new Date().toISOString() };
      setConfigs((old) => (editing ? old.map((c) => (c.id === editing ? cfg : c)) : [...old, cfg]));
      setEditor(false); setEditing(null); setErrors([]);
      setNotice("Configuration validated on the server and added. It is kept in this draft; save it to reuse it elsewhere.");
    } catch {
      setErrors(["The workspace API is not reachable."]);
    } finally { setChecking(false); }
  };
  const saveConfig = async (cfg: ReplayConfig) => {
    const envelope = await call("configuration.save", { name: cfg.name, strategy_kind: cfg.strategy, parameters: cfg.parameters, scenario: cfg.scenario, execution: cfg.execution, research_policy: set.research_policy }).catch(() => null);
    if (!envelope?.ok) { setNotice(`Not saved: ${envelope?.error?.reason || "the workspace API is not reachable"}.`); return; }
    setNotice(`Saved “${cfg.name}” for reuse. It appears under Saved configurations in Start from.`);
    onSaved();
  };
  const edit = (cfg: ReplayConfig) => { setName(cfg.name); setParams(flatten(cfg.parameters)); setBase(cfg.base); setScenario(cfg.scenario); setExecution({ ...cfg.execution }); setEditing(cfg.id); setEditor(true); setErrors([]); setNotice(""); };
  const add = () => { setName(`Replay configuration ${configs.length + 1}`); loadBase("defaults"); setScenario(defaultScenario); setExecution({ ...scenarioOf(defaultScenario).values }); setEditing(null); setEditor(true); setErrors([]); setQuery(""); setAll(false); setNotice(""); };
  const renderParam = (meta: FieldMeta) => <ParamInput key={meta.key} meta={meta} value={params[meta.key]} baseline={baseline[meta.key]} locked={policyKeys.has(meta.key)} lockedText="From set policy" onChange={(v) => { setParams((old) => ({ ...old, [meta.key]: v })); setErrors([]); }} />;
  const groupsByOrigin = (group: Base["group"]) => caps.bases.filter((item) => item.group === group);

  return <div className="r-setup-layout w-builder"><div className="r-stack">
    <Panel title="1. Use a saved window set" sub="Choose data here, or start from Data Library → Use in experiment." action={<Button onClick={onChooseData}>Choose / change window set</Button>}>
      <div className="r-dataset-card"><div className="r-dataset-icon">▦</div><div><strong>{set.name || "Unnamed window set"}</strong><p>{dateRange(set.start_ns, set.end_ns)} · {set.block_ids?.length || 0} recording{set.block_ids?.length === 1 ? "" : "s"}</p><code>{set.selection_id}</code></div><strong className="w-set-count">{set.included}<small> windows selected</small></strong></div>
      <div className="w-policy-line"><Tag tone={exploratory ? "" : "warn"}>{policyLabel(set.research_policy)}</Tag><span>{exploratory ? policy.description : "This set has no named research policy. Replay does not invent one."}</span></div>
      <p className="r-note">No development / holdout partition belongs to this set. The policy is visible in each resolved configuration before validation.</p>
      {!exploratory && <Notice tone="warning" title="This set cannot be replayed yet">Save the same windows again in Data under Research v2.1. That creates a different set; this one is not modified.</Notice>}
    </Panel>
    {configs.length > 0 && <Panel title="Ready in this replay batch" sub="Each row is one explicit, named configuration—not an automatic A/B variant." action={<Tag>{configs.length} validated</Tag>}>
      <div className="w-config-list">{configs.map((cfg, i) => <div key={cfg.id} className={editing === cfg.id ? "editing" : ""}><span className="w-config-number">{i + 1}</span><div><strong>{cfg.name}</strong><p>{caps.strategies.find((s) => s.kind === cfg.strategy)?.label || cfg.strategy} · {scenarioOf(cfg.scenario).label}</p><code>{cfg.fingerprint.slice(0, 16)} · {Object.keys(flatten(cfg.parameters)).length} resolved strategy fields · from {cfg.baseLabel}</code></div><div className="inline"><button className="text-button" onClick={() => edit(cfg)}>Edit</button><button className="text-button" onClick={() => void saveConfig(cfg)}>Save configuration</button><button className="text-button" onClick={() => { setConfigs((old) => old.filter((c) => c.id !== cfg.id)); if (editing === cfg.id) { setEditing(null); setEditor(false); } }}>Remove</button></div></div>)}</div>
      {!editor && <div className="w-after-validation"><div><strong>Your configuration is ready.</strong><span>Run it on its own, or add another configuration to this same replay.</span></div><div className="inline"><Button primary disabled={!experimentName.trim() || busy} onClick={() => onPreview(configs, experimentName.trim())}>Preview replay →</Button><Button onClick={add}>＋ Add another configuration</Button></div></div>}
    </Panel>}
    {notice && <div className="w-local-notice" role="status">{notice}</div>}
    {editor && <Panel title={editing ? "Edit this configuration" : configs.length ? "Configure another configuration in this replay" : "2. Configure your replay"} sub="Start from one base. Edit what matters. Give it a name when you validate." action={<Tag>{editing ? "Unvalidated edit" : "Draft"}</Tag>}>
      <div className="r-two-fields"><Field label="Strategy"><select value={strategy} onChange={() => undefined}>{caps.strategies.map((s) => <option key={s.kind} value={s.kind} disabled={!s.available}>{s.label}{s.available ? "" : ` · ${s.reason}`}</option>)}</select></Field>
        <Field label="Start from"><select value={base} onChange={(e) => loadBase(e.target.value)}><optgroup label="Source configurations">{groupsByOrigin("source").map((b) => <option key={b.id} value={b.id}>{b.id === "defaults" ? b.label : `${b.label} · YAML`}</option>)}</optgroup>{groupsByOrigin("saved").length > 0 && <optgroup label="Saved configurations">{groupsByOrigin("saved").map((b) => <option key={b.id} value={b.id}>{b.label}</option>)}</optgroup>}{groupsByOrigin("candidate").length > 0 && <optgroup label="Frozen candidates">{groupsByOrigin("candidate").map((b) => <option key={b.id} value={b.id}>{b.label}</option>)}</optgroup>}</select></Field></div>
      <div className="w-base-source"><span>Base source</span><code>{findBase(base).source}{findBase(base).group === "source" && base !== "defaults" ? " · strategy parameters only, not venue or host settings" : ""}</code></div>
      <div className="w-editor-toolbar"><Field label="Find any strategy or execution parameter"><input placeholder="Search edge, retry, volatility, delay…" value={query} onChange={(e) => setQuery(e.target.value)} /></Field><button className="text-button" onClick={() => setAll((v) => !v)}>{all ? "Show essentials first" : "Show all parameters"}</button></div>
      {!all && !query && <><div className="w-subheading"><strong>Essentials</strong><span>Six controls to start. Every other field is available below.</span></div><div className="w-param-grid">{caps.essentials.map((key) => fieldOf(key)).filter((f): f is FieldMeta => Boolean(f)).map(renderParam)}</div></>}
      <div className="w-section-groups">{caps.groups.map((group) => {
        const fields = caps.fields.filter((f) => group.sections.includes(f.section) && matches(f.key, f.label) && (all || query || !caps.essentials.includes(f.key)));
        if (!fields.length) return null;
        return <details key={group.id + String(all) + query} open={all || Boolean(query)}><summary><div><strong>{group.name}</strong><span>{group.description}</span></div><Tag>{fields.length} fields</Tag><i>⌄</i></summary><div className="w-group-body">{group.id === "quality" && <Notice title="Policy fields are resolved here, not merged later">The marked controls come from {policyLabel(set.research_policy)}. To change that rule, choose a different policy in Data and save a new set. Other settlement fields remain editable.</Notice>}{group.id === "exit" && <p className="r-note">Retry and exit settings remain visible even when disabled. Strategy exits and re-entry require managed_exit.</p>}<div className="w-param-grid">{fields.map(renderParam)}</div></div></details>;
      })}</div>
      {query && !countMatching && <p className="r-note">No strategy field matches. Execution fields may match below.</p>}
      <div className="w-subheading r-space"><strong>Execution assumptions</strong><Tag>Shared replay engine</Tag></div>
      <Field label="Execution scenario"><select value={scenario} onChange={(e) => { setScenario(e.target.value); setExecution({ ...scenarioOf(e.target.value).values }); }}>{caps.scenarios.map((s) => <option key={s.name} value={s.name}>{s.label}</option>)}</select></Field>
      <div className="w-assumption-summary"><span>Feed delay <b>{show(execution.feed_delay_ms)} ms</b></span><span>Order delay <b>{show(execution.order_delay_ms)} ms</b></span><span>Depth share <b>{show(execution.participation)}</b></span><span>Price penalty <b>{show(execution.adverse_price_ticks)} tick(s)</b></span></div>
      <details className="w-execution" open={all || Boolean(query)}><summary>Inspect and edit all execution assumptions <Tag>{Object.keys(execution).length} fields{executionChanged.length ? " · modified" : ""}</Tag></summary><p className="r-note">Source: {scenarioMeta.source}. These are assumptions, not calibrated venue latency or historical fees. Metadata identifies the source template.</p><div className="w-param-grid">{scenarioMeta.fields.filter((f) => matches(f.key, f.label)).map((f) => <ParamInput key={f.key} meta={{ ...f, type: f.type === "number" || f.integer || Number.isFinite(Number(executionBaseline[f.key])) && typeof executionBaseline[f.key] !== "boolean" && String(executionBaseline[f.key]).trim() !== "" ? "number" : "text" }} value={execution[f.key]} baseline={executionBaseline[f.key]} locked={scenarioMeta.locked.includes(f.key)} lockedText="Scenario metadata" onChange={(v) => { setExecution((old) => ({ ...old, [f.key]: v })); setErrors([]); }} />)}</div></details>
      <div className="w-validation"><Field label="Configuration name"><input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Hold · smaller entry edge" /></Field>
        <div className="w-validation-summary"><span>{changed.length} strategy changes from base</span><span>{executionChanged.length} execution changes</span><span>{policyKeys.size} explicit fields from set policy</span></div>
        {errors.length > 0 && <div className="w-form-errors" role="alert"><strong>Check these before adding the configuration</strong><ul>{errors.map((error, i) => <li key={i}>{error}</li>)}</ul></div>}
        <div className="inline"><Button primary disabled={!exploratory || checking} onClick={() => void validate()}>{checking ? "Validating on the server…" : editing ? "Validate and update configuration" : "Validate and add configuration"}</Button>{configs.length > 0 && <Button onClick={() => { setEditor(false); setEditing(null); setErrors([]); }}>Discard draft edits</Button>}</div>
        <p className="r-note">The server validates the complete strategy schema and execution scenario. Nothing is written to your project configuration files.</p></div>
    </Panel>}
    {!editor && configs.length === 0 && <Panel title="Add a replay configuration"><Button primary onClick={add}>Configure a replay</Button></Panel>}
  </div><aside className="r-sticky-review"><Panel title="Replay batch" sub="Run explicit configurations on the entire set."><Field label="Experiment / batch name"><input value={experimentName} onChange={(e) => setExperimentName(e.target.value)} /></Field><div className="r-workload"><strong>{set.included * configs.length}</strong><span>replay units</span><small>{set.included} windows × {configs.length} validated configuration(s)</small></div><dl className="details compact"><div><dt>Mode</dt><dd>Replay · no split or ranking</dd></div><div><dt>Data policy</dt><dd>{policyLabel(set.research_policy)}</dd></div><div><dt>Backend grouping</dt><dd>{groupsInBatch || "—"} experiment(s)<small>Same strategy + execution assumptions share an experiment</small></dd></div><div><dt>Traces</dt><dd>Decisions · quotes · model · execution</dd></div><div><dt>ETA</dt><dd>Not measured</dd></div></dl><Notice title={editor ? "Finish the open configuration" : "Ready for preview"}>{editor ? "Validate it or discard the edits before reviewing the batch." : "The next page lists the exact configurations, windows and assumptions."}</Notice><Button primary disabled={editor || !configs.length || !experimentName.trim() || busy} onClick={() => onPreview(configs, experimentName.trim())}>{busy ? "Building the preview…" : "Preview replay →"}</Button><p className="r-note">No objective or holdout belongs to Replay. Tune introduces them separately in Phase 3.</p></Panel></aside></div>;
}
