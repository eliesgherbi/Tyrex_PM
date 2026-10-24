import type { LibraryWindow } from './dataLibraryMock';

export type WindowFilters = {
 outcome:'any'|'UP'|'DOWN'|'unresolved'|'unknown';
 labelSource:'any'|'provider'|'proxy'|'unknown';
 dailyTime:boolean; fromTime:string; untilTime:string; timeZone:'UTC'|'Europe/Paris';
 weekdays:number[];
};
export const defaultWindowFilters:WindowFilters={outcome:'any',labelSource:'any',dailyTime:false,fromTime:'08:00',untilTime:'20:00',timeZone:'UTC',weekdays:[1,2,3,4,5,6,7]};
export const weekdays=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
// Metadata for future filter controls. Unavailable measurements never silently match nothing.
export const futureFilters=[
 {id:'reversal',label:'Reversal pattern',reason:'Define the series, crossing threshold, observation interval and measurement version first.'},
 {id:'volatility',label:'Volatility regime',reason:'Requires a supported volatility measurement, units and lookback.'},
 {id:'liquidity',label:'Liquidity / spread',reason:'Requires a supported book measurement and sampling interval.'},
];
const minutes=(value:string)=>{if(!/^([01]\d|2[0-3]):[0-5]\d$/.test(value))return null;const [h,m]=value.split(':').map(Number);return h*60+m;};
export function filtersError(f:WindowFilters):string|null{
 if(!f.weekdays.length)return 'Choose at least one day of the week.';
 if(f.dailyTime&&(minutes(f.fromTime)===null||minutes(f.untilTime)===null))return 'Enter valid daily start and end times.';
 if(f.dailyTime&&f.fromTime===f.untilTime)return 'Daily start and end must differ. Turn off the time filter to include a full day.';
 return null;
}
export function clockFor(start:string,timeZone:WindowFilters['timeZone']){
 const parts=new Intl.DateTimeFormat('en-GB',{timeZone,hourCycle:'h23',hour:'2-digit',minute:'2-digit',weekday:'short'}).formatToParts(new Date(start));
 const part=(type:string)=>parts.find(p=>p.type===type)?.value??'';
 return {time:part('hour')+':'+part('minute'),day:weekdays.indexOf(part('weekday'))+1};
}
export function filterReason(w:LibraryWindow,f:WindowFilters):string|null{
 // Guard before reading labels, including for label-source and outcome-count filters.
 if(w.reserved)return 'Reserved evaluation · protected';
 if(filtersError(f))return 'Invalid filter definition';
 const clock=clockFor(w.start,f.timeZone);
 if(!f.weekdays.includes(clock.day))return 'Outside selected weekdays';
 if(f.dailyTime){
  const current=minutes(clock.time)!,from=minutes(f.fromTime)!,until=minutes(f.untilTime)!;
  const inside=from<until?current>=from&&current<until:current>=from||current<until;
  if(!inside)return 'Outside daily time range';
 }
 if(f.outcome!=='any'&&w.outcome!==f.outcome)return 'Does not match selected market outcome';
 if(f.labelSource==='provider'&&w.labelSource!=='Provider confirmed')return 'Provider-confirmed outcome required';
 if(f.labelSource==='proxy'&&w.labelSource!=='Payout-derived proxy')return 'Payout-derived proxy required';
 if(f.labelSource==='unknown'&&w.labelSource!=='Not available')return 'Outcome label is available';
 return null;
}
export const matchesWindowFilters=(w:LibraryWindow,f:WindowFilters)=>filterReason(w,f)===null;
export const outcomeConditioned=(f:WindowFilters)=>f.outcome!=='any'||f.labelSource!=='any';
export function filterDescriptions(f:WindowFilters):string[]{
 const out:string[]=[];
 if(f.outcome!=='any')out.push('Market outcome: '+({UP:'Up',DOWN:'Down',unresolved:'Unresolved',unknown:'Unknown'}[f.outcome]));
 if(f.labelSource!=='any')out.push('Label source: '+({provider:'Provider confirmed',proxy:'Payout-derived proxy',unknown:'Not available'}[f.labelSource]));
 if(f.dailyTime)out.push(`Daily ${f.fromTime}–${f.untilTime} ${f.timeZone}${f.fromTime>f.untilTime?' · overnight':''}`);
 if(f.weekdays.length!==7)out.push(f.weekdays.map(d=>weekdays[d-1]).join(', ')+' · '+f.timeZone);
 return out;
}
