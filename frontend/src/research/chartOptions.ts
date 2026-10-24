// Ported from the approved mockup (prototypes/ihm-review/src/options.ts).
// Same palette, axes, legend and markers; times are the saved replay clock in UTC.
import type { Option } from "../design/Chart";
import { utc, type EventRow, type ResultRow, type Tick, asNumber, windowLabel } from "./model";

export type Palette = { text: string; muted: string; grid: string; blue: string; teal: string; market: string; surface: string; green: string; red: string };
export const light: Palette = { text: "#202838", muted: "#7c8596", grid: "#e9edf3", blue: "#4a65dc", teal: "#27867b", market: "#747d8d", surface: "#ffffff", green: "#28826b", red: "#bd5e62" };
export const dark: Palette = { text: "#e2e7f0", muted: "#9ba5b6", grid: "#303746", blue: "#8b9df5", teal: "#68b7aa", market: "#b2bac8", surface: "#191e28", green: "#78b99d", red: "#d78c92" };
export const paletteFor = (theme: string) => (theme === "dark" ? dark : light);

// A host-skipped row has no usable inputs. A gap longer than 1.5 s is drawn as a gap.
function linePoints(ticks: Tick[], value: (t: Tick) => number | null) {
  const out: (number | null)[][] = [];
  let last: number | undefined;
  for (const tick of ticks) {
    if (tick.action === "HOST_SKIPPED") { if (last !== undefined) { out.push([last + 1, null]); last = undefined; } continue; }
    const v = value(tick);
    if (v === null) { out.push([tick.ms, null]); last = undefined; continue; }
    if (last !== undefined && tick.ms - last > 1500) out.push([last + 1, null]);
    out.push([tick.ms, v]);
    last = tick.ms;
  }
  return out;
}

export type TimelineInput = {
  ticks: Tick[];
  events: EventRow[];
  start: number;
  end: number;
  rangeStart: number | null;
  warmup: boolean;
  palette: Palette;
  range: string;
  selected: number | null;
};

export function timelineBounds({ ticks, events, start, end, rangeStart, warmup, range }: Pick<TimelineInput, "ticks" | "events" | "start" | "end" | "rangeStart" | "warmup" | "range">) {
  const min = warmup && rangeStart != null ? rangeStart : start, max = end + 15000;
  const request = events.find((e) => e.kind === "request" && e.ms != null)?.ms ?? null;
  const lastTick = ticks.length ? ticks[ticks.length - 1].ms : max;
  if (range === "entry" && request != null) return { min, max, from: Math.max(min, request - 40000), to: Math.min(max, request + 60000) };
  if (range === "hold" && request != null) return { min, max, from: request, to: Math.min(max, Math.max(lastTick, request + 1000)) };
  return { min, max, from: min, to: max };
}

