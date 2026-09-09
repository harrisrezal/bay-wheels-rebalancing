#!/usr/bin/env python3
"""Recalibrate van detection: replace a fixed count with a surprise threshold.

THE DEFECT. The original rule was "a change of >= 5 bikes in one interval is a van".
That number silently depends on two things it never recorded: the length of the
interval, and how busy the station is. At Montgomery St BART, 5 departures in 5 minutes
is a 1-in-61,705 event; in 20 minutes it is 1-in-126. Same station, same five people,
490x different meaning. Downsampling the collection exposed it: "van events" rose from
217 to 2,341 as sampling got coarser, which is impossible — looking less often cannot
reveal more trucks. It was rider churn being relabelled.

THE FIX. Ask how surprising the movement is under the boring explanation, using this
station's own demand rate and the actual interval:

    lambda = station demand rate x interval length
    van if  P(X >= |change| | Poisson(lambda)) < ALPHA

That is scale-free: it means the same thing at a dead station and a commute hub, at
2 minutes and at 20.

THE CIRCULARITY, AND WHY THIS ITERATES. Demand rates are computed from departures with
van events removed — using the very labels we are trying to fix. So this alternates:
labels -> rates -> better labels -> better rates, until the labels stop moving. That is
the EM pattern. Coupling is weak (vans are a few percent of intervals), so it should
settle in a handful of rounds; the loop reports how much moved each time, because a
label set that keeps churning would mean the estimate is fragile and worth saying so.
"""

import math
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "analysis.duckdb"
ALPHA = 1e-4          # a movement this surprising is called a van
INTERVAL_MIN = 5      # the grid the deltas are built on
MAX_ROUNDS = 6
FLOOR = 3             # never call a change smaller than this a van, however quiet


def min_van_count(lam, alpha=ALPHA, cap=60):
    """Smallest k where P(X >= k) < alpha under Poisson(lam)."""
    if lam <= 0:
        return FLOOR
    cum, term = math.exp(-lam), math.exp(-lam)
    for k in range(1, cap):
        if 1 - cum < alpha:
            return max(k, FLOOR)
        term *= lam / k
        cum += term
    return cap


def rates(con):
    """Departure and arrival rates per station-hour, excluding current van labels."""
    return con.execute(f"""
    WITH o AS (
      SELECT g.station_id,
             CASE WHEN dayofweek(g.bucket AT TIME ZONE 'America/Los_Angeles') IN (0,6)
                  THEN 'weekend' ELSE 'weekday' END AS daytype,
             CAST(strftime(g.bucket AT TIME ZONE 'America/Los_Angeles','%H') AS INT) AS hour,
             lag(g.num_bikes_available) OVER w > 0 AND lag(g.is_serving) OVER w AS out_ok,
             lag(g.num_docks_available) OVER w > 0                              AS in_ok,
             CASE WHEN d.event_class='rider_churn' AND d.d_bikes < 0 THEN -d.d_bikes ELSE 0 END AS dep,
             CASE WHEN d.event_class='rider_churn' AND d.d_bikes > 0 THEN  d.d_bikes ELSE 0 END AS arr
      FROM grid_status g LEFT JOIN station_deltas d USING (station_id, bucket)
      WHERE g.num_bikes_available IS NOT NULL
      WINDOW w AS (PARTITION BY g.station_id ORDER BY g.bucket))
    SELECT station_id, daytype, hour,
           sum(dep) / nullif(count(*) FILTER (WHERE out_ok), 0) * 12 AS dep_per_hour,
           sum(arr) / nullif(count(*) FILTER (WHERE in_ok),  0) * 12 AS arr_per_hour
    FROM o WHERE out_ok IS NOT NULL GROUP BY 1,2,3""").fetchall()


