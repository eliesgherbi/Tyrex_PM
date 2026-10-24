// The approved mockup Chart, with one correction: a click on a filtered marker series
// reports its category, not its position in the filtered data.
import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import { LineChart, BarChart, ScatterChart } from "echarts/charts";
import { GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, MarkLineComponent, GraphicComponent, AxisPointerComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsCoreOption } from "echarts/core";

echarts.use([LineChart, BarChart, ScatterChart, GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, MarkLineComponent, GraphicComponent, AxisPointerComponent, CanvasRenderer]);

export type Option = EChartsCoreOption;

export function Chart({ option, height = 260, onTime, onCategory, label }: { option: Option; height?: number; onTime?: (ms: number) => void; onCategory?: (index: number) => void; label: string }) {
  const ref = useRef<HTMLDivElement>(null), instance = useRef<echarts.ECharts | null>(null), timeRef = useRef(onTime), categoryRef = useRef(onCategory);
  timeRef.current = onTime;
  categoryRef.current = onCategory;
  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current, undefined, { renderer: "canvas" });
    instance.current = chart;
    chart.on("click", (params: any) => {
      if (!categoryRef.current) return;
      const category = Array.isArray(params.value) ? Number(params.value[0]) : params.dataIndex;
      if (typeof category === "number" && Number.isFinite(category)) categoryRef.current(category);
    });
    const click = (event: any) => {
      if (!timeRef.current) return;
      if (!chart.containPixel({ gridIndex: 0 }, [event.offsetX, event.offsetY]) && !chart.containPixel({ gridIndex: 1 }, [event.offsetX, event.offsetY])) return;
      const value = chart.convertFromPixel({ xAxisIndex: 0 }, event.offsetX);
      if (typeof value === "number") timeRef.current(value);
    };
    chart.getZr().on("click", click);
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => { observer.disconnect(); chart.getZr().off("click", click); chart.dispose(); instance.current = null; };
  }, []);
  useEffect(() => { instance.current?.setOption(option, { notMerge: true }); }, [option]);
  return <div className="chart" ref={ref} style={{ height }} role="img" aria-label={label} />;
}