export function timelineOption(input: TimelineInput): Option {
  const { ticks, events, start, end, palette: p, selected } = input;
  const bounds = timelineBounds(input);
  const series: any[] = [];
  for (const [leg, index] of [["UP", 0], ["DOWN", 1]] as const) {
    const isUp = leg === "UP", axis = index * 2;
    const add = (name: string, value: (t: Tick) => number | null, color: string, yAxis: number, dash = false, width = 2) => {
      series.push({ name, type: "line", xAxisIndex: index, yAxisIndex: yAxis, data: linePoints(ticks, value), showSymbol: false, connectNulls: false, clip: true, lineStyle: { color, width, type: dash ? "dashed" : "solid" }, itemStyle: { color }, emphasis: { focus: "series" }, animation: false });
    };
    // Binary model: P(Down) = 1 − P(Up); the conservative Down estimate is 1 − upper P(Up).
    add("Point estimate P̂", (t) => (isUp ? t.upPoint : t.upPoint === null ? null : 1 - t.upPoint), p.blue, axis);
    add("Conservative estimate", (t) => (isUp ? t.upLow : t.upHigh === null ? null : 1 - t.upHigh), p.teal, axis, true);
    add("Book midpoint", (t) => (isUp ? t.upMid : t.downMid), p.market, axis + 1, false, 1.6);
    add("Best ask", (t) => (isUp ? t.upAsk : t.downAsk), p.market, axis + 1, true, 1);
    const visible = events.filter((e) => e.side === leg && e.ms != null);
    for (const kind of ["request", "fill", "cancel", "exit"] as const) {
      const rows = visible.filter((e) => e.kind === kind && e.y !== null);
      if (!rows.length) continue;
      series.push({ name: kind === "request" ? "Entry request" : kind === "fill" ? "Fill" : kind === "exit" ? "Host sale" : "Cancelled remainder", type: "scatter", xAxisIndex: index, yAxisIndex: kind === "request" ? axis : axis + 1, data: rows.map((e) => [e.ms, e.y]), symbol: kind === "request" ? "triangle" : kind === "cancel" ? "rect" : "circle", symbolSize: kind === "request" ? 11 : 9, itemStyle: { color: p.text, borderColor: p.surface, borderWidth: 1.5 }, z: 8 });
    }
    const lines = visible.filter((e) => e.kind === "request" || e.kind === "arrival");
    const marks: any[] = [{ xAxis: start, name: "Window opens", label: { show: false } }, { xAxis: end, name: "Window closes", label: { show: false } }];
    for (const event of lines) marks.push({ xAxis: event.ms, name: event.label, label: { show: event.kind === "request", formatter: "Request", color: p.muted, fontSize: 10, position: "insideEndTop" } });
    series.push({ name: "Window boundaries", type: "line", xAxisIndex: index, yAxisIndex: axis, data: [], silent: true, markLine: { silent: true, symbol: "none", animation: false, lineStyle: { color: p.grid, width: 1 }, data: marks }, tooltip: { show: false } });
    if (selected != null) series.push({ name: "Selected time", type: "line", xAxisIndex: index, yAxisIndex: axis, data: [], silent: true, markLine: { symbol: "none", label: { show: false }, lineStyle: { color: p.blue, type: "solid", width: 1 }, data: [{ xAxis: selected }] }, tooltip: { show: false } });
  }
  const baseAxis = { type: "time", min: bounds.min, max: bounds.max, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: p.muted, fontSize: 10, formatter: (v: number) => utc(v), hideOverlap: true }, splitLine: { show: false }, axisPointer: { show: true, label: { formatter: (v: any) => utc(v.value) } } };
  const y = (gridIndex: number, price: boolean) => ({ type: "value", gridIndex, min: 0, max: 1, interval: 0.25, position: price ? "right" : "left", name: price ? "USDC/share" : "P [0,1]", nameTextStyle: { color: p.muted, fontSize: 10 }, axisLabel: { color: p.muted, fontSize: 10, formatter: (v: number) => v.toFixed(2) }, splitLine: { show: !price, lineStyle: { color: p.grid } }, axisLine: { show: false }, axisTick: { show: false } });
  const span = bounds.max - bounds.min || 1;
  const pct = (v: number) => Math.max(0, Math.min(100, ((v - bounds.min) / span) * 100));
  return {
    animation: false, backgroundColor: "transparent", textStyle: { fontFamily: "Inter, system-ui, sans-serif" },
    legend: { top: 4, left: 64, textStyle: { color: p.muted, fontSize: 11 }, itemWidth: 18, itemHeight: 3, data: ["Point estimate P̂", "Conservative estimate", "Book midpoint", "Best ask"] },
    grid: [{ left: 62, right: 64, top: 65, height: 172 }, { left: 62, right: 64, top: 305, height: 172 }],
    graphic: [{ type: "text", left: 14, top: 46, style: { text: "UP", fill: p.text, fontSize: 11, fontWeight: 700 } }, { type: "text", left: 14, top: 286, style: { text: "DOWN", fill: p.text, fontSize: 11, fontWeight: 700 } }],
    tooltip: { trigger: "axis", backgroundColor: p.surface, borderColor: p.grid, textStyle: { color: p.text, fontSize: 11 }, axisPointer: { type: "cross" }, formatter: (params: any[]) => params.filter((x) => x.value && x.value[1] !== null && x.value[1] !== undefined).map((x, i) => (i === 0 ? "<b>" + utc(x.value[0]) + " UTC</b><br/>" : "") + x.marker + x.seriesName + ": " + Number(x.value[1]).toFixed(3) + (/midpoint|ask|Fill|sale|Cancel/.test(x.seriesName) ? " USDC/share" : "")).join("<br/>") },
    axisPointer: { link: [{ xAxisIndex: [0, 1] }] },
    xAxis: [{ ...baseAxis, gridIndex: 0 }, { ...baseAxis, gridIndex: 1, name: "Time · UTC", nameLocation: "middle", nameGap: 26, nameTextStyle: { color: p.muted, fontSize: 10 } }],
    yAxis: [y(0, false), y(0, true), y(1, false), y(1, true)],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1], start: pct(bounds.from), end: pct(bounds.to), filterMode: "none" },
      { type: "slider", xAxisIndex: [0, 1], bottom: 9, height: 19, showDetail: false, borderColor: p.grid, backgroundColor: p.grid, fillerColor: p.blue + "18", handleSize: "85%", handleStyle: { color: p.blue, borderWidth: 0 }, textStyle: { color: p.muted }, filterMode: "none", start: pct(bounds.from), end: pct(bounds.to) },
    ],
    series,
  };
}

