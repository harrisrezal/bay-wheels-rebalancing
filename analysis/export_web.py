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

# The viz shows a week; at the full 5-minute grid that is ~2,000 frames and a file
# several MB. A human scrubbing a week does not need 5-minute resolution, so every
# other bucket is kept. The analysis tables stay at full resolution.
STRIDE = 2


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

    # Joined to demand so the page can show health RELATIVE TO DEMAND, which is the
    # Phase 1 finding: a station can be stocked and still be failing.
    rows = con.execute("""
        SELECT g.bucket, g.station_id, g.num_bikes_available, g.num_ebikes_available,
               g.num_docks_available, g.is_serving, g.is_returning,
               coalesce(d.ebike_demand_per_hour, 0) AS demand,
               coalesce(d.low_confidence, TRUE)     AS lowconf
        FROM grid_status g
        LEFT JOIN demand_baseline d
          ON d.station_id = g.station_id
         AND d.hour = CAST(strftime(g.bucket AT TIME ZONE 'America/Los_Angeles','%H') AS INT)
         AND d.daytype = CASE WHEN dayofweek(g.bucket AT TIME ZONE 'America/Los_Angeles') IN (0,6)
                              THEN 'weekend' ELSE 'weekday' END
        WHERE g.num_bikes_available IS NOT NULL
        ORDER BY g.bucket""").fetchall()
    from collections import defaultdict
    byb = defaultdict(list)
    for r in rows:
        byb[r[0]].append(r)
    buckets = sorted(byb)[::STRIDE]
    bpos = {b: i for i, b in enumerate(buckets)}

    series, frames, dframes, ebk = [], [], [], []
    for b in buckets:
        state = ["9"] * len(st)
        dstate = ["9"] * len(st)
        ecount = [0] * len(st)
        tb = te = starved = empty = full = short = 0
        for _, sid, bikes, e, docks, serving, returning, demand, lowconf in byb[b]:
            i = idx.get(sid)
            tb += bikes or 0
            te += e or 0
            if e == 0 and serving:      starved += 1
            if bikes == 0 and serving:  empty += 1
            if docks == 0 and returning == 1: full += 1
            # Falsely healthy: stocked, but not enough to cover an hour of demand.
            insufficient = serving and not lowconf and 0 < (e or 0) < demand
            if insufficient: short += 1
            if i is None:
                continue
            ecount[i] = min(e or 0, 35)
            state[i] = ("4" if not serving else "2" if bikes == 0
                        else "1" if e == 0 else "3" if docks == 0 else "0")
            # 5 = stocked but short of demand; 1 = no ebikes at all under real demand
            dstate[i] = ("4" if not serving else
                         "1" if (e or 0) == 0 and demand > 0.1 else
                         "5" if insufficient else "0")
        series.append({"t": b.isoformat(), "bikes": tb, "ebikes": te,
                       "starved": starved, "empty": empty, "full": full, "short": short})
        frames.append("".join(state))
        dframes.append("".join(dstate))
        ebk.append(ecount)

    # Van events. Pickups and dropoffs are observed separately; we never see that a
    # pickup at A and a dropoff at B were the same vehicle, so no routes are inferred.
    import bisect
    vans = []
    for b, sid, d in con.execute("""
            SELECT bucket, station_id, d_bikes FROM station_deltas
            WHERE event_class LIKE 'inferred_truck%' ORDER BY bucket""").fetchall():
        if sid not in idx:
            continue
        j = bisect.bisect_left(buckets, b)          # nearest retained frame
        if j >= len(buckets):
            j = len(buckets) - 1
        if j > 0 and abs((buckets[j-1] - b).total_seconds()) < abs((buckets[j] - b).total_seconds()):
            j -= 1
        vans.append([j, idx[sid], int(d)])

    out = {
        "stations": [{"n": s[1], "la": round(s[2], 5), "lo": round(s[3], 5),
                      "c": s[4], "z": zone(s[5], s[2], s[3])} for s in st],
        "zoneNames": ["San Francisco", "East Bay", "San Jose"],
        "series": series, "frames": frames, "dframes": dframes,
        "ebikes": ebk, "vans": vans,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(OUT, "w"), separators=(",", ":"))

    hrs = len(series) * 5 * STRIDE / 60
    print(f"{OUT.relative_to(ROOT)}  {OUT.stat().st_size/1024:.0f} KB")
    print(f"  {len(series)} buckets ({hrs:.1f}h) x {len(st)} stations")
    print(f"  {len(vans)} van events")
    print(f"  window {series[0]['t']} → {series[-1]['t']}")
    con.close()


if __name__ == "__main__":
    sys.exit(main())
