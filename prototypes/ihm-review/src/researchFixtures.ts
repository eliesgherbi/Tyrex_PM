import { START, baseWindows, makeTrace, eventsFor, type Strategy, type WindowRow, type Tick } from './mock';
export type ProfileId = 'reference' | 'cautious';
export type ExecutionId = 'base' | 'slow';
export const profiles = {
  reference: { name: 'Reference hold', version: 'v1', edge: .03, hash: 'demo-cfg-a71c' },
  cautious: { name: 'Higher edge', version: 'v2', edge: .08, hash: 'demo-cfg-b29e' },
};
export const strategyProfile = (strategy: Strategy, id: ProfileId) => strategy === 'ask70'
  ? { ...profiles.reference, name: 'Ask threshold 0.70', hash: 'demo-cfg-ask70-70' }
  : profiles[id];
export const execution = {
  base: { name: 'Book depth · base friction', latency: 200, fee: .03, hash: 'demo-exec-04ef' },
  slow: { name: 'Book depth · delayed arrival', latency: 800, fee: .03, hash: 'demo-exec-08ba' },
};
export type ReplaySnapshot = { profile: ProfileId; execution: ExecutionId; strategy: Strategy; name: string; sourceSetName: string; sourceSetId: string; baseline: string; criteria: string; createdAt: string };
export const initialSnapshot: ReplaySnapshot = { profile: 'reference', execution: 'base', strategy: 'settlement_value', name: 'Reference hold · development check', sourceSetName: 'BTC five-minute · design sample', sourceSetId: 'demo-set-8-v1', baseline: 'Reference hold v1 · same 8 windows / base friction', criteria: 'Report every window; keep unknown P&L separate; no execution while a required book is invalid.', createdAt: '2026-10-21T14:08:00Z' };
// These are illustrative UI fixtures, never loaded from the warehouse or replay engine.
export function fixtureRows(profile: ProfileId, scenario: ExecutionId, strategy: Strategy): WindowRow[] {
  return baseWindows.map((r, i) => {
    let row = { ...r };
    if (i === 7) row.pnl = null; // Inputs blocked: do not invent zero research performance.
    if (profile === 'cautious' && i === 1) row = { ...row, status: 'skipped', requested: false, side: null, qty: 0, price: 0, fee: 0, pnl: 0 };
    if (scenario === 'slow' && row.qty) {
      row.qty = Math.floor(Math.min(row.qty, 4.97 / (row.price + .012)) * 100) / 100;
      row.price += .012;
    }
    if (strategy === 'ask70' && row.qty) {
      row.price = .72; row.qty = 6.4; row.salePrice = row.status === 'unresolved' ? undefined : i % 2 ? .705 : .735;
    }
    if (row.qty && row.pnl !== null) row.pnl = Number((row.qty * (row.salePrice ?? (row.side === row.winner ? 1 : 0)) - row.qty * row.price - row.fee).toFixed(4));
    return row;
  });
}
export function fixtureTrace(index: number, strategy: Strategy, row: WindowRow): Tick[] {
  const source = makeTrace(index, strategy, row);
  return source.map(t => {
    const wiggle = .008 * Math.sin((t.t - 58) / 28);
    const isDown = row.side === 'DOWN' || index === 1;
    const chosenAsk = (row.price || baseWindows[index].price || .47) + wiggle;
    const upAsk = isDown ? 1.025 - chosenAsk : chosenAsk;
    const downAsk = isDown ? chosenAsk : 1.025 - chosenAsk;
    const p = index === 1 ? .41 - .00025 * t.t : isDown ? .36 - .00025 * t.t : .63 + .00025 * t.t;
    const gap = (t.t >= 106 && t.t <= 111) || (t.t >= 215 && t.t <= 230) || (index === 7 && t.t >= 25);
    const action = gap ? 'HOST_SKIPPED' : t.action;
    return { ...t, action, reason: gap ? 'INPUTS_UNAVAILABLE' : t.reason, eligible: t.t >= 25 && !gap,
      upPoint: t.t >= 25 && !gap && strategy === 'settlement_value' ? p : null,
      upLow: t.t >= 25 && !gap && strategy === 'settlement_value' ? p - .045 : null,
      upHigh: t.t >= 25 && !gap && strategy === 'settlement_value' ? p + .055 : null,
      upAsk, downAsk, upMid: upAsk - .012, downMid: downAsk - .014,
    };
  });
}
export function evidenceAt(index: number, second: number, strategy: Strategy, row: WindowRow, scenario: ExecutionId) {
  const trace = fixtureTrace(index, strategy, row);
  const tick = trace.find(t => t.t === Math.floor(second)) ?? trace[118];
  const invalid = tick.action === 'HOST_SKIPPED';
  const action = invalid ? 'Skip' : second < 25 ? 'Wait' : !row.requested ? 'Wait' : second < 52 ? 'Wait' : second === 52 ? 'Enter' : second < 58 ? 'Await fill' : !row.qty ? 'No fill' : second < 65 ? 'Commit fill' : strategy === 'ask70' && second >= 282 ? 'Exit' : 'Hold';
  const reason = invalid ? 'Required book unavailable. No strategy order can be sent.' : second < 25 ? 'Warm-up is still incomplete.' : !row.requested ? 'No eligible entry in this window.' : second < 52 ? 'Entry eligibility has not been reached.' : second === 52 ? 'Entry gates pass at the saved decision.' : !row.qty ? 'The requested quantity did not fill. This episode closed.' : action === 'Commit fill' ? 'A fill is confirmed; commitment under the saved lifecycle is pending.' : action === 'Exit' ? 'The host requests flattening before the close.' : second < 58 ? 'The entry is pending; a request is not a fill.' : 'A confirmed position is tracked under its saved lifecycle policy.';
  const events = eventsFor(row, trace, strategy).map(e => e.kind === 'arrival' ? { ...e, t: 52 + execution[scenario].latency / 1000 } : e);
  return { tick, invalid, action, reason, trace, events, time: START + index * 300000 + second * 1000 };
}
