#!/usr/bin/env python3
"""Generate the static dataset for the web visualisation.

Everything the page needs is precomputed here and shipped as one JSON file: no API,
no database connection from the browser, no Supabase egress, and the page loads
instantly. Re-run before deploying to refresh the window.
"""

import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "public" / "fleet.json"


def zone(region, lat, lon):
    if region in ("12", "13", "14") or (lon > -122.35 and lat > 37.7):
        return 1                      # East Bay
    if region == "5" or lat < 37.5:
        return 2                      # San Jose
    return 0                          # San Francisco


def main():
    con = duckdb.connect(str(ROOT / "data" / "analysis.duckdb"))

    st = con.execute("""SELECT DISTINCT ON (station_id) station_id, name, lat, lon,
                               capacity, region_id
                        FROM station_info WHERE lat IS NOT NULL
                        ORDER BY station_id, fetched_on DESC""").fetchall()
    idx = {s[0]: i for i, s in enumerate(st)}

    rows = con.execute("""SELECT bucket, station_id, num_bikes_available, num_ebikes_available,
                                 num_docks_available, is_serving, is_returning
                          FROM grid_status WHERE num_bikes_available IS NOT NULL
                          ORDER BY bucket""").fetchall()
    from collections import defaultdict
    byb = defaultdict(list)
    for r in rows:
        byb[r[0]].append(r)
    buckets = sorted(byb)
    bpos = {b: i for i, b in enumerate(buckets)}

    series, frames, ebk = [], [], []
    for b in buckets:
        state = ["9"] * len(st)
        ecount = [0] * len(st)
        tb = te = starved = empty = full = 0
        for _, sid, bikes, e, docks, serving, returning in byb[b]:
            i = idx.get(sid)
            tb += bikes or 0
            te += e or 0
            if e == 0 and serving:      starved += 1
            if bikes == 0 and serving:  empty += 1
            if docks == 0 and returning == 1: full += 1
            if i is None:
                continue
            ecount[i] = min(e or 0, 35)
            state[i] = ("4" if not serving else "2" if bikes == 0
                        else "1" if e == 0 else "3" if docks == 0 else "0")
        series.append({"t": b.isoformat(), "bikes": tb, "ebikes": te,
                       "starved": starved, "empty": empty, "full": full})
        frames.append("".join(state))
        ebk.append(ecount)

    # Van events. Pickups and dropoffs are observed separately; we never see that a
    # pickup at A and a dropoff at B were the same vehicle, so no routes are inferred.
    vans = []
    for b, sid, d in con.execute("""
            SELECT bucket, station_id, d_bikes FROM station_deltas
            WHERE event_class LIKE 'inferred_truck%' ORDER BY bucket""").fetchall():
        if b in bpos and sid in idx:
            vans.append([bpos[b], idx[sid], int(d)])

    out = {
        "stations": [{"n": s[1], "la": round(s[2], 5), "lo": round(s[3], 5),
                      "c": s[4], "z": zone(s[5], s[2], s[3])} for s in st],
        "zoneNames": ["San Francisco", "East Bay", "San Jose"],
        "series": series, "frames": frames, "ebikes": ebk, "vans": vans,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(OUT, "w"), separators=(",", ":"))

    hrs = len(series) * 5 / 60
    print(f"{OUT.relative_to(ROOT)}  {OUT.stat().st_size/1024:.0f} KB")
    print(f"  {len(series)} buckets ({hrs:.1f}h) x {len(st)} stations")
    print(f"  {len(vans)} van events")
    print(f"  window {series[0]['t']} → {series[-1]['t']}")
    con.close()


if __name__ == "__main__":
    sys.exit(main())
