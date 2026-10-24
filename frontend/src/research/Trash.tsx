// Delete into the recoverable trash, restore, and permanent removal with an in-page confirmation.
import { useEffect, useState } from "react";
import { call } from "../api";

export const setStatusText: Record<string, { text: string; tone: string; detail: string }> = {
  current: { text: "Current rules", tone: "ok", detail: "Saved under replay_research_v2_1." },
  legacy_policy: { text: "Previous policy", tone: "warn", detail: "Saved under an earlier rule (exploratory 95% or research v2 with a 30 s cap). Still replayable; re-save under current rules to update it." },
  not_replayable: { text: "Not replayable", tone: "warn", detail: "No research policy, or a diagnostic set." },
};
export const runStatusText: Record<string, { text: string; tone: string }> = {
  current: { text: "Current scenario", tone: "good" },
  legacy_scenario: { text: "Legacy scenario", tone: "warn" },
  set_deleted: { text: "Set deleted", tone: "warn" },
};

export function DeleteControl({ what, disabled, disabledReason, onDelete }: { what: string; disabled?: boolean; disabledReason?: string; onDelete: () => Promise<string | null> }) {
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (disabled) return <span className="trash-note" title={disabledReason}>{disabledReason || "Cannot delete now"}</span>;
  if (!asking) return <button type="button" className="text-button trash-delete" onClick={() => { setError(""); setAsking(true); }}>Delete</button>;
  return <span className="trash-confirm" role="group" aria-label={`Delete ${what}`}>
    <span>Move {what} to trash?</span>
    <button type="button" className="text-button trash-delete" disabled={busy} onClick={async () => { setBusy(true); const problem = await onDelete(); setBusy(false); if (problem) setError(problem); else setAsking(false); }}>{busy ? "Moving…" : "Move to trash"}</button>
    <button type="button" className="text-button" disabled={busy} onClick={() => setAsking(false)}>Cancel</button>
    {error && <span className="trash-error">{error}</span>}
  </span>;
}

type Entry = { entry_id: string; kind: string; label: string; subject_id: string; deleted_at: string; files: { path: string }[]; runs_still_referencing?: string[]; job_ids?: string[]; size_bytes?: number; age_days?: number };
const size = (bytes?: number) => (bytes == null ? "" : bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1e3))} kB`);
const kindText: Record<string, string> = { window_set: "Window set", replay_run: "Replay run", replay_preview: "Unstarted preview" };

export function TrashPanel({ version, onChanged }: { version: number; onChanged: () => void }) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [error, setError] = useState("");
  const [purging, setPurging] = useState<string | null>(null);
  const load = async () => {
    try {
      const envelope = await call("trash.list", {});
      if (envelope.ok) setEntries((envelope.result?.entries as Entry[]) || []); else setError(envelope.error?.reason || "Trash could not be read.");
    } catch { setError("The workspace API is not reachable."); }
  };
  useEffect(() => { void load(); }, [version]);
  const act = async (operation: string, request: Record<string, unknown>) => {
    setError("");
    try {
      const envelope = await call(operation, request);
      if (!envelope.ok) { setError(envelope.error?.reason || "The request was refused."); return; }
      setPurging(null);
      await load();
      onChanged();
    } catch { setError("The workspace API is not reachable."); }
  };
  return <section className="panel trash-panel" aria-label="Trash">
    <div className="panel-head"><div><h3>Trash · {entries.length}{entries.length ? ` · ${size(entries.reduce((sum, item) => sum + (item.size_bytes || 0), 0))}` : ""}</h3><p>Deleted sets and results stay here until you remove them permanently. Restore puts every file back where it was.</p></div>
      {entries.length > 0 && (purging === "all"
        ? <span className="trash-confirm"><span>Remove all {entries.length} permanently?</span><button type="button" className="text-button trash-delete" onClick={() => void act("trash.purge", { confirm: "delete permanently" })}>Delete permanently</button><button type="button" className="text-button" onClick={() => setPurging(null)}>Cancel</button></span>
        : <span className="trash-actions">{entries.some((item) => (item.age_days || 0) >= 30) && (purging === "old"
          ? <span className="trash-confirm"><span>Remove {entries.filter((item) => (item.age_days || 0) >= 30).length} entries older than 30 days?</span><button type="button" className="text-button trash-delete" onClick={() => void act("trash.purge", { confirm: "delete permanently", older_than_days: 30 })}>Delete permanently</button><button type="button" className="text-button" onClick={() => setPurging(null)}>Cancel</button></span>
          : <button type="button" className="text-button trash-delete" onClick={() => setPurging("old")}>Remove entries older than 30 days</button>)}<button type="button" className="text-button trash-delete" onClick={() => setPurging("all")}>Empty trash</button></span>)}
    </div>
    {error && <p className="trash-error">{error}</p>}
    {!entries.length ? <p className="trash-note">Nothing in the trash.</p> : <div className="trash-list">{entries.map((entry) => <div key={entry.entry_id} className="trash-row">
      <div><strong>{entry.label}</strong><small>{kindText[entry.kind] || entry.kind} · deleted {entry.deleted_at.slice(0, 16).replace("T", " ")} UTC ({entry.age_days ?? 0} days ago) · {size(entry.size_bytes)} · {entry.files.length} file group(s){entry.runs_still_referencing?.length ? ` · ${entry.runs_still_referencing.length} run(s) still name this set` : ""}</small></div>
      <div className="trash-actions">
        <button type="button" className="text-button" onClick={() => void act("trash.restore", { entry_id: entry.entry_id })}>Restore</button>
        {purging === entry.entry_id
          ? <span className="trash-confirm"><span>Remove permanently?</span><button type="button" className="text-button trash-delete" onClick={() => void act("trash.purge", { confirm: "delete permanently", entry_ids: [entry.entry_id] })}>Delete permanently</button><button type="button" className="text-button" onClick={() => setPurging(null)}>Cancel</button></span>
          : <button type="button" className="text-button trash-delete" onClick={() => setPurging(entry.entry_id)}>Delete permanently</button>}
      </div>
    </div>)}</div>}
  </section>;
}