def main():
    con = duckdb.connect(str(DB))
    before = con.execute("""SELECT count(*) FROM station_deltas
                            WHERE event_class LIKE 'inferred_truck%'""").fetchone()[0]
    print(f"starting labels: {before:,} van intervals (fixed threshold of 5)\n")
    print(f"  {'round':>5} {'vans':>7} {'changed':>9} {'median threshold':>17}")

    prev = None
    for rnd in range(1, MAX_ROUNDS + 1):
        # thresholds from current rates
        rows = rates(con)
        thr = [(s, dt, h,
                min_van_count((dp or 0) * INTERVAL_MIN / 60),
                min_van_count((ar or 0) * INTERVAL_MIN / 60))
               for s, dt, h, dp, ar in rows]
        con.execute("""CREATE OR REPLACE TEMP TABLE thresholds
                       (station_id VARCHAR, daytype VARCHAR, hour INT,
                        out_k INT, in_k INT)""")
        con.executemany("INSERT INTO thresholds VALUES (?,?,?,?,?)", thr)
        con.execute("""CREATE OR REPLACE TEMP TABLE rate_snapshot
                       (station_id VARCHAR, daytype VARCHAR, hour INT,
                        dep_per_hour DOUBLE, arr_per_hour DOUBLE)""")
        con.executemany("INSERT INTO rate_snapshot VALUES (?,?,?,?,?)",
                        [(s_, d_, h_, dp or 0.0, ar or 0.0) for s_, d_, h_, dp, ar in rows])

        con.execute("""
        CREATE OR REPLACE TEMP TABLE relabelled AS
        SELECT d.station_id, d.bucket,
          CASE
            WHEN d.carried_forward                                   THEN 'no_observation'
            WHEN abs(d.d_bikes + d.d_docks) > 1 AND abs(d.d_bikes) >= 3
                                                                     THEN 'feed_artifact'
            -- surprise test, direction-aware: leaving is judged against the departure
            -- rate, arriving against the arrival rate
            WHEN d.d_bikes <= -coalesce(t.out_k, 5)                   THEN 'inferred_truck_pickup'
            WHEN d.d_bikes >=  coalesce(t.in_k,  5)                   THEN 'inferred_truck_dropoff'
            WHEN d.d_bikes <> 0                                       THEN 'rider_churn'
            ELSE 'idle'
          END AS new_class
        FROM station_deltas d
        LEFT JOIN thresholds t
          ON t.station_id = d.station_id
         AND t.hour = CAST(strftime(d.bucket AT TIME ZONE 'America/Los_Angeles','%H') AS INT)
         AND t.daytype = CASE WHEN dayofweek(d.bucket AT TIME ZONE 'America/Los_Angeles') IN (0,6)
                              THEN 'weekend' ELSE 'weekday' END
        """)
        changed = con.execute("""SELECT count(*) FROM station_deltas d
            JOIN relabelled r USING (station_id, bucket)
            WHERE d.event_class <> r.new_class""").fetchone()[0]
        con.execute("""UPDATE station_deltas d SET event_class = r.new_class
                       FROM relabelled r
                       WHERE d.station_id=r.station_id AND d.bucket=r.bucket""")

        vans = con.execute("""SELECT count(*) FROM station_deltas
                              WHERE event_class LIKE 'inferred_truck%'""").fetchone()[0]
        med = con.execute("SELECT median(out_k) FROM thresholds").fetchone()[0]
        print(f"  {rnd:>5} {vans:>7,} {changed:>9,} {med:>17.0f}")
        if changed == 0 or vans == prev:
            print(f"\n  converged after {rnd} round(s)")
            break
        prev = vans
    else:
        print(f"\n  did NOT converge in {MAX_ROUNDS} rounds — labels are unstable, treat with care")

    print(f"\n  van intervals: {before:,} (fixed 5)  ->  {vans:,} (surprise-based)")
    # Report the spread over ACTIVE station-hours. Half of all cells have zero
    # departures, so plain quartiles all land in the dead zone and make a varying
    # threshold look constant — which is exactly how the first run misread itself.
    print("\n  threshold by how busy the station-hour is:")
    for lbl, lo, hi in (("no demand at all", -1, 0.001), ("quiet (<0.5/hr)", 0.001, 0.5),
                        ("moderate (0.5-2/hr)", 0.5, 2.0), ("busy (>2/hr)", 2.0, 1e9)):
        r = con.execute(f"""SELECT count(*), min(out_k), max(out_k)
            FROM thresholds t JOIN (SELECT station_id, daytype, hour,
              out_k AS k FROM thresholds) x USING (station_id, daytype, hour)
            WHERE t.out_k IS NOT NULL""").fetchone()
        print(f"    {lbl:<22}", end="")
        rr = con.execute(f"""SELECT count(*), min(out_k), max(out_k) FROM thresholds
            WHERE station_id || daytype || hour IN (
              SELECT station_id || daytype || hour FROM rate_snapshot
              WHERE dep_per_hour > {lo} AND dep_per_hour <= {hi})""").fetchone()
        if rr[0]:
            print(f" n={rr[0]:>6,}   k = {rr[1]}-{rr[2]} bikes")
        else:
            print(" none")
    con.close()


if __name__ == "__main__":
    sys.exit(main())
