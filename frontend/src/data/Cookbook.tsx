// Set cookbook: recipe cards for the set builder, their parameters, the thresholds a
// recipe resolved to, and saving a custom set's filters as a new recipe.
import { useEffect, useState } from "react";
import { call } from "../api";

export type CookbookParameter = { path: string; label: string; type: "time" | "weekdays" | "quantile" | "number" };
export type CookbookRecipe = { id: string; version: number; title: string; summary: string; why: string; policy: string; parameters: CookbookParameter[]; defaults: Record<string, unknown>; invalid?: string };
export type CookbookSpec = { id: string; version: number; parameters: Record<string, unknown> };
export type CookbookRecord = { id: string; version: number; title: string; parameters?: Record<string, unknown>; resolved_quantiles?: Record<string, { percentiles: (number | null)[]; values: (number | null)[]; ranked: number }> };

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const FEATURE_TEXT: Record<string, [string, string]> = {
  realised_vol_bp: ["Realised volatility of the reference", "bp"],
  move_bp: ["Move from K to close", "bp"],
  spread: ["Mean bid-ask spread", "USDC/share"],
  wide_spread_share: ["Time with a spread wider than one tick", ""],
  hour_utc: ["Hour of the window start (UTC)", "h"],
};

export function useCookbook() {
  const [recipes, setRecipes] = useState<CookbookRecipe[]>([]);
  const [folder, setFolder] = useState("");
  const load = async () => {
    const envelope = await call("cookbook.list", {}).catch(() => null);
    if (envelope?.ok) {
      setRecipes((envelope.result?.recipes as CookbookRecipe[]) || []);
      setFolder(String(envelope.result?.folder || ""));
    }
  };
  useEffect(() => { void load(); }, []);
  return { recipes, folder, reload: load };
}

export function CookbookCards({ recipes, folder, selected, onPick }: { recipes: CookbookRecipe[]; folder: string; selected: string | null; onPick: (recipe: CookbookRecipe) => void }) {
  const usable = recipes.filter((item) => !item.invalid);
  const broken = recipes.filter((item) => item.invalid);
  return <>
    <div className="dl-recipes">
      {usable.map((item) => (
        <button key={item.id} className={"dl-recipe " + (selected === item.id ? "active" : "")} onClick={() => onPick(item)}>
          <strong>{item.title}</strong>
          <p>{item.summary}</p>
          {item.why && <p className="plot-caption">{item.why}</p>}
          <small className="plot-caption">Recipe {item.id} · version {item.version}</small>
        </button>
      ))}
    </div>
    {broken.map((item) => <p key={item.id} className="dv-error">Recipe file {item.id} is not valid: {item.invalid}</p>)}
    <p className="plot-caption">Recipes are files in {folder || "config/research/set_recipes"}. A set made from one keeps its copy of the recipe and the parameters you used.</p>
  </>;
}

export function RecipeParameters({ recipe, spec, onChange }: { recipe: CookbookRecipe; spec: CookbookSpec; onChange: (spec: CookbookSpec) => void }) {
  const value = (path: string) => (path in spec.parameters ? spec.parameters[path] : recipe.defaults[path]);
  const set = (path: string, next: unknown) => onChange({ ...spec, parameters: { ...spec.parameters, [path]: next } });
  return <div className="dv-recipe-params">
    <p className="plot-caption">{recipe.summary}</p>
    {recipe.parameters.length === 0 && <p className="plot-caption">This recipe has no parameter to change.</p>}
    <div className="dl-scope-fields">
      {recipe.parameters.map((param) => {
        const current = value(param.path);
        if (param.type === "time") return <label key={param.path} className="field"><span>{param.label}</span><input type="time" value={String(current ?? "")} onChange={(event) => set(param.path, event.target.value)} /></label>;
        if (param.type === "quantile") return <label key={param.path} className="field"><span>{param.label}</span><input type="number" min={0} max={100} step={1} value={Math.round(Number(current ?? 0) * 100)} onChange={(event) => set(param.path, Math.min(100, Math.max(0, Number(event.target.value))) / 100)} /></label>;
        if (param.type === "number") return <label key={param.path} className="field"><span>{param.label}</span><input type="number" value={Number(current ?? 0)} onChange={(event) => set(param.path, Number(event.target.value))} /></label>;
        const days = (current as number[]) || [];
        return <div key={param.path} className="inline">{DAYS.map((label, index) => <label key={label} className="check"><input type="checkbox" checked={days.includes(index + 1)} onChange={() => set(param.path, days.includes(index + 1) ? days.filter((day) => day !== index + 1) : [...days, index + 1].sort())} />{label}</label>)}</div>;
      })}
    </div>
    {Object.keys(spec.parameters).length > 0 && <button className="text-button" onClick={() => onChange({ ...spec, parameters: {} })}>Back to the recipe defaults</button>}
  </div>;
}

export function ResolvedThresholds({ record }: { record?: CookbookRecord | null }) {
  const entries = Object.entries(record?.resolved_quantiles || {});
  if (!entries.length) return null;
  const fmt = (value: number | null) => (value == null ? "" : Number(value).toFixed(2));
  return <p className="plot-caption">{entries.map(([key, item]) => {
    const [label, unit] = FEATURE_TEXT[key] || [key, ""];
    const [low, high] = item.values;
    const [qLow, qHigh] = item.percentiles;
    const range = low != null && high != null ? `between ${fmt(low)} and ${fmt(high)}` : low != null ? `at least ${fmt(low)}` : `at most ${fmt(high)}`;
    const basis = [qLow != null ? `${Math.round(qLow * 100)}th` : null, qHigh != null ? `${Math.round(qHigh * 100)}th` : null].filter(Boolean).join(" to ");
    return `${label} ${range} ${unit} (${basis} percentile of ${item.ranked} windows that pass the data policy). These values are saved with the set.`;
  }).join(" ")}</p>;
}

export function SaveAsRecipe({ filters, policy, onSaved }: { filters: unknown; policy: string; onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [message, setMessage] = useState("");
  const save = async () => {
    const envelope = await call("cookbook.save", { title, summary, filters, policy }).catch(() => null);
    if (!envelope?.ok) { setMessage(envelope?.error?.reason || "The workspace API is not reachable."); return; }
    setMessage(`Saved as recipe ${String(envelope.result?.id)} (version 1). It is now a card under Goal.`);
    setOpen(false); onSaved();
  };
  if (!open) return <p className="plot-caption"><button className="text-button" onClick={() => setOpen(true)}>Save these filters as a cookbook recipe</button>{message && ` ${message}`}</p>;
  return <div className="dv-derive">
    <label className="field"><span>Recipe title</span><input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="e.g. Paris evening, weekdays" /></label>
    <label className="field"><span>What it selects</span><input value={summary} onChange={(event) => setSummary(event.target.value)} /></label>
    <p className="plot-caption">Saves the time zone, daily hours, weekdays and market-condition ranges. Picked windows and outcome filters are not part of a recipe.</p>
    <span className="trash-actions"><button className="button primary" disabled={!title.trim()} onClick={() => void save()}>Save recipe</button><button className="text-button" onClick={() => setOpen(false)}>Cancel</button></span>
    {message && <span className="dv-error">{message}</span>}
  </div>;
}
