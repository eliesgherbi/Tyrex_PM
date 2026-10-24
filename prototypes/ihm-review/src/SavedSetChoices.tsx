import { reviewWindowSets, type WindowSetContext } from './workflowReviewModel';
import { Button, Tag } from './WorkflowReviewUI';
export function SavedSetChoices({ selected, onUse }: { selected: WindowSetContext; onUse: (set: WindowSetContext) => void }) {
  return <div className="w-set-choices">{reviewWindowSets.map(set => <section key={set.id} className={'w-saved-set-choice ' + (selected.id === set.id ? 'selected' : '')}><div className="inline spread"><Tag>Saved window set · design fixture</Tag><strong className="w-set-count">{set.count}<small> windows</small></strong></div><h3>{set.name}</h3><p>{set.dateRange} · {set.recordings} recording</p><div className="w-policy-line"><Tag>{set.policy}</Tag><code>{set.id}</code></div><Button primary onClick={() => onUse(set)}>Use in experiment →</Button></section>)}</div>;
}
