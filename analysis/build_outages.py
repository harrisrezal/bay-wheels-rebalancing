#!/usr/bin/env python3
"""Build outage_events — discrete service failures, weighted by demand.

Turns per-bucket state into events: a contiguous run where a station could not serve.
Two kinds, both standard terms in the rebalancing literature:

  starvation  no ebikes while open for rental      -> nobody can start a trip
  saturation  no free docks while open for returns -> nobody can finish one

Two qualifiers separate real failures from noise:

  DURATION. A run must last at least MIN_MINUTES. A station empty for one 5-minute
  bucket between two rentals is normal churn, not an outage.

  DEMAND. A run only counts where somebody actually wanted a bike. An empty station at
  3am is correct inventory placement, not a failure. Without this qualifier the numbers
  inflate and every hour looks equally bad — which is what the pre-step-4 figures did.

Lost trips come from demand_baseline, so they inherit its lower-bound caveat: the rate
during a stockout is assumed equal to the rate before it, when a station empties
precisely because demand was high.

SCOPE: EBIKES ONLY, and this is a choice rather than an oversight. Ebikes are 82.5% of
trips, and a station holding only classic bikes looks stocked while serving under a
fifth of demand — the failure mode invisible to any inventory count, which is the whole
subject here.

What that leaves out, measured rather than waved away: classic starvation runs at 19.7%
of station-hours against ebikes' 18.6%, so stations run dry for classic riders just as
often. Each dry hour costs less because classic demand averages 0.25/hr per station
against 0.86 for ebikes. Scaling the measured ebike figure by that ratio puts classic
losses near 5,200 trips against 17,970 — so ignoring classic understates total harm by
roughly 29%.

Worth stating in any writeup, and worth flagging that classic bikes are cheaper to
rent, so the riders being ignored here may skew price-sensitive. That is an equity
question this analysis does not answer.
"""

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "analysis.duckdb"
MIN_MINUTES = 15            # 3 consecutive 5-minute buckets
MIN_DEMAND_PER_HOUR = 0.10  # below this nobody meaningfully wanted a bike


def build(con, min_minutes=MIN_MINUTES):
    con.execute(f"""
    CREATE OR REPLACE TEMP TABLE runs AS
    WITH s AS (
        SELECT g.bucket, g.station_id,
               (g.num_ebikes_available = 0 AND g.is_serving)                AS starved,
               (g.num_docks_available  = 0 AND g.is_returning = 1)          AS saturated,
               d.ebike_demand_per_hour, d.low_confidence,
               row_number() OVER (PARTITION BY g.station_id ORDER BY g.bucket) AS rn
        FROM grid_status g
        LEFT JOIN demand_baseline d
          ON d.station_id = g.station_id
         AND d.hour = CAST(strftime(g.bucket AT TIME ZONE 'America/Los_Angeles','%H') AS INT)
         AND d.daytype = CASE WHEN dayofweek(g.bucket AT TIME ZONE 'America/Los_Angeles') IN (0,6)
                              THEN 'weekend' ELSE 'weekday' END
        WHERE g.num_bikes_available IS NOT NULL
    ),
    -- gaps-and-islands: rn minus a per-state row number is constant within a run
    islands AS (
        SELECT *,
               rn - row_number() OVER (PARTITION BY station_id, starved   ORDER BY bucket) AS grp_starve,
               rn - row_number() OVER (PARTITION BY station_id, saturated ORDER BY bucket) AS grp_sat
        FROM s
    )
    SELECT 'starvation' AS kind, station_id, grp_starve AS grp,
           min(bucket) AS started_at, max(bucket) AS ended_at,
           count(*) * 5 AS minutes,
           sum(ebike_demand_per_hour) / 12.0 AS lost_trips,
           avg(ebike_demand_per_hour) AS demand_per_hour,
           bool_or(low_confidence) AS low_confidence
    FROM islands WHERE starved GROUP BY 1,2,3
    UNION ALL
    SELECT 'saturation', station_id, grp_sat,
           min(bucket), max(bucket), count(*) * 5,
           NULL, avg(ebike_demand_per_hour), bool_or(low_confidence)
    FROM islands WHERE saturated GROUP BY 1,2,3
    """)
    con.execute(f"""
    CREATE OR REPLACE TABLE outage_events AS
    SELECT * FROM runs
    WHERE minutes >= {min_minutes} AND coalesce(demand_per_hour, 0) >= {MIN_DEMAND_PER_HOUR}
    """)


def main():
    con = duckdb.connect(str(DB))
    build(con)

    print("=== OUTAGE EVENTS (>=15 min, demand-qualified) ===")
    for kind, n, st, med, p90, mx, lost in con.execute("""
        SELECT kind, count(*), count(DISTINCT station_id),
               median(minutes), quantile_cont(minutes, 0.9), max(minutes), sum(lost_trips)
        FROM outage_events GROUP BY 1 ORDER BY 2 DESC""").fetchall():
        print(f"  {kind:<11} {n:>5} events across {st:>3} stations   "
              f"median {med:>4.0f}m  p90 {p90:>5.0f}m  max {mx:>5.0f}m"
              + (f"   lost trips {lost:,.0f}" if lost else ""))

    print("\n=== CONCENTRATION: how few stations cause the failure? ===")
    rows = con.execute("""
      WITH s AS (SELECT station_id, sum(minutes)/60.0 h FROM outage_events
                 WHERE kind='starvation' GROUP BY 1),
           t AS (SELECT sum(h) tot FROM s)
      SELECT station_id, h, sum(h) OVER (ORDER BY h DESC) / (SELECT tot FROM t) cum,
             row_number() OVER (ORDER BY h DESC) r FROM s""").fetchall()
    tot_st = con.execute("SELECT count(DISTINCT station_id) FROM grid_status").fetchone()[0]
    for target in (0.25, 0.50, 0.80):
        hit = next((r for _, _, cum, r in rows if cum >= target), None)
        if hit:
            print(f"  {target:.0%} of all starved hours comes from the worst "
                  f"{hit:>3} stations ({hit/tot_st:.1%} of the network)")

    print("\n=== WORST STATIONS (starved hours, demand-qualified) ===")
    print(f"  {'station':<38} {'hours':>7} {'events':>7} {'lost trips':>11}")
    for name, h, n, lost in con.execute("""
        SELECT i.name, sum(o.minutes)/60.0, count(*), sum(o.lost_trips)
        FROM outage_events o
        JOIN (SELECT DISTINCT ON(station_id) station_id,name FROM station_info
              ORDER BY station_id, fetched_on DESC) i USING (station_id)
        WHERE o.kind='starvation' GROUP BY 1 ORDER BY 2 DESC LIMIT 8""").fetchall():
        print(f"  {name[:38]:<38} {h:>7.1f} {n:>7} {lost:>11.0f}")

    print("\n=== THRESHOLD SENSITIVITY (does 15 min drive the answer?) ===")
    print(f"  {'min duration':>12} {'events':>8} {'starved hrs':>12} {'lost trips':>11}")
    for m in (5, 10, 15, 30, 60):
        build(con, m)
        r = con.execute("""SELECT count(*), sum(minutes)/60.0, sum(lost_trips)
                           FROM outage_events WHERE kind='starvation'""").fetchone()
        print(f"  {m:>10} m {r[0]:>8,} {r[1]:>12.0f} {r[2]:>11,.0f}")
    build(con)  # restore the canonical threshold
    con.close()


if __name__ == "__main__":
    sys.exit(main())
