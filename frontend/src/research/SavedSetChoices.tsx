// Ported from the approved mockup (prototypes/ihm-review/src/SavedSetChoices.tsx) with real saved sets.
import { Button, Tag } from "../../../prototypes/ihm-review/src/WorkflowReviewUI";
import { dateRange, policyLabel, replayablePolicy, type SetSummary } from "./model";
import { setStatusText } from "./Trash";

export function replayRefusal(set: SetSummary): string {
  if (set.recipe === "missing_inputs") return "Missing-input diagnostic sets are not replayable.";
  if (set.legacy_unpinned) return "Saved before file pins; save it again in Data.";
  if (!replayablePolicy(set.research_policy)) return "No named research policy; save it again under Research v2.1.";
  if (!set.included) return "This set has no windows.";
  return "";
}

export function SavedSetChoices({ sets, selected, loading, error, onUse }: { sets: SetSummary[]; selected: string | null; loading: boolean; error: string; onUse: (set: SetSummary) => void }) {
  if (loading && !sets.length) return <p className="r-note">Loading saved window sets…</p>;
  if (error) return <p className="r-note">Saved sets could not be listed: {error}</p>;
  if (!sets.length) return <p className="r-note">No saved window set yet. Save one in Data first.</p>;
  return <div className="w-set-choices">{sets.map((set) => {
    const refusal = replayRefusal(set);
    return <section key={set.selection_id} className={"w-saved-set-choice " + (selected === set.selection_id ? "selected" : "")}>
      <div className="inline spread"><Tag tone={refusal ? "warn" : set.status === "current" ? "good" : "warn"}>{refusal ? "Not replayable as saved" : setStatusText[set.status || ""]?.text || "Saved window set"}</Tag><strong className="w-set-count">{set.included}<small> windows</small></strong></div>
      <h3>{set.name || "Unnamed window set"}</h3>
      <p>{dateRange(set.start_ns, set.end_ns)} · {set.block_ids?.length || 0} recording{set.block_ids?.length === 1 ? "" : "s"}</p>
      <div className="w-policy-line"><Tag>{policyLabel(set.research_policy)}</Tag><code>{set.selection_id}</code></div>
      {refusal && <p className="r-note">{refusal}</p>}
      <Button primary disabled={Boolean(refusal)} onClick={() => onUse(set)}>{selected === set.selection_id ? "Start a new replay on this set →" : "Use in experiment →"}</Button>
    </section>;
  })}</div>;
}
