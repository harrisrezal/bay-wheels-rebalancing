"use client";

import { useEffect, useState } from "react";
import type { Fleet } from "@/lib/types";
import FleetMonitor from "./FleetMonitor";

/**
 * fleet.json is ~600 KB. Importing it would inline the whole dataset into the JS
 * bundle as an object literal, which ships and parses slowly. Serving it from
 * /public and fetching keeps the bundle small, lets the CDN gzip it, and JSON.parse
 * is far faster than evaluating an equivalent literal.
 */
export default function FleetLoader() {
  const [data, setData] = useState<Fleet | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetch("/fleet.json")
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
        return r.json();
      })
      .then((d: Fleet) => alive && setData(d))
      .catch((e: Error) => alive && setError(e.message));
    return () => { alive = false; };
  }, []);

  if (error) {
    return (
      <main style={S.center}>
        <p style={S.msg}>
          Couldn&rsquo;t load the fleet data ({error}).<br />
          <span style={S.sub}>Reload the page to try again.</span>
        </p>
      </main>
    );
  }
  if (!data) {
    return (
      <main style={S.center}>
        <p style={S.msg} className="mono">Loading a day of fleet state…</p>
      </main>
    );
  }
  return <FleetMonitor data={data} />;
}

const S: Record<string, React.CSSProperties> = {
  center: {
    position: "fixed", inset: 0, display: "grid", placeItems: "center",
    background: "var(--ground)",
  },
  msg: { color: "var(--muted)", fontSize: 13, textAlign: "center", lineHeight: 1.6 },
  sub: { color: "var(--faint)", fontSize: 12 },
};
