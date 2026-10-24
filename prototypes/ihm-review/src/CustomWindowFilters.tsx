import { defaultWindowFilters,filtersError,futureFilters,weekdays,type WindowFilters } from './windowSetFilters';

export function WindowSetFilters({value,onChange}:{value:WindowFilters;onChange:(v:WindowFilters)=>void}){
 const set=(next:Partial<WindowFilters>)=>onChange({...value,...next});
 return <div className="dl-custom-filters">
  <div className="inline spread"><div><h3>Build your custom selection</h3><p>Combine filters, then include all matches or refine individual windows.</p></div><button className="text-button" onClick={()=>onChange({...defaultWindowFilters,weekdays:[1,2,3,4,5,6,7]})}>Reset custom filters</button></div>
  <div className="dl-filter-grid">
   <label className="field"><span>Market closed / outcome</span><select value={value.outcome} onChange={e=>set({outcome:e.target.value as WindowFilters['outcome']})}><option value="any">Any outcome</option><option value="UP">Up won</option><option value="DOWN">Down won</option><option value="unresolved">Unresolved</option><option value="unknown">Unknown</option></select></label>
   <label className="field"><span>Outcome label source</span><select value={value.labelSource} onChange={e=>set({labelSource:e.target.value as WindowFilters['labelSource']})}><option value="any">Any source · keep provenance visible</option><option value="provider">Provider confirmed only</option><option value="proxy">Payout-derived proxy only</option><option value="unknown">Label not available</option></select></label>
  </div>
  <p className="plot-caption">“Up won” describes the market outcome, not a strategy's profit. A payout-derived direction stays provisional. Real recordings may have no provider-confirmed labels; zero matches is valid.</p>
  <div className="dl-daily-heading"><label className="check"><input type="checkbox" checked={value.dailyTime} onChange={e=>set({dailyTime:e.target.checked})}/>Filter by time of day</label><span>24-hour clock · window start · end exclusive</span></div>
  <div className="dl-clock-fields"><label className="field"><span>Daily from</span><input type="time" disabled={!value.dailyTime} value={value.fromTime} onInput={e=>set({fromTime:e.currentTarget.value})} onChange={e=>set({fromTime:e.target.value})}/></label><label className="field"><span>Daily until</span><input type="time" disabled={!value.dailyTime} value={value.untilTime} onInput={e=>set({untilTime:e.currentTarget.value})} onChange={e=>set({untilTime:e.target.value})}/></label><label className="field"><span>Time zone · clock & weekdays</span><select value={value.timeZone} onChange={e=>set({timeZone:e.target.value as WindowFilters['timeZone']})}><option value="UTC">UTC</option><option value="Europe/Paris">Europe/Paris · daylight saving aware</option></select></label></div>
  <p className="plot-caption">The date bounds above remain UTC. Daily hours and weekdays use {value.timeZone}.</p>
  {value.dailyTime&&value.fromTime>value.untilTime&&<p className="dl-time-note">Overnight range: from {value.fromTime} to midnight, then midnight until {value.untilTime} on each selected calendar day.</p>}
  <div className="dl-weekday-heading"><span>Days of the week · {value.timeZone}</span><button className="text-button" onClick={()=>set({weekdays:[1,2,3,4,5,6,7]})}>All days</button></div>
  <div className="dl-weekdays">{weekdays.map((day,i)=><label key={day} className={value.weekdays.includes(i+1)?'active':''}><input type="checkbox" aria-label={day} checked={value.weekdays.includes(i+1)} onChange={e=>set({weekdays:e.target.checked?[...value.weekdays,i+1].sort((a,b)=>a-b):value.weekdays.filter(d=>d!==i+1)})}/>{day}</label>)}</div>
  {filtersError(value)&&<p className="inline-warning" role="alert">{filtersError(value)}</p>}
  <details className="dl-future-filters"><summary>More scenario filters · future extensions</summary><p>Presets and custom selections will use the same versioned filter definitions. Unsupported measurements stay explicit.</p>{futureFilters.map(f=><div key={f.id}><button disabled>{f.label} · unavailable</button><small>{f.reason}</small></div>)}</details>
 </div>;
}
