export type Availability = "complete" | "partial" | "pending" | "missing" | "unknown";
export type LibraryWindow = {
 id:string; start:string; collection:string; session:string; reserved:boolean; duplicate:boolean;
 raw:Availability; normalized:Availability; up:Availability; down:Availability; reference:Availability; quality:Availability; warmup:Availability;
 outcome:"UP"|"DOWN"|"unresolved"|"unknown"; labelSource:"Provider confirmed"|"Payout-derived proxy"|"Not available";
};
export type DataRecipe="eligible"|"manual"|"missing";
export type WindowSet={id:string;name:string;createdAt:string;recipe:DataRecipe;purpose:string;ids:string[];excluded:{id:string;reason:string}[];previewId:string;start:string;end:string;requirements:string[];sourceChoice:string;referenceChoice?:string;version:string;diagnostic:boolean;filters?:import('./windowSetFilters').WindowFilters;selectionMode?:'matches'|'picked';outcomeConditioned?:boolean;collection?:string};
export const tracks=[['raw','Recording'],['normalized','Normalization'],['up','Up book'],['down','Down book'],['reference','Reference'],['quality','Quality'],['outcome','Outcome']] as const;
const originalWindows:LibraryWindow[]=Array.from({length:24},(_,i)=>{
 const day=19+Math.floor(i/8),slot=i%8;
 return {id:`DEMO-BTC-${day}-${String(slot+1).padStart(2,'0')}`,start:`2026-10-${day}T10:${String(slot*5).padStart(2,'0')}:00Z`,
 collection:day===20?'demo-btc-recovery':'demo-btc-main',session:`demo-session-${day}`,reserved:slot===7,duplicate:i===11,
 raw:i===19?'missing':'complete',normalized:[4,10,18].includes(i)?'pending':i===19?'missing':'complete',
 up:i===9?'partial':i===19?'missing':'complete',down:i===6?'partial':i===19?'missing':'complete',
 reference:[5,20].includes(i)?'partial':i===19?'missing':'complete',quality:i===12?'unknown':[4,10,18].includes(i)?'pending':i===19?'missing':'complete',
 warmup:[2,14].includes(i)?'partial':i===19?'missing':'complete',
 outcome:i===22?'unresolved':i===19?'unknown':i%3===0?'DOWN':'UP',labelSource:[19,22].includes(i)?'Not available':i%3===0?'Provider confirmed':'Payout-derived proxy'};
});
// Additional clock examples preserve all original v2 identities and timestamps.
const clockWindows:LibraryWindow[]=Array.from({length:18},(_,i)=>{
 const day=19+Math.floor(i/6),slot=i%6,time=['00:00','07:55','08:00','19:55','20:00','23:55'][slot];
 return {id:`DEMO-BTC-${day}-${String(slot+9).padStart(2,'0')}`,start:`2026-10-${day}T${time}:00Z`,collection:day===20?'demo-btc-recovery':'demo-btc-main',session:`demo-session-${day}`,reserved:false,duplicate:false,
 raw:'complete',normalized:slot===4?'pending':'complete',up:'complete',down:'complete',reference:'complete',quality:slot===4?'pending':'complete',warmup:'complete',
 outcome:slot===1||slot===3?'DOWN':'UP',labelSource:slot===2?'Provider confirmed':'Payout-derived proxy'};
});
export const libraryWindows:LibraryWindow[]=[...originalWindows,...clockWindows].sort((a,b)=>a.start.localeCompare(b.start));
export const recipeNames:Record<DataRecipe,string>={eligible:'Eligible windows',manual:'Custom window set',missing:'Missing-input diagnostic'};
export const stamp=(v:string)=>new Date(v).toISOString().slice(0,16).replace('T',' ');
export const timeLabel=(v:string)=>v.slice(11,16);
export function inputIssues(w:LibraryWindow):string[]{
 const out:string[]=[];
 for(const [key,label] of [['raw','No recorded observations'],['normalized','Normalization pending'],['up','Up book incomplete'],['down','Down book incomplete'],['reference','Reference incomplete'],['quality','Quality not assessed'],['warmup','Warm-up incomplete']] as const)
  if(w[key]!=='complete')out.push(label);
 return out;
}
export function readiness(w:LibraryWindow):string {
 if(w.reserved)return 'Reserved evaluation';
 if(w.raw!=='complete')return 'Not recorded';
 if(w.normalized!=='complete'||w.quality==='pending')return 'Preparation needed';
 if(w.quality==='unknown')return 'Assessment unknown';
 if(inputIssues(w).length)return 'Coverage gap';
 if(w.duplicate)return 'Source choice needed';
 return 'Inputs ready';
}
export function previewKey(value:unknown):string {
 const s=JSON.stringify(value);let hash=2166136261;
 for(let i=0;i<s.length;i++){hash^=s.charCodeAt(i);hash=Math.imul(hash,16777619);}
 return 'demo-preview-'+(hash>>>0).toString(16);
}
