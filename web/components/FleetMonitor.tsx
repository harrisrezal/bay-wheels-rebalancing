"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ScatterplotLayer } from "@deck.gl/layers";
import { MapboxOverlay } from "@deck.gl/mapbox";
import { Map as BaseMap, useControl } from "react-map-gl/maplibre";
import type { Fleet } from "@/lib/types";
import { STATE } from "@/lib/types";
import Timeline from "./Timeline";
import styles from "./FleetMonitor.module.css";

const BASEMAP = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";

const RGB = {
  healthy:  [ 57,  65,  79] as [number, number, number],
  noEbikes: [ 76, 201, 232] as [number, number, number],
  empty:    [226,  87,  76] as [number, number, number],
  full:     [201, 169,  97] as [number, number, number],
  off:      [ 62,  68,  79] as [number, number, number],
};

const VIEWS = [
  { name: "San Francisco", longitude: -122.428, latitude: 37.771, zoom: 12.4 },
  { name: "East Bay",      longitude: -122.262, latitude: 37.828, zoom: 12.2 },
  { name: "San Jose",      longitude: -121.891, latitude: 37.338, zoom: 12.6 },
  { name: "All",           longitude: -122.19,  latitude: 37.61,  zoom: 9.2  },
];

const STEP_MS = 110;

/**
 * deck.gl as a maplibre control rather than as the container.
 *
 * With <DeckGL><Map/></DeckGL>, deck owns the DOM and only renders its children once
 * it has initialised, so anything that delays or fails deck's setup takes the basemap
 * with it — the map never mounts and you get points floating on the page background.
 * Inverting it makes maplibre the root: the basemap always renders, and deck draws
 * over it as an overlay.
 */
function DeckOverlay(props: { layers: unknown[]; getTooltip?: unknown }) {
  const overlay = useControl(() => new MapboxOverlay({ interleaved: false, ...props } as never));
  (overlay as MapboxOverlay).setProps(props as never);
  return null;
}

