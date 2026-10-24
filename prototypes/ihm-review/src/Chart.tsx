import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import { LineChart, BarChart, ScatterChart, HeatmapChart } from "echarts/charts";
import { GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, MarkLineComponent, MarkPointComponent, VisualMapComponent, GraphicComponent, AxisPointerComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsCoreOption } from "echarts/core";
echarts.use([LineChart,BarChart,ScatterChart,HeatmapChart,GridComponent,TooltipComponent,LegendComponent,DataZoomComponent,MarkLineComponent,MarkPointComponent,VisualMapComponent,GraphicComponent,AxisPointerComponent,CanvasRenderer]);
export type Option=EChartsCoreOption;
export function Chart({option,height=260,onTime,onCategory,label}:{option:Option;height?:number;onTime?:(ms:number)=>void;onCategory?:(index:number)=>void;label:string}) {
 const ref=useRef<HTMLDivElement>(null),instance=useRef<echarts.ECharts|null>(null),timeRef=useRef(onTime),categoryRef=useRef(onCategory);
 timeRef.current=onTime;categoryRef.current=onCategory;
 useEffect(()=>{
  if(!ref.current)return;
  const chart=echarts.init(ref.current,undefined,{renderer:"canvas"});instance.current=chart;
  chart.on("click",(params:any)=>{if(categoryRef.current&&typeof params.dataIndex==="number")categoryRef.current(params.dataIndex);});
  const click=(event:any)=>{
   if(!timeRef.current)return;
   if(!chart.containPixel({gridIndex:0},[event.offsetX,event.offsetY])&&!chart.containPixel({gridIndex:1},[event.offsetX,event.offsetY]))return;
   const value=chart.convertFromPixel({xAxisIndex:0},event.offsetX);
   if(typeof value==="number")timeRef.current(value);
  };
  chart.getZr().on("click",click);
  const observer=new ResizeObserver(()=>chart.resize());observer.observe(ref.current);
  return()=>{observer.disconnect();chart.getZr().off("click",click);chart.dispose();instance.current=null;};
 },[]);
 useEffect(()=>{instance.current?.setOption(option,{notMerge:true});},[option]);
 return <div className="chart" ref={ref} style={{height}} role="img" aria-label={label}/>;
}

