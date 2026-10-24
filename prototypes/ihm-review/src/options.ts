import type { Option } from "./Chart";
import { START, utc, type Tick, type EventRow, type Strategy, type WindowRow } from "./mock";
export type Palette={text:string;muted:string;grid:string;blue:string;teal:string;market:string;surface:string;green:string;red:string};
export const light:Palette={text:"#202838",muted:"#7c8596",grid:"#e9edf3",blue:"#4a65dc",teal:"#27867b",market:"#747d8d",surface:"#ffffff",green:"#28826b",red:"#bd5e62"};
export const dark:Palette={text:"#e2e7f0",muted:"#9ba5b6",grid:"#303746",blue:"#8b9df5",teal:"#68b7aa",market:"#b2bac8",surface:"#191e28",green:"#78b99d",red:"#d78c92"};
const linePoints=(ticks:Tick[],value:(t:Tick)=>number|null,strict:boolean)=>{
 const out:(number|null)[][]=[];let last:number|undefined;
 for(const tick of ticks) {
  if(tick.action==="HOST_SKIPPED")continue;
  const v=value(tick);if(v===null){out.push([tick.ms,null]);last=undefined;continue;}
  if(last!==undefined&&tick.ms-last>(strict?1500:10000))out.push([last+1,null]);
  out.push([tick.ms,v]);last=tick.ms;
 }
 return out;
};
export function timelineOption({trace,events,row,strategy,windowIndex,warmup,strict,showAsk,point,conservative,provenance,palette:p,range}:
 {trace:Tick[];events:EventRow[];row:WindowRow;strategy:Strategy;windowIndex:number;warmup:boolean;strict:boolean;showAsk:boolean;point:boolean;conservative:boolean;provenance:string;palette:Palette;range:string}):Option {
 const start=START+windowIndex*300000,model=strategy==="settlement_value";
 const series:any[]=[];
 for(const [leg,index]of [["UP",0],["DOWN",1]] as const) {
  const isUp=leg==="UP",axis=index*2;
  const add=(name:string,value:(t:Tick)=>number|null,color:string,yAxis:number,dash=false,width=2)=>{
   series.push({name,type:"line",xAxisIndex:index,yAxisIndex:yAxis,data:linePoints(trace,value,strict),
    showSymbol:false,connectNulls:false,clip:true,lineStyle:{color,width,type:dash?"dashed":"solid"},itemStyle:{color},
    emphasis:{focus:"series"},animation:false});
  };
  if(model&&point)add("Point estimate P̂",t=>isUp?t.upPoint:t.upPoint===null?null:1-t.upPoint,p.blue,axis);
  if(model&&conservative)add("Conservative estimate",t=>isUp?t.upLow:t.upHigh===null?null:1-t.upHigh,p.teal,axis,true);
  add("Book midpoint",t=>isUp?t.upMid:t.downMid,p.market,axis+1,false,1.6);
  if(showAsk)add("Best ask",t=>isUp?t.upAsk:t.downAsk,p.market,axis+1,true,1);
  if(!model)add("Entry ask threshold",()=>.70,p.teal,axis+1,true,1.3);
  const visible=events.filter(e=>e.side===leg&&(provenance==="saved"||e.kind==="request"));
  const markers=visible.filter(e=>e.y!==null && (model||e.kind!=="request"));
  for(const kind of ["request","fill","cancel","exit"]as const) {
   const rows=markers.filter(e=>e.kind===kind);if(!rows.length)continue;
   series.push({name:kind==="request"?"Entry request":kind==="fill"?"Fill":kind==="exit"?"Host sale":"Cancelled remainder",type:"scatter",
    xAxisIndex:index,yAxisIndex:kind==="request"&&model?axis:axis+1,
    data:rows.map(e=>[start+e.t*1000,e.y]),symbol:kind==="request"?"triangle":kind==="cancel"?"rect":"circle",
    symbolSize:kind==="request"?11:9,itemStyle:{color:p.text,borderColor:p.surface,borderWidth:1.5},z:8});
  }
  const lines=visible.filter(e=>e.kind==="request"||e.kind==="arrival");
  const marks:any[]=[{xAxis:start,name:"Window opens",label:{show:false}},{xAxis:start+300000,name:"Window closes",label:{show:false}}];
  for(const event of lines)marks.push({xAxis:start+event.t*1000,name:event.label,label:{show:event.kind==="request",formatter:"Request",color:p.muted,fontSize:10,position:"insideEndTop"}});
  series.push({name:"Window boundaries",type:"line",xAxisIndex:index,yAxisIndex:axis,data:[],silent:true,
   markLine:{silent:true,symbol:"none",animation:false,lineStyle:{color:p.grid,width:1},data:marks},tooltip:{show:false}});
 }
 const min=start-(warmup?60000:0),max=start+315000;
 const baseAxis={type:"time",min,max,axisLine:{show:false},axisTick:{show:false},axisLabel:{color:p.muted,fontSize:10,formatter:(v:number)=>utc(v)},splitLine:{show:false},axisPointer:{show:true,label:{formatter:(v:any)=>utc(v.value)}}};
 const y=(gridIndex:number,price:boolean)=>({type:"value",gridIndex,min:0,max:1,interval:.25,position:price?"right":"left",
  name:price?"USDC/share":model?"P [0,1]":"",nameTextStyle:{color:p.muted,fontSize:10},axisLabel:{color:p.muted,fontSize:10,formatter:(v:number)=>v.toFixed(2)},
  splitLine:{show:!price,lineStyle:{color:p.grid}},axisLine:{show:false},axisTick:{show:false}});
 return {animation:false,backgroundColor:"transparent",textStyle:{fontFamily:"Inter, system-ui, sans-serif"},
  legend:{top:4,left:64,textStyle:{color:p.muted,fontSize:11},itemWidth:18,itemHeight:3,
   data:[...(model?[...(point?["Point estimate P̂"]:[]),...(conservative?["Conservative estimate"]:[])]:["Entry ask threshold"]),"Book midpoint",...(showAsk?["Best ask"]:[])]},
  grid:[{left:62,right:64,top:65,height:172},{left:62,right:64,top:305,height:172}],
  graphic:[{type:"text",left:14,top:46,style:{text:"UP",fill:p.text,fontSize:11,fontWeight:700}},
   {type:"text",left:14,top:286,style:{text:"DOWN",fill:p.text,fontSize:11,fontWeight:700}}],
  tooltip:{trigger:"axis",backgroundColor:p.surface,borderColor:p.grid,textStyle:{color:p.text,fontSize:11},
   axisPointer:{type:"cross"},formatter:(params:any[])=>params.filter(x=>x.value&&x.value[1]!==null).map((x,i)=>(i===0?"<b>"+utc(x.value[0])+" UTC</b><br/>":"")+x.marker+x.seriesName+": "+Number(x.value[1]).toFixed(3)+(x.seriesName.includes("midpoint")||x.seriesName.includes("ask")||x.seriesName.includes("Fill")?" USDC/share":"")).join("<br/>")},
  axisPointer:{link:[{xAxisIndex:[0,1]}]},xAxis:[{...baseAxis,gridIndex:0},{...baseAxis,gridIndex:1,name:"Time · UTC",nameLocation:"middle",nameGap:26,nameTextStyle:{color:p.muted,fontSize:10}}],
  yAxis:[y(0,false),y(0,true),y(1,false),y(1,true)],
  dataZoom:[{type:"inside",xAxisIndex:[0,1],start:range==="entry"?0:range==="hold"?40:0,end:range==="entry"?42:100,filterMode:"none"},
   {type:"slider",xAxisIndex:[0,1],bottom:9,height:19,showDetail:false,borderColor:p.grid,backgroundColor:p.grid,
    fillerColor:p.blue+"18",handleSize:"85%",handleStyle:{color:p.blue,borderWidth:0},textStyle:{color:p.muted},filterMode:"none",
    start:range==="hold"?40:0,end:range==="entry"?42:100}],
  series};
}
export function outcomesOption(rows:WindowRow[],p:Palette,selected:number):Option {
 return {animation:false,grid:{left:40,right:15,top:15,bottom:28},tooltip:{trigger:"axis",backgroundColor:p.surface,borderColor:p.grid,textStyle:{color:p.text}},
  xAxis:{type:"category",data:rows.map(r=>r.time),axisLine:{show:false},axisTick:{show:false},axisLabel:{color:p.muted,fontSize:10}},
  yAxis:{type:"value",axisLabel:{color:p.muted,fontSize:10},splitLine:{lineStyle:{color:p.grid}}},
  series:[{type:"bar",barMaxWidth:28,data:rows.map((r,i)=>({value:r.pnl,itemStyle:{color:r.pnl===null?p.market:!r.qty?p.grid:r.pnl>=0?p.green:p.red,
   opacity:i===selected?1:.65,borderColor:i===selected?p.text:"transparent",borderWidth:i===selected?1:0}}))},
   {name:"Unresolved fill",type:"scatter",symbol:"diamond",symbolSize:9,data:rows.map((r,i)=>r.pnl===null?[i,0]:null).filter(Boolean),itemStyle:{color:p.market}}],
 };
}
export function distributionOption(rows:WindowRow[],p:Palette):Option {
 const known=rows.filter(r=>r.qty>0&&r.pnl!==null),edges=[-6,-3,0,3,6],counts=edges.slice(0,-1).map((e,i)=>known.filter(r=>(r.pnl??0)>=e&&(r.pnl??0)<edges[i+1]).length);
 return {animation:false,grid:{left:35,right:12,top:14,bottom:30},tooltip:{trigger:"axis",backgroundColor:p.surface,borderColor:p.grid,textStyle:{color:p.text}},
  xAxis:{type:"category",data:["−6 to −3","−3 to 0","0 to 3","3 to 6"],axisLabel:{color:p.muted,fontSize:10},axisLine:{show:false},axisTick:{show:false}},
  yAxis:{type:"value",minInterval:1,axisLabel:{color:p.muted,fontSize:10},splitLine:{lineStyle:{color:p.grid}}},
  series:[{type:"bar",data:counts,barCategoryGap:"9%",itemStyle:{color:p.blue,opacity:.75},label:{show:true,position:"top",color:p.muted,fontSize:10}}]};
}

