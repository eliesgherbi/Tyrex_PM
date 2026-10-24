import { parameterSnapshot as source } from './parameterSnapshot';
import { fixtureRows, fixtureTrace, type ProfileId } from './researchFixtures';
import { eventsFor, START, type WindowRow, type Tick } from './mock';

export type Value = string | number | boolean | string[];
export type Values = Record<string, Value>;
export type ScenarioName = 'friction' | 'favorable';
export type ReplayConfig = { id: string; name: string; strategy: 'settlement_value'; base: string; parameters: Values; scenario: ScenarioName; execution: Values; fingerprint: string; validatedAt: string };
export type WindowSetContext = { id: string; name: string; count: number; policy: string; recordings: number; dateRange: string; windowIndices?: number[] };
export type ReplayRun = { id: string; experimentId: string; name: string; mode: 'Replay'; set: WindowSetContext; configs: ReplayConfig[]; state: 'completed' | 'running' | 'stopped'; completed: number; total: number; createdAt: string; fixture: boolean };
export type ResearchRow = WindowRow & { index: number; qualified: boolean; limitation: string };
export type SavedCandidate = { id: string; name: string; config: ReplayConfig; run: ReplayRun; resultId: string; rationale: string; frozen: boolean; savedAt: string; origin: 'Replay' };
export const snapshot = source;
export const sampleSet: WindowSetContext = { id: 'design-set-8-v2', name: 'BTC five-minute · workflow sample', count: 8, policy: 'exploratory95_v1', recordings: 1, dateRange: '21 Oct 2026 · 10:00–10:40 UTC' };
export const reviewWindowSets: WindowSetContext[] = [sampleSet, { ...sampleSet, id: 'design-set-first-four-v1', name: 'BTC five-minute · first four windows', count: 4, dateRange: '21 Oct 2026 · 10:00–10:20 UTC', windowIndices: [0, 1, 2, 3] }];
export function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)); }
export function digest(value: unknown) { let hash = 2166136261; for (const ch of JSON.stringify(value)) hash = Math.imul(hash ^ ch.charCodeAt(0), 16777619); return 'design-' + (hash >>> 0).toString(16); }
export function policyValues(values: Values): Values { return { ...clone(values), 'quality.accepted_k_qualities': ['CONFIRMED_CANONICAL', 'PROVISIONAL'], 'quality.require_verified_settlement_spec': false }; }
export function baseValues(base: string): Values { return policyValues(base === 'yaml' ? source.yamlBase.values : source.defaults); }
export function scenarioValues(name: ScenarioName): Values { return clone(source.scenarios[name].values) as Values; }
export const scenarioLabel = (name: ScenarioName) => name === 'friction' ? 'Friction · assumed costs & delay' : 'Favorable · generous fill assumptions';
export const policyKeys = new Set(['quality.accepted_k_qualities', 'quality.require_verified_settlement_spec']);
export const commonKeys = ['entry.min_edge_per_share', 'entry.tau_min_s', 'entry.tau_max_s', 'sizing.max_debit_usdc', 'entry.max_entry_price', 'exit.policy'];
export const groups = [
  { id: 'entry', name: 'Entry & position size', description: 'When to enter, price limits, spending and order sizing.', sections: ['entry', 'sizing'] },
  { id: 'model', name: 'Model & volatility', description: 'Probability inputs, uncertainty bands and volatility estimator.', sections: ['model', 'volatility'] },
  { id: 'exit', name: 'Exit, retry & re-entry', description: 'Lifecycle choices and bounded recovery of orders and positions.', sections: ['exit', 'retry', 'reentry'] },
  { id: 'quality', name: 'Settlement & research policy', description: 'Payoff interpretation, input quality and the visible set policy.', sections: ['settlement', 'quality'] },
  { id: 'fees', name: 'Strategy fee estimates', description: 'The strategy’s fee curve. Compare it with the simulation fee convention.', sections: ['fees'] },
  { id: 'veto', name: 'Model / market disagreement', description: 'Optional entry veto; does not alter existing holds or exits.', sections: ['disagreement_veto'] },
];
export const labels: Record<string, string> = {
  'entry.min_edge_per_share': 'Minimum conservative edge', 'entry.tau_min_s': 'Stop new entries this close to expiry', 'entry.tau_max_s': 'Start entries with this much time left',
  'sizing.max_debit_usdc': 'Maximum acquisition spending', 'entry.max_entry_price': 'Maximum entry price', 'exit.policy': 'Position lifecycle',
  'quality.accepted_k_qualities': 'Accepted price-to-beat quality', 'quality.require_verified_settlement_spec': 'Require verified settlement specification',
  'model.backend': 'Probability model', 'volatility.half_life_s': 'Volatility half-life', 'exit.value_exit_enabled': 'Enable value exit', 'retry.enabled': 'Allow bounded entry retry',
  'reentry.max_trades_per_window': 'Maximum trades per window', 'order_delay_ms': 'Order arrival delay', 'feed_delay_ms': 'Market-data delay', 'participation': 'Available displayed depth',
};
export function fieldLabel(key: string) { const field = key.split('.').at(-1)!; return labels[key] ?? field.replaceAll('_', ' ').replace(/^./, v => v.toUpperCase()); }
export function unit(key: string) { return key.endsWith('_ms') ? 'ms' : key.endsWith('_s') ? 'seconds' : key.includes('usdc') ? 'USDC' : key.includes('per_share') || key.endsWith('price') ? 'USDC / share' : key.endsWith('_bps') ? 'basis points' : ''; }
export const choices: Record<string, string[]> = {
  'model.backend': ['endpoint_diffusion', 'twap_proxy_diffusion'], 'model.spot_input': ['binance_spot', 'aligned_reference'],
  'settlement.comparison': ['AT_OR_ABOVE_K', 'STRICTLY_ABOVE_K'], 'settlement.provenance_status': ['VERIFIED', 'ASSUMED', 'PROXY'],
  'entry.target_mode': ['cap_only', 'require_target'], 'exit.policy': ['hold_to_resolution', 'managed_exit'], 'exit.value_exit_basis': ['upper', 'point'],
  'exit.residual_policy': ['hold_to_resolution'], 'reentry.rearm': ['signal_reset', 'cooldown'], 'volatility.variance_time_policy': ['per_sample_v0', 'elapsed_variance_v1'],
};
export function configErrors(values: Values, execution: Values, name: string): string[] {
  const errors: string[] = [], n = (key: string) => Number(values[key]);
  if (!name.trim()) errors.push('Give this configuration a name.');
  for (const field of source.fields) if (field.type === 'number' && (!Number.isFinite(n(field.key)) || field.integer && !Number.isInteger(n(field.key)))) errors.push(fieldLabel(field.key) + ': enter a valid ' + (field.integer ? 'whole number.' : 'number.'));
  if (n('entry.tau_min_s') < 0 || n('entry.tau_max_s') < n('entry.tau_min_s')) errors.push('The entry time band must have 0 ≤ minimum ≤ maximum.');
  if (n('entry.min_edge_per_share') < 0 || n('sizing.max_debit_usdc') <= 0) errors.push('Edge must be non-negative and acquisition spending must be positive.');
  if (!(n('entry.max_entry_price') > 0 && n('entry.max_entry_price') < 1)) errors.push('Maximum entry price must be between 0 and 1.');
  if (values['exit.policy'] === 'hold_to_resolution' && (values['exit.value_exit_enabled'] || values['exit.thesis_stop_enabled'] || n('reentry.max_trades_per_window') > 1)) errors.push('Value exit, thesis stop and re-entry require managed_exit.');
  if (values['exit.value_exit_trend_enabled'] && !values['exit.value_exit_enabled']) errors.push('Exit trend requires value exit to be enabled.');
  if (values['disagreement_veto.enabled'] && !(n('disagreement_veto.max_gap_pp') > 0 && n('disagreement_veto.max_gap_pp') <= 100)) errors.push('An enabled disagreement veto requires a gap in (0, 100].');
  if (values['disagreement_veto.recheck_at_dispatch'] && !values['disagreement_veto.enabled']) errors.push('Dispatch veto recheck requires the disagreement veto.');
  if (!(Number(execution.participation) > 0 && Number(execution.participation) <= 1)) errors.push('Execution depth participation must be in (0, 1].');
  for (const [key, value] of Object.entries(execution)) if ((typeof value === 'number' || typeof scenarioValues('friction')[key] === 'number') && (!Number.isFinite(Number(value)) || Number(value) < 0)) errors.push(fieldLabel(key) + ': use a non-negative execution value.');
  return errors;
}
export function makeConfig(name = 'My first replay', base = 'defaults', edge?: number): ReplayConfig {
  const parameters = baseValues(base); if (edge !== undefined) parameters['entry.min_edge_per_share'] = String(edge);
  const execution = scenarioValues('friction');
  return { id: digest([name, parameters]), name, strategy: 'settlement_value', base, parameters, scenario: 'friction', execution, fingerprint: digest({ parameters, execution }), validatedAt: new Date().toISOString() };
}
export function resultId(run: ReplayRun, cfg: ReplayConfig) { return run.id + '/' + cfg.fingerprint; }
export function resultsFor(cfg: ReplayConfig, set?: WindowSetContext): ResearchRow[] {
  const variant: ProfileId = Number(cfg.parameters['entry.min_edge_per_share']) >= .08 ? 'cautious' : 'reference';
  return fixtureRows(variant, 'base', 'settlement_value').map((r, index) => ({ ...r, index, qualified: ![3, 5, 7].includes(index), limitation: index === 3 ? 'Incomplete execution evidence · omitted from qualified total' : index === 5 ? 'Unresolved outcome · P&L unknown' : index === 7 ? 'Inputs blocked · not evaluable' : 'Exploratory policy · provider-reported outcome / provisional K' })).filter(r => !set?.windowIndices || set.windowIndices.includes(r.index));
}
export function summary(cfg: ReplayConfig, set?: WindowSetContext) {
  const rows = resultsFor(cfg, set), qualified = rows.filter(r => r.qualified), filled = qualified.filter(r => r.qty > 0), requests = qualified.filter(r => r.requested);
  return { qualified: qualified.length, unqualified: rows.length - qualified.length, pnl: filled.filter(r => r.pnl !== null).reduce((s, r) => s + r.pnl!, 0), requests: requests.length, filled: filled.length, entryRate: requests.length / qualified.length, fillRate: requests.length ? filled.length / requests.length : null };
}
export function sampleRuns(): ReplayRun[] {
  return [
    { id: 'design-run-102', experimentId: 'design-exp-102', name: 'Entry sensitivity · saved example', mode: 'Replay', set: clone(sampleSet), configs: [makeConfig('Original entry', 'yaml', .05), makeConfig('Higher edge', 'yaml', .08)], state: 'completed', completed: 16, total: 16, createdAt: '2026-10-22T09:35:00Z', fixture: true },
    { id: 'design-run-101', experimentId: 'design-exp-101', name: 'First look · saved example', mode: 'Replay', set: clone(sampleSet), configs: [makeConfig('Schema-default check')], state: 'completed', completed: 8, total: 8, createdAt: '2026-10-22T09:05:00Z', fixture: true },
  ];
}
export function traceFor(index: number, cfg: ReplayConfig) {
  const row = resultsFor(cfg)[index];
  const trace: Tick[] = fixtureTrace(index, 'settlement_value', row).map(t => {
    const gap = index === 7 ? t.t >= 25 : (t.t >= 108 && t.t <= 110) || (t.t >= 218 && t.t <= 220);
    const down = row.side === 'DOWN' || index === 1;
    const p = index === 1 ? .41 - .00025 * t.t : down ? .36 - .00025 * t.t : .63 + .00025 * t.t;
    const action = gap ? 'HOST_SKIPPED' : t.t < 25 ? 'WARMUP' : !row.requested ? 'SKIP' : t.t < 52 ? 'WAIT' : t.t === 52 ? 'ENTER' : t.t < 58 ? 'ENTERING' : !row.qty ? 'CLOSED_NO_FILL' : t.t < 65 ? 'COMMITTING' : 'HOLD';
    return { ...t, action, eligible: t.t >= 25 && !gap, reason: gap ? 'INPUTS_UNAVAILABLE' : action === 'ENTER' ? 'ENTRY_ELIGIBLE' : action === 'SKIP' ? 'EDGE_BELOW_COST' : action === 'COMMITTING' ? 'FILL_AWAITING_COMMIT' : action === 'HOLD' ? 'POSITION_TRACKED' : action === 'CLOSED_NO_FILL' ? 'UNFILLED_EPISODE_CLOSED' : 'WAITING_FOR_ENTRY', upPoint: !gap && t.t >= 25 ? p : null, upLow: !gap && t.t >= 25 ? p - .045 : null, upHigh: !gap && t.t >= 25 ? p + .055 : null };
  });
  const events = eventsFor(row, trace, 'settlement_value').map(e => e.kind === 'arrival' ? { ...e, t: 52 + Number(cfg.execution.order_delay_ms) / 1000 } : e);
  return { row, trace, events, start: START + index * 300000 };
}
