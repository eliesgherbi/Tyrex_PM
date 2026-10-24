export type Strategy = "settlement_value" | "ask70";
export type ConfigId = "reference" | "tighter";
export type Scenario = "friction" | "delayed";
export type Leg = "UP" | "DOWN";
export type WindowRow = {
 id: string; time: string; side: Leg | null; status: "won" | "lost" | "partial" | "unresolved" | "unfilled" | "skipped";
 qty: number; price: number; fee: number; pnl: number | null; requested: boolean; winner: Leg | null; salePrice?: number;
};
export const START = Date.UTC(2026, 9, 21, 10, 0);
export const baseWindows: WindowRow[] = [
 {id:"DEMO-01",time:"10:00",side:"UP",status:"won",qty:10,price:.47,fee:.03,pnl:5.27,requested:true,winner:"UP"},
 {id:"DEMO-02",time:"10:05",side:"DOWN",status:"lost",qty:10,price:.48,fee:.03,pnl:-4.83,requested:true,winner:"UP"},
 {id:"DEMO-03",time:"10:10",side:null,status:"skipped",qty:0,price:0,fee:0,pnl:0,requested:false,winner:"DOWN"},
 {id:"DEMO-04",time:"10:15",side:"UP",status:"partial",qty:5,price:.46,fee:.02,pnl:2.68,requested:true,winner:"UP"},
 {id:"DEMO-05",time:"10:20",side:"DOWN",status:"unfilled",qty:0,price:.47,fee:0,pnl:0,requested:true,winner:"DOWN"},
 {id:"DEMO-06",time:"10:25",side:"DOWN",status:"unresolved",qty:10,price:.47,fee:.03,pnl:null,requested:true,winner:null},
 {id:"DEMO-07",time:"10:30",side:"UP",status:"won",qty:9.8,price:.50,fee:.03,pnl:4.87,requested:true,winner:"UP"},
 {id:"DEMO-08",time:"10:35",side:null,status:"skipped",qty:0,price:0,fee:0,pnl:0,requested:false,winner:"DOWN"},
];
export function windowsFor(config:ConfigId,scenario:Scenario,strategy:Strategy): WindowRow[] {
 return baseWindows.map((original,i) => {
  const row={...original};
  if(config==="tighter" && i===1) return {...row,status:"skipped",qty:0,price:0,fee:0,pnl:0,requested:false,side:null};
  if(strategy==="ask70" && row.qty>0) {
   row.qty=Math.round(4.6/.72*100)/100;row.price=.72;row.fee=.03;
   if(row.status!=="unresolved")row.salePrice=i%2===0?.735:.705;
   row.pnl=row.status==="unresolved"?null:Number((row.qty*row.salePrice!-row.qty*row.price-row.fee).toFixed(4));
  }
  if(scenario==="delayed" && row.qty>0) {row.price+=.012; if(row.pnl!==null) row.pnl-=row.qty*.012;}
  return row;
 });
}
export function metrics(rows:WindowRow[]) {
 const filled=rows.filter(r=>r.qty>0),resolved=filled.filter(r=>r.pnl!==null);
 const requests=rows.filter(r=>r.requested);
 return {windows:rows.length, requests:requests.length,fills:filled.length,unfilled:requests.length-filled.length,
  skipped:rows.length-requests.length,unresolved:filled.length-resolved.length,resolved:resolved.length,
  wins:resolved.filter(r=>(r.pnl??0)>0).length,losses:resolved.filter(r=>(r.pnl??0)<0).length,
  pnl:!filled.length?0:resolved.length?resolved.reduce((s,r)=>s+(r.pnl??0),0):null,
  spend:filled.reduce((s,r)=>s+r.qty*r.price+r.fee,0),
  spendResolved:resolved.reduce((s,r)=>s+r.qty*r.price+r.fee,0)};
}
export type Tick = {t:number;ms:number;upPoint:number|null;upLow:number|null;upHigh:number|null;upMid:number;downMid:number;upAsk:number;downAsk:number;sigma:number;reference:number;eligible:boolean;action:string;reason:string};
const clamp=(v:number)=>Math.max(.02,Math.min(.98,v));
export function makeTrace(windowIndex:number,strategy:Strategy,selectedRow:WindowRow=baseWindows[windowIndex]):Tick[] {
 const start=START+windowIndex*300_000, side=selectedRow.side??"UP";
 return Array.from({length:376},(_,i)=>{
  const t=i-60,skip=(t>=106&&t<=111)||(t>=215&&t<=230),p=clamp((side==="UP"?.56:.37)+t*.00055+.03*Math.sin(t/33));
  const upMid=strategy==="ask70"?(side==="UP"?.685+t*.00023:.295-t*.00012):.44+t*.00020+.017*Math.sin(t/27+windowIndex);
  const downMid=strategy==="ask70"?(side==="DOWN"?.685+t*.00023:.295-t*.00012):.55-t*.00018+.014*Math.cos(t/31+windowIndex);
  const low=clamp(p-.045-.007*Math.sin(t/21)), high=clamp(p+.055+.005*Math.sin(t/24));
  const eligible=t>=25&&!skip;
  const action=skip?"HOST_SKIPPED":t<25?"WARMUP":!selectedRow.requested?"SKIP":t<52?"WAIT":t===52?"ENTER":t<58?"ENTERING":t<65?(selectedRow.qty?"COMMITTING":"ENTERING"):!selectedRow.qty?"CLOSED_NO_FILL":strategy==="ask70"&&t>=282?(selectedRow.salePrice===undefined?"EXIT_PENDING":"FLAT"):"HOLD";
  const reason=action==="HOST_SKIPPED"?"INPUTS_UNAVAILABLE":action==="WARMUP"?(strategy==="ask70"?"BOOK_NOT_READY":"MODEL_NOT_READY"):action==="SKIP"?"EDGE_BELOW_COST":action==="ENTER"?"ENTRY_ELIGIBLE":action==="HOLD"?"POSITION_TRACKED":action==="COMMITTING"?"FILL_AWAITING_COMMIT":action==="CLOSED_NO_FILL"?"UNFILLED_EPISODE_CLOSED":action==="FLAT"?"HOST_FLATTEN_CONFIRMED":action==="EXIT_PENDING"?"HOST_EXIT_OUTCOME_UNKNOWN":"WAITING_FOR_ENTRY";
  return {t,ms:start+t*1000,upPoint:eligible&&strategy==="settlement_value"?p:null,upLow:eligible&&strategy==="settlement_value"?low:null,
   upHigh:eligible&&strategy==="settlement_value"?high:null,upMid:clamp(upMid),downMid:clamp(downMid),upAsk:clamp(upMid+.012),downAsk:clamp(downMid+.014),
   sigma:.34+.02*Math.sin(t/17),reference:68420+windowIndex*7+t*.11+9*Math.sin(t/33),eligible,action,reason};
 });
}
export type EventRow={t:number;label:string;kind:"request"|"arrival"|"fill"|"cancel"|"exit";side:Leg;y:number|null;meaning:string;origin:string};
export function eventsFor(row:WindowRow,trace:Tick[],strategy:Strategy):EventRow[] {
 if(!row.requested)return [];
 const point=trace.find(x=>x.t===52)!;
 const p=row.side==="DOWN"?(point.upHigh===null?null:1-point.upHigh):point.upLow;
 const out:EventRow[]=[{t:52,label:"Entry requested",kind:"request",side:row.side!,y:strategy==="settlement_value"?p:null,meaning:strategy==="settlement_value"?"Conservative model probability; not a fill":"Decision timestamp; no model probability",origin:"strategy decision"},
  {t:54,label:"Order arrived",kind:"arrival",side:row.side!,y:null,meaning:"Venue-arrival time; no price coordinate",origin:"simulated execution"}];
 if(row.qty>0) out.push({t:58,label:row.status==="partial"?"Partial BUY fill":"BUY fill confirmed",kind:"fill",side:row.side!,y:row.price,meaning:"Paid token price · USDC/share",origin:"simulated execution"});
 if(row.status==="partial"||row.status==="unfilled")out.push({t:62,label:"Unfilled remainder cancelled",kind:"cancel",side:row.side!,y:row.price,meaning:"Order limit price · USDC/share",origin:"simulated execution"});
 if(strategy==="ask70"&&row.qty>0)out.push({t:282,label:row.salePrice===undefined?"Host flatten requested · outcome uncertain":"Host flatten SELL",kind:"exit",side:row.side!,y:row.salePrice??null,meaning:row.salePrice===undefined?"Timestamp only; no confirmed sale price":"Illustrative sale price · USDC/share",origin:"host mandatory exit"});
 return out;
}
export const fmtMoney=(v:number|null)=>v===null?"Unknown":(v<0?"−":v>0?"+":"")+Math.abs(v).toFixed(2);
export const utc=(ms:number)=>new Date(ms).toISOString().slice(11,19);
export const labels:Record<WindowRow["status"],string>={won:"Resolved win",lost:"Resolved loss",partial:"Partial fill",unresolved:"Unresolved",unfilled:"No fill",skipped:"No entry"};

