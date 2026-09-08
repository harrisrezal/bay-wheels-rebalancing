#!/usr/bin/env python3
"""Validate demand_baseline against the trip archive.

demand_baseline is built entirely from one week of September GBFS snapshots. The trip
CSVs are three months of May-July records — a completely independent measurement, from
a different source, over a different period, with no supply information at all.

They should not agree on absolute volume: different months, and the trip files count
only ~85% of rides (the rest are dockless with no station attribution). But if the
method works they must agree on SHAPE — which stations are busy, and what the day looks
like. If two independent sources disagree on that, the estimate is wrong and it needs
to be said rather than shipped.
"""

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "analysis.duckdb"


def main():
    con = duckdb.connect(str(DB))

    con.execute("""
    CREATE OR REPLACE TEMP VIEW cmp AS
    WITH latest AS (
        SELECT DISTINCT ON (station_id) station_id, short_name
        FROM station_info WHERE short_name IS NOT NULL
        ORDER BY station_id, fetched_on DESC),
    grid AS (
        SELECT d.station_id, avg(d.ebike_demand_per_hour) AS est
        FROM demand_baseline d WHERE NOT d.low_confidence AND d.daytype='weekday'
        GROUP BY 1),
    trip AS (   -- weekday electric departures per station-hour, May-Jul
        SELECT start_station_id AS short_name, count(*)/(65.0*24) AS obs
        FROM trips
        WHERE start_station_id IS NOT NULL AND rideable_type='electric_bike'
          AND dayofweek(started_at) NOT IN (0,6)
        GROUP BY 1)
    SELECT l.station_id, l.short_name, g.est, t.obs
    FROM latest l JOIN grid g USING (station_id) JOIN trip t USING (short_name)
    """)

    n = con.execute("SELECT count(*) FROM cmp").fetchone()[0]
    pear, spear = con.execute("""
        SELECT corr(est, obs),
               corr(re, ro) FROM (
          SELECT est, obs,
                 rank() OVER (ORDER BY est) re, rank() OVER (ORDER BY obs) ro FROM cmp)
    """).fetchone()
    print("=== STATION-LEVEL AGREEMENT (independent sources) ===")
    print(f"  stations compared          {n}")
    print(f"  Pearson  (levels)          {pear:+.3f}")
    print(f"  Spearman (rank order)      {spear:+.3f}   <- the one that matters\n")

    print("  busiest stations by each method (top 8):")
    rows = con.execute("""
        SELECT short_name,
               rank() OVER (ORDER BY est DESC) r_grid,
               rank() OVER (ORDER BY obs DESC) r_trip
        FROM cmp QUALIFY r_grid <= 8 ORDER BY r_grid""").fetchall()
    print(f"    {'station':<12} {'rank (Sept grid)':>17} {'rank (May-Jul trips)':>21}")
    for sn, rg, rt in rows:
        flag = "" if abs(rg-rt) <= 20 else "   <-- disagree"
        print(f"    {sn:<12} {rg:>17} {rt:>21}{flag}")

    print("\n=== HOURLY SHAPE AGREEMENT ===")
    rows = con.execute("""
    WITH g AS (SELECT hour AS hr, avg(ebike_demand_per_hour) AS v FROM demand_baseline
               WHERE NOT low_confidence AND daytype='weekday' GROUP BY 1),
         t AS (SELECT CAST(strftime(started_at,'%H') AS INT) AS hr, count(*) AS v
               FROM trips WHERE rideable_type='electric_bike'
                 AND dayofweek(started_at) NOT IN (0,6) GROUP BY 1)
    SELECT g.hr, g.v / (SELECT max(v) FROM g), t.v * 1.0 / (SELECT max(v) FROM t)
    FROM g JOIN t USING (hr) ORDER BY g.hr""").fetchall()
    print(f"    {'hr':>3}  {'Sept grid (normalised)':<28} {'May-Jul trips':<28}")
    for h, gv, tv in rows:
        print(f"    {h:02d}  {'█'*int(gv*26):<28} {'█'*int(tv*26):<28}")
    shape = con.execute("""
    WITH g AS (SELECT hour AS hr, avg(ebike_demand_per_hour) AS v FROM demand_baseline
               WHERE NOT low_confidence AND daytype='weekday' GROUP BY 1),
         t AS (SELECT CAST(strftime(started_at,'%H') AS INT) AS hr, count(*) AS v
               FROM trips WHERE rideable_type='electric_bike'
                 AND dayofweek(started_at) NOT IN (0,6) GROUP BY 1)
    SELECT corr(g.v, t.v) FROM g JOIN t USING (hr)""").fetchone()[0]
    print(f"\n  hourly-shape correlation   {shape:+.3f}")
    con.close()


if __name__ == "__main__":
    sys.exit(main())
