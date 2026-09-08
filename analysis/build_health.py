#!/usr/bin/env python3
"""Redefine station health against demand instead of against zero.

The old definition was a residual invented for the map: not empty, not ebike-starved,
not full. Under it a station holding one ebike counted as healthy — one rental from
failure at rush hour, and perfectly fine at 3am. It could not tell those apart because
it never looked at demand.

The field uses a target inventory interval instead: healthy means stock is sufficient
for expected demand over the window before you could next intervene. Liang (2024):
"inventory and target inventories are computed such that they maximize the desired
service-level."

ACTION_WINDOW_MIN is set from this system's own observed operator behaviour, not a
guess. Gaps between inferred van events at different stations are bimodal: about five
minutes when a van works consecutive stations in one cluster, and 45-80 minutes between
separate dispatch runs. The second mode is the one that matters — how long before a van
reaches an area it is not already in.
"""

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "analysis.duckdb"
ACTION_WINDOW_MIN = 60


def main():
    con = duckdb.connect(str(DB))
    con.execute("DROP TABLE IF EXISTS station_health")
    con.execute(f"""
    CREATE TABLE station_health AS
    SELECT
        g.bucket, g.station_id, g.num_ebikes_available AS ebikes, g.is_serving,
        d.ebike_demand_per_hour * {ACTION_WINDOW_MIN} / 60.0 AS expected_demand,
        d.low_confidence,
        -- Old: anything not at zero.
        (g.num_ebikes_available > 0 AND g.is_serving)                       AS healthy_old,
        -- New: enough stock to cover expected demand until a van could arrive.
        (g.is_serving AND g.num_ebikes_available
           >= d.ebike_demand_per_hour * {ACTION_WINDOW_MIN} / 60.0)         AS healthy_new,
        -- Stations the old definition called healthy while they were already failing.
        (g.is_serving AND g.num_ebikes_available > 0
           AND g.num_ebikes_available
               < d.ebike_demand_per_hour * {ACTION_WINDOW_MIN} / 60.0)      AS falsely_healthy
    FROM grid_status g
    JOIN demand_baseline d
      ON d.station_id = g.station_id
     AND d.hour = CAST(strftime(g.bucket AT TIME ZONE 'America/Los_Angeles','%H') AS INT)
     AND d.daytype = CASE WHEN dayofweek(g.bucket AT TIME ZONE 'America/Los_Angeles') IN (0,6)
                          THEN 'weekend' ELSE 'weekday' END
    WHERE g.num_bikes_available IS NOT NULL
    """)

    r = con.execute("""SELECT count(*),
        count(*) FILTER (WHERE healthy_old)      * 1.0/count(*),
        count(*) FILTER (WHERE healthy_new)      * 1.0/count(*),
        count(*) FILTER (WHERE falsely_healthy)  * 1.0/count(*)
        FROM station_health WHERE NOT low_confidence""").fetchone()
    print(f"station_health: {r[0]:,} cells (excluding low-confidence demand)\n")
    print(f"  healthy, OLD definition (has any ebike)   {r[1]:>7.1%}")
    print(f"  healthy, NEW definition (covers demand)   {r[2]:>7.1%}")
    print(f"  FALSELY healthy — stocked but not enough  {r[3]:>7.1%}\n")
    print(f"  The old definition overstated health by {r[1]-r[2]:.1%} of all station-hours.\n")

    print("  where the old definition was most wrong (weekday, by hour):")
    for h, old, new in con.execute("""
        SELECT CAST(strftime(bucket AT TIME ZONE 'America/Los_Angeles','%H') AS INT) AS hr,
               count(*) FILTER (WHERE healthy_old)*1.0/count(*),
               count(*) FILTER (WHERE healthy_new)*1.0/count(*)
        FROM station_health
        WHERE NOT low_confidence
          AND dayofweek(bucket AT TIME ZONE 'America/Los_Angeles') NOT IN (0,6)
        GROUP BY 1 ORDER BY 1""").fetchall():
        gap = old - new
        print(f"    {h:02d}:00  old {old:>5.0%}   new {new:>5.0%}   overstated by {gap:>5.0%} {'█'*int(gap*40)}")
    con.close()


if __name__ == "__main__":
    sys.exit(main())
