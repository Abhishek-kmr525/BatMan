"use client";

import { useEffect, useRef } from "react";
import {
  createChart,
  createSeriesMarkers,
  ColorType,
  IChartApi,
  ISeriesApi,
  CandlestickSeries,
  LineSeries,
  HistogramSeries,
  CrosshairMode,
  UTCTimestamp,
  CandlestickData,
  LineData,
  HistogramData,
} from "lightweight-charts";

export type Candle = {
  t: number; // open time ms
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
};

export type Marker = {
  time: number; // unix seconds
  position: "aboveBar" | "belowBar" | "inBar";
  color: string;
  shape: "arrowUp" | "arrowDown" | "circle" | "square";
  text?: string;
};

export type ChartLevel = {
  price: number;
  color: string;
  label: string;
};

type Props = {
  candles: Candle[];
  levels?: ChartLevel[];
  markers?: Marker[];
  height?: number;
  symbol?: string;
  interval?: string;
};

export default function CandleChart({
  candles,
  levels = [],
  markers = [],
  height = 480,
  symbol = "BTCUSDT",
  interval = "5m",
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const levelSeriesRefs = useRef<ISeriesApi<"Line">[]>([]);
  const markersPluginRef = useRef<any | null>(null);

  // Initialize chart once.
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#0b0f18" },
        textColor: "#9fb2d3",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "#1d2a44" },
        horzLines: { color: "#1d2a44" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#22314c" },
      timeScale: { borderColor: "#22314c", timeVisible: true, secondsVisible: false },
      width: containerRef.current.clientWidth,
      height,
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#63ffbe",
      downColor: "#ff8f9a",
      borderUpColor: "#63ffbe",
      borderDownColor: "#ff8f9a",
      wickUpColor: "#63ffbe",
      wickDownColor: "#ff8f9a",
    });

    const volumeSeries = chart.addSeries(HistogramSeries, {
      color: "rgba(139,193,255,0.4)",
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
    });
    chart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.85, bottom: 0 },
    });

    chartRef.current = chart;
    seriesRef.current = candleSeries;
    volumeRef.current = volumeSeries;

    const handleResize = () => {
      if (containerRef.current && chartRef.current) {
        chartRef.current.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener("resize", handleResize);
    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      volumeRef.current = null;
      levelSeriesRefs.current = [];
      markersPluginRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [height]);

  // Update candle + volume data.
  useEffect(() => {
    if (!seriesRef.current || !volumeRef.current) return;
    if (!candles || candles.length === 0) return;
    const cdata: CandlestickData[] = candles.map((k) => ({
      time: Math.floor(k.t / 1000) as UTCTimestamp,
      open: k.o,
      high: k.h,
      low: k.l,
      close: k.c,
    }));
    const vdata: HistogramData[] = candles.map((k) => ({
      time: Math.floor(k.t / 1000) as UTCTimestamp,
      value: k.v,
      color: k.c >= k.o ? "rgba(99,255,190,0.45)" : "rgba(255,143,154,0.45)",
    }));
    seriesRef.current.setData(cdata);
    volumeRef.current.setData(vdata);
    // Add markers via v5 plugin API.
    const mk = (markers || []).map((m) => ({
      time: m.time as UTCTimestamp,
      position: m.position,
      color: m.color,
      shape: m.shape,
      text: m.text || "",
    }));
    if (markersPluginRef.current) {
      markersPluginRef.current.setMarkers(mk);
    } else if (mk.length > 0) {
      markersPluginRef.current = createSeriesMarkers(seriesRef.current, mk);
    }
  }, [candles, markers]);

  // Update price levels (SL/TP horizontal lines).
  useEffect(() => {
    if (!chartRef.current || !seriesRef.current) return;
    // Remove old level lines.
    for (const s of levelSeriesRefs.current) {
      try {
        chartRef.current.removeSeries(s);
      } catch {}
    }
    levelSeriesRefs.current = [];
    if (!levels || levels.length === 0) return;
    if (!candles || candles.length === 0) return;
    const startTime = Math.floor(candles[0].t / 1000) as UTCTimestamp;
    const endTime = Math.floor(candles[candles.length - 1].t / 1000) as UTCTimestamp;
    for (const lvl of levels) {
      const line = chartRef.current.addSeries(LineSeries, {
        color: lvl.color,
        lineWidth: 1,
        lineStyle: 2, // dashed
        title: lvl.label,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      const lineData: LineData[] = [
        { time: startTime, value: lvl.price },
        { time: endTime, value: lvl.price },
      ];
      line.setData(lineData);
      levelSeriesRefs.current.push(line);
    }
  }, [levels, candles]);

  return (
    <div style={{ position: "relative", width: "100%", height }}>
      <div
        style={{
          position: "absolute",
          top: 8,
          left: 10,
          zIndex: 2,
          color: "#8bc1ff",
          fontSize: 12,
          fontWeight: 600,
          textShadow: "0 0 4px rgba(11,15,24,0.95)",
        }}
      >
        {symbol} · {interval}
      </div>
      <div ref={containerRef} style={{ width: "100%", height }} />
    </div>
  );
}