// Window outcomes. Qualified numeric P&L is a bar. Every other window keeps a baseline
// marker so it stays discoverable; the marker is not a zero and is not summed.
export function outcomesOption(rows: ResultRow[], p: Palette, selected: number): Option {
  const pnl = rows.map((r) => (r.qualified === true && r.filled === true ? asNumber(r.realized_pnl) : null));
  const marker = (i: number, kind: string) => (pnl[i] === null && kind === markerKind(rows[i]) ? [i, 0] : null);
  const known = pnl.filter((v): v is number => v !== null);
  const extent = Math.max(1, ...known.map((v) => Math.abs(v)));
  const markers = [
    { name: "No trade · qualified", kind: "notrade", symbol: "circle", color: p.grid, border: p.market },
    { name: "Request · no fill", kind: "nofill", symbol: "triangle", color: p.market, border: p.market },
    { name: "Unqualified / unknown · not zero", kind: "excluded", symbol: "diamond", color: p.market, border: p.market },
  ];
  return {
    animation: false,
    grid: { left: 44, right: 15, top: 34, bottom: 28 },
    legend: { top: 0, right: 8, textStyle: { color: p.muted, fontSize: 10 }, itemWidth: 10, itemHeight: 8, data: ["Qualified simulated P&L", ...markers.map((m) => m.name)] },
    tooltip: {
      trigger: "item", backgroundColor: p.surface, borderColor: p.grid, textStyle: { color: p.text, fontSize: 11 },
      formatter: (item: any) => {
        const row = rows[Array.isArray(item.value) ? item.value[0] : item.dataIndex];
        if (!row) return "";
        const head = `<b>${windowLabel(row)} UTC</b><br/>`;
        if (item.seriesType === "bar") return head + `${Number(item.value).toFixed(2)} USDC · qualified`;
        return head + item.seriesName + "<br/>Excluded from the qualified total";
      },
    },
    xAxis: { type: "category", data: rows.map((r) => windowLabel(r).slice(0, 5)), axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: p.muted, fontSize: 10, hideOverlap: true } },
    yAxis: { type: "value", min: -extent, max: extent, name: "USDC", nameTextStyle: { color: p.muted, fontSize: 10 }, axisLabel: { color: p.muted, fontSize: 10, formatter: (v: number) => v.toFixed(1) }, splitLine: { lineStyle: { color: p.grid } } },
    series: [
      { name: "Qualified simulated P&L", type: "bar", barMaxWidth: 28, itemStyle: { color: p.green }, data: pnl.map((v, i) => ({ value: v, itemStyle: { color: v === null ? p.market : v >= 0 ? p.green : p.red, opacity: i === selected ? 1 : 0.65, borderColor: i === selected ? p.text : "transparent", borderWidth: i === selected ? 1 : 0 } })) },
      ...markers.map((m) => ({ name: m.name, type: "scatter", symbol: m.symbol, symbolSize: 9, itemStyle: { color: m.color, borderColor: m.border, borderWidth: 1 }, data: rows.map((_r, i) => marker(i, m.kind)).filter(Boolean) })),
    ],
  };
}

export function markerKind(row: ResultRow): string {
  if (row.qualified !== true) return "excluded";
  if (row.entered !== true) return "notrade";
  if (row.filled !== true) return "nofill";
  return "excluded";
}