export default function FleetMonitor({ data }: { data: Fleet }) {
  const { stations, series, frames, ebikes, vans } = data;
  const [i, setI] = useState(0);
  const [mode, setMode] = useState<"all" | "ebike">("all");
  const [playing, setPlaying] = useState(false);
  const [view, setView] = useState({ ...VIEWS[0], pitch: 0, bearing: 0 });
  const [mapError, setMapError] = useState<string | null>(null);
  const raf = useRef<number | null>(null);
  const last = useRef(0);

  // Van events bucketed by frame, so the map can pulse them as time passes.
  const vansByBucket = useMemo(() => {
    const m = new Map<number, [number, number][]>();
    for (const [b, s, d] of vans) {
      if (!m.has(b)) m.set(b, []);
      m.get(b)!.push([s, d]);
    }
    return m;
  }, [vans]);

  useEffect(() => {
    if (!playing) return;
    const tick = (t: number) => {
      if (t - last.current > STEP_MS) {
        last.current = t;
        setI((p) => (p + 1) % series.length);
      }
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => { if (raf.current) cancelAnimationFrame(raf.current); };
  }, [playing, series.length]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === " ") { e.preventDefault(); setPlaying((p) => !p); }
      if (e.key === "ArrowRight") setI((p) => Math.min(series.length - 1, p + 1));
      if (e.key === "ArrowLeft")  setI((p) => Math.max(0, p - 1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [series.length]);

  const frame = frames[i];
  const eb = ebikes[i];

  const layers = useMemo(() => {
    const pts = stations.map((s, k) => ({ s, k }));
    const colorFor = (code: string): [number, number, number] =>
      code === STATE.NO_EBIKES ? RGB.noEbikes
      : code === STATE.EMPTY   ? RGB.empty
      : code === STATE.FULL    ? RGB.full
      : code === STATE.OFF     ? RGB.off
      : RGB.healthy;

    const visible = mode === "ebike"
      ? pts.filter(({ k }) => frame[k] !== STATE.HEALTHY)
      : pts;

    const stationLayer = new ScatterplotLayer({
      id: `stations-${mode}`,
      data: visible,
      pickable: true,
      radiusUnits: "pixels",
      stroked: false,
      getPosition: ({ s }) => [s.lo, s.la],
      getFillColor: ({ k }) => {
        const c = colorFor(frame[k]);
        const alpha = frame[k] === STATE.HEALTHY || frame[k] === STATE.OFF ? 130 : 235;
        return [...c, alpha] as [number, number, number, number];
      },
      getRadius: ({ k }) => {
        const code = frame[k];
        if (code === STATE.EMPTY || code === STATE.NO_EBIKES) return 5.5;
        // healthy stations scale with how much ebike supply they actually hold
        return 3 + Math.min(eb[k], 12) * 0.28;
      },
      updateTriggers: { getFillColor: [i, mode], getRadius: [i, mode] },
    });

    // Halo behind failing stations, so problems read at low zoom.
    const haloLayer = new ScatterplotLayer({
      id: `halo-${mode}`,
      data: visible.filter(({ k }) =>
        frame[k] === STATE.EMPTY || frame[k] === STATE.NO_EBIKES),
      radiusUnits: "pixels",
      stroked: false,
      getPosition: ({ s }) => [s.lo, s.la],
      getFillColor: ({ k }) =>
        [...colorFor(frame[k]), 34] as [number, number, number, number],
      getRadius: 17,
      updateTriggers: { getFillColor: [i, mode] },
    });

    // Van events pulse for ~4 buckets (20 min) after they happen, then fade out.
    const pulses: { pos: [number, number]; age: number; d: number }[] = [];
    for (let back = 0; back <= 4; back++) {
      for (const [sIdx, d] of vansByBucket.get(i - back) ?? []) {
        const s = stations[sIdx];
        pulses.push({ pos: [s.lo, s.la], age: back, d });
      }
    }
    const vanLayer = new ScatterplotLayer({
      id: "vans",
      data: pulses,
      radiusUnits: "pixels",
      filled: false,
      stroked: true,
      lineWidthUnits: "pixels",
      getLineWidth: (p) => Math.max(0.7, 2.4 - p.age * 0.45),
      getPosition: (p) => p.pos,
      getLineColor: (p) =>
        [255, 255, 255, Math.max(0, 210 - p.age * 48)] as [number, number, number, number],
      getRadius: (p) => 9 + p.age * 8 + Math.min(Math.abs(p.d), 20) * 0.5,
      updateTriggers: { getRadius: [i], getLineColor: [i], getLineWidth: [i] },
    });

    return [haloLayer, stationLayer, vanLayer];
  }, [stations, frame, eb, i, mode, vansByBucket]);

  const d = series[i];
  const when = new Date(d.t);
  const hour = when.getHours();
  const isNight = hour >= 23 || hour < 6;
  const vansNow = (vansByBucket.get(i) ?? []).length;

  const jump = useCallback((v: (typeof VIEWS)[number]) => {
    setView((s) => ({ ...s, longitude: v.longitude, latitude: v.latitude, zoom: v.zoom }));
  }, []);

  return (
    <main className={styles.root}>
      <BaseMap
        reuseMaps
        mapStyle={BASEMAP}
        longitude={view.longitude}
        latitude={view.latitude}
        zoom={view.zoom}
        onMove={(e) => setView((s) => ({ ...s, ...e.viewState }))}
        onError={(e) => setMapError(e.error?.message ?? "basemap failed to load")}
        dragRotate={false}
        style={{ position: "absolute", inset: 0 }}
      >
        <DeckOverlay
          layers={layers}
          getTooltip={({ object }: any) =>
            object?.s && {
              html: `<b>${object.s.n}</b><br/>${eb[object.k]} ebikes · ${object.s.c} docks`,
              style: {
                background: "rgba(18,22,30,.95)", color: "#E9E6E0", fontSize: "12px",
                padding: "7px 10px", borderRadius: "6px", border: "1px solid #242B38",
                fontFamily: "Archivo, sans-serif",
              },
            }
          }
        />
      </BaseMap>

      {mapError && (
        <div className={styles.mapError} role="status">
          <strong>Basemap unavailable</strong>
          <span>{mapError}</span>
          <span className={styles.mapErrorHint}>
            Station data below is unaffected — only the map tiles failed.
          </span>
        </div>
      )}

      <header className={styles.head}>
        <p className={`${styles.eyebrow} mono`}>Bay Wheels · 633 stations · sampled every 2 min</p>
        <h1 className={styles.h1}>
          Most stations look fine.<br />Until you ask for an <em>ebike</em>.
        </h1>
      </header>

      <section className={styles.panel} aria-label="Fleet state">
        <div className={styles.clockRow}>
          <div>
            <div className={`${styles.clock} mono`}>
              {String(when.getHours()).padStart(2, "0")}:
              {String(when.getMinutes()).padStart(2, "0")}
            </div>
            <div className={`${styles.day} mono`}>
              {when.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" })} · PT
            </div>
          </div>
          <button className={styles.play} onClick={() => setPlaying((p) => !p)}>
            {playing ? "❚❚" : "▶"}
          </button>
        </div>

        <div className={styles.seg} role="group" aria-label="Availability measure">
          <button aria-pressed={mode === "all"} onClick={() => setMode("all")}>All bikes</button>
          <button aria-pressed={mode === "ebike"} onClick={() => setMode("ebike")}>Ebikes only</button>
        </div>

        <dl className={styles.stats}>
          <div className={styles.statEbike}>
            <dt>No ebikes</dt><dd className="mono">{d.starved}</dd>
          </div>
          <div className={styles.statEmpty}>
            <dt>No bikes at all</dt><dd className="mono">{d.empty}</dd>
          </div>
          <div className={styles.statFull}>
            <dt>No free dock</dt><dd className="mono">{d.full}</dd>
          </div>
          <div>
            <dt>{mode === "ebike" ? "Ebikes available" : "Bikes available"}</dt>
            <dd className="mono">{(mode === "ebike" ? d.ebikes : d.bikes).toLocaleString("en-US")}</dd>
          </div>
        </dl>

        <div className={`${styles.vanRow} ${isNight ? styles.vanNight : ""} mono`}>
          {isNight
            ? "23:00–06:00 · no rebalancing detected all night"
            : vansNow > 0
              ? `${vansNow} rebalancing van event${vansNow > 1 ? "s" : ""} right now`
              : "no van activity this minute"}
        </div>

        <div className={styles.jump}>
          {VIEWS.map((v) => (
            <button key={v.name} onClick={() => jump(v)} className="mono">{v.name}</button>
          ))}
        </div>
      </section>

      <Timeline series={series} index={i} mode={mode} onScrub={setI} />

      <ul className={`${styles.legend} mono`}>
        <li><i style={{ background: "var(--dim)" }} />Healthy</li>
        <li><i style={{ background: "var(--electric)" }} />No ebikes</li>
        <li><i style={{ background: "var(--alarm)" }} />No bikes at all</li>
        <li><i style={{ background: "var(--brass)" }} />No free docks</li>
        <li><i className={styles.ring} />Van event</li>
      </ul>
    </main>
  );
}
