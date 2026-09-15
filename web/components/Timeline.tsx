"use client";

import { useCallback, useEffect, useRef } from "react";
import type { Point } from "@/lib/types";
import styles from "./Timeline.module.css";

const CSSVar = (n: string) =>
  getComputedStyle(document.documentElement).getPropertyValue(n).trim();

export default function Timeline({
  series, index, mode, onScrub,
}: {
  series: Point[];
  index: number;
  mode: "all" | "ebike" | "demand" | "risk";
  onScrub: (i: number) => void;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  const dragging = useRef(false);

  const draw = useCallback(() => {
    const c = ref.current;
    if (!c) return;
    const dpr = window.devicePixelRatio || 1;
    const W = c.clientWidth, H = c.clientHeight;
    c.width = W * dpr; c.height = H * dpr;
    const x = c.getContext("2d");
    if (!x) return;
    x.setTransform(dpr, 0, 0, dpr, 0, 0);
    x.clearRect(0, 0, W, H);

    const pad = { t: 14, b: 22, l: 0, r: 0 };
    const ih = H - pad.t - pad.b;
    const vals = series.map((d) =>
      mode === "risk" ? (d.doomed ?? 0)
      : mode === "demand" ? (d.short ?? 0)
      : mode === "ebike" ? d.starved : d.empty);
    const mx = Math.max(...vals) * 1.15 || 1;
    const X = (k: number) => (k * W) / (series.length - 1);
    const Y = (v: number) => pad.t + ih - (v / mx) * ih;
    const col = mode === "risk" ? CSSVar("--alarm")
      : mode === "demand" ? CSSVar("--warn")
      : mode === "ebike" ? CSSVar("--electric") : CSSVar("--alarm");

    // Overnight band: the window where no rebalancing was detected at all.
    x.fillStyle = "rgba(76,201,232,.055)";
    let bandStart: number | null = null;
    series.forEach((d, k) => {
      const h = new Date(d.t).getHours();
      const night = h >= 23 || h < 6;
      if (night && bandStart === null) bandStart = k;
      if ((!night || k === series.length - 1) && bandStart !== null) {
        x.fillRect(X(bandStart), pad.t, X(k) - X(bandStart), ih);
        bandStart = null;
      }
    });

    const g = x.createLinearGradient(0, pad.t, 0, pad.t + ih);
    g.addColorStop(0, col + "70"); g.addColorStop(1, col + "06");
    x.beginPath(); x.moveTo(X(0), pad.t + ih);
    vals.forEach((v, k) => x.lineTo(X(k), Y(v)));
    x.lineTo(X(series.length - 1), pad.t + ih); x.closePath();
    x.fillStyle = g; x.fill();

    x.beginPath();
    vals.forEach((v, k) => (k ? x.lineTo(X(k), Y(v)) : x.moveTo(X(k), Y(v))));
    x.strokeStyle = col; x.lineWidth = 1.7; x.stroke();

    x.fillStyle = CSSVar("--faint");
    x.font = '10px "IBM Plex Mono", ui-monospace, monospace';
    series.forEach((d, k) => {
      const t = new Date(d.t);
      if (t.getMinutes() === 0 && t.getHours() % 3 === 0) {
        x.globalAlpha = 0.35;
        x.beginPath(); x.moveTo(X(k), pad.t); x.lineTo(X(k), pad.t + ih);
        x.strokeStyle = CSSVar("--line"); x.lineWidth = 1; x.stroke();
        x.globalAlpha = 1;
        x.fillText(String(t.getHours()).padStart(2, "0"), X(k) - 6, H - 7);
      }
    });

    x.beginPath(); x.moveTo(X(index), pad.t - 5); x.lineTo(X(index), pad.t + ih);
    x.strokeStyle = CSSVar("--ink"); x.lineWidth = 1.4; x.stroke();
    x.beginPath(); x.arc(X(index), Y(vals[index]), 4.2, 0, Math.PI * 2);
    x.fillStyle = CSSVar("--ground"); x.fill();
    x.strokeStyle = col; x.lineWidth = 2.4; x.stroke();
  }, [series, index, mode]);

  useEffect(() => { draw(); }, [draw]);
  useEffect(() => {
    const r = () => draw();
    window.addEventListener("resize", r);
    return () => window.removeEventListener("resize", r);
  }, [draw]);

  const scrub = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const c = ref.current; if (!c) return;
    const r = c.getBoundingClientRect();
    const k = Math.round(((e.clientX - r.left) / r.width) * (series.length - 1));
    onScrub(Math.max(0, Math.min(series.length - 1, k)));
  };

  return (
    <div className={styles.wrap}>
      <div className={`${styles.head} mono`}>
        <span>{mode === "risk" ? "Stations with under an hour of stock left"
          : mode === "demand" ? "Stations stocked but short of demand"
          : mode === "ebike" ? "Stations with no ebikes" : "Stations with no bikes at all"}</span>
        <span className={styles.hint}>shaded 23:00–06:00 · no rebalancing detected · drag to scrub</span>
      </div>
      <canvas
        ref={ref}
        className={styles.canvas}
        onPointerDown={(e) => { dragging.current = true; e.currentTarget.setPointerCapture(e.pointerId); scrub(e); }}
        onPointerMove={(e) => { if (dragging.current) scrub(e); }}
        onPointerUp={() => { dragging.current = false; }}
      />
    </div>
  );
}
